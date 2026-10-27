import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, readFile, readdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawn } from 'node:child_process';
import { BACKGROUND_CONTEXT as ctx } from '@earendil-works/chord/context';
import { createModels } from '@earendil-works/pi-ai/models';
import { fauxProvider, fauxAssistantMessage, fauxToolCall } from '@earendil-works/pi-ai/providers/faux';
import { Harness, createRegistry, defineExtension, defineTool } from '@earendil-works/pi-durable';
import { openNodeSqliteStorage } from '@earendil-works/pi-durable/storage/sqlite/node';
import { runTick } from '../../runtime/tick.mjs';
import { DeskState } from '../../runtime/workflow.mjs';
import { dispatch, beforeTool, roleExtensions, roles } from '../../runtime/roles.mjs';
import { createModelAdapter, fixtureAdapter, validateGeneration } from '../../runtime/models.mjs';
import { digest, DeskError, parseStrictJSON } from '../../runtime/contracts.mjs';
import { workerBridge } from '../../runtime/worker.mjs';
import { validateJob } from '../../runtime/schemas.mjs';
const temp = async t => { const d = await mkdtemp(join(tmpdir(), 'trading-runtime-')); t.after(() => rm(d, { recursive: true, force: true })); return d; };
const fixture = () => ({ eventId: 'fixture:001', provider: 'fixture' });
async function readState(dir) {
  const h = await Harness.open(await openNodeSqliteStorage(join(dir, 'agent.sqlite')), { models: createModels(), registry: createRegistry() }, ctx);
  try { return JSON.parse(JSON.stringify(await h.snapshot(DeskState, ctx))); } finally { await h.close(ctx); }
}
function child(script, timeout = 25000) {
  return new Promise((resolveChild, reject) => {
    const p = spawn(process.execPath, ['--input-type=module', '-e', script], { cwd: resolve('.'), stdio: ['ignore', 'pipe', 'pipe'] });
    let output = ''; p.stdout.on('data', x => { output += x; }); p.stderr.on('data', x => { output += x; });
    const timer = setTimeout(() => p.kill('SIGKILL'), timeout);
    p.on('error', reject); p.on('close', (code, signal) => { clearTimeout(timer); resolveChild({ code, signal, output }); });
  });
}

test('role lists are immutable and forbidden calls fail before handlers', async () => {
  let executed = false;
  for (const role of Object.keys(roles)) {
    assert.ok(beforeTool(role, { name: 'place_order' }).block);
    await assert.rejects(dispatch(role, { name: 'place_order', arguments: {} }, { place_order: () => { executed = true; } }), /forbidden_tool/);
  }
  await assert.rejects(dispatch('desk', { name: 'campaign_status', arguments: { campaign_id: 'x', extra: true } }, { campaign_status: () => { executed = true; } }), /invalid_arguments/);
  assert.equal(executed, false);
  assert.throws(() => roles.author.tools.push('bash'), TypeError);
});

test('real Pi Durable beforeTool hook blocks an offered rogue tool', async t => {
  const dir = await temp(t); let executed = 0;
  const faux = fauxProvider({ models: [{ id: 'guard-test' }], tokensPerSecond: Infinity });
  faux.setResponses([fauxAssistantMessage(fauxToolCall('rogue', {})), fauxAssistantMessage('done')]);
  const models = createModels(); models.setProvider(faux.provider);
  const registry = createRegistry(); const author = roleExtensions().find(e => e.name === 'trading-author');
  const rogue = defineExtension({ name: 'rogue-test', tools: [defineTool({ name: 'rogue', description: 'negative-test only', parameters: { type: 'object', properties: {}, additionalProperties: false }, execute: async () => { executed++; return {}; } })] });
  registry.install(author); registry.install(rogue);
  const h = await Harness.open(await openNodeSqliteStorage(join(dir, 'agent.sqlite')), { models, registry, settings: { retry: { enabled: false }, compaction: { enabled: false } } }, ctx);
  try {
    const root = await h.root(ctx, { agent: { model: { provider: faux.getModel().provider, modelId: faux.getModel().id }, extensions: [author, rogue] } });
    const settled = await (await root.submit({ type: 'input', content: 'exercise guard' }, ctx)).wait(ctx);
    assert.equal(settled.status, 'done'); assert.equal(executed, 0);
  } finally { await h.close(ctx); }
});

