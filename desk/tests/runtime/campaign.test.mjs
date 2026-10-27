import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, readFile, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { runCampaign, freezeCampaignPlan, candidatesFromFleet } from '../../runtime/campaign.mjs';
import { runFleet } from '../../runtime/author-fleet.mjs';
import { DeskError } from '../../runtime/contracts.mjs';
import { readLedger, toRows, exportEpisodes } from '../../runtime/episodes.mjs';
import { planFor, proposal, failedJob } from '../fixtures/campaign.mjs';

async function temp(t) { const root = await mkdtemp(join(tmpdir(), 'campaign-')); t.after(() => rm(root, { recursive: true, force: true })); return root; }
const delay = ms => new Promise(done => setTimeout(done, ms));
const failed = async ({ message }) => failedJob(message);

test('one manager bounds concurrency to three and includes all failed/timed-out attempts in N and episodes', async t => {
  const root = await temp(t); const plan = await planFor(root, Array.from({ length: 7 }, (_, i) => proposal(`candidate-${i}`)));
  let active = 0, peak = 0; const directories = new Set();
  const summary = await runCampaign({ plan, stateDir: root, jobRunner: async ({ dir, message }) => {
    directories.add(dir); active++; peak = Math.max(peak, active); await delay(15); active--;
    return failedJob(message, 'timed_out', 'fixture_timeout');
  } });
  assert.equal(peak, 3); assert.equal(active, 0); assert.equal(directories.size, 7);
  assert.equal(summary.N, 7); assert.equal(summary.registry_attempt_count, summary.ledger_decision_count);
  assert.equal(summary.ledger_outcome_count, 7); assert.equal(summary.episodes.timed_out, 7);
  assert.ok(summary.candidates.every(c => c.verdict.N === 7 && !c.verdict.passed && c.verdict.qualification === 'research_only'));
  assert.equal(summary.survivor_lineages, 0); assert.equal(summary.orders, false); assert.equal(summary.paper, false);
  const rows = toRows(await readLedger(summary.episodes.path.replace('.export.jsonl', '.jsonl')));
  assert.equal(rows.length, 7); assert.ok(rows.every(r => r.outcome.status === 'timed_out'));
});

test('finished jobs are not repeated on resume and episode bytes stay identical', async t => {
  const root = await temp(t); const plan = await planFor(root); let calls = 0;
  const first = await runCampaign({ plan, stateDir: root, jobRunner: async args => { calls++; return failed(args); } });
  const before = await readFile(first.episodes.path, 'utf8');
  const second = await runCampaign({ plan, stateDir: root, jobRunner: async () => { throw Error('must not run'); } });
  assert.equal(calls, 2); assert.equal(second.N, 2); assert.equal(second.attempts_sha256, first.attempts_sha256);
  assert.equal(await readFile(second.episodes.path, 'utf8'), before);
  assert.equal(second.episodes.export_sha256, first.episodes.export_sha256);
  assert.equal(second.summary_sha256, first.summary_sha256);
  const again = await exportEpisodes({ stateDir: join(second.episodes.path, '../..'), campaignId: plan.campaign_id });
  assert.equal(again.export_sha256, first.episodes.export_sha256);
});

test('active manager lock blocks a second manager and failed work is drained before release', async t => {
  const root = await temp(t); const plan = await planFor(root); let entered; const gate = new Promise(r => { entered = r; });
  let finish; const waiting = new Promise(r => { finish = r; });
  const first = runCampaign({ plan, stateDir: root, jobRunner: async args => { entered(); await waiting; return failed(args); } });
  await gate;
  await assert.rejects(runCampaign({ plan, stateDir: root, jobRunner: failed }), /campaign_manager_active/);
  finish(); await first;
});

test('STOP pauses admissions, and removing it resumes the same immutable campaign', async t => {
  const root = await temp(t); const plan = await planFor(root); plan.limits.concurrency = 1;
  const dir = join(root, 'campaigns', plan.campaign_id);
  const first = await runCampaign({ plan, stateDir: root, jobRunner: failed, onCheckpoint: async e => {
    if (e.kind === 'result_committed') await writeFile(join(dir, 'STOP'), 'stop');
  } });
  assert.equal(first.status, 'paused'); assert.equal(first.N, 1); assert.equal(first.queued, 1);
  await rm(join(dir, 'STOP'));
  const second = await runCampaign({ plan, stateDir: root, jobRunner: failed });
  assert.equal(second.status, 'completed'); assert.equal(second.N, 2); assert.equal(second.ledger_decision_count, 2);
});

