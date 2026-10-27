// Episode ledger for the future research-action selector (specs/jev-algo-research.md, Phase 2).
// Design only: nothing here downloads, trains or calls a selector model.
//
// A decision record saves what a selector could see at the moment it chose a research action.
// Its outcome is a separate, later record, so no outcome field can leak into the visible state.
// Both live in one append-only, hash-chained JSONL file per campaign with a single writer.
import { mkdir, readFile, open, unlink, writeFile, rename } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { DeskError, digest, requireThat, parseStrictJSON } from './contracts.mjs';
import { readRegistry } from './author-fleet.mjs';

export const ACTIONS = Object.freeze(['test_candidate', 'repair_spec', 'reject', 'request_more_evidence', 'escalate', 'abstain']);
export const OUTCOME_STATUSES = Object.freeze(['completed', 'rejected', 'duplicate', 'timed_out', 'failed', 'unknown']);
export const CHOOSERS = Object.freeze(['rules', 'random', 'model', 'human']);
const STAGES = ['not_registered', 'registered', 'dev_passed', 'validation_passed', 'holdout_passed', 'paper_passed', 'eligible_for_live'];
// Stages a selector or author may see (task 1.b item 4). Anything later can only be known from the
// sealed holdout (or from paper evidence gated on it), so it is refused as holdout taint.
export const VISIBLE_STAGES = Object.freeze(['not_registered', 'registered', 'dev_passed', 'validation_passed']);
// label_kind (task 1.b item 3): only prospective decisions, recorded before downstream evaluation,
// may train a selector. Admission imitation and retrospective proof records never can.
export const LABEL_KINDS = Object.freeze(['prospective', 'admission_imitation', 'retrospective']);
const FAMILIES = ['sma_cross_long', 'momentum_long', 'mean_reversion_long', 'breakout_long', 'post_earnings_drift_long'];
const TIMEFRAMES = ['1m', '5m', '15m', '1h', '1d'];
const SYMBOL = /^[A-Z][A-Z0-9]{0,4}([.-][A-Z])?$/;
const ZERO = '0'.repeat(64);
const ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const HASH = /^[0-9a-f]{64}$/;
const TIME = /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$/;
const CODE = /^[a-z0-9_:]{1,80}$/;

// Visible state is an allowlist. Anything that could carry the answer is refused, at any depth.
const STATE_KEYS = ['stage', 'candidate', 'attempts_in_lineage', 'prior_errors', 'budget', 'evidence'];
const FORBIDDEN_KEYS = new Set(['outcome', 'outcomes', 'reward', 'label', 'labels', 'next_stage', 'future', 'holdout', 'holdout_result',
  'verdict_after', 'resolved_at', 'chosen_action', 'eventual']);
const EVIDENCE_PARTITIONS = new Set(['development', 'validation']); // the holdout is sealed and never visible

const isObject = v => v !== null && typeof v === 'object' && !Array.isArray(v);
function exactKeys(value, required, optional = []) {
  requireThat(isObject(value) && required.every(k => Object.hasOwn(value, k)) && Object.keys(value).every(k => required.includes(k) || optional.includes(k)), 'invalid_episode_record');
}
function scanForbidden(value, depth = 0) {
  requireThat(depth <= 8, 'invalid_episode_record');
  if (Array.isArray(value)) { value.forEach(v => scanForbidden(v, depth + 1)); return; }
  if (!isObject(value)) return;
  for (const [k, v] of Object.entries(value)) { requireThat(!FORBIDDEN_KEYS.has(k), 'outcome_leak'); scanForbidden(v, depth + 1); }
}

/** Host-assigned lineage group: strategy mechanism only. Parameters and symbols collapse into one
 * group so variants never straddle train/test splits. Unparsed attempts group by author lane. */
export function lineageGroup({ family = null, timeframe = null, lane = null } = {}) {
  if (family) return `lin:${digest({ family, timeframe_class: timeframe === '1d' ? 'daily' : 'intraday' }).slice(0, 16)}`;
  requireThat(typeof lane === 'string' && ID.test(lane), 'invalid_lineage');
  return `lin:unparsed:${lane}`;
}