test('fixture completes five roles, preserves receipt accounting and deduplicates triggers', async t => {
  const stateDir = await temp(t);
  const first = await runTick({ ...fixture(), stateDir });
  assert.equal(first.job.state, 'completed');
  assert.deepEqual(first.job.outputs.map(o => o.output.provider), Array(5).fill('fixture'));
  assert.equal(first.job.receipt.metrics.final_equity, '1005.40');
  assert.equal(first.job.receipt.metrics.commission_total, '2.30');
  assert.equal(first.job.receipt.promoting, false);
  assert.equal(first.job.events.filter(e => e.event_type === 'proposal_recorded').length, 1);
  const again = await runTick({ ...fixture(), stateDir });
  assert.equal(again.job.job_id, first.job.job_id);
  assert.equal(again.job.events.length, first.job.events.length);
  assert.equal(Object.keys(again.cards).length, 1);
  assert.equal(validateJob(first.job), first.job);
  assert.throws(() => validateJob({ ...first.job, unexpected: true }), /invalid_durable_job/);
  const independent = await runTick({ eventId: 'fixture:002', stateDir });
  assert.notEqual(independent.job.job_id, first.job.job_id);
  assert.equal(independent.job.receipt.receipt_sha256, first.job.receipt.receipt_sha256);
  await assert.rejects(runTick({ ...fixture(), stateDir, payload: { changed: true } }), /trigger_payload_conflict/);
  let previous = null;
  for (const { event_sha256, ...event } of first.job.events) { assert.equal(event.previous_event_sha256, previous); assert.equal(digest(event), event_sha256); previous = event_sha256; }
});

test('foreground phase checkpoint resumes without repeating accepted proposals', async t => {
  const stateDir = await temp(t);
  const partial = await runTick({ ...fixture(), stateDir, maxPhases: 3 });
  assert.equal(partial.job.phase, 'validate'); assert.equal(partial.job.outputs.length, 3);
  const done = await runTick({ ...fixture(), stateDir });
  assert.equal(done.job.state, 'completed');
  assert.equal(done.job.events.filter(e => e.event_type === 'proposal_recorded').length, 1);
  assert.equal(done.job.outputs.length, 5);
});

test('SIGKILL restart recovers actual durable checkpoint and stale writer lock', async t => {
  const stateDir = await temp(t);
  const result = await child(`import {runTick} from './runtime/tick.mjs'; await runTick({eventId:'fixture:001',stateDir:${JSON.stringify(stateDir)},afterPhase:p=>{if(p==='propose') process.kill(process.pid,'SIGKILL')}});`);
  assert.equal(result.signal, 'SIGKILL', result.output);
  const done = await runTick({ ...fixture(), stateDir });
  assert.equal(done.job.state, 'completed');
  assert.equal(done.job.outputs.length, 5);
  assert.equal(done.job.events.filter(e => e.event_type === 'trial_registered').length, 1);
});

test('orphaned completed backtest reconciles artifacts after SIGKILL without a response cache', async t => {
  const stateDir = await temp(t);
  const result = await child(`import {runTick} from './runtime/tick.mjs'; import {workerBridge} from './runtime/worker.mjs'; const worker=workerBridge({stateDir:${JSON.stringify(stateDir)}}); const call=worker.call; worker.call=async (...args)=>{const result=await call(...args); if(args[0].op==='run_backtest') process.kill(process.pid,'SIGKILL'); return result;}; await runTick({eventId:'fixture:001',stateDir:${JSON.stringify(stateDir)},worker});`);
  assert.equal(result.signal, 'SIGKILL', result.output);
  const before = await readState(stateDir); const job = Object.values(before.jobs)[0];
  assert.equal(job.inflight.type, 'worker'); assert.equal(job.inflight.message.op, 'run_backtest');
  await rm(join(stateDir, 'worker-replies', `${job.inflight.operation_id}.json`));
  const worker = workerBridge({ stateDir });
  const done = await runTick({ ...fixture(), stateDir, worker });
  assert.equal(done.job.state, 'completed');
  assert.equal(done.job.outputs.length, 5);
  assert.equal(done.job.events.filter(e => e.event_type === 'result_recorded').length, 1);
  assert.equal(done.job.events.filter(e => e.event_type === 'card_saved').length, 1);
});