test('resumption refuses changed plans, evaluator inputs and edited registry journals', async t => {
  const root = await temp(t); const plan = await planFor(root); await runCampaign({ plan, stateDir: root, jobRunner: failed });
  await assert.rejects(runCampaign({ plan: { ...plan, initial_cash: '20000' }, stateDir: root, jobRunner: failed }), /campaign_plan_drift/);
  const db = new DatabaseSync(join(root, 'campaigns', plan.campaign_id, 'campaign.sqlite'));
  const event = JSON.parse(db.prepare('SELECT payload FROM journal WHERE seq=0').get().payload); event.candidate_id = 'edited';
  db.prepare('UPDATE journal SET payload=? WHERE seq=0').run(JSON.stringify(event)); db.close();
  await assert.rejects(runCampaign({ plan, stateDir: root, jobRunner: failed }), /campaign_integrity_failure/);
});

test('invalid concurrency, budgets, paper/order keys and weakened screens reject before admission', async t => {
  const root = await temp(t); const plan = await planFor(root);
  for (const n of [0, 4]) await assert.rejects(freezeCampaignPlan({ ...plan, limits: { ...plan.limits, concurrency: n } }), /invalid_campaign_limits/);
  await assert.rejects(freezeCampaignPlan({ ...plan, limits: { ...plan.limits, max_attempts: 1 } }), /campaign_attempt_limit/);
  await assert.rejects(freezeCampaignPlan({ ...plan, paper: true }), /invalid_campaign_plan/);
  await assert.rejects(freezeCampaignPlan({ ...plan, orders: true }), /invalid_campaign_plan/);
  await assert.rejects(freezeCampaignPlan({ ...plan, screen: { ...plan.screen, min_independent_trades: 1 } }), /campaign_screen_changed/);
});

test('holdout and validation registry entries are refused before any job starts', async t => {
  const root = await temp(t); const plan = await planFor(root);
  const registry = JSON.parse(await readFile(plan.registry, 'utf8'));
  for (const partition of ['validation', 'holdout']) {
    registry.datasets[plan.dataset_id].partition = partition; await writeFile(plan.registry, JSON.stringify(registry));
    await assert.rejects(runCampaign({ plan, stateDir: root, jobRunner: failed }), /dataset_access_denied/);
  }
});

test('unverified external candidates cannot claim real agent authorship', async t => {
  const root = await temp(t); const plan = await planFor(root);
  plan.candidates[0].source = { kind: 'unverified_external', agent_written: true };
  await assert.rejects(freezeCampaignPlan(plan), /invalid_campaign_plan/);
});

test('a timed-out fleet attempt with no spec remains in the campaign N and exported episodes', async t => {
  const root = await temp(t);
  await assert.rejects(runFleet({ campaignId: 'timed-out-source', stateDir: root, rounds: 1,
    adapter: { provider: 'fixture', model_id: 'fault-timeout', async generate() { throw new DeskError('provider_result_unknown'); } } }), /provider_result_unknown/);
  const candidates = await candidatesFromFleet({ stateDir: root, campaignId: 'timed-out-source' });
  const plan = await planFor(root, candidates);
  const summary = await runCampaign({ plan, stateDir: root, jobRunner: async () => { throw Error('no spec to run'); } });
  assert.equal(summary.N, 1); assert.equal(summary.counts.timed_out, 1);
  assert.equal(summary.episodes.decisions, 1); assert.equal(summary.episodes.timed_out, 1);
  assert.equal(summary.candidates[0].agent_written, false);
});

test('edited episode exports are refused on resume rather than silently overwritten', async t => {
  const root = await temp(t); const plan = await planFor(root);
  const result = await runCampaign({ plan, stateDir: root, jobRunner: failed });
  await writeFile(result.episodes.path, 'edited\n');
  await assert.rejects(runCampaign({ plan, stateDir: root, jobRunner: failed }), /campaign_episode_integrity_failure/);
});