/** Visible state at decision time. Evidence must be observed no later than the decision. */
export function validateState(state, decidedAt) {
  exactKeys(state, ['stage', 'candidate', 'attempts_in_lineage', 'prior_errors', 'budget'], ['evidence']);
  scanForbidden(state);
  requireThat(STAGES.includes(state.stage), 'invalid_episode_record');
  requireThat(VISIBLE_STAGES.includes(state.stage), 'holdout_taint');
  // Strict scalars: no free-text field can smuggle a label past the key scan.
  const c = state.candidate;
  exactKeys(c, ['family', 'timeframe', 'symbol', 'strategy_sha256', 'rule_sha256']);
  requireThat((c.family === null || FAMILIES.includes(c.family)) && (c.timeframe === null || TIMEFRAMES.includes(c.timeframe)) &&
    (c.symbol === null || (typeof c.symbol === 'string' && SYMBOL.test(c.symbol))) &&
    (c.strategy_sha256 === null || HASH.test(c.strategy_sha256)) && (c.rule_sha256 === null || HASH.test(c.rule_sha256)), 'invalid_episode_record');
  requireThat(Number.isSafeInteger(state.attempts_in_lineage) && state.attempts_in_lineage >= 0 && state.attempts_in_lineage <= 1000000, 'invalid_episode_record');
  requireThat(Array.isArray(state.prior_errors) && state.prior_errors.length <= 10 && state.prior_errors.every(c => typeof c === 'string' && CODE.test(c)), 'invalid_episode_record');
  exactKeys(state.budget, ['model_calls_spent', 'backtests_spent', 'model_calls_remaining', 'backtests_remaining']);
  requireThat(Object.values(state.budget).every(n => Number.isSafeInteger(n) && n >= 0), 'invalid_episode_record');
  for (const item of state.evidence ?? []) {
    // partition and taint are host-set; holdout-derived evidence is refused.
    exactKeys(item, ['partition', 'taint', 'observed_at', 'passed', 'failures', 'adjusted_pvalue', 'verdict_sha256']);
    requireThat(EVIDENCE_PARTITIONS.has(item.partition), 'outcome_leak');
    requireThat(item.taint === 'none', 'holdout_taint');
    requireThat(typeof item.passed === 'boolean' && Array.isArray(item.failures) && item.failures.length <= 10 && item.failures.every(f => typeof f === 'string' && CODE.test(f)) &&
      (item.adjusted_pvalue === null || (typeof item.adjusted_pvalue === 'string' && /^(0|1)(\.[0-9]{1,12})?$/.test(item.adjusted_pvalue))), 'invalid_episode_record');
    requireThat(TIME.test(item.observed_at) && item.observed_at <= decidedAt, 'outcome_leak');
    requireThat(HASH.test(item.verdict_sha256), 'invalid_episode_record');
  }
  return state;
}

function validateDecision(r) {
  exactKeys(r, ['schema_version', 'kind', 'campaign_id', 'seq', 'decision_id', 'episode_id', 'lineage_group', 'decided_at',
    'state', 'state_sha256', 'offered_actions', 'chosen_action', 'chooser', 'worker', 'prev_entry_sha256', 'entry_sha256'], ['label_kind']);
  requireThat(r.label_kind === undefined || LABEL_KINDS.includes(r.label_kind), 'invalid_episode_record');
  requireThat(r.schema_version === 1 && ID.test(r.decision_id) && ID.test(r.episode_id) && /^lin:[A-Za-z0-9:_-]{1,80}$/.test(r.lineage_group) && TIME.test(r.decided_at), 'invalid_episode_record');
  validateState(r.state, r.decided_at);
  requireThat(r.state_sha256 === digest(r.state), 'invalid_episode_record');
  requireThat(Array.isArray(r.offered_actions) && r.offered_actions.length >= 1 && new Set(r.offered_actions).size === r.offered_actions.length &&
    r.offered_actions.every(a => ACTIONS.includes(a)) && r.offered_actions.includes('abstain'), 'invalid_offered_actions');
  requireThat(r.offered_actions.includes(r.chosen_action), 'action_not_offered');
  exactKeys(r.chooser, ['kind', 'id', 'version'], ['seed']);
  requireThat(CHOOSERS.includes(r.chooser.kind) && ID.test(r.chooser.id), 'invalid_episode_record');
  exactKeys(r.worker, ['role', 'slot', 'provider', 'model_id', 'agent_written']);
  requireThat(typeof r.worker.agent_written === 'boolean', 'invalid_episode_record');
}
function validateOutcome(r, decisions) {
  exactKeys(r, ['schema_version', 'kind', 'campaign_id', 'seq', 'decision_id', 'resolved_at', 'status', 'code', 'stage_reached',
    'receipts', 'cost', 'prev_entry_sha256', 'entry_sha256']);
  const decision = decisions.get(r.decision_id);
  requireThat(decision, 'unknown_decision');
  requireThat(OUTCOME_STATUSES.includes(r.status) && (r.code === null || CODE.test(r.code)) && STAGES.includes(r.stage_reached), 'invalid_episode_record');
  requireThat(TIME.test(r.resolved_at) && r.resolved_at >= decision.decided_at, 'outcome_before_decision');
  requireThat(Array.isArray(r.receipts) && r.receipts.every(h => HASH.test(h)), 'invalid_episode_record');
  exactKeys(r.cost, ['model_calls', 'backtests', 'wall_ms']);
  requireThat(Object.values(r.cost).every(n => Number.isSafeInteger(n) && n >= 0), 'invalid_episode_record');
}