test('malformed model output and forbidden tool never produce a card', async t => {
  for (const [id, generate] of [['malformed', async () => ({ bad: true })], ['forbidden', async () => ({ tool_calls: [{ name: 'place_order', arguments: {} }], usage: { input_tokens: 1, output_tokens: 1 }, provider: 'fixture', model_id: 'fixture-sma-v1' })]]) {
    const stateDir = join(await temp(t), id);
    const result = await runTick({ ...fixture(), stateDir, model: { ...fixtureAdapter(), generate } });
    assert.ok(['failed', 'rejected'].includes(result.job.state));
    assert.equal(result.job.receipt, null); assert.equal(Object.keys(result.cards).length, 0);
  }
});

test('invalid author proposal is recorded before semantic validation rejects it', async t => {
  const stateDir = await temp(t); const model = fixtureAdapter(); const generate = model.generate;
  model.generate = async input => { const out = await generate(input); if (input.role === 'author') out.tool_calls[0].arguments.strategy.parameters.slow_window = 1; return out; };
  const result = await runTick({ ...fixture(), stateDir, model });
  assert.equal(result.job.state, 'rejected');
  assert.equal(result.job.events.filter(e => e.event_type === 'proposal_recorded').length, 1);
  assert.equal(Object.keys(result.cards).length, 0);
});

test('unavailable provider retries are bounded and persisted across ticks', async t => {
  const stateDir = await temp(t); let calls = 0;
  const model = { provider: 'fixture', model_id: 'fixture-sma-v1', generate: async () => { calls++; throw new DeskError('provider_unavailable', { transient: true }); } };
  let result;
  for (let n = 0; n < 3; n++) {
    result = await runTick({ ...fixture(), stateDir, model });
    if (result.job.state === 'retry_wait') await new Promise(done => setTimeout(done, Math.max(0, result.job.next_attempt_at - Date.now()) + 10));
  }
  assert.equal(calls, 3); assert.equal(result.job.state, 'failed');
  assert.equal(result.job.attempts_by_phase.admit, 3); assert.equal(result.job.budget.model_calls, 3);
  assert.equal(Object.keys(result.cards).length, 0);
});

test('unknown model completion is not retried or treated as an empty success', async t => {
  const stateDir = await temp(t); let calls = 0;
  const model = { provider: 'localhost', model_id: 'test-local', generate: async () => { calls++; throw new DeskError('provider_result_unknown', { uncertain: true }); } };
  const result = await runTick({ ...fixture(), stateDir, model });
  assert.equal(result.job.state, 'unknown'); assert.equal(result.job.receipt, null);
  const again = await runTick({ ...fixture(), stateDir, model });
  assert.equal(again.job.state, 'unknown'); assert.equal(calls, 1); assert.ok(again.job.budget.tokens > 0);
});

