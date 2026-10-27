import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, rm, readFile, readdir, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { runFleet, fixtureFleetAdapter, readRegistry, freezeObservation } from '../../runtime/author-fleet.mjs';
import { openExposure, readExposure, globalSearchSize } from '../../runtime/exposure.mjs';
import { loadCampaign, runReferee } from '../../runtime/referee.mjs';
import { exportLeaderboard, buildLeaderboard } from '../../runtime/leaderboard.mjs';
import { openLedger, readLedger, toRows, ingestFleetAttempts, trainingRows, validateState, lineageGroup } from '../../runtime/episodes.mjs';
import { ACCEPTANCE_CONTRACT_SHA256, RESEARCH_ACCEPTED_V1, evaluateAcceptance, acceptedLineages, requireContract } from '../../runtime/acceptance.mjs';
import { digest } from '../../runtime/contracts.mjs';

let clock = 0;
const now = () => new Date(Date.UTC(2026, 9, 26, 12, 0, 0) + clock++).toISOString();
async function temp(t) { const d = await mkdtemp(join(tmpdir(), 'fair-')); t.after(() => rm(d, { recursive: true, force: true })); return d; }
// Shift every lookback so a second campaign proposes genuinely different strategies.
const shifted = by => { const base = fixtureFleetAdapter(); return { ...base, async generate(input) {
  const out = await base.generate(input); const p = out.tool_calls[0].arguments.strategy.parameters;
  for (const k of ['lookback_bars', 'fast_window', 'entry_channel_bars']) if (Number.isInteger(p[k])) p[k] += by;
  return out; } }; };
async function tree(dir) {
  const out = {};
  async function walk(d) { for (const name of await readdir(d)) { const p = join(d, name); if ((await stat(p)).isDirectory()) await walk(p); else out[p] = createHash('sha256').update(await readFile(p)).digest('hex'); } }
  await walk(dir); return out;
}

test('item 1: every author in one round sees the same frozen observation', async t => {
  const dir = await temp(t);
  await runFleet({ campaignId: 'snap', stateDir: dir, rounds: 2, now });
  const entries = await readRegistry(join(dir, 'candidates', 'snap.jsonl'));
  const round1 = entries.slice(0, 4), round2 = entries.slice(4, 8);
  assert.equal(new Set(round1.map(e => e.author.observation_sha256)).size, 1);
  assert.equal(new Set(round2.map(e => e.author.observation_sha256)).size, 1);
  assert.notEqual(round1[0].author.observation_sha256, round2[0].author.observation_sha256);
  assert.equal(round1[0].author.observation_sha256, freezeObservation('snap', []).observation_sha256, 'round 1 sees an empty registry, not earlier same-round authors');
});

test('item 2: N is global across campaigns, retries count once, and the blind-selection cap survives a rename', async t => {
  const dir = await temp(t);
  const a = await runFleet({ campaignId: 'camp-a', stateDir: dir, rounds: 1, now });
  const b = await runFleet({ campaignId: 'camp-b', stateDir: dir, rounds: 1, now, adapter: shifted(3) });
  assert.equal(a.search_size_n, 4); assert.equal(b.search_size_n, 4); assert.equal(b.global_search_size_n, 8);
  const c = await runFleet({ campaignId: 'camp-c', stateDir: dir, rounds: 1, now }); // same strategies as camp-a
  assert.equal(c.global_search_size_n, 8);
  assert.equal(globalSearchSize(await readExposure(dir)), 8);
  const { searchN, candidates } = await loadCampaign({ stateDir: dir, campaignId: 'camp-a' });
  assert.equal(searchN, 8);
  const target = candidates.find(x => x.spec.timeframe === '1d');
  const seal = (body, f) => ({ ...body, [f]: digest(body) });
  const verdict = N => seal({ kind: 'walkforward_verdict', partition: 'development', strategy_sha256: target.spec.strategy_sha256, preregistration_sha256: target.prereg.preregistration_sha256,
    N, adjustment: { passed: true, adjusted_pvalue: '0.01' }, passed: true, failures: [], data_classification: 'historical_research' }, 'verdict_sha256');
  const r = await runReferee({ stateDir: dir, campaignId: 'camp-a', verdicts: [verdict(4)], now });
  assert.ok(r.verdicts.find(v => v.strategy_sha256 === target.spec.strategy_sha256).failing.criteria.includes('stale_search_size'));
  const exp = await openExposure(dir, { now });
  try {
    const cfg = id => ({ experiment_id: id, families: ['trend_v1'], seeds: [11] });
    await exp.recordBlindSelection({ experimentConfig: cfg('x1'), sourceId: 'sel-1' });
    await exp.recordBlindSelection({ experimentConfig: cfg('x2'), sourceId: 'sel-2' });
    await exp.recordBlindSelection({ experimentConfig: cfg('x3'), sourceId: 'sel-3' });
    await assert.rejects(exp.recordBlindSelection({ experimentConfig: cfg('renamed'), sourceId: 'sel-4' }), /blind_selection_cap/);
    await exp.recordBlindSelection({ experimentConfig: { ...cfg('other'), seeds: [12] }, sourceId: 'sel-5' }); // a genuinely different experiment
  } finally { await exp.close(); }
});

