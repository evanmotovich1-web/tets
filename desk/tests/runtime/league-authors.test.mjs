import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { runAuthorGeneration, buildLaneAdapters, validateLanes, DEFAULT_LANES } from '../../league/authors.mjs';
import { fixtureFleetAdapter, readRegistry } from '../../runtime/author-fleet.mjs';
import { DeskError, digest } from '../../runtime/contracts.mjs';
import { scopedFixtureAuthor } from '../../league/proof.mjs';
import { runCampaign, candidatesFromFleet } from '../../runtime/campaign.mjs';
import { planFor, failedJob } from '../fixtures/campaign.mjs';
import { globalSearchSize, readExposure } from '../../runtime/exposure.mjs';

let clock = 0;
const now = () => new Date(Date.UTC(2026, 9, 26, 14, 0, 0) + clock++).toISOString();
async function temp(t) { const d = await mkdtemp(join(tmpdir(), 'lanes-')); t.after(() => rm(d, { recursive: true, force: true })); return d; }
const LANES = cap => ({ schema_version: 1, kind: 'league_author_lanes', max_attempts_per_author: cap, lanes: {
  author_codex: { provider: 'cli_codex', model_id: 'gpt-6.1-sol', price_per_mtok: { input: 1, output: 4 } },
  author_claude: { provider: 'cli_claude', model_id: 'claude-sonnet-5-5' },
  author_hermes: { provider: 'localhost', model_id: 'qwen3.5:2b', base_url: 'http://127.0.0.1:11434/v1' },
  author_pi: { provider: 'fixture', model_id: 'fixture-fleet-v1' } } });

// A mocked real-model transport: fixture strategy content, but reported as a real model call.
function realModel(provider, model_id, { failOn = () => false } = {}) {
  const base = fixtureFleetAdapter(); let calls = 0;
  return { provider, model_id, get calls() { return calls; }, async generate(input) {
    calls++; const fail = failOn(calls); if (fail) throw new DeskError(fail, { uncertain: fail === 'provider_result_unknown' });
    const out = await base.generate(input);
    return { ...out, provider, model_id, usage: { input_tokens: 900, output_tokens: 150 },
      provenance: { transport: provider === 'localhost' ? 'http' : 'cli', agent_written: true, model_version: `${model_id} mock`, raw_response_sha256: digest({ calls, provider }) } };
  } };
}

test('mixed generation: one author unavailable, one timing out, the rest continue under equal caps', async t => {
  const dir = await temp(t);
  const codex = realModel('cli_codex', 'gpt-6.1-sol');
  const claude = realModel('cli_claude', 'claude-sonnet-5-5', { failOn: () => 'author_unavailable' });
  const hermes = realModel('localhost', 'qwen3.5:2b', { failOn: n => (n === 2 ? 'provider_result_unknown' : false) });
  const factories = { cli_codex: () => codex, cli_claude: () => claude, localhost: () => hermes };
  const g = await runAuthorGeneration({ stateDir: dir, campaignId: 'mixed', lanes: LANES(3), factories, now });
  assert.deepEqual(g.lanes.map(l => [l.role, l.status]), [['author_codex', 'enabled'], ['author_claude', 'enabled'], ['author_hermes', 'enabled'], ['author_pi', 'enabled']]);
  const entries = await readRegistry(join(dir, 'candidates', 'mixed.jsonl'));
  const by = role => entries.filter(e => e.author.role === role);
  assert.equal(by('author_codex').length, 3); assert.equal(by('author_pi').length, 3);
  assert.deepEqual(by('author_claude').map(e => [e.status, e.rejection.code]), [['unavailable', 'author_unavailable']]);
  assert.equal(claude.calls, 1, 'an unavailable author is not retried or replaced');
  assert.deepEqual(by('author_hermes').map(e => e.status), ['registered', 'timed_out']);
  assert.equal(hermes.calls, 2);
  assert.ok(entries.every(e => e.author.agent_written === (e.author.provider !== 'fixture' && e.status !== 'unavailable' && e.status !== 'timed_out')));
  // Frozen view: every author in a round shares one observation hash.
  for (const round of [entries.filter(e => e.seq < 4)]) assert.equal(new Set(round.map(e => e.author.observation_sha256)).size, 1);
  assert.equal(g.summary.unavailable, 1); assert.equal(g.summary.timed_out, 1);
  assert.equal(g.authorship_host_verified, false, 'mocked adapters never count as host-verified authorship');
  assert.equal(g.lanes.find(l => l.role === 'author_pi').adapter_source, 'built_in');
  assert.equal(g.summary.global_search_size_n, entries.filter(e => e.status !== 'duplicate').length, 'unavailable and timed-out attempts count toward N');
});

test('every call is logged with tokens and cost; the log is hash-chained', async t => {
  const dir = await temp(t);
  const codex = realModel('cli_codex', 'gpt-6.1-sol');
  await runAuthorGeneration({ stateDir: dir, campaignId: 'calls', lanes: { ...LANES(2), lanes: { author_codex: LANES(2).lanes.author_codex, author_pi: LANES(2).lanes.author_pi } },
    factories: { cli_codex: () => codex }, now });
  const lines = (await readFile(join(dir, 'candidates', 'calls', 'calls.jsonl'), 'utf8')).split('\n').filter(Boolean).map(l => JSON.parse(l));
  assert.equal(lines.length, 4);
  let prev = '0'.repeat(64);
  for (const l of lines) { const { call_sha256, ...body } = l; assert.equal(l.prev_call_sha256, prev); assert.equal(digest(body), call_sha256); prev = call_sha256; }
  const c = lines.filter(l => l.provider === 'cli_codex');
  assert.equal(c.length, 2); assert.deepEqual(c[0].usage, { input_tokens: 900, output_tokens: 150 });
  assert.equal(c[0].cost_usd, 0.0015, '900 x $1/Mtok + 150 x $4/Mtok');
  assert.equal(lines.find(l => l.provider === 'fixture').cost_usd, null);
});

