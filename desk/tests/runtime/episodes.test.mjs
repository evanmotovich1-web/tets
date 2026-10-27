import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, readFile, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { runFleet, fixtureFleetAdapter, readRegistry } from '../../runtime/author-fleet.mjs';
import { DeskError } from '../../runtime/contracts.mjs';
import { ACTIONS, openLedger, readLedger, ingestFleetAttempts, toRows, exportEpisodes, lineageGroup, assertGroupedSplit } from '../../runtime/episodes.mjs';

let clock = 0;
const now = () => new Date(Date.UTC(2026, 9, 25, 16, 0, 0) + clock++).toISOString();
async function temp(t) { const d = await mkdtemp(join(tmpdir(), 'episodes-')); t.after(() => rm(d, { recursive: true, force: true })); return d; }
const BUDGET = { model_calls: 50, backtests: 50 };
const worker = { role: 'author_codex', slot: 'codex', provider: 'localhost', model_id: 'qwen3.5:2b', agent_written: true };
const chooser = { kind: 'rules', id: 'baseline_rules', version: '1' };
const state = (extra = {}) => ({ stage: 'dev_passed', attempts_in_lineage: 1, prior_errors: [],
  candidate: { family: 'momentum_long', timeframe: '1d', symbol: 'SPY', strategy_sha256: 'a'.repeat(64), rule_sha256: 'b'.repeat(64) },
  budget: { model_calls_spent: 3, backtests_spent: 1, model_calls_remaining: 47, backtests_remaining: 49 }, ...extra });

// A fleet run with one invalid spec, one duplicate rule set and one timed-out call.
async function messyFleet(dir) {
  const base = fixtureFleetAdapter(); let calls = 0;
  const adapter = { ...base, async generate(input) {
    const n = ++calls;
    if (n === 6) throw new DeskError('provider_result_unknown', { uncertain: true });
    const out = await base.generate(input);
    if (n === 1) out.tool_calls[0].arguments.strategy.parameters.enter_z = '-9';
    return out;
  } };
  await runFleet({ campaignId: 'messy', stateDir: dir, rounds: 1, now }); // 4 clean attempts first
  await assert.rejects(runFleet({ campaignId: 'messy', stateDir: dir, rounds: 2, now, adapter }), /provider_result_unknown/);
  return readRegistry(join(dir, 'candidates', 'messy.jsonl'));
}

test('the action table is fixed and always offers abstain', () => {
  assert.deepEqual(ACTIONS, ['test_candidate', 'repair_spec', 'reject', 'request_more_evidence', 'escalate', 'abstain']);
});

test('every fleet attempt, including rejected, duplicate and timed-out, becomes exactly one resolved decision', async t => {
  const dir = await temp(t);
  const entries = await messyFleet(dir);
  const statuses = entries.map(e => e.status);
  assert.ok(statuses.includes('rejected') && statuses.includes('timed_out'), JSON.stringify(statuses));
  const ledger = await openLedger({ stateDir: dir, campaignId: 'messy', now });
  const first = await ingestFleetAttempts({ ledger, stateDir: dir, campaignId: 'messy', budget: BUDGET });
  const again = await ingestFleetAttempts({ ledger, stateDir: dir, campaignId: 'messy', budget: BUDGET });
  await ledger.close();
  assert.equal(first.added, entries.length); assert.equal(again.added, 0);
  const rows = toRows(await readLedger(ledger.path));
  assert.equal(rows.length, entries.length);
  assert.ok(rows.every(r => r.label_status === 'resolved'));
  const byStatus = s => rows.filter(r => r.outcome.status === s).length;
  assert.equal(byStatus('completed'), entries.filter(e => e.status === 'registered').length);
  assert.equal(byStatus('rejected'), entries.filter(e => e.status === 'rejected').length);
  assert.equal(byStatus('timed_out'), 1);
  assert.equal(byStatus('duplicate'), entries.filter(e => e.status === 'duplicate').length);
  for (const r of rows) assert.ok(!JSON.stringify(r.state).includes('"outcome"'));
  assert.ok(rows.filter(r => r.outcome.status !== 'completed').every(r => r.chosen_action === 'reject'));
});

test('a timed-out author call is logged before the fleet stops and counts toward N', async t => {
  const dir = await temp(t);
  const entries = await messyFleet(dir);
  const timedOut = entries.find(e => e.status === 'timed_out');
  assert.deepEqual(timedOut.rejection, { stage: 'author', code: 'provider_result_unknown' });
  await assert.rejects(readFile(join(dir, 'candidates', 'messy.lock')), { code: 'ENOENT' });
});