test('provider configuration rejects remote localhost routes, paid calls and proxies', async () => {
  assert.throws(() => createModelAdapter('localhost', { TRADING_DESK_MODEL_ID: 'x', TRADING_DESK_LOCAL_BASE_URL: 'http://example.com/v1' }), /non_loopback_endpoint/);
  assert.throws(() => createModelAdapter('deepseek', { TRADING_DESK_MODEL_ID: 'x' }), /paid_inference_disabled/);
  assert.throws(() => createModelAdapter('deepseek', { TRADING_DESK_MODEL_ID: 'x', TRADING_DESK_ALLOW_PAID_INFERENCE: '1', DEEPSEEK_API_KEY: 'test-only-not-a-key', DEEPSEEK_BASE_URL: 'https://openrouter.ai' }), /not_direct_deepseek_endpoint/);
  const adapter = createModelAdapter('localhost', { TRADING_DESK_MODEL_ID: 'x', TRADING_DESK_LOCAL_BASE_URL: 'http://127.0.0.1:11434/v1' }, async () => { throw new Error('network unavailable'); });
  await assert.rejects(adapter.generate({ role: 'desk', messages: [], tools: [], budget: { max_output_tokens: 10 } }), /provider_result_unknown/);
  assert.throws(() => validateGeneration({ tool_calls: [], usage: { input_tokens: 0, output_tokens: 0 }, provider: 'fixture', model_id: 'x' }, 'desk'), /malformed_model_output/);
});

test('one active writer is enforced before opening storage', async t => {
  const stateDir = await temp(t); const model = fixtureAdapter(); const generate = model.generate;
  let enter, release;
  const started = new Promise(done => { enter = done; }); const gate = new Promise(done => { release = done; });
  model.generate = async input => { if (input.role === 'desk') { enter(); await gate; } return generate(input); };
  const first = runTick({ ...fixture(), stateDir, model });
  await started;
  try { await assert.rejects(runTick({ ...fixture(), stateDir }), /writer_already_active/); } finally { release(); }
  assert.equal((await first).job.state, 'completed');
});

test('strict JSON rejects duplicate keys, nonfinite numbers and deep structures', () => {
  assert.throws(() => parseStrictJSON('{"x":1,"x":2}'), /duplicate_json_key/);
  assert.throws(() => parseStrictJSON(String.raw`{"x":1,"\u0078":2}`), /duplicate_json_key/);
  assert.throws(() => parseStrictJSON('{"x":1e999}'), /nonfinite_json_number/);
  assert.throws(() => parseStrictJSON('['.repeat(33) + '0' + ']'.repeat(33)), /json_nesting_limit/);
  assert.deepEqual(parseStrictJSON('{"x":"a:b","nested":{"x":1},"array":[{"x":2}]}'), { x: 'a:b', nested: { x: 1 }, array: [{ x: 2 }] });
});

test('model-call budget is enforced across persisted phases', async t => {
  const stateDir = await temp(t);
  const result = await runTick({ ...fixture(), stateDir, budget: { max_model_calls: 1 } });
  assert.equal(result.job.state, 'failed'); assert.equal(result.job.last_error_code, 'budget_exhausted');
  assert.equal(result.job.budget.model_calls, 1); assert.equal(Object.keys(result.cards).length, 0);
});

test('unknown worker result enters reconciliation and is never blindly rerun', async t => {
  const stateDir = await temp(t); const worker = workerBridge({ stateDir }); const call = worker.call; let runs = 0;
  worker.call = async (...args) => { if (args[0].op === 'run_backtest') { runs++; throw new DeskError('backtest_result_unknown', { uncertain: true }); } return call(...args); };
  const first = await runTick({ ...fixture(), stateDir, worker });
  assert.equal(first.job.state, 'unknown'); assert.equal(first.job.receipt, null);
  const again = await runTick({ ...fixture(), stateDir, worker });
  assert.equal(again.job.state, 'unknown'); assert.equal(runs, 1); assert.equal(Object.keys(again.cards).length, 0);
});

test('fresh receipt verification detects artifact tampering before reuse', async t => {
  const stateDir = await temp(t);
  const done = await runTick({ ...fixture(), stateDir });
  const receipt = done.job.receipt;
  const worker = workerBridge({ stateDir });
  const checked = await worker.call({ op: 'read_receipt', receipt_sha256: receipt.receipt_sha256 });
  assert.equal(checked.ok, true);
  await rm(join(stateDir, 'artifacts', `${receipt.artifacts.fills_sha256}.json`));
  const broken = await worker.call({ op: 'read_receipt', receipt_sha256: receipt.receipt_sha256 });
  assert.equal(broken.ok, false); assert.equal(broken.result.metrics, null);
  await worker.close();
});
