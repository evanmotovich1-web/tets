import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, readFile, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { runFleet, fixtureFleetAdapter } from '../../runtime/author-fleet.mjs';
import { runReferee, loadCampaign, STAGES } from '../../runtime/referee.mjs';
import { buildLeaderboard } from '../../runtime/leaderboard.mjs';
import { digest } from '../../runtime/contracts.mjs';
import { verifyRecord } from '../../runtime/execution/contracts.mjs';

let tick = 0;
const now = () => new Date(Date.UTC(2026, 9, 25, 14, 0, 0) + tick++).toISOString();
const seal = (body, field) => ({ ...body, [field]: digest(body) });
const PAPER = { min_market_days: 5, min_round_trips: 10, max_fill_model_drift_bps: '25', min_excess_return_vs_buy_and_hold_pct: '0' };

async function campaign(t, { paperCriteria = null } = {}) {
  const dir = await mkdtemp(join(tmpdir(), 'referee-')); t.after(() => rm(dir, { recursive: true, force: true }));
  const base = fixtureFleetAdapter();
  const adapter = { ...base, async generate(input) {
    const out = await base.generate(input);
    if (paperCriteria) out.tool_calls[1].arguments.preregistration.paper_criteria = paperCriteria;
    return out;
  } };
  await runFleet({ campaignId: 'ref', stateDir: dir, rounds: 1, now, adapter });
  const { candidates, searchN } = await loadCampaign({ stateDir: dir, campaignId: 'ref' });
  return { dir, candidates, searchN, target: candidates.find(c => c.spec.timeframe === '1d') };
}

function verdict(c, partition, { N = 4, passed = true, data = 'historical_research', p = '0.01' } = {}) {
  return seal({ kind: 'walkforward_verdict', partition, strategy_sha256: c.spec.strategy_sha256, preregistration_sha256: c.prereg.preregistration_sha256,
    result_sha256: 'a'.repeat(64), N, search_family_sha256: 'b'.repeat(64), adjustment: { passed, adjusted_pvalue: p }, passed, failures: passed ? [] : ['holm_adjustment'],
    data_classification: data, qualification: 'research_only', promoting: false }, 'verdict_sha256');
}
const full = (c, opts) => ['development', 'validation', 'holdout'].map(p => verdict(c, p, opts));
const paperVerdict = (c, passed = true) => seal({ kind: 'paper_verdict', strategy_sha256: c.spec.strategy_sha256,
  preregistration_sha256: c.prereg.preregistration_sha256, passed, failures: passed ? [] : ['min_round_trips'] }, 'paper_verdict_sha256');

test('historical_unverified cannot produce holdout_passed or promotion even with sealed passing research verdicts', async t => {
  const { dir, target } = await campaign(t);
  for (const unverifiedPartition of ['development', 'validation', 'holdout']) {
    const verdicts = full(target).map(v => v.partition === unverifiedPartition ? verdict(target, v.partition, { data: 'historical_unverified' }) : v);
    const result = await runReferee({ stateDir: dir, campaignId: 'ref', verdicts, now });
    const judged = result.verdicts.find(v => v.strategy_sha256 === target.spec.strategy_sha256);
    assert.notEqual(judged.stage, 'holdout_passed');
    assert.notEqual(judged.stage, 'eligible_for_live');
    assert.ok(judged.failing.criteria.includes('synthetic_data'));
    assert.equal(result.promotions.length, 0);
  }
});

test('ladder has no shadow stage and paper is optional', () => {
  assert.deepEqual(STAGES, ['registered', 'dev_passed', 'validation_passed', 'holdout_passed', 'paper_passed', 'eligible_for_live']);
  assert.ok(!STAGES.some(s => s.includes('shadow')));
});

test('paper disabled: holdout pass goes straight to eligible_for_live with a verifiable promotion, never a live record', async t => {
  const { dir, target, candidates } = await campaign(t);
  assert.equal(target.prereg.schema_version, 2); assert.equal(target.prereg.paper_criteria, null);
  assert.ok(!('shadow_criteria' in target.prereg));
  const result = await runReferee({ stateDir: dir, campaignId: 'ref', verdicts: full(target), now });
  const v = result.verdicts.find(x => x.strategy_sha256 === target.spec.strategy_sha256);
  assert.equal(v.stage, 'eligible_for_live', JSON.stringify(v.failing));
  assert.equal(v.paper.enabled, false); assert.equal(v.backtest.partition, 'holdout'); assert.ok(!('shadow' in v));
  assert.equal(result.promotions.length, 1);
  const promo = JSON.parse(await readFile(join(dir, 'referee', 'ref', 'promotions', `${target.spec.strategy_sha256}.json`), 'utf8'));
  assert.ok(verifyRecord(promo)); assert.equal(promo.kind, 'promotion'); assert.equal(promo.stage, 'eligible_for_live');
  assert.equal(promo.author_role, 'referee'); assert.equal(promo.revoked, false);
  assert.ok(result.verdicts.every(x => x.qualification === 'research_only' && x.promoting === false));
  assert.ok(result.verdicts.filter(x => x !== v).every(x => x.stage === 'registered' && x.failing.criteria[0] === 'development_not_evaluated'));
  assert.equal(result.verdicts.length, candidates.length);
});

