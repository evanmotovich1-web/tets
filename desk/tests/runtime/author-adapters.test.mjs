import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { createHash } from 'node:crypto';
import { mkdtemp, rm, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createModelAdapter, createCliAuthorAdapter, validateGeneration } from '../../runtime/models.mjs';
import { runFleet, readRegistry, fixtureFleetAdapter, selectAuthorAdapter } from '../../runtime/author-fleet.mjs';
import { config } from '../../runtime/contracts.mjs';

const sha = bytes => createHash('sha256').update(bytes).digest('hex');
let clock = 0;
const now = () => new Date(Date.UTC(2026, 9, 25, 15, 0, 0) + clock++).toISOString();
async function temp(t) { const d = await mkdtemp(join(tmpdir(), 'adapters-')); t.after(() => rm(d, { recursive: true, force: true })); return d; }
const LOCAL = { TRADING_DESK_MODEL_ID: 'qwen3.5:2b', TRADING_DESK_LOCAL_BASE_URL: 'http://127.0.0.1:11434/v1' };

// A real-looking strategy plus preregistration, as a model would return them.
const STRATEGY = { schema_version: 2, family: 'momentum_long', symbol: 'SPY', timeframe: '1d',
  parameters: { lookback_bars: 40, enter_return_bps: '150', exit_return_bps: '0' },
  position: { sizing: 'fixed_notional', notional_usd: '25.00' }, rationale: 'Model-written: multi-week ETF momentum persists.' };
const PREREG = { schema_version: 2, kind: 'preregistration',
  cost_model: { commission_bps: '0', slippage_bps_per_side: '5', regulatory_fee_bps_on_sells: '0.3', rounding: 'half_even' },
  baselines: ['buy_and_hold', 'cash'],
  pass_criteria: { min_independent_trades: 30, max_drawdown_pct: '15', min_excess_return_vs_buy_and_hold_pct: '0', alpha: '0.05',
    test: 'stationary_block_bootstrap_excess_daily_returns', bootstrap: { block_bars: 10, resamples: 10000, seed: 0 } }, paper_criteria: null };
function openAIBody(variant = 0) {
  const strategy = { ...STRATEGY, parameters: { ...STRATEGY.parameters, lookback_bars: 40 + variant } };
  return JSON.stringify({ model: 'qwen3.5:2b', system_fingerprint: 'fp_ollama', usage: { prompt_tokens: 900, completion_tokens: 120 },
    choices: [{ message: { tool_calls: [
      { function: { name: 'submit_strategy', arguments: JSON.stringify({ strategy }) } },
      { function: { name: 'submit_preregistration', arguments: JSON.stringify({ preregistration: PREREG }) } }] } }] });
}
const fetchReturning = bodies => { let i = 0; return async () => new Response(bodies[Math.min(i++, bodies.length - 1)], { status: 200 }); };

test('localhost adapter records agent-written provenance with the raw response hash and reported version', async () => {
  const body = openAIBody();
  const adapter = createModelAdapter('localhost', LOCAL, fetchReturning([body]));
  const g = await adapter.generate({ role: 'author_claude', messages: [{ role: 'user', content: '{}' }], tools: [], budget: config.budget });
  assert.equal(g.provenance.transport, 'http'); assert.equal(g.provenance.agent_written, true);
  assert.equal(g.provenance.raw_response_sha256, sha(Buffer.from(body)));
  assert.equal(g.provenance.model_version, 'qwen3.5:2b fp_ollama');
  assert.equal(g.tool_calls[0].name, 'submit_strategy');
});

test('an unreachable or missing local model reports author_unavailable instead of falling back', async () => {
  const refused = async () => { throw Object.assign(new TypeError('fetch failed'), { cause: { code: 'ECONNREFUSED' } }); };
  const call = adapter => adapter.generate({ role: 'author_claude', messages: [], tools: [], budget: config.budget });
  await assert.rejects(call(createModelAdapter('localhost', LOCAL, refused)), /author_unavailable/);
  await assert.rejects(call(createModelAdapter('localhost', LOCAL, async () => new Response('{}', { status: 404 }))), /author_unavailable/);
});

test('fixture provenance can never claim to be agent-written', () => {
  const calls = [{ name: 'submit_strategy', arguments: { strategy: {} } }];
  assert.throws(() => validateGeneration({ tool_calls: calls, usage: { input_tokens: 1, output_tokens: 1 }, provider: 'fixture', model_id: 'x',
    provenance: { transport: 'fixture', agent_written: true, model_version: null, raw_response_sha256: 'a'.repeat(64) } }, 'author_claude'), /malformed_model_output/);
});

function fakeSpawn(script) {
  const calls = [];
  const spawnFn = (bin, args, options) => {
    calls.push({ bin, args, options });
    const child = new EventEmitter();
    child.stdout = new PassThrough(); child.stderr = new PassThrough(); child.stdin = new PassThrough();
    let input = ''; child.stdin.on('data', d => { input += d; });
    child.kill = () => {};
    child.stdin.on('finish', () => {
      const result = script({ bin, args, input });
      if (result.error) return child.emit('error', result.error);
      child.stdout.end(result.stdout ?? ''); child.emit('close', result.code ?? 0);
    });
    return child;
  };
  return { spawnFn, calls };
}
const toolJson = JSON.stringify({ tool_calls: [{ name: 'submit_strategy', arguments: { strategy: STRATEGY } }, { name: 'submit_preregistration', arguments: { preregistration: PREREG } }] });