test('item 3: admission imitation rows are labelled and never trainable; prospective rows are', async t => {
  const dir = await temp(t);
  await runFleet({ campaignId: 'labels', stateDir: dir, rounds: 1, now });
  const ledger = await openLedger({ stateDir: dir, campaignId: 'labels', now });
  await ingestFleetAttempts({ ledger, stateDir: dir, campaignId: 'labels', budget: { model_calls: 50, backtests: 50 } });
  const state = { stage: 'not_registered', attempts_in_lineage: 0, prior_errors: [], candidate: { family: 'momentum_long', timeframe: '1d', symbol: 'SPY', strategy_sha256: null, rule_sha256: null },
    budget: { model_calls_spent: 0, backtests_spent: 0, model_calls_remaining: 1, backtests_remaining: 1 } };
  const d = await ledger.recordDecision({ episodeId: 'pro-1', lineage: lineageGroup({ family: 'momentum_long', timeframe: '1d' }), state, offeredActions: ['test_candidate', 'abstain'],
    chosenAction: 'test_candidate', chooser: { kind: 'rules', id: 'campaign', version: '1' }, worker: { role: 'author_codex', slot: 'codex', provider: 'cli_codex', model_id: 'm', agent_written: true }, labelKind: 'prospective' });
  await ledger.recordOutcome({ decisionId: d.decision_id, status: 'completed', stageReached: 'registered' });
  await ledger.close();
  const rows = toRows(await readLedger(ledger.path));
  const imitation = rows.filter(r => r.label_kind === 'admission_imitation');
  assert.equal(imitation.length, 4);
  assert.throws(() => trainingRows(rows), /label_kind_not_trainable/);
  assert.equal(trainingRows(rows.filter(r => r.label_kind === 'prospective')).length, 1);
  assert.throws(() => trainingRows([{ label_status: 'resolved' }]), /label_kind_unknown/);
});

test('item 4: holdout-derived stages, free-text labels and tainted evidence are refused', () => {
  const ok = { stage: 'validation_passed', attempts_in_lineage: 0, prior_errors: [], candidate: { family: 'momentum_long', timeframe: '1d', symbol: 'PASS1', strategy_sha256: null, rule_sha256: null },
    budget: { model_calls_spent: 0, backtests_spent: 0, model_calls_remaining: 1, backtests_remaining: 1 } };
  const at = '2026-10-26T12:00:00.000Z';
  validateState(ok, at);
  for (const stage of ['holdout_passed', 'paper_passed', 'eligible_for_live']) assert.throws(() => validateState({ ...ok, stage }, at), /holdout_taint/);
  assert.throws(() => validateState({ ...ok, candidate: { ...ok.candidate, symbol: 'this strategy passed the holdout' } }, at), /invalid_episode_record/);
  assert.throws(() => validateState({ ...ok, candidate: { ...ok.candidate, family: 'winner' } }, at), /invalid_episode_record/);
  const ev = { partition: 'validation', taint: 'none', observed_at: at, passed: true, failures: [], adjusted_pvalue: '0.01', verdict_sha256: 'a'.repeat(64) };
  validateState({ ...ok, evidence: [ev] }, at);
  assert.throws(() => validateState({ ...ok, evidence: [{ ...ev, taint: 'holdout' }] }, at), /holdout_taint/);
  assert.throws(() => validateState({ ...ok, evidence: [{ ...ev, failures: ['passed holdout'] }] }, at), /invalid_episode_record/);
});