test('CLI lanes stay disabled without the opt-in flag and are recorded, not replaced', async t => {
  const dir = await temp(t);
  const lanes = { ...LANES(1), lanes: { author_codex: LANES(1).lanes.author_codex, author_claude: LANES(1).lanes.author_claude, author_pi: LANES(1).lanes.author_pi } };
  const g = await runAuthorGeneration({ stateDir: dir, campaignId: 'noflag', lanes, env: {}, now });
  assert.deepEqual(g.lanes.filter(l => l.status === 'disabled').map(l => [l.role, l.reason]), [['author_codex', 'cli_author_disabled'], ['author_claude', 'cli_author_disabled']]);
  const entries = await readRegistry(join(dir, 'candidates', 'noflag.jsonl'));
  assert.deepEqual([...new Set(entries.map(e => e.author.role))], ['author_pi']);
  const sealed = JSON.parse(await readFile(join(dir, 'candidates', 'noflag', 'generation.json'), 'utf8'));
  const { manifest_sha256, ...body } = sealed; assert.equal(digest(body), manifest_sha256);
  await assert.rejects(runAuthorGeneration({ stateDir: dir, campaignId: 'none', lanes: { ...LANES(1), lanes: { author_codex: LANES(1).lanes.author_codex } }, env: {}, now }), /no_author_available/);
});

test('a lane whose adapter reports a different model identity is refused', () => {
  const { lanes } = buildLaneAdapters(LANES(1), {}, { cli_codex: () => realModel('cli_codex', 'some-other-model'), cli_claude: () => realModel('cli_claude', 'claude-sonnet-5-5'),
    localhost: () => realModel('localhost', 'qwen3.5:2b') });
  assert.equal(lanes.find(l => l.role === 'author_codex').reason, 'lane_identity_mismatch');
  assert.throws(() => validateLanes({ ...LANES(1), max_attempts_per_author: 99 }), /invalid_author_cap/);
  assert.throws(() => validateLanes({ ...LANES(1), lanes: { author_x: { provider: 'fixture', model_id: 'f' } } }), /invalid_author_lanes/);
  validateLanes(DEFAULT_LANES);
});

test('bounded generation restart keeps unavailable lanes closed and preserves the four-attempt cap', async t => {
  const dir=await temp(t);const dead=realModel('localhost','qwen3.5:2b',{failOn:()=> 'provider_result_unknown'});
  const lanes={...LANES(4),lanes:{author_hermes:LANES(4).lanes.author_hermes,author_pi:LANES(4).lanes.author_pi}};
  for(let i=0;i<5;i++)await runAuthorGeneration({stateDir:dir,campaignId:'resume-authors',lanes,rounds:1,now,factories:{localhost:()=>dead}});
  const entries=await readRegistry(join(dir,'candidates/resume-authors.jsonl'));
  assert.equal(dead.calls,1);assert.equal(entries.filter(e=>e.author.role==='author_hermes').length,1);
  assert.equal(entries.filter(e=>e.author.role==='author_pi').length,4);
});

test('host data and replay scope are pinned before authoring; duplicate fleet attempts need no missing spec file', async t => {
  const dir=await temp(t),adapter=scopedFixtureAuthor();
  const make=()=>({ ...adapter, async generate(input) {
    const out=await adapter.generate(input);out.tool_calls[0].arguments.strategy.parameters={fast_window:2,slow_window:9};return out;
  }});
  const lanes={schema_version:1,kind:'league_author_lanes',max_attempts_per_author:2,lanes:{author_pi:{provider:'fixture',model_id:'fixture-fleet-v1'}}};
  const scope={symbol:'SPY',timeframe:'1d',family:'sma_cross_long',quantity:'1',order_type:'market'};
  await runAuthorGeneration({stateDir:dir,campaignId:'scope',lanes,rounds:2,now,researchScope:scope,factories:{fixture:make}});
  const candidates=await candidatesFromFleet({stateDir:dir,campaignId:'scope'});
  assert.equal(candidates.length,2);assert.equal(candidates[1].source.entry.status,'duplicate');assert.equal(candidates[1].strategy,null);
  const before=globalSearchSize(await readExposure(dir));const plan=await planFor(join(dir,'data'),candidates);
  const result=await runCampaign({plan,stateDir:dir,jobRunner:async({message})=>failedJob(message,'rejected','fixture_scope_test')});
  assert.equal(result.N,2);assert.equal(result.ledger_decision_count,2);assert.equal(result.ledger_outcome_count,2);assert.equal(result.counts.duplicate,1);
  assert.equal(globalSearchSize(await readExposure(dir)),before,'backtesting does not double-count the same fleet attempts');
  await assert.rejects(runAuthorGeneration({stateDir:dir,campaignId:'scope',lanes,rounds:1,now,researchScope:{...scope,quantity:'2'},factories:{fixture:make}}),/invalid_research_scope/);
});