test('visible state refuses outcome fields, holdout evidence and evidence from after the decision', async t => {
  const dir = await temp(t);
  const ledger = await openLedger({ stateDir: dir, campaignId: 'leak', now });
  const base = { episodeId: 'ep-1', lineage: lineageGroup({ family: 'momentum_long', timeframe: '1d' }), offeredActions: ['test_candidate', 'reject', 'abstain'], chosenAction: 'test_candidate', chooser, worker };
  const evidence = (partition, observed_at) => [{ partition, taint: 'none', observed_at, passed: true, failures: [], adjusted_pvalue: '0.01', verdict_sha256: 'c'.repeat(64) }];
  await assert.rejects(ledger.recordDecision({ ...base, state: { ...state(), outcome: 'completed' } }), /invalid_episode_record|outcome_leak/);
  await assert.rejects(ledger.recordDecision({ ...base, state: state({ candidate: { ...state().candidate, reward: 1 } }) }), /invalid_episode_record|outcome_leak/);
  await assert.rejects(ledger.recordDecision({ ...base, state: state({ evidence: evidence('holdout', '2026-10-25T00:00:00.000Z') }) }), /outcome_leak/);
  await assert.rejects(ledger.recordDecision({ ...base, state: state({ evidence: evidence('validation', '2099-01-01T00:00:00.000Z') }) }), /outcome_leak/);
  await assert.rejects(ledger.recordDecision({ ...base, offeredActions: ['test_candidate', 'reject'], state: state() }), /invalid_offered_actions/);
  await assert.rejects(ledger.recordDecision({ ...base, chosenAction: 'escalate', state: state() }), /action_not_offered/);
  const ok = await ledger.recordDecision({ ...base, state: state({ evidence: evidence('development', '2026-10-25T00:00:00.000Z') }) });
  assert.match(ok.decision_id, /^dec:/);
  assert.equal(ledger.records().length, 1, 'refused decisions write nothing');
  await ledger.close();
});

test('outcomes are written once, after the decision, and unresolved decisions stay labelled unresolved', async t => {
  const dir = await temp(t);
  const ledger = await openLedger({ stateDir: dir, campaignId: 'flow', now });
  const lineage = lineageGroup({ family: 'breakout_long', timeframe: '15m' });
  const a = await ledger.recordDecision({ episodeId: 'ep-a', lineage, state: state(), offeredActions: [...ACTIONS], chosenAction: 'request_more_evidence', chooser, worker });
  const b = await ledger.recordDecision({ episodeId: 'ep-b', lineage, state: state(), offeredActions: [...ACTIONS], chosenAction: 'abstain', chooser: { kind: 'random', id: 'seeded', version: '1', seed: 7 }, worker });
  await ledger.recordOutcome({ decisionId: a.decision_id, status: 'completed', stageReached: 'validation_passed', receipts: ['d'.repeat(64)], cost: { model_calls: 0, backtests: 1, wall_ms: 900 } });
  await assert.rejects(ledger.recordOutcome({ decisionId: a.decision_id, status: 'failed', stageReached: 'dev_passed' }), /outcome_already_recorded/);
  await assert.rejects(ledger.recordOutcome({ decisionId: 'dec:missing', status: 'failed', stageReached: 'dev_passed' }), /unknown_decision/);
  await ledger.close();
  const rows = toRows(await readLedger(ledger.path));
  assert.equal(rows.find(r => r.decision_id === a.decision_id).outcome.stage_reached, 'validation_passed');
  const pending = rows.find(r => r.decision_id === b.decision_id);
  assert.equal(pending.label_status, 'unresolved'); assert.equal(pending.outcome, null);
});

test('replay: the export is byte-identical across runs and tampering breaks the chain', async t => {
  const dir = await temp(t);
  await runFleet({ campaignId: 'replay', stateDir: dir, rounds: 1, now });
  const ledger = await openLedger({ stateDir: dir, campaignId: 'replay', now });
  await ingestFleetAttempts({ ledger, stateDir: dir, campaignId: 'replay', budget: BUDGET });
  await ledger.close();
  const one = await exportEpisodes({ stateDir: dir, campaignId: 'replay', out: join(dir, 'one.jsonl') });
  const two = await exportEpisodes({ stateDir: dir, campaignId: 'replay', out: join(dir, 'two.jsonl') });
  assert.equal(await readFile(join(dir, 'one.jsonl'), 'utf8'), await readFile(join(dir, 'two.jsonl'), 'utf8'));
  assert.equal(one.export_sha256, two.export_sha256); assert.equal(one.decisions, 4); assert.equal(one.unresolved, 0);
  const lines = (await readFile(ledger.path, 'utf8')).split('\n').filter(Boolean);
  const edited = JSON.parse(lines[0]); edited.chosen_action = 'reject'; lines[0] = JSON.stringify(edited);
  await writeFile(ledger.path, lines.join('\n') + '\n');
  await assert.rejects(readLedger(ledger.path), /ledger_tampered/);
});

test('lineage groups collapse parameters and symbols, and grouped splits refuse a crossing lineage', async t => {
  const a = lineageGroup({ family: 'momentum_long', timeframe: '1d' });
  assert.equal(a, lineageGroup({ family: 'momentum_long', timeframe: '1d' }));
  assert.notEqual(a, lineageGroup({ family: 'momentum_long', timeframe: '5m' }));
  assert.equal(lineageGroup({ family: 'momentum_long', timeframe: '5m' }), lineageGroup({ family: 'momentum_long', timeframe: '15m' }));
  assert.equal(lineageGroup({ lane: 'contrarian' }), 'lin:unparsed:contrarian');
  const rows = [{ lineage_group: a, i: 0 }, { lineage_group: a, i: 1 }, { lineage_group: 'lin:x', i: 2 }];
  assert.deepEqual(assertGroupedSplit(rows, r => (r.lineage_group === a ? 'train' : 'test')), { train: 1, test: 1 });
  assert.throws(() => assertGroupedSplit(rows, r => (r.i === 1 ? 'test' : 'train')), /lineage_crosses_split/);
});

test('one writer at a time', async t => {
  const dir = await temp(t);
  const ledger = await openLedger({ stateDir: dir, campaignId: 'lock', now });
  await assert.rejects(openLedger({ stateDir: dir, campaignId: 'lock', now }), /ledger_already_open/);
  await ledger.close();
  await (await openLedger({ stateDir: dir, campaignId: 'lock', now })).close();
});