test('item 4: a private holdout verdict change cannot alter what authors see', async t => {
  const dir = await temp(t);
  await runFleet({ campaignId: 'private', stateDir: dir, rounds: 1, now });
  const before = freezeObservation('private', await readRegistry(join(dir, 'candidates', 'private.jsonl'))).observation_sha256;
  const { candidates } = await loadCampaign({ stateDir: dir, campaignId: 'private' });
  const c = candidates[0];
  const seal = (body, f) => ({ ...body, [f]: digest(body) });
  for (const passed of [true, false]) {
    const verdicts = ['development', 'validation', 'holdout'].map(partition => seal({ kind: 'walkforward_verdict', partition, strategy_sha256: c.spec.strategy_sha256,
      preregistration_sha256: c.prereg.preregistration_sha256, N: 99, adjustment: { passed, adjusted_pvalue: '0.01' }, passed: partition === 'holdout' ? passed : true, failures: [], data_classification: 'historical_research' }, 'verdict_sha256'));
    await runReferee({ stateDir: dir, campaignId: 'private', verdicts, now });
    assert.equal(freezeObservation('private', await readRegistry(join(dir, 'candidates', 'private.jsonl'))).observation_sha256, before);
  }
});

test('item 5: the leaderboard export writes nothing and matches the operator board', async t => {
  const dir = await temp(t);
  await runFleet({ campaignId: 'board', stateDir: dir, rounds: 1, now });
  const before = await tree(dir);
  const { board, paths } = await exportLeaderboard({ stateDir: dir, campaignId: 'board', date: '2026-10-26', now: () => '2026-10-26T12:00:00.000Z' });
  assert.deepEqual(await tree(dir), before);
  assert.equal(paths, null); assert.equal(board.read_only, true); assert.deepEqual(board.promotions, []);
  const operator = await buildLeaderboard({ stateDir: dir, campaignId: 'board', date: '2026-10-26', now: () => '2026-10-26T12:00:00.000Z' });
  assert.deepEqual(board.ranked, operator.board.ranked); assert.deepEqual(board.unranked, operator.board.unranked); assert.equal(board.N, operator.board.N);
  assert.notDeepEqual(await tree(dir), before, 'only the explicit operator command writes');
});

test('item 7: the acceptance contract is frozen, lineage-capped and fails closed', () => {
  assert.match(ACCEPTANCE_CONTRACT_SHA256, /^[0-9a-f]{64}$/);
  requireContract(ACCEPTANCE_CONTRACT_SHA256);
  assert.throws(() => requireContract('0'.repeat(64)), /acceptance_contract_changed/);
  const good = { valid_spec: true, authentic_receipt: true, independent_replay_match: true, adjusted_pvalue: '0.01', independent_trades: 40,
    max_drawdown_pct: '10', excess_return_vs_buy_and_hold_pct: '2', cost_stress_multiplier: 2, excess_under_cost_stress_pct: '0.5' };
  assert.equal(evaluateAcceptance(good).accepted, true);
  assert.deepEqual(evaluateAcceptance({ ...good, excess_under_cost_stress_pct: '-0.1' }).failures, ['cost_stress']);
  assert.deepEqual(evaluateAcceptance({}).accepted, false);
  const stricter = { ...RESEARCH_ACCEPTED_V1, requires: { ...RESEARCH_ACCEPTED_V1.requires, min_independent_trades: 50 } };
  assert.equal(evaluateAcceptance(good, stricter).accepted, false, 'a changed threshold changes the verdict');
  const rows = [{ id: 'a', lineage_group: 'lin:1', evidence: good }, { id: 'b', lineage_group: 'lin:1', evidence: good }, { id: 'c', lineage_group: 'lin:2', evidence: { ...good, valid_spec: false } }];
  const out = acceptedLineages(rows);
  assert.deepEqual(out.survivors, [{ lineage_group: 'lin:1', id: 'a' }]); assert.equal(out.lineages_searched, 2);
  assert.equal(out.authority, 'research_label_only_never_moves_a_referee_stage');
});