/** Reads and verifies the whole chain; also enforces one outcome per decision. */
export async function readLedger(path) {
  let text;
  try { text = await readFile(path, 'utf8'); } catch (e) { if (e.code === 'ENOENT') return []; throw e; }
  const records = text.split('\n').filter(Boolean).map(line => parseStrictJSON(line));
  const decisions = new Map(), resolved = new Set();
  let prev = ZERO;
  records.forEach((r, seq) => {
    const { entry_sha256, ...body } = r;
    requireThat(r.seq === seq && r.prev_entry_sha256 === prev && digest(body) === entry_sha256, 'ledger_tampered');
    if (r.kind === 'selector_decision') { validateDecision(r); requireThat(!decisions.has(r.decision_id), 'duplicate_decision'); decisions.set(r.decision_id, r); }
    else if (r.kind === 'decision_outcome') { validateOutcome(r, decisions); requireThat(!resolved.has(r.decision_id), 'outcome_already_recorded'); resolved.add(r.decision_id); }
    else throw new DeskError('invalid_episode_record');
    prev = entry_sha256;
  });
  return records;
}

/** Single-writer ledger handle. The campaign manager owns it; workers never write here. */
export async function openLedger({ stateDir, campaignId, now = () => new Date().toISOString() }) {
  requireThat(typeof campaignId === 'string' && ID.test(campaignId), 'invalid_campaign_id');
  const dir = join(resolve(stateDir), 'episodes'); await mkdir(dir, { recursive: true, mode: 0o700 });
  const path = join(dir, `${campaignId}.jsonl`), lockPath = join(dir, `${campaignId}.lock`);
  let lock;
  try { lock = await open(lockPath, 'wx', 0o600); } catch (e) { if (e.code === 'EEXIST') throw new DeskError('ledger_already_open'); throw e; }
  let records, out;
  try { records = await readLedger(path); out = await open(path, 'a', 0o600); }
  catch (e) { await lock.close(); await unlink(lockPath); throw e; }
  const decisions = new Map(records.filter(r => r.kind === 'selector_decision').map(r => [r.decision_id, r]));
  async function append(fields, check) {
    const body = { schema_version: 1, campaign_id: campaignId, seq: records.length, ...fields, prev_entry_sha256: records.length ? records.at(-1).entry_sha256 : ZERO };
    const record = { ...body, entry_sha256: digest(body) };
    check(record);
    await out.write(JSON.stringify(record) + '\n'); await out.sync();
    records.push(record); return record;
  }
  return {
    path,
    /** Save what the selector saw and chose. decision_id is derived, so a replayed decision is refused. */
    async recordDecision({ episodeId, lineage, state, offeredActions, chosenAction, chooser, worker, labelKind }) {
      const decidedAt = now();
      const decision_id = `dec:${digest({ campaignId, episodeId, decidedAt, seq: records.length }).slice(0, 24)}`;
      return append({ kind: 'selector_decision', decision_id, episode_id: episodeId, lineage_group: lineage, decided_at: decidedAt,
        state, state_sha256: digest(state), offered_actions: offeredActions, chosen_action: chosenAction, chooser, worker,
        ...(labelKind === undefined ? {} : { label_kind: labelKind }) },
        r => { validateDecision(r); requireThat(!decisions.has(r.decision_id), 'duplicate_decision'); decisions.set(r.decision_id, r); });
    },
    /** The deterministic result of the chosen action, written once, after the fact. */
    async recordOutcome({ decisionId, status, code = null, stageReached, receipts = [], cost = { model_calls: 0, backtests: 0, wall_ms: 0 } }) {
      requireThat(!records.some(r => r.kind === 'decision_outcome' && r.decision_id === decisionId), 'outcome_already_recorded');
      return append({ kind: 'decision_outcome', decision_id: decisionId, resolved_at: now(), status, code, stage_reached: stageReached, receipts, cost },
        r => validateOutcome(r, decisions));
    },
    records: () => records.slice(),
    async close() { await out.close(); await lock.close(); await unlink(lockPath); }
  };
}

