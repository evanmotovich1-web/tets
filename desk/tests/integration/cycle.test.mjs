import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, readFile, cp, writeFile, mkdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { spawn } from 'node:child_process';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { runTick } from '../../runtime/tick.mjs';
import { dispatch, roles } from '../../runtime/roles.mjs';
import { fixtureAdapter } from '../../runtime/models.mjs';
import { workerBridge } from '../../runtime/worker.mjs';

const APP = fileURLToPath(new URL('../..', import.meta.url));
const PYTHON = join(APP, 'backtest/.venv/bin/python');
const SECRET = /sk-|api_key|bearer |DEEPSEEK_API_KEY|OPENROUTER_API_KEY|authorization/i;

const temp = async t => {
  const dir = await mkdtemp(join(tmpdir(), 'trading-integration-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  return dir;
};
function run(command, args, { cwd = APP, timeout = 30000, env = process.env } = {}) {
  return new Promise((resolveRun, reject) => {
    const child = spawn(command, args, { cwd, env, shell: false, stdio: ['ignore', 'pipe', 'pipe'] });
    let stdout = '', stderr = '';
    const timer = setTimeout(() => child.kill('SIGKILL'), timeout);
    child.stdout.on('data', chunk => { stdout += chunk; });
    child.stderr.on('data', chunk => { stderr += chunk; });
    child.on('error', reject);
    child.on('close', (code, signal) => { clearTimeout(timer); resolveRun({ code, signal, stdout, stderr }); });
  });
}
function assertResearchCard(result) {
  const card = result.cards[result.job.job_id];
  assert.equal(result.job.state, 'completed');
  assert.equal(result.job.receipt.metrics.final_equity, '1005.40');
  assert.equal(result.job.receipt.metrics.commission_total, '2.30');
  assert.equal(result.job.receipt.promoting, false);
  assert.equal(result.job.receipt.qualification, 'research_only');
  assert.equal(card.promoting, false);
  assert.equal(card.qualification, 'research_only');
  assert.equal(card.receipt_sha256, result.job.receipt.receipt_sha256);
  assert.equal(result.job.review.verdict, 'research_valid');
  assert.deepEqual(result.job.outputs.map(item => item.phase), ['admit', 'scout', 'propose', 'backtest', 'referee']);
  assert.equal(SECRET.test(JSON.stringify({ card, receipt: result.job.receipt, review: result.job.review })), false);
}

test('foreground fixture tick saves one research card and repeats as the same job', async t => {
  const stateDir = await temp(t);
  const first = await run(process.execPath, [join(APP, 'runtime/tick.mjs'), '--event-id', 'fixture:001', '--provider', 'fixture', '--state-dir', stateDir]);
  assert.equal(first.code, 0, first.stderr);
  const printed = JSON.parse(first.stdout);
  assert.equal(printed.state, 'completed');
  assert.equal(printed.card.promoting, false);
  assert.equal(printed.card.qualification, 'research_only');
  const again = await runTick({ eventId: 'fixture:001', provider: 'fixture', stateDir });
  assert.equal(again.job.job_id, printed.job_id);
  assert.equal(again.cards[printed.job_id].receipt_sha256, printed.card.receipt_sha256);
  assert.equal(again.job.events.filter(event => event.event_type === 'trial_registered').length, 1);
});

test('identical requests on different events share one receipt and do not call a paid provider', async t => {
  const stateDir = await temp(t);
  const first = await runTick({ eventId: 'fixture:001', provider: 'fixture', stateDir });
  const second = await runTick({ eventId: 'fixture:002', provider: 'fixture', stateDir });
  assertResearchCard(first);
  assertResearchCard(second);
  assert.notEqual(first.job.job_id, second.job.job_id);
  assert.equal(first.job.receipt.receipt_sha256, second.job.receipt.receipt_sha256);
  assert.deepEqual(first.job.outputs.map(item => item.output.provider), Array(5).fill('fixture'));
  assert.deepEqual(second.job.outputs.map(item => item.output.provider), Array(5).fill('fixture'));
});

test('checkpoint resume and SIGKILL recovery finish the same trial once', async t => {
  const resumed = await temp(t);
  const partial = await runTick({ eventId: 'fixture:001', provider: 'fixture', stateDir: resumed, maxPhases: 2 });
  assert.equal(partial.job.phase, 'propose');
  assert.equal(partial.cards[partial.job.job_id], undefined);
  const done = await runTick({ eventId: 'fixture:001', provider: 'fixture', stateDir: resumed });
  assertResearchCard(done);
  assert.equal(done.job.events.filter(event => event.event_type === 'proposal_recorded').length, 1);

  const killed = await temp(t);
  const child = await run(process.execPath, ['--input-type=module', '-e', `import {runTick} from './runtime/tick.mjs'; await runTick({eventId:'fixture:001',provider:'fixture',stateDir:${JSON.stringify(killed)},afterPhase:phase=>{if(phase==='scout') process.kill(process.pid,'SIGKILL')}});`], { timeout: 25000 });
  assert.equal(child.signal, 'SIGKILL', child.stderr + child.stdout);
  const recovered = await runTick({ eventId: 'fixture:001', provider: 'fixture', stateDir: killed });
  assertResearchCard(recovered);
  assert.equal(recovered.job.events.filter(event => event.event_type === 'trial_registered').length, 1);
  assert.equal(recovered.job.events.filter(event => event.event_type === 'card_saved').length, 1);
});

test('forbidden order tool is rejected before a handler and saves no card', async t => {
  let executed = false;
  assert.equal(roles.runner.tools.includes('run_backtest'), true);
  assert.equal(roles.runner.tools.includes('place_order'), false);
  await assert.rejects(dispatch('runner', { name: 'place_order', arguments: {} }, { place_order: () => { executed = true; } }), /forbidden_tool/);
  assert.equal(executed, false);
  const stateDir = await temp(t);
  const model = fixtureAdapter();
  model.generate = async () => ({ tool_calls: [{ name: 'place_order', arguments: {} }], usage: { input_tokens: 1, output_tokens: 1 }, provider: 'fixture', model_id: 'fixture-sma-v1' });
  const result = await runTick({ eventId: 'fixture:001', provider: 'fixture', stateDir, model });
  assert.equal(result.job.state, 'rejected');
  assert.equal(result.job.receipt, null);
  assert.equal(Object.keys(result.cards).length, 0);
});

test('worker bridge rejects a future available_at and a sealed holdout id', async t => {
  const stateDir = await temp(t);
  const fixture = join(stateDir, 'fixture');
  await cp(join(APP, 'backtest/fixtures'), fixture, { recursive: true });
  const rewrite = await run(PYTHON, [join(APP, 'tests/integration/delay_bar.py'), fixture], { timeout: 20000 });
  assert.equal(rewrite.code, 0, rewrite.stderr);
  const worker = workerBridge({ stateDir, registry: join(fixture, 'registry.json') });
  try {
    const request = JSON.parse(await readFile(join(APP, 'backtest/fixtures/request.json'), 'utf8'));
    const delayed = await worker.call(request);
    assert.equal(delayed.ok, false);
    assert.equal(delayed.error.code, 'delayed_or_revised_bar');
    assert.equal(delayed.result.promoting, false);
    assert.equal(delayed.result.metrics, null);
  } finally { await worker.close(); }

  const sealed = join(stateDir, 'sealed');
  await mkdir(sealed);
  await writeFile(join(sealed, 'registry.json'), JSON.stringify({
    schema_version: 1, kind: 'dataset_registry',
    datasets: { 'sealed:holdout:v1': { classification: 'sealed_holdout', partition: 'holdout', manifest: 'absent.json', data: 'absent.jsonl', manifest_sha256: '0'.repeat(64) } }
  }));
  const holdout = workerBridge({ stateDir: join(stateDir, 'holdout-artifacts'), registry: join(sealed, 'registry.json') });
  try {
    const denied = await holdout.call({ op: 'read_manifest', dataset_id: 'sealed:holdout:v1' });
    assert.equal(denied.ok, false);
    assert.equal(denied.error.code, 'dataset_access_denied');
    await assert.rejects(readFile(join(sealed, 'absent.jsonl'), 'utf8'));
  } finally { await holdout.close(); }
});
