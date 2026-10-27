// Real-author league lanes (task 3.a). Each lane binds one author role to one explicit adapter and
// model identity: cli_codex, cli_claude, localhost or fixture. All authors in a round share the
// frozen observation (runtime/author-fleet.mjs freezeObservation), the same token budget and the same
// attempt cap. A lane that cannot start (flag off, binary missing) is recorded as disabled in the
// generation manifest; an author that fails mid-run is logged as an attempt. Nothing is replaced.
// Host-mutated league genomes stay `strategy_genome` (league/runtime.py); only these lanes write models.
import { readFileSync } from 'node:fs';
import { mkdir, writeFile, rename } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { DeskError, digest, requireThat } from '../runtime/contracts.mjs';
import { createModelAdapter, createCliAuthorAdapter } from '../runtime/models.mjs';
import { runFleet, fixtureFleetAdapter, LANES } from '../runtime/author-fleet.mjs';

export const PROVIDERS = Object.freeze(['cli_codex', 'cli_claude', 'localhost', 'fixture']);
export const DEFAULT_LANES = JSON.parse(readFileSync(new URL('./author-lanes.json', import.meta.url), 'utf8'));
const MODEL = /^[A-Za-z0-9][A-Za-z0-9._:\/-]{0,127}$/;

export function validateLanes(config) {
  requireThat(config && config.schema_version === 1 && config.kind === 'league_author_lanes', 'invalid_author_lanes');
  requireThat(Number.isSafeInteger(config.max_attempts_per_author) && config.max_attempts_per_author >= 1 && config.max_attempts_per_author <= 12, 'invalid_author_cap');
  const roles = Object.keys(config.lanes ?? {});
  requireThat(roles.length >= 1 && roles.every(r => LANES.includes(r)), 'invalid_author_lanes');
  for (const role of roles) {
    const l = config.lanes[role];
    requireThat(PROVIDERS.includes(l.provider) && typeof l.model_id === 'string' && MODEL.test(l.model_id), 'invalid_author_lane');
    if (l.provider === 'localhost') requireThat(typeof l.base_url === 'string' && (l.timeout_ms === undefined || Number.isSafeInteger(l.timeout_ms)), 'invalid_author_lane');
    if (l.price_per_mtok !== undefined) requireThat(Number.isFinite(l.price_per_mtok.input) && Number.isFinite(l.price_per_mtok.output), 'invalid_author_lane');
  }
  return config;
}

/** One adapter per lane, or a recorded reason it is disabled. Never a substitute adapter. */
export function buildLaneAdapters(config, env = process.env, factories = {}) {
  const make = {
    fixture: () => fixtureFleetAdapter(),
    localhost: l => createModelAdapter('localhost', { TRADING_DESK_MODEL_ID: l.model_id, TRADING_DESK_LOCAL_BASE_URL: l.base_url,
      ...(l.timeout_ms ? { TRADING_DESK_MODEL_TIMEOUT_MS: String(l.timeout_ms) } : {}) }),
    cli_codex: l => createCliAuthorAdapter('codex', { ...env, TRADING_DESK_MODEL_ID: l.model_id }),
    cli_claude: l => createCliAuthorAdapter('claude', { ...env, TRADING_DESK_MODEL_ID: l.model_id }),
    ...factories
  };
  const adapters = {}, lanes = [];
  for (const [role, l] of Object.entries(config.lanes)) {
    try {
      const a = make[l.provider](l);
      // The declared model identity must be what the adapter reports.
      requireThat(a.provider === (l.provider === 'fixture' ? 'fixture' : l.provider) && (l.provider === 'fixture' || a.model_id === l.model_id), 'lane_identity_mismatch');
      if (l.price_per_mtok) a.price_per_mtok = l.price_per_mtok;
      // An injected (non built-in) adapter is flagged: its agent_written claim is not host-verified.
      adapters[role] = a; lanes.push({ role, provider: l.provider, model_id: l.model_id, status: 'enabled', reason: null, adapter_source: Object.hasOwn(factories ?? {}, l.provider) ? 'injected' : 'built_in' });
    } catch (e) {
      if (!(e instanceof DeskError)) throw e;
      lanes.push({ role, provider: l.provider, model_id: l.model_id, status: 'disabled', reason: e.code, adapter_source: Object.hasOwn(factories ?? {}, l.provider) ? 'injected' : 'built_in' });
    }
  }
  return { adapters, lanes };
}

/** One bounded author generation through the existing fleet (registry, preregistration, dedupe,
 * exposure index, call log). Returns the fleet summary plus a sealed generation manifest. */
export async function runAuthorGeneration({ stateDir, campaignId, lanes = DEFAULT_LANES, env = process.env, factories, rounds, now, worker, datasetBindings, datasetIds, researchScope }) {
  const config = validateLanes(JSON.parse(JSON.stringify(lanes)));
  const { adapters, lanes: laneStatus } = buildLaneAdapters(config, env, factories);
  const root = resolve(stateDir ?? fileURLToPath(new URL('../runtime/.state', import.meta.url)));
  const manifest = { schema_version: 1, kind: 'author_generation', campaign_id: campaignId, lanes_sha256: digest(config),
    max_attempts_per_author: config.max_attempts_per_author, lanes: laneStatus, agent_kind: 'model_author', promoting: false, qualification: 'research_only',
    // Authorship from injected adapters (tests, harnesses) is never evidence of real model authorship.
    authorship_host_verified: laneStatus.every(l => l.adapter_source === 'built_in') };
  requireThat(Object.keys(adapters).length >= 1, 'no_author_available');
  const summary = await runFleet({ stateDir: root, campaignId, laneAdapters: adapters, authorCap: config.max_attempts_per_author,
    rounds: rounds ?? config.max_attempts_per_author, now, worker, datasetBindings, datasetIds, researchScope });
  const sealed = { ...manifest, summary, manifest_sha256: digest({ ...manifest, summary }) };
  const dir = join(root, 'candidates', campaignId); await mkdir(dir, { recursive: true, mode: 0o700 });
  const tmp = join(dir, `generation.json.${process.pid}.tmp`); await writeFile(tmp, JSON.stringify(sealed, null, 2) + '\n', { mode: 0o600 }); await rename(tmp, join(dir, 'generation.json'));
  return sealed;
}

// CLI: node league/authors.mjs --campaign-id ID [--state-dir DIR] [--lanes FILE]
if (import.meta.url === pathToFileURL(process.argv[1] ?? '').href) {
  const args = Object.fromEntries(process.argv.slice(2).reduce((acc, a, i, all) => (a.startsWith('--') ? [...acc, [a.slice(2), all[i + 1]]] : acc), []));
  const lanes = args.lanes ? JSON.parse(readFileSync(args.lanes, 'utf8')) : DEFAULT_LANES;
  runAuthorGeneration({ stateDir: args['state-dir'], campaignId: args['campaign-id'], lanes })
    .then(r => process.stdout.write(JSON.stringify({ ok: true, campaign_id: r.campaign_id, lanes: r.lanes, registered: r.summary.registered, unavailable: r.summary.unavailable,
      timed_out: r.summary.timed_out, rejected: r.summary.rejected, agent_written_registered: r.summary.agent_written_registered, global_search_size_n: r.summary.global_search_size_n }) + '\n'))
    .catch(e => { process.stdout.write(JSON.stringify({ ok: false, error: e.code ?? 'author_generation_failure' }) + '\n'); process.exitCode = 1; });
}