/** Every fleet attempt (registered, rejected, duplicate, timed_out) becomes one admission decision:
 * the host validator chose test_candidate or reject, and the attempt status is its outcome.
 * Idempotent: attempts already ingested (by registry entry hash) are skipped. */
export async function ingestFleetAttempts({ ledger, stateDir, campaignId, budget }) {
  const entries = await readRegistry(join(resolve(stateDir), 'candidates', `${campaignId}.jsonl`));
  const seen = new Set(ledger.records().filter(r => r.kind === 'selector_decision').map(r => r.episode_id));
  const lineageCounts = new Map(); let added = 0;
  for (const e of entries) {
    const lineage = lineageGroup({ family: e.family, timeframe: e.timeframe, lane: e.author.lane });
    const before = lineageCounts.get(lineage) ?? 0; lineageCounts.set(lineage, before + 1);
    const episodeId = `att:${e.entry_sha256.slice(0, 24)}`;
    if (seen.has(episodeId)) continue;
    const state = { stage: 'not_registered', attempts_in_lineage: before, prior_errors: [],
      candidate: { family: e.family, timeframe: e.timeframe, symbol: null, strategy_sha256: e.strategy_sha256, rule_sha256: e.rule_sha256 },
      budget: { model_calls_spent: e.seq, backtests_spent: 0, model_calls_remaining: Math.max(0, budget.model_calls - e.seq), backtests_remaining: budget.backtests } };
    const decision = await ledger.recordDecision({ episodeId, lineage, state, offeredActions: ['test_candidate', 'reject', 'abstain'],
      chosenAction: e.status === 'registered' ? 'test_candidate' : 'reject',
      chooser: { kind: 'rules', id: 'host_validator', version: 'author-fleet-v2' }, labelKind: 'admission_imitation',
      worker: { role: e.author.role, slot: e.author.slot, provider: e.author.provider, model_id: e.author.model_id, agent_written: e.author.agent_written === true } });
    const status = { registered: 'completed', rejected: 'rejected', duplicate: 'duplicate', timed_out: 'timed_out', unavailable: 'failed' }[e.status] ?? 'unknown';
    await ledger.recordOutcome({ decisionId: decision.decision_id, status, code: e.rejection?.code ?? null, stageReached: e.status === 'registered' ? 'registered' : 'not_registered',
      receipts: [e.entry_sha256], cost: { model_calls: 1, backtests: 0, wall_ms: 0 } });
    added++;
  }
  return { attempts: entries.length, added };
}

/** Training rows for a future selector. Deterministic: same ledger, same bytes. Every decision is
 * exported; ones without an outcome are labelled unresolved, never dropped or guessed. */
