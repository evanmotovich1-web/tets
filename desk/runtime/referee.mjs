// Referee (docs/preregistration.md "Stages", "Referee rules", "Demotion").
// Reads sealed records only. No brokerage tools, no network, no model calls.
// The highest record it ever writes is a promotion at stage eligible_for_live;
// live trading additionally needs a separate Evan approval record (risk-limits.md).
import { mkdir, readFile, writeFile, rename, appendFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { digest, requireThat, parseStrictJSON } from './contracts.mjs';
import { readRegistry, searchSize } from './author-fleet.mjs';
import { readExposure, globalSearchSize } from './exposure.mjs';

// Backtest-only ladder. Shadow testing is retired (2026-10-25). paper_passed is an
// optional stage that only applies when a campaign explicitly enables paper money.
export const STAGES = ['registered', 'dev_passed', 'validation_passed', 'holdout_passed', 'paper_passed', 'eligible_for_live'];
const PARTITION_STAGE = { development: 'dev_passed', validation: 'validation_passed', holdout: 'holdout_passed' };
export const PAPER_DISABLED = Object.freeze({ enabled: false });

export function sealed(record, field) {
  if (!record || typeof record !== 'object' || typeof record[field] !== 'string') return false;
  const { [field]: hash, ...body } = record;
  return digest(body) === hash;
}

export function readJsonl(text, maxBytes = 1 << 20) {
  return text.split('\n').filter(line => line.trim()).map(line => parseStrictJSON(line, maxBytes));
}

/** Candidate set and search size N from the fleet registry (docs/authoring-fleet.md). */
export async function loadCampaign({ stateDir, campaignId }) {
  const dir = join(stateDir, 'candidates');
  const entries = await readRegistry(join(dir, `${campaignId}.jsonl`)); // throws registry_tampered on a broken chain
  requireThat(entries.length > 0, 'campaign_not_found');
  // Global N (task 1.b item 2): the larger of this campaign's count and the desk-wide exposure index,
  // so starting a new campaign can never shrink the correction for luck.
  const searchN = Math.max(searchSize(entries), globalSearchSize(await readExposure(stateDir)));
  const candidates = [];
  for (const entry of entries.filter(e => e.status === 'registered')) {
    const spec = parseStrictJSON(await readFile(join(dir, campaignId, 'specs', `${entry.strategy_sha256}.json`), 'utf8'));
    const prereg = parseStrictJSON(await readFile(join(dir, campaignId, 'preregistrations', `${entry.preregistration_sha256}.json`), 'utf8'));
    requireThat(sealed(spec, 'strategy_sha256') && spec.strategy_sha256 === entry.strategy_sha256, 'spec_integrity_failure');
    requireThat(sealed(prereg, 'preregistration_sha256') && prereg.strategy_sha256 === spec.strategy_sha256, 'preregistration_integrity_failure');
    candidates.push({ entry, spec, prereg });
  }
  return { entries, searchN, candidates };
}

function paperFailures({ prereg, spec }, record) {
  if (prereg.schema_version !== 2 || !prereg.paper_criteria) return ['paper_criteria_missing'];
  if (!record) return ['paper_not_evaluated'];
  // A future paper runner writes this sealed record; the referee only checks it.
  if (!sealed(record, 'paper_verdict_sha256') || record.kind !== 'paper_verdict' || record.strategy_sha256 !== spec.strategy_sha256 ||
      record.preregistration_sha256 !== prereg.preregistration_sha256) return ['paper_integrity_failure'];
  if (record.passed !== true) return record.failures?.length ? record.failures : ['paper_failed'];
  return [];
}

/** One candidate through the stage ladder. Stages run in order; none can be skipped. */
export function judge({ candidate, verdicts = [], paperVerdicts = [], paper = PAPER_DISABLED, searchN, campaignId, decidedAt }) {
  const { entry, spec, prereg } = candidate;
  const receipts = {}; let stage = 'registered', failing = null, deepest = null;
  for (const partition of ['development', 'validation', 'holdout']) {
    const v = verdicts.find(x => x.partition === partition && x.preregistration_sha256 === prereg.preregistration_sha256);
    const criteria = [];
    if (!v) criteria.push(`${partition}_not_evaluated`);
    else {
      const intact = sealed(v, 'verdict_sha256') && v.kind === 'walkforward_verdict' && v.strategy_sha256 === spec.strategy_sha256;
      if (!intact) criteria.push('verdict_integrity_failure');
      if (!(Number.isSafeInteger(v.N) && v.N >= searchN)) criteria.push('stale_search_size');
      if (v.data_classification !== 'historical_research') criteria.push('synthetic_data');
      if (v.passed !== true) criteria.push(...(v.failures?.length ? v.failures : ['verdict_failed']));
      receipts[partition] = v.verdict_sha256 ?? null;
      if (intact) deepest = { partition, passed: v.passed === true, adjusted_pvalue: v.adjustment?.adjusted_pvalue ?? null, data_classification: v.data_classification };
    }
    if (criteria.length) { failing = { stage: PARTITION_STAGE[partition], criteria }; break; }
    stage = PARTITION_STAGE[partition];
  }
  let paperReceipt = null;
  if (!failing && paper.enabled === true) {
    const record = paperVerdicts.find(x => x.strategy_sha256 === spec.strategy_sha256 && x.preregistration_sha256 === prereg.preregistration_sha256);
    const criteria = paperFailures(candidate, record);
    if (criteria.length) failing = { stage: 'paper_passed', criteria };
    else { stage = 'paper_passed'; paperReceipt = record.paper_verdict_sha256; }
  }
  if (!failing) stage = 'eligible_for_live';
  const body = {
    schema_version: 2, kind: 'referee_verdict', campaign_id: campaignId, decided_at: decidedAt,
    strategy_sha256: spec.strategy_sha256, preregistration_sha256: prereg.preregistration_sha256, rule_sha256: entry.rule_sha256,
    symbol: spec.symbol, family: spec.family, timeframe: spec.timeframe, author: entry.author,
    stage, failing, N: searchN, receipts, backtest: deepest,
    paper: { enabled: paper.enabled === true, paper_verdict_sha256: paperReceipt },
    qualification: 'research_only', promoting: false
  };
  return { ...body, referee_verdict_sha256: digest(body) };
}

async function writeAtomic(path, value) {
  const tmp = `${path}.${process.pid}.tmp`;
  await writeFile(tmp, JSON.stringify(value, null, 2) + '\n', { mode: 0o600 });
  await rename(tmp, path);
}

/** Shape matches runtime/execution/service.mjs authorize(): kind promotion, stage eligible_for_live, author_role referee. */
export function promotionRecord(verdict, createdAt, revoked = false) {
  const body = { schema_version: 1, kind: 'promotion', stage: 'eligible_for_live', author_role: 'referee',
    strategy_sha256: verdict.strategy_sha256, symbol: verdict.symbol, preregistration_sha256: verdict.preregistration_sha256,
    referee_verdict_sha256: verdict.referee_verdict_sha256, created_at: createdAt, revoked,
    requires: 'separate evan_approval record; this record never authorizes an order by itself' };
  return { ...body, record_sha256: digest(body) };
}

/** Read-only judging (task 1.b item 5): same verdicts as runReferee, but it never creates a
 * folder, appends a verdict or writes a promotion. Views and authors use this; only the explicit
 * operator command runReferee writes state. */
export async function judgeReadOnly({ stateDir, campaignId, verdicts = [], paperVerdicts = [], paper = PAPER_DISABLED, now = () => new Date().toISOString() }) {
  const root = resolve(stateDir ?? fileURLToPath(new URL('./.state', import.meta.url)));
  const { searchN, candidates } = await loadCampaign({ stateDir: root, campaignId });
  const decidedAt = now();
  const results = candidates.map(candidate => judge({ candidate, verdicts, paperVerdicts, paper, searchN, campaignId, decidedAt }));
  return { campaign_id: campaignId, decided_at: decidedAt, N: searchN, paper_enabled: paper.enabled === true, verdicts: results,
    promotions: [], revocations: [], would_promote: results.filter(v => v.stage === 'eligible_for_live').map(v => v.strategy_sha256),
    read_only: true, qualification: 'research_only' };
}

export async function runReferee({ stateDir, campaignId, verdicts = [], paperVerdicts = [], paper = PAPER_DISABLED, now = () => new Date().toISOString() }) {
  const root = resolve(stateDir ?? fileURLToPath(new URL('./.state', import.meta.url)));
  const { searchN, candidates } = await loadCampaign({ stateDir: root, campaignId });
  const decidedAt = now();
  const results = candidates.map(candidate => judge({ candidate, verdicts, paperVerdicts, paper, searchN, campaignId, decidedAt }));
  const out = join(root, 'referee', campaignId); const promoDir = join(out, 'promotions');
  await mkdir(promoDir, { recursive: true, mode: 0o700 });
  await appendFile(join(out, 'verdicts.jsonl'), results.map(v => JSON.stringify(v)).join('\n') + '\n', { mode: 0o600 });
  const promotions = [], revocations = [];
  for (const v of results) {
    const path = join(promoDir, `${v.strategy_sha256}.json`);
    let current = null;
    try { current = parseStrictJSON(await readFile(path, 'utf8')); } catch (e) { if (e.code !== 'ENOENT') throw e; }
    // A revoked hash is retired for good (preregistration.md "Demotion"); only a new spec can return.
    if (v.stage === 'eligible_for_live' && !current) {
      const record = promotionRecord(v, decidedAt); await writeAtomic(path, record); promotions.push(record);
    } else if (v.stage !== 'eligible_for_live' && current && !current.revoked) {
      // Demotion: a promoted hash that no longer qualifies is revoked, never silently kept.
      const record = promotionRecord(v, decidedAt, true); await writeAtomic(path, record); revocations.push(record);
    }
  }
  return { campaign_id: campaignId, decided_at: decidedAt, N: searchN, paper_enabled: paper.enabled === true, verdicts: results, promotions, revocations, qualification: 'research_only' };
}
