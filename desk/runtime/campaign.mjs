// Bounded foreground batch on the existing desk worker/evaluator. SQLite and the
// 3.a episode projection belong to this manager; jobs own isolated directories.
import { DatabaseSync } from 'node:sqlite';
import { spawn } from 'node:child_process';
import { hostname } from 'node:os';
import { randomUUID } from 'node:crypto';
import { mkdir, readFile, readdir, open, unlink, rename, stat, realpath } from 'node:fs/promises';
import { join, resolve, dirname, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { APP, DeskError, digest, requireThat, copy, parseStrictJSON, verifyReceipt } from './contracts.mjs';
import { readRegistry, ruleHash } from './author-fleet.mjs';
import { openLedger, exportEpisodes, lineageGroup, readLedger, toRows } from './episodes.mjs';
import { openExposure } from './exposure.mjs';
import { ACCEPTANCE_CONTRACT_SHA256 } from './acceptance.mjs';

const ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const HASH = /^[0-9a-f]{64}$/;
const CODE = /^[a-z0-9_:]{1,80}$/;
const ZERO = '0'.repeat(64);
const ROOT = fileURLToPath(APP);
const SCREEN = Object.freeze({ min_independent_trades: 30, max_drawdown_pct: '15', min_excess_return_vs_buy_and_hold_pct: '0',
  alpha: '0.05', test: 'stationary_block_bootstrap_excess_daily_returns', bootstrap: { block_bars: 10, resamples: 1000, seed: 0 } });
const exact = (v, keys) => requireThat(v && typeof v === 'object' && !Array.isArray(v) &&
  Object.keys(v).length === keys.length && keys.every(k => Object.hasOwn(v, k)), 'invalid_campaign_plan');
const seal = (v, field) => ({ ...v, [field]: digest(v) });
function verify(v, field) { const { [field]: hash, ...body } = v; requireThat(HASH.test(hash) && digest(body) === hash, 'campaign_integrity_failure'); }
async function json(path) { return parseStrictJSON(await readFile(path, 'utf8'), 8 * 1024 * 1024); }
async function exists(path) { try { await stat(path); return true; } catch (e) { if (e.code === 'ENOENT') return false; throw e; } }
async function atomic(path, value) {
  const temporary = `${path}.${randomUUID()}.tmp`; const fd = await open(temporary, 'wx', 0o600);
  try { await fd.writeFile(JSON.stringify(value) + '\n'); await fd.sync(); } finally { await fd.close(); }
  await rename(temporary, path);
  const dir = await open(dirname(path), 'r'); try { await dir.sync(); } finally { await dir.close(); }
}

/** Existing frozen fleet attempts, including ones that never produced a spec. */
export async function candidatesFromFleet({ stateDir, campaignId }) {
  requireThat(ID.test(campaignId), 'invalid_campaign_id');
  const dir = join(resolve(stateDir), 'candidates');
  const entries = await readRegistry(join(dir, `${campaignId}.jsonl`));
  return Promise.all(entries.map(async entry => {
    if (['registered', 'duplicate'].includes(entry.status)) requireThat(HASH.test(entry.strategy_sha256), 'invalid_source_attempt');
    return { id: `fleet:${entry.seq}`, source: { kind: 'fleet', entry }, strategy: entry.status === 'registered'
      ? await json(join(dir, campaignId, 'specs', `${entry.strategy_sha256}.json`)) : null };
  }));
}

function origin(candidate) {
  const source = candidate.source;
  if (source.kind !== 'fleet') return { role: 'author', slot: 'frozen', provider: source.kind, model_id: 'host_frozen', agent_written: false };
  const e = source.entry; verify(e, 'entry_sha256');
  requireThat(['registered', 'rejected', 'duplicate', 'timed_out', 'unavailable'].includes(e.status), 'invalid_source_attempt');
  if (candidate.strategy) requireThat(candidate.strategy.strategy_sha256 === e.strategy_sha256, 'source_strategy_mismatch');
  const a = e.author;
  requireThat(a && typeof a.role === 'string' && typeof a.slot === 'string' && typeof a.provider === 'string' && typeof a.model_id === 'string', 'invalid_source_attempt');
  if (a.agent_written) requireThat(['http', 'cli'].includes(a.transport) && a.provider !== 'fixture' && HASH.test(a.raw_response_sha256), 'invalid_source_attempt');
  return { role: a.role, slot: a.slot, provider: a.provider, model_id: a.model_id, agent_written: a.agent_written === true };
}

export async function freezeCampaignPlan(input) {
  const plan = copy(input);
  exact(plan, ['schema_version', 'kind', 'campaign_id', 'registry', 'dataset_id', 'evaluation_window', 'initial_cash',
    'cost_model', 'walkforward', 'screen', 'limits', 'candidates']);
  requireThat(plan.schema_version === 1 && plan.kind === 'backtest_campaign' && ID.test(plan.campaign_id) && ID.test(plan.dataset_id), 'invalid_campaign_plan');
  exact(plan.limits, ['concurrency', 'max_attempts', 'max_wall_ms', 'job_timeout_ms']);
  const l = plan.limits;
  requireThat(Number.isInteger(l.concurrency) && l.concurrency >= 1 && l.concurrency <= 3 && Number.isInteger(l.max_attempts) && l.max_attempts >= 1 && l.max_attempts <= 50 &&
    Number.isInteger(l.job_timeout_ms) && l.job_timeout_ms >= 1000 && l.job_timeout_ms <= 20000 &&
    Number.isInteger(l.max_wall_ms) && l.max_wall_ms >= l.job_timeout_ms + 1000 && l.max_wall_ms <= 60000, 'invalid_campaign_limits');
  requireThat(Array.isArray(plan.candidates) && plan.candidates.length >= 1 && plan.candidates.length <= l.max_attempts, 'campaign_attempt_limit');
  requireThat(new Set(plan.candidates.map(c => c.id)).size === plan.candidates.length, 'duplicate_candidate_id');
  for (const c of plan.candidates) {
    exact(c, ['id', 'strategy', 'source']); requireThat(ID.test(c.id), 'invalid_candidate_id');
    requireThat(c.source && ['fixture', 'unverified_external', 'fleet'].includes(c.source.kind), 'invalid_source_attempt');
    exact(c.source, c.source.kind === 'fleet' ? ['kind', 'entry'] : ['kind']); origin(c);
    requireThat(c.strategy && typeof c.strategy === 'object' || c.source.kind === 'fleet' && ['rejected', 'duplicate', 'timed_out', 'unavailable'].includes(c.source.entry.status), 'invalid_source_attempt');
  }
  exact(plan.walkforward, ['initial_train_bars', 'test_bars']);
  requireThat(Object.values(plan.walkforward).every(n => Number.isInteger(n) && n >= 2 && n <= 9999), 'invalid_campaign_plan');
  // A fixed development screen, not the future research_accepted training label.
  requireThat(digest(plan.screen) === digest(SCREEN), 'campaign_screen_changed');
  exact(plan.cost_model, ['commission_bps', 'slippage_bps_per_side', 'rounding']);
  requireThat(plan.cost_model.rounding === 'half_even' && ['commission_bps', 'slippage_bps_per_side'].every(k =>
    typeof plan.cost_model[k] === 'string' && /^(0|[1-9][0-9]*)(\.[0-9]+)?$/.test(plan.cost_model[k]) && Number(plan.cost_model[k]) <= 1000) && Number(plan.cost_model.slippage_bps_per_side) >= 5, 'invalid_campaign_costs');
  requireThat(typeof plan.initial_cash === 'string' && /^(0|[1-9][0-9]*)(\.[0-9]+)?$/.test(plan.initial_cash) && Number(plan.initial_cash) > 0, 'invalid_campaign_plan');
  plan.initial_cash = decimalText(decimal(plan.initial_cash));
  for (const key of ['commission_bps', 'slippage_bps_per_side']) plan.cost_model[key] = decimalText(decimal(plan.cost_model[key]));
  exact(plan.evaluation_window, ['start', 'end_exclusive']);
  requireThat(Object.values(plan.evaluation_window).every(t => typeof t === 'string' && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$/.test(t)) && plan.evaluation_window.start < plan.evaluation_window.end_exclusive, 'invalid_campaign_plan');
  plan.registry = resolve(plan.registry);
  const registry = await json(plan.registry); const entry = registry.datasets?.[plan.dataset_id];
  requireThat(entry?.partition === 'development' && HASH.test(entry.manifest_sha256), 'dataset_access_denied');
  const sources = {};
  for (const dir of ['backtest', 'runtime']) {
    for (const name of (await readdir(join(ROOT, dir))).filter(n => n.endsWith(dir === 'backtest' ? '.py' : '.mjs')).sort()) sources[`${dir}/${name}`] = digest((await readFile(join(ROOT, dir, name))).toString());
  }
  requireThat(['synthetic', 'historical_unverified', 'historical_research'].includes(entry.classification), 'dataset_access_denied');
  const bindings = { registry_sha256: digest(registry), manifest_sha256: entry.manifest_sha256, classification: entry.classification, source_sha256: digest(sources) };
  requireThat(Buffer.byteLength(JSON.stringify(plan)) <= 1024 * 1024, 'campaign_plan_too_large');
  return seal({ ...plan, bindings }, 'plan_sha256');
}

export const campaignScreen = () => copy(SCREEN);

async function acquire(dir) {
  const path = join(dir, 'manager.lock'); const token = randomUUID();
  for (let tries = 0; tries < 2; tries++) {
    try {
      const fd = await open(path, 'wx', 0o600);
      try { await fd.writeFile(JSON.stringify({ pid: process.pid, host: hostname(), token })); await fd.sync(); } finally { await fd.close(); }
      return async () => { requireThat((await json(path)).token === token, 'manager_lock_changed'); await unlink(path); };
    } catch (e) {
      if (e.code !== 'EEXIST') throw e;
      let guard;
      try { guard = await open(`${path}.reclaim`, 'wx', 0o600); } catch (e) { if (e.code === 'EEXIST') throw new DeskError('campaign_recovery_active'); throw e; }
      try {
        const owner = await json(path); requireThat(owner.host === hostname() && Number.isSafeInteger(owner.pid) && owner.pid > 0, 'invalid_manager_lock');
        try { process.kill(owner.pid, 0); throw new DeskError('campaign_manager_active'); } catch (e) { if (e.code !== 'ESRCH') throw e; }
        await unlink(path);
      } finally { await guard.close(); await unlink(`${path}.reclaim`); }
    }
  }
  throw new DeskError('campaign_manager_active');
}

function journal(db, campaignId = null) {
  const rows = campaignId === null ? db.prepare('SELECT payload FROM journal ORDER BY seq').all()
    : db.prepare('SELECT payload FROM campaign_journal WHERE campaign_id=? ORDER BY seq').all(campaignId);
  const entries = rows.map(r => JSON.parse(r.payload));
  let previous = ZERO;
  entries.forEach((entry, i) => { verify(entry, 'entry_sha256'); requireThat(entry.seq === i && entry.previous_sha256 === previous, 'campaign_integrity_failure'); previous = entry.entry_sha256; });
  return entries;
}
function append(db, entries, event, campaignId = null) {
  const entry = seal({ seq: entries.length, previous_sha256: entries.at(-1)?.entry_sha256 ?? ZERO, ...event }, 'entry_sha256');
  if (campaignId === null) db.prepare('INSERT INTO journal VALUES (?,?)').run(entry.seq, JSON.stringify(entry));
  else db.prepare('INSERT INTO campaign_journal VALUES (?,?,?)').run(campaignId, entry.seq, JSON.stringify(entry));
  entries.push(entry); return entry;
}
function attemptsOf(events) {
  const attempts = [];
  for (const event of events) {
    if (event.kind === 'attempt') { requireThat(event.attempt === attempts.length, 'campaign_integrity_failure'); attempts.push({ admission: event, result: null }); }
    else { requireThat(event.kind === 'result' && attempts[event.attempt] && !attempts[event.attempt].result, 'campaign_integrity_failure'); attempts[event.attempt].result = event; }
  }
  return attempts;
}
function jobMessage(plan, candidate) {
  return { strategy: candidate.strategy, dataset_id: plan.dataset_id, evaluation_window: plan.evaluation_window,
    initial_cash: plan.initial_cash, cost_model: plan.cost_model, screen: plan.screen, walkforward: plan.walkforward };
}
function validateResult(result, message, plan) {
  verify(result, 'job_result_sha256');
  requireThat(result.kind === 'campaign_job_result' && result.input_sha256 === digest(message) && result.qualification === 'research_only' && result.promoting === false &&
    ['completed', 'rejected', 'timed_out', 'failed', 'unknown'].includes(result.status), 'campaign_job_integrity_failure');
  if (result.status === 'completed') {
    verify(result.spec, 'strategy_sha256'); verify(result.request, 'request_sha256'); verify(result.evaluation, 'evaluation_sha256'); verifyReceipt(result.receipt);
    requireThat(result.receipt.status === 'completed' && result.request.request_sha256 === result.receipt.request_sha256 &&
      result.request.strategy_sha256 === result.spec.strategy_sha256 && result.request.manifest_sha256 === plan.bindings.manifest_sha256 &&
      result.manifest_sha256 === plan.bindings.manifest_sha256 && digest(result.request.evaluation_window) === digest(plan.evaluation_window) &&
      Number(result.request.initial_cash) === Number(plan.initial_cash) && digest(result.request.cost_model) === digest(plan.cost_model), 'campaign_job_integrity_failure');
  } else requireThat(CODE.test(result.code), 'campaign_job_integrity_failure');
  return result;
}
function failure(message, status, code) {
  return seal({ schema_version: 1, kind: 'campaign_job_result', qualification: 'research_only', promoting: false,
    input_sha256: digest(message), status, code }, 'job_result_sha256');
}

async function verifyJobArtifacts(result, dir) {
  if (result.status !== 'completed') return;
  const artifacts = join(dir, 'artifacts');
  for (const [field, value] of [['request_sha256', result.request], ['receipt_sha256', result.receipt], ['strategy_sha256', result.spec]]) {
    const stored = await json(join(artifacts, `${value[field]}.json`)); verify(stored, field);
    requireThat(digest(stored) === digest(value), 'campaign_job_integrity_failure');
  }
  const manifest = await json(join(artifacts, `${result.manifest_sha256}.json`)); verify(manifest, 'manifest_sha256');
  requireThat(manifest.partition === 'development' && manifest.classification === result.classification, 'campaign_job_integrity_failure');
  for (const kind of ['orders', 'fills', 'equity']) {
    const hash = result.receipt.artifacts[`${kind}_sha256`]; const value = await json(join(artifacts, `${hash}.json`));
    requireThat(digest(value) === hash && value.request_sha256 === result.request.request_sha256 && value.kind === kind, 'campaign_job_integrity_failure');
  }
}

// Exact decimal comparisons keep Holm's decision identical at the threshold.
function decimal(text) {
  requireThat(typeof text === 'string' && /^-?(0|[1-9][0-9]*)(\.[0-9]+)?$/.test(text) && text.length <= 100, 'invalid_campaign_metric');
  return { value: BigInt(text.replace('.', '')), scale: (text.split('.')[1] ?? '').length };
}
function cmp(a, b) { const scale = Math.max(a.scale, b.scale); const x = a.value * 10n ** BigInt(scale - a.scale), y = b.value * 10n ** BigInt(scale - b.scale); return x < y ? -1 : x > y ? 1 : 0; }
const mul = (a, n) => ({ value: a.value * BigInt(n), scale: a.scale });
function decimalText(a) { const s = a.value.toString().padStart(a.scale + 1, '0'); return a.scale ? `${s.slice(0, -a.scale)}.${s.slice(-a.scale)}`.replace(/0+$/, '').replace(/\.$/, '') : s; }
function holm(pvalues) {
  const ordered = pvalues.map((p, i) => [decimal(p), i]).sort((a, b) => cmp(a[0], b[0]) || a[1] - b[1]);
  const adjusted = new Map(); let maximum = decimal('0');
  ordered.forEach(([p, index], rank) => {
    requireThat(cmp(p, decimal('0')) >= 0 && cmp(p, decimal('1')) <= 0, 'invalid_campaign_metric');
    let value = mul(p, ordered.length - rank); if (cmp(value, decimal('1')) > 0) value = decimal('1');
    if (cmp(value, maximum) > 0) maximum = value;
    adjusted.set(index, maximum);
  });
  return adjusted;
}

/** Native subprocess, never detached. The child has its own alarm as well. */
export async function runCandidateJob({ dir, message, registry, timeoutMs }) {
  await mkdir(dir, { recursive: true, mode: 0o700 });
  const python = join(ROOT, 'backtest/.venv/bin/python');
  return new Promise(resolveJob => {
    let error = null; let bytes = 0;
    const child = spawn(python, ['-m', 'backtest.campaign_job'], { cwd: ROOT, shell: false, stdio: ['pipe', 'pipe', 'pipe'],
      env: { PATH: `${dirname(python)}:/usr/bin:/bin`, PYTHONNOUSERSITE: '1', OMP_NUM_THREADS: '1', OPENBLAS_NUM_THREADS: '1',
        TRADING_DESK_REGISTRY: registry, TRADING_DESK_CAMPAIGN_JOB: dir, TRADING_DESK_JOB_TIMEOUT_MS: String(timeoutMs) } });
    const timer = setTimeout(() => { error = failure(message, 'timed_out', 'job_timeout'); child.kill('SIGKILL'); }, timeoutMs + 500);
    const discard = data => { bytes += data.length; if (bytes > 1024 * 1024) { error = failure(message, 'failed', 'job_output_limit'); child.kill('SIGKILL'); } };
    child.stdout.on('data', discard); child.stderr.on('data', discard); child.stdin.on('error', () => {});
    child.on('error', () => { error = failure(message, 'failed', 'job_unavailable'); });
    child.on('close', async () => {
      clearTimeout(timer);
      try { resolveJob(error ?? await json(join(dir, 'result.json'))); }
      catch { resolveJob(failure(message, 'unknown', 'job_result_missing')); }
    });
    child.stdin.end(JSON.stringify(message));
  });
}

async function project({ dir, plan, events }) {
  // SQLite is authoritative. Rebuild into a fresh directory, so a torn JSONL
  // append or stale 3.a file lock after SIGKILL cannot lose an admitted attempt.
  const generation = join(dir, 'projections', digest(events));
  const metadataPath = join(generation, 'projection.json');
  if (await exists(generation)) {
    const metadata = await json(metadataPath); verify(metadata, 'projection_sha256');
    const records = await readLedger(join(generation, 'episodes', `${plan.campaign_id}.jsonl`));
    const rows = toRows(records); const body = rows.map(r => JSON.stringify(r)).join('\n') + (rows.length ? '\n' : '');
    requireThat(metadata.events_sha256 === digest(events) && digest(rows) === metadata.export.export_sha256 &&
      await readFile(metadata.export.path, 'utf8') === body, 'campaign_episode_integrity_failure');
    return metadata.export;
  }
  const staging = join(dir, 'projections', randomUUID());
  let timestamp; const ledger = await openLedger({ stateDir: staging, campaignId: plan.campaign_id, now: () => timestamp });
  const decisions = new Map();
  try {
    for (const e of events) {
      timestamp = e.at;
      // Campaign decisions are recorded before the job runs, so they are prospective.
      if (e.kind === 'attempt') decisions.set(e.attempt, await ledger.recordDecision({ ...e.decision, labelKind: 'prospective' }));
      else await ledger.recordOutcome({ decisionId: decisions.get(e.attempt).decision_id, status: e.status, code: e.code,
        stageReached: e.status === 'completed' ? 'registered' : 'not_registered', receipts: e.receipts, cost: e.cost });
    }
  } finally { await ledger.close(); }
  const exported = await exportEpisodes({ stateDir: staging, campaignId: plan.campaign_id });
  exported.path = join(generation, 'episodes', `${plan.campaign_id}.export.jsonl`);
  await atomic(join(staging, 'projection.json'), seal({ events_sha256: digest(events), export: exported }, 'projection_sha256'));
  await rename(staging, generation);
  const parent = await open(dirname(generation), 'r'); try { await parent.sync(); } finally { await parent.close(); }
  return exported;
}

function screen(attempts, plan) {
  const pvalues = attempts.map(a => a.result?.output?.evaluation?.pvalue ?? '1');
  const adjusted = holm(pvalues);
  const lineages = {}; const candidates = attempts.map((a, index) => {
    const r = a.result, value = r?.output, metrics = value?.evaluation?.metrics;
    const failures = [];
    if (!r || r.status !== 'completed') failures.push(r?.code ?? 'unresolved');
    if (!a.admission.decision.worker.agent_written) failures.push('agent_authorship_unproven');
    if (value?.classification !== 'historical_research') failures.push('data_not_qualified');
    if (metrics) {
      if (metrics.independent_trades < plan.screen.min_independent_trades) failures.push('minimum_independent_trades');
      if (cmp(mul(decimal(metrics.max_drawdown), 100), decimal(plan.screen.max_drawdown_pct)) > 0) failures.push('drawdown');
      if (cmp(decimal(metrics.excess_return_vs_buy_and_hold_pct), decimal(plan.screen.min_excess_return_vs_buy_and_hold_pct)) <= 0) failures.push('buy_and_hold_excess');
    } else failures.push('no_evaluation');
    if (cmp(adjusted.get(index), decimal(plan.screen.alpha)) > 0) failures.push('holm_adjustment');
    const lineage = value?.spec ? lineageGroup({ family: value.spec.family, timeframe: value.spec.timeframe }) : a.admission.decision.lineage;
    lineages[lineage] ??= { attempts: 0, survivors: 0, survivor_ids: [] }; lineages[lineage].attempts++;
    // Collapse survivors to one per family/timeframe lineage.
    const passed = failures.length === 0;
    if (passed && lineages[lineage].survivors === 0) { lineages[lineage].survivors = 1; lineages[lineage].survivor_ids.push(a.admission.candidate_id); }
    return { id: a.admission.candidate_id, lineage_group: lineage, status: r?.status ?? 'unknown', code: r?.code ?? null,
      agent_written: a.admission.decision.worker.agent_written, strategy_sha256: value?.spec?.strategy_sha256 ?? null,
      receipt_sha256: value?.receipt?.receipt_sha256 ?? null, metrics: metrics ?? null,
      verdict: { qualification: 'research_only', promoting: false, screen: 'development_screen_v1', passed, failures,
        N: attempts.length, adjusted_pvalue: decimalText(adjusted.get(index)) } };
  });
  return { candidates, lineages, survivor_lineages: Object.values(lineages).filter(g => g.survivors).length };
}

export async function runCampaign({ plan: input, stateDir, stateStore = null, jobRunner = runCandidateJob, onCheckpoint = async () => {} }) {
  const plan = await freezeCampaignPlan(input);
  const dir = join(resolve(stateDir), 'campaigns', plan.campaign_id); await mkdir(dir, { recursive: true, mode: 0o700 });
  if (plan.bindings.classification !== 'synthetic') {
    const state = await realpath(join(ROOT, 'runtime/.state'));
    requireThat((await realpath(dir)).startsWith(state + sep), 'gitignored_state_required');
  }
  const unlock = await acquire(dir); let db; const pending = new Map();
  const started = Date.now();
  try {
    // League scheduling and native campaign admissions share one store. Existing
    // standalone campaigns retain their on-disk format and replay semantics.
    requireThat(stateStore === null || resolve(stateStore) === join(resolve(stateDir), 'campaign.sqlite'), 'invalid_campaign_store');
    const namespace = stateStore === null ? null : plan.campaign_id;
    db = new DatabaseSync(stateStore === null ? join(dir, 'campaign.sqlite') : resolve(stateStore));
    db.exec('PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;');
    if (namespace === null) db.exec('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL); CREATE TABLE IF NOT EXISTS journal (seq INTEGER PRIMARY KEY, payload TEXT NOT NULL)');
    else db.exec('CREATE TABLE IF NOT EXISTS campaign_metadata (campaign_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(campaign_id,key)); CREATE TABLE IF NOT EXISTS campaign_journal (campaign_id TEXT NOT NULL, seq INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(campaign_id,seq))');
    const stored = namespace === null ? db.prepare('SELECT value FROM metadata WHERE key=?').get('plan')
      : db.prepare('SELECT value FROM campaign_metadata WHERE campaign_id=? AND key=?').get(namespace, 'plan');
    if (stored) requireThat(digest(JSON.parse(stored.value)) === digest(plan), 'campaign_plan_drift');
    else if (namespace === null) db.prepare('INSERT INTO metadata VALUES (?,?)').run('plan', JSON.stringify(plan));
    else db.prepare('INSERT INTO campaign_metadata VALUES (?,?,?)').run(namespace, 'plan', JSON.stringify(plan));
    const events = journal(db, namespace); let attempts = attemptsOf(events);
    for (let i = 0; i < attempts.length; i++) if (attempts[i].result) {
      const output = attempts[i].result.output;
      validateResult(output, jobMessage(plan, plan.candidates[i]), plan);
      await verifyJobArtifacts(output, attempts[i].admission.job_dir);
    }
    const commit = async (index, output) => {
      const admission = attempts[index].admission; const candidate = plan.candidates[index]; const message = jobMessage(plan, candidate);
      validateResult(output, message, plan);
      await verifyJobArtifacts(output, admission.job_dir);
      let status = output.status, code = output.code;
      if (candidate.source.kind === 'fleet' && candidate.source.entry.status === 'duplicate') { status = 'duplicate'; code = 'duplicate_spec'; }
      if (status === 'completed') {
        const duplicate = attempts.slice(0, index).some(a => a.result?.output?.spec && ruleHash(a.result.output.spec) === ruleHash(output.spec));
        if (duplicate || candidate.source.kind === 'fleet' && candidate.source.entry.status === 'duplicate') { status = 'duplicate'; code = 'duplicate_spec'; }
      }
      append(db, events, { kind: 'result', attempt: index, at: new Date().toISOString(), status, code, output,
        receipts: output.receipt ? [output.receipt.receipt_sha256] : [],
        cost: { model_calls: 0, backtests: candidate.strategy ? 1 : 0, wall_ms: Math.max(0, Date.now() - Date.parse(admission.at)) } }, namespace);
      attempts = attemptsOf(events); await onCheckpoint({ kind: 'result_committed', attempt: index, dir });
    };
    // An interrupted output may be reused; an absent one stays unknown. No
    // automatic resubmission, including across a crash before process launch.
    for (let i = 0; i < attempts.length; i++) if (!attempts[i].result) {
      const a = attempts[i].admission; const message = jobMessage(plan, plan.candidates[i]); let output;
      try { output = await json(join(a.job_dir, 'result.json')); } catch (e) { if (e.code !== 'ENOENT') throw e; output = failure(message, 'unknown', 'interrupted_result_unknown'); }
      await commit(i, output);
    }
    let next = attempts.length; let consume = attempts.length;
    while (next < plan.candidates.length || pending.size) {
      while (next < plan.candidates.length && pending.size < plan.limits.concurrency && !(await exists(join(dir, 'STOP'))) &&
        Date.now() - started < plan.limits.max_wall_ms - plan.limits.job_timeout_ms - 1000) {
        const index = next++; const candidate = plan.candidates[index]; const raw = candidate.strategy;
        const family = ['sma_cross_long', 'momentum_long', 'mean_reversion_long', 'breakout_long', 'post_earnings_drift_long'].includes(raw?.family) ? raw.family : null;
        const timeframe = ['1m', '5m', '15m', '1h', '1d'].includes(raw?.timeframe ?? '1d') ? raw?.timeframe ?? '1d' : null;
        const lineage = lineageGroup({ family, timeframe, lane: 'unparsed' });
        const jobDir = join(dir, 'jobs', String(index).padStart(4, '0'), randomUUID());
        const decision = { episodeId: `campaign:${plan.plan_sha256.slice(0, 20)}:${index}`, lineage,
          state: { stage: 'not_registered', candidate: { family, timeframe, symbol: typeof raw?.symbol === 'string' && /^[A-Z][A-Z0-9]{0,4}([.-][A-Z])?$/.test(raw.symbol) ? raw.symbol : null, strategy_sha256: null, rule_sha256: null },
            attempts_in_lineage: attempts.filter(a => a.admission.decision.lineage === lineage).length, prior_errors: [],
            budget: { model_calls_spent: 0, model_calls_remaining: 0, backtests_spent: index, backtests_remaining: plan.limits.max_attempts - index } },
          offeredActions: ['test_candidate', 'reject', 'abstain'], chosenAction: candidate.strategy ? 'test_candidate' : 'reject',
          chooser: { kind: 'rules', id: 'bounded_campaign', version: '1' }, worker: origin(candidate) };
        append(db, events, { kind: 'attempt', attempt: index, candidate_id: candidate.id, at: new Date().toISOString(), job_dir: jobDir, decision }, namespace);
        attempts = attemptsOf(events); await onCheckpoint({ kind: 'attempt_committed', attempt: index, dir });
        const message = jobMessage(plan, candidate);
        const run = candidate.strategy ? Promise.resolve().then(() => jobRunner({ dir: jobDir, message, registry: plan.registry, timeoutMs: plan.limits.job_timeout_ms }))
          : Promise.resolve(failure(message, candidate.source.entry.status === 'unavailable' ? 'failed' : candidate.source.entry.status === 'duplicate' ? 'rejected' : candidate.source.entry.status, candidate.source.entry.rejection?.code ?? 'source_rejected'));
        pending.set(index, run.catch(() => failure(message, 'unknown', 'job_result_unknown')));
      }
      if (!pending.size) break;
      const output = await pending.get(consume); pending.delete(consume);
      await onCheckpoint({ kind: 'job_finished', attempt: consume, dir });
      await commit(consume++, output);
    }
    const projection = await project({ dir, plan, events });
    attempts = attemptsOf(events);
    requireThat(projection.decisions === attempts.length && projection.resolved === attempts.filter(a => a.result).length && projection.unresolved === 0, 'campaign_episode_count_mismatch');
    const status = attempts.length === plan.candidates.length ? 'completed' : 'paused';
    const screened = screen(attempts, plan);
    // Every admitted attempt joins the desk-wide exposure index (task 1.b item 2). Idempotent per
    // strategy, so reruns and the league's per-seed campaigns of one genome count once.
    const exposure = await openExposure(stateDir);
    try {
      for (const c of screened.candidates) {
        const source = plan.candidates.find(candidate => candidate.id === c.id).source;
        await exposure.record({ scope: 'campaign_attempt', key: source.kind === 'fleet' ? source.entry.rule_sha256 ?? source.entry.entry_sha256
          : c.strategy_sha256 ?? digest({ plan_sha256: plan.plan_sha256, candidate_id: c.id }),
          sourceId: `campaign:${plan.campaign_id}:${c.id}`.slice(0, 128), lineageGroup: c.lineage_group, datasetFamily: plan.dataset_id.slice(0, 128) });
      }
    } finally { await exposure.close(); }
    const counts = Object.fromEntries(['completed', 'rejected', 'duplicate', 'timed_out', 'failed', 'unknown'].map(s => [s, screened.candidates.filter(c => c.status === s).length]));
    const summary = seal({ schema_version: 1, kind: 'campaign_summary', campaign_id: plan.campaign_id, plan_sha256: plan.plan_sha256,
      qualification: 'research_only', promoting: false, paper: false, orders: false, status, N: attempts.length,
      acceptance_contract_sha256: ACCEPTANCE_CONTRACT_SHA256,
      planned_candidates: plan.candidates.length, queued: plan.candidates.length - attempts.length,
      registry_attempt_count: attempts.length, ledger_decision_count: projection.decisions, ledger_outcome_count: projection.resolved,
      attempts_sha256: events.at(-1)?.entry_sha256 ?? ZERO, counts, episodes: projection, ...screened }, 'summary_sha256');
    await atomic(join(dir, 'summary.json'), summary);
    if (status === 'completed') await atomic(join(dir, 'DONE.json'), { summary_sha256: summary.summary_sha256, qualification: 'research_only' });
    return summary;
  } finally {
    await Promise.allSettled([...pending.values()]); db?.close(); await unlock();
  }
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? '').href) {
  const args = process.argv.slice(2);
  const option = name => args[args.indexOf(name) + 1];
  Promise.resolve().then(async () => {
    requireThat(args.includes('--plan') && args.includes('--state-dir'), 'campaign_arguments_required');
    const result = await runCampaign({ plan: await json(resolve(option('--plan'))), stateDir: resolve(option('--state-dir')) });
    process.stdout.write(JSON.stringify(result) + '\n');
  }).catch(e => { process.stdout.write(JSON.stringify({ ok: false, error: e.code ?? 'campaign_failed' }) + '\n'); process.exitCode = 1; });
}