export function toRows(records) {
  const outcomes = new Map(records.filter(r => r.kind === 'decision_outcome').map(r => [r.decision_id, r]));
  return records.filter(r => r.kind === 'selector_decision').map(d => {
    const o = outcomes.get(d.decision_id);
    return { schema_version: 1, kind: 'selector_example', campaign_id: d.campaign_id, decision_id: d.decision_id, episode_id: d.episode_id,
      lineage_group: d.lineage_group, state: d.state, state_sha256: d.state_sha256, offered_actions: d.offered_actions,
      chosen_action: d.chosen_action, chooser: d.chooser, worker: d.worker,
      outcome: o ? { status: o.status, code: o.code, stage_reached: o.stage_reached, cost: o.cost, receipts: o.receipts } : null,
      label_status: o ? 'resolved' : 'unresolved', source_entry_sha256: d.entry_sha256,
      // Older ledgers have no label_kind; rows stay byte-identical for them (cached projections).
      ...(d.label_kind === undefined ? {} : { label_kind: d.label_kind }) };
  });
}

/** Rows a selector may be trained on (task 1.b item 3): resolved prospective decisions only.
 * Admission imitation, retrospective proof records and unlabelled legacy rows are refused, not
 * silently dropped, so a caller cannot mistake them for policy or winner labels. */
export function trainingRows(rows) {
  for (const row of rows) {
    requireThat(row.label_kind !== undefined, 'label_kind_unknown');
    requireThat(row.label_kind === 'prospective', 'label_kind_not_trainable');
  }
  return rows.filter(r => r.label_status === 'resolved');
}

/** Phase 3 guard: no lineage group may appear in more than one split. */
export function assertGroupedSplit(rows, splitOf) {
  const seen = new Map();
  for (const row of rows) {
    const split = splitOf(row);
    requireThat(typeof split === 'string', 'invalid_split');
    const prior = seen.get(row.lineage_group);
    requireThat(prior === undefined || prior === split, 'lineage_crosses_split');
    seen.set(row.lineage_group, split);
  }
  return Object.fromEntries([...new Set(seen.values())].map(s => [s, [...seen].filter(([, v]) => v === s).length]));
}

export async function exportEpisodes({ stateDir, campaignId, out }) {
  const root = resolve(stateDir);
  const records = await readLedger(join(root, 'episodes', `${campaignId}.jsonl`));
  const rows = toRows(records);
  const body = rows.map(r => JSON.stringify(r)).join('\n') + (rows.length ? '\n' : '');
  const target = out ?? join(root, 'episodes', `${campaignId}.export.jsonl`);
  const tmp = `${target}.${process.pid}.tmp`; await writeFile(tmp, body, { mode: 0o600 }); await rename(tmp, target);
  const count = s => rows.filter(r => r.outcome?.status === s).length;
  return { ok: true, campaign_id: campaignId, path: target, export_sha256: digest(rows), decisions: rows.length,
    resolved: rows.filter(r => r.label_status === 'resolved').length, unresolved: rows.filter(r => r.label_status === 'unresolved').length,
    completed: count('completed'), rejected: count('rejected'), duplicate: count('duplicate'), timed_out: count('timed_out'),
    lineage_groups: new Set(rows.map(r => r.lineage_group)).size, qualification: 'research_only' };
}

// CLI: node runtime/episodes.mjs <ingest|export> --campaign-id ID [--state-dir DIR] [--out FILE]
if (import.meta.url === pathToFileURL(process.argv[1] ?? '').href) {
  const [command, ...rest] = process.argv.slice(2);
  const args = Object.fromEntries(rest.reduce((acc, a, i, all) => (a.startsWith('--') ? [...acc, [a.slice(2), all[i + 1]]] : acc), []));
  const stateDir = args['state-dir'] ?? fileURLToPath(new URL('./.state', import.meta.url));
  const campaignId = args['campaign-id'];
  const run = async () => {
    if (command === 'export') return exportEpisodes({ stateDir, campaignId, out: args.out });
    requireThat(command === 'ingest', 'unsupported_command');
    const ledger = await openLedger({ stateDir, campaignId });
    try { return { ok: true, ...(await ingestFleetAttempts({ ledger, stateDir, campaignId, budget: { model_calls: 50, backtests: 50 } })) }; }
    finally { await ledger.close(); }
  };
  run().then(r => process.stdout.write(JSON.stringify(r) + '\n'))
    .catch(e => { process.stdout.write(JSON.stringify({ ok: false, error: e.code ?? 'episodes_failure' }) + '\n'); process.exitCode = 1; });
}