test('CLI authors stay off without the explicit opt-in flag and never spawn', () => {
  const { spawnFn, calls } = fakeSpawn(() => ({}));
  assert.throws(() => createCliAuthorAdapter('codex', { TRADING_DESK_MODEL_ID: 'gpt-5.5' }, { spawnFn }), /cli_author_disabled/);
  assert.throws(() => selectAuthorAdapter('cli_claude', { TRADING_DESK_MODEL_ID: 'claude-sonnet-5-5' }), /cli_author_disabled/);
  assert.equal(calls.length, 0);
});

test('codex CLI adapter runs read-only without a shell and parses the final JSON', async () => {
  const { spawnFn, calls } = fakeSpawn(({ args }) => args[0] === '--version' ? { stdout: 'codex-cli 0.99.0\n' }
    : { stdout: `thinking...\n${toolJson}\n` });
  const env = { TRADING_DESK_ALLOW_CLI_AUTHOR: '1', TRADING_DESK_MODEL_ID: 'gpt-5.5' };
  const g = await createCliAuthorAdapter('codex', env, { spawnFn }).generate({ role: 'author_codex', messages: [{ role: 'user', content: 'ctx' }], tools: [], budget: config.budget });
  const run = calls.find(c => c.args[0] === 'exec');
  assert.deepEqual(run.args.slice(0, 3), ['exec', '--sandbox', 'read-only']); assert.equal(run.options.shell, false);
  assert.equal(g.provider, 'cli_codex'); assert.equal(g.provenance.transport, 'cli'); assert.equal(g.provenance.agent_written, true);
  assert.equal(g.provenance.model_version, 'gpt-5.5 via codex-cli 0.99.0');
  assert.equal(g.provenance.raw_response_sha256, sha(Buffer.from(`thinking...\n${toolJson}\n`)));
});

test('claude CLI adapter disables tools, reads the JSON envelope, and a missing binary is author_unavailable', async () => {
  const envelope = JSON.stringify({ type: 'result', is_error: false, result: toolJson, usage: { input_tokens: 800, output_tokens: 90 } });
  const { spawnFn, calls } = fakeSpawn(({ args }) => args[0] === '--version' ? { stdout: '2.9.0 (Claude Code)\n' } : { stdout: envelope });
  const env = { TRADING_DESK_ALLOW_CLI_AUTHOR: '1', TRADING_DESK_MODEL_ID: 'claude-sonnet-5-5' };
  const g = await createCliAuthorAdapter('claude', env, { spawnFn }).generate({ role: 'author_claude', messages: [], tools: [], budget: config.budget });
  const run = calls.find(c => c.args[0] === '-p');
  assert.ok(run.args.includes('--tools') && run.args[run.args.indexOf('--tools') + 1] === '');
  assert.deepEqual(g.usage, { input_tokens: 800, output_tokens: 90 });
  const missing = fakeSpawn(() => ({ error: Object.assign(new Error('spawn claude ENOENT'), { code: 'ENOENT' }) }));
  await assert.rejects(createCliAuthorAdapter('claude', env, { spawnFn: missing.spawnFn })
    .generate({ role: 'author_claude', messages: [], tools: [], budget: config.budget }), /author_unavailable/);
});

test('fleet registry separates agent-written candidates from fixture templates', async t => {
  const dir = await temp(t);
  let n = 0; const bodies = [0, 1, 2, 3].map(openAIBody);
  const local = createModelAdapter('localhost', LOCAL, async () => new Response(bodies[n++ % bodies.length], { status: 200 }));
  const real = await runFleet({ campaignId: 'real-authors', stateDir: dir, rounds: 1, now, adapter: local });
  assert.equal(real.registered, 4); assert.equal(real.agent_written_registered, 4); assert.equal(real.fixture_registered, 0);
  for (const e of await readRegistry(real.registry)) {
    assert.equal(e.author.agent_written, true); assert.equal(e.author.transport, 'http');
    assert.equal(e.author.model_version, 'qwen3.5:2b fp_ollama'); assert.match(e.author.raw_response_sha256, /^[0-9a-f]{64}$/);
    assert.match(e.strategy_sha256, /^[0-9a-f]{64}$/); assert.match(e.author.prompt_sha256, /^[0-9a-f]{64}$/);
  }
  const fixture = await runFleet({ campaignId: 'fixture-authors', stateDir: dir, rounds: 1, now, adapter: fixtureFleetAdapter() });
  assert.equal(fixture.agent_written_registered, 0); assert.equal(fixture.fixture_registered, 4);
  assert.ok((await readRegistry(fixture.registry)).every(e => e.author.agent_written === false && e.author.transport === 'fixture'));
});

test('an unavailable author stops the run with author_unavailable, logs nothing, and releases the lock', async t => {
  const dir = await temp(t);
  const refused = async () => { throw Object.assign(new TypeError('fetch failed'), { cause: { code: 'ECONNREFUSED' } }); };
  await assert.rejects(runFleet({ campaignId: 'down', stateDir: dir, rounds: 1, now, adapter: createModelAdapter('localhost', LOCAL, refused) }), /author_unavailable/);
  assert.equal(await readFile(join(dir, 'candidates', 'down.jsonl'), 'utf8').catch(() => ''), '');
  await assert.rejects(readFile(join(dir, 'candidates', 'down.lock')), { code: 'ENOENT' });
});