test('paper enabled: needs preregistered paper criteria and a sealed passing paper verdict', async t => {
  const plain = await campaign(t);
  const missing = await runReferee({ stateDir: plain.dir, campaignId: 'ref', verdicts: full(plain.target), paper: { enabled: true }, now });
  const m = missing.verdicts.find(x => x.strategy_sha256 === plain.target.spec.strategy_sha256);
  assert.equal(m.stage, 'holdout_passed'); assert.deepEqual(m.failing, { stage: 'paper_passed', criteria: ['paper_criteria_missing'] });

  const { dir, target } = await campaign(t, { paperCriteria: PAPER });
  assert.deepEqual(target.prereg.paper_criteria, PAPER);
  const cases = [
    [[], 'paper_not_evaluated'],
    [[{ ...paperVerdict(target), passed: false }], 'paper_integrity_failure'],
    [[paperVerdict(target, false)], 'min_round_trips']
  ];
  for (const [paperVerdicts, code] of cases) {
    const r = await runReferee({ stateDir: dir, campaignId: 'ref', verdicts: full(target), paperVerdicts, paper: { enabled: true }, now });
    const v = r.verdicts.find(x => x.strategy_sha256 === target.spec.strategy_sha256);
    assert.equal(v.stage, 'holdout_passed', code); assert.ok(v.failing.criteria.includes(code), JSON.stringify(v.failing));
    assert.equal(r.promotions.length, 0);
  }
  const pass = await runReferee({ stateDir: dir, campaignId: 'ref', verdicts: full(target), paperVerdicts: [paperVerdict(target)], paper: { enabled: true }, now });
  const v = pass.verdicts.find(x => x.strategy_sha256 === target.spec.strategy_sha256);
  assert.equal(v.stage, 'eligible_for_live'); assert.equal(v.paper.paper_verdict_sha256, paperVerdict(target).paper_verdict_sha256);
  assert.equal(pass.promotions.length, 1);
});

test('stale N, synthetic data and a missing holdout each block promotion', async t => {
  const { dir, target } = await campaign(t);
  const cases = [
    [[verdict(target, 'development', { N: 2 })], 'registered', 'stale_search_size'],
    [[verdict(target, 'development', { data: 'synthetic' })], 'registered', 'synthetic_data'],
    [full(target).slice(0, 2), 'validation_passed', 'holdout_not_evaluated'],
    [[...full(target).slice(0, 2), verdict(target, 'holdout', { passed: false })], 'validation_passed', 'holm_adjustment']
  ];
  for (const [verdicts, stage, code] of cases) {
    const result = await runReferee({ stateDir: dir, campaignId: 'ref', verdicts, now });
    const v = result.verdicts.find(x => x.strategy_sha256 === target.spec.strategy_sha256);
    assert.equal(v.stage, stage, code); assert.ok(v.failing.criteria.includes(code), JSON.stringify(v.failing));
    assert.equal(result.promotions.length, 0);
  }
});

test('a tampered verdict is rejected', async t => {
  const { dir, target } = await campaign(t);
  const forged = full(target); forged[0] = { ...forged[0], N: 99 };
  const r = await runReferee({ stateDir: dir, campaignId: 'ref', verdicts: forged, now });
  assert.ok(r.verdicts.find(x => x.strategy_sha256 === target.spec.strategy_sha256).failing.criteria.includes('verdict_integrity_failure'));
});

test('demotion revokes the promotion and the same hash is never re-promoted', async t => {
  const { dir, target } = await campaign(t);
  await runReferee({ stateDir: dir, campaignId: 'ref', verdicts: full(target), now });
  const failing = await runReferee({ stateDir: dir, campaignId: 'ref', verdicts: [...full(target).slice(0, 2), verdict(target, 'holdout', { passed: false })], now });
  assert.equal(failing.revocations.length, 1);
  const path = join(dir, 'referee', 'ref', 'promotions', `${target.spec.strategy_sha256}.json`);
  assert.equal(JSON.parse(await readFile(path, 'utf8')).revoked, true);
  const again = await runReferee({ stateDir: dir, campaignId: 'ref', verdicts: full(target), now });
  assert.equal(again.promotions.length, 0);
  assert.equal(JSON.parse(await readFile(path, 'utf8')).revoked, true);
});

test('backtest-only leaderboard ranks validation and holdout verdicts, never development alone', async t => {
  const { dir, candidates } = await campaign(t);
  const [a, b, c] = candidates;
  const verdicts = [...full(a, { p: '0.02' }), ...full(b, { p: '0.01' }).slice(0, 2), verdict(c, 'development')];
  const vpath = join(dir, 'verdicts.jsonl');
  await writeFile(vpath, verdicts.map(v => JSON.stringify(v)).join('\n') + '\n');
  const { board, markdown, paths } = await buildLeaderboard({ stateDir: dir, campaignId: 'ref', verdictPaths: [vpath], date: '2026-10-30', now });
  assert.equal(board.ranking_basis, 'backtest_only'); assert.equal(board.paper_enabled, false);
  assert.deepEqual(board.ranked.map(r => r.strategy_sha256), [a.spec.strategy_sha256, b.spec.strategy_sha256]);
  assert.equal(board.ranked[0].stage, 'eligible_for_live'); assert.equal(board.ranked[1].backtest.partition, 'validation');
  const devOnly = board.unranked.find(r => r.strategy_sha256 === c.spec.strategy_sha256);
  assert.equal(devOnly.rank, null); assert.equal(devOnly.reason, 'development_only');
  assert.ok(board.unranked.filter(r => r !== devOnly).every(r => r.reason === 'no_backtest_verdict'));
  assert.match(markdown, /Backtest-only ranking/); assert.match(markdown, /Evan's separate written approval/);
  assert.doesNotMatch(markdown, /shadow \(dry-run\)|Shadow days/);
  assert.match(await readFile(paths.markdown, 'utf8'), /\| 1 \|/);
  await assert.rejects(buildLeaderboard({ stateDir: dir, campaignId: 'ref', verdictPaths: [vpath], paperPaths: [vpath], now }), /paper_disabled/);
});
