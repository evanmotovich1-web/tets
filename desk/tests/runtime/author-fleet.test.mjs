import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, readFile, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { runFleet, readRegistry, ruleHash, fixtureFleetAdapter } from '../../runtime/author-fleet.mjs';
import { createModelAdapter } from '../../runtime/models.mjs';
import { digest } from '../../runtime/contracts.mjs';

async function temp(t) { const dir = await mkdtemp(join(tmpdir(), 'fleet-')); t.after(() => rm(dir, { recursive: true, force: true })); return dir; }
let clock = 0;
const now = () => new Date(Date.UTC(2026, 9, 25, 14, 0, 0) + clock++).toISOString();

test('fixture fleet registers >=20 deduplicated candidates, each preregistered on disk', async t => {
  const dir = await temp(t);
  const result = await runFleet({ campaignId: 'fleet-test', stateDir: dir, now });
  assert.equal(result.entries, 24);
  assert.equal(result.registered, 22);
  assert.equal(result.duplicate, 1);
  assert.equal(result.rejected, 1);
  assert.deepEqual(result.rejections.map(r => r.code).sort(), ['duplicate_spec', 'parameter_out_of_range']);
  const entries = await readRegistry(result.registry);
  const registered = entries.filter(e => e.status === 'registered');
  assert.equal(new Set(registered.map(e => e.rule_sha256)).size, 22);
  assert.equal(new Set(registered.map(e => e.author.role)).size, 4);
  for (const e of registered) {
    const spec = JSON.parse(await readFile(join(dir, 'candidates', 'fleet-test', 'specs', `${e.strategy_sha256}.json`), 'utf8'));
    const prereg = JSON.parse(await readFile(join(dir, 'candidates', 'fleet-test', 'preregistrations', `${e.preregistration_sha256}.json`), 'utf8'));
    assert.equal(spec.schema_version, 2);
    assert.equal(ruleHash(spec), e.rule_sha256);
    assert.equal(prereg.strategy_sha256, e.strategy_sha256);
    const { preregistration_sha256, ...body } = prereg;
    assert.equal(digest(body), preregistration_sha256);
    assert.ok(prereg.created_at <= e.recorded_at);
    assert.equal(prereg.author.model, e.author.model_id);
  }
  const dup = entries.find(e => e.status === 'duplicate');
  assert.equal(entries[dup.duplicate_of].rule_sha256, dup.rule_sha256);
});

test('rerun appends to the same chain and tampering is detected', async t => {
  const dir = await temp(t);
  const first = await runFleet({ campaignId: 'fleet-chain', stateDir: dir, rounds: 1, now });
  assert.equal(first.registered, 4);
  const second = await runFleet({ campaignId: 'fleet-chain', stateDir: dir, rounds: 1, now });
  assert.equal(second.entries, 8);
  const lines = (await readFile(second.registry, 'utf8')).split('\n').filter(Boolean);
  const edited = JSON.parse(lines[0]); edited.status = 'rejected'; lines[0] = JSON.stringify(edited);
  await writeFile(second.registry, lines.join('\n') + '\n');
  await assert.rejects(readRegistry(second.registry), /registry_tampered/);
});

test('loose preregistration, missing preregistration and forbidden tools reject without registering', async t => {
  const dir = await temp(t); const base = fixtureFleetAdapter(); let n = 0;
  const adapter = { provider: 'fixture', model_id: 'probe', async generate(input) {
    const out = await base.generate(input); n++;
    if (n === 1) out.tool_calls[1].arguments.preregistration.pass_criteria.max_drawdown_pct = '40';
    if (n === 2) out.tool_calls.pop();
    if (n === 3) out.tool_calls[1].arguments.preregistration.cost_model.slippage_bps_per_side = '1';
    if (n === 4) out.tool_calls.push({ name: 'place_order', arguments: {} });
    return out;
  } };
  const result = await runFleet({ campaignId: 'fleet-reject', stateDir: dir, rounds: 1, adapter, now });
  assert.equal(result.registered, 0);
  assert.deepEqual(result.rejections.map(r => r.code), ['criteria_too_loose', 'not_preregistered', 'cost_floor', 'forbidden_tool']);
});

test('paid provider stays behind the existing opt-in flag', () => {
  assert.throws(() => createModelAdapter('deepseek', { TRADING_DESK_MODEL_ID: 'm', DEEPSEEK_API_KEY: 'x' }), /paid_inference_disabled/);
});

test('bound fleet preregistrations are accepted by the Python walk-forward validator and rejected attempts count', async t => {
  const dir = await temp(t);
  const datasetBindings = Object.fromEntries(['SPY@1d', 'QQQ@15m', 'GLD@1h'].map(key => [key, {
    development: 'a'.repeat(64), validation: 'b'.repeat(64), holdout_id: 'sealed-test-window'
  }]));
  const result = await runFleet({ campaignId: 'fleet-bound', stateDir: dir, rounds: 1, now, datasetBindings });
  assert.equal(result.registered, 4); assert.equal(result.bound_for_walkforward, 4);
  const entries = await readRegistry(result.registry);
  const { spawnSync } = await import('node:child_process');
  const { fileURLToPath } = await import('node:url');
  const app = fileURLToPath(new URL('../../', import.meta.url));
  for (const e of entries) {
    const spec = JSON.parse(await readFile(join(dir, 'candidates', 'fleet-bound', 'specs', `${e.strategy_sha256}.json`), 'utf8'));
    const prereg = JSON.parse(await readFile(join(dir, 'candidates', 'fleet-bound', 'preregistrations', `${e.preregistration_sha256}.json`), 'utf8'));
    assert.equal(prereg.author.role, 'author'); assert.equal(prereg.author.slot, e.author.slot);
    assert.deepEqual(prereg.datasets, datasetBindings[`${spec.symbol}@${spec.timeframe}`]);
    const p = spawnSync(join(app, 'backtest/.venv/bin/python'), ['-c', 'import sys,json;from backtest.walkforward import validate_preregistration;v=json.load(sys.stdin);validate_preregistration(v[0],v[1],"2026-12-01T00:00:00.000Z")'],
      { cwd: app, input: JSON.stringify([prereg, spec]), encoding: 'utf8', timeout: 15000 });
    assert.equal(p.status, 0, p.stderr);
  }
  await assert.rejects(runFleet({ campaignId: 'fleet-bound', stateDir: dir, rounds: 1, now,
    datasetBindings: { ...datasetBindings, 'SPY@1d': { ...datasetBindings['SPY@1d'], validation: 'c'.repeat(64) } } }), /record_conflict/);
  await assert.rejects(runFleet({ campaignId: 'fleet-null-holdout', stateDir: dir, rounds: 1, now,
    datasetBindings: { 'SPY@1d': { ...datasetBindings['SPY@1d'], holdout_id: null } } }), /invalid_dataset_bindings/);
  await assert.rejects(readFile(join(dir, 'candidates', 'fleet-null-holdout.lock')), { code: 'ENOENT' });
  // Failure must release the foreground writer lock.
  await assert.rejects(readFile(join(dir, 'candidates', 'fleet-bound.lock')), { code: 'ENOENT' });
});

test('invalid specs count in the search size while duplicate rules do not', async t => {
  const dir = await temp(t), base = fixtureFleetAdapter(); let calls = 0;
  const adapter = { ...base, async generate(input) {
    const out = await base.generate(input); if (++calls === 1) out.tool_calls[0].arguments.strategy.parameters.enter_z = '-9'; return out;
  } };
  const result = await runFleet({ campaignId: 'count-reject', stateDir: dir, rounds: 1, now, adapter });
  assert.equal(result.registered, 3); assert.equal(result.rejected, 1); assert.equal(result.search_size_n, 4);
});
