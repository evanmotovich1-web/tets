// Host-owned global exposure index (AGENT-FARM-PLAN.md section 2; task 1.b item 2).
// One append-only, hash-chained JSONL per desk state root counts every strategy tried across
// campaigns, fleet rounds, league generations and experiments, so the correction for luck can
// never shrink by starting a new campaign. It also caps blind selections per frozen experiment.
import { mkdir, readFile, open, unlink } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { DeskError, digest, requireThat, parseStrictJSON } from './contracts.mjs';

export const SCOPES = Object.freeze(['fleet_attempt', 'campaign_attempt', 'league_candidate', 'blind_selection']);
export const MAX_BLIND_SELECTIONS = 3;
const ZERO = '0'.repeat(64);
const HASH = /^[0-9a-f]{64}$/;
const ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;

export const exposurePath = stateDir => join(resolve(stateDir), 'exposure', 'index.jsonl');

/** The experiment key ignores ids and names, so renaming an experiment cannot reset its cap. */
export function experimentKey(config) {
  const strip = v => Array.isArray(v) ? v.map(strip) : v && typeof v === 'object'
    ? Object.fromEntries(Object.entries(v).filter(([k]) => !['experiment_id', 'id', 'name', 'campaign_id', 'label'].includes(k)).map(([k, x]) => [k, strip(x)])) : v;
  return digest(strip(config));
}

export async function readExposure(stateDir) {
  let text;
  try { text = await readFile(exposurePath(stateDir), 'utf8'); } catch (e) { if (e.code === 'ENOENT') return []; throw e; }
  const rows = text.split('\n').filter(Boolean).map(line => parseStrictJSON(line));
  let prev = ZERO;
  rows.forEach((r, seq) => {
    const { entry_sha256, ...body } = r;
    requireThat(r.seq === seq && r.prev_entry_sha256 === prev && digest(body) === entry_sha256, 'exposure_tampered');
    requireThat(SCOPES.includes(r.scope) && HASH.test(r.key) && ID.test(r.source_id), 'exposure_tampered');
    prev = entry_sha256;
  });
  return rows;
}

/** Global N: distinct strategy keys across every non-selection scope. Retries of one key count once. */
export function globalSearchSize(rows) {
  return new Set(rows.filter(r => r.scope !== 'blind_selection').map(r => r.key)).size;
}

/** Single-writer handle. Callers hold it for one run and close it. */
export async function openExposure(stateDir, { now = () => new Date().toISOString() } = {}) {
  const path = exposurePath(stateDir), lockPath = `${path}.lock`;
  await mkdir(join(resolve(stateDir), 'exposure'), { recursive: true, mode: 0o700 });
  let lock;
  try { lock = await open(lockPath, 'wx', 0o600); } catch (e) { if (e.code === 'EEXIST') throw new DeskError('exposure_already_open'); throw e; }
  let rows, out;
  try { rows = await readExposure(stateDir); out = await open(path, 'a', 0o600); }
  catch (e) { await lock.close(); await unlink(lockPath); throw e; }
  const keys = new Set(rows.map(r => `${r.scope}:${r.key}`));
  async function append(fields) {
    const body = { schema_version: 1, kind: 'exposure_entry', seq: rows.length, recorded_at: now(), ...fields, prev_entry_sha256: rows.length ? rows.at(-1).entry_sha256 : ZERO };
    const row = { ...body, entry_sha256: digest(body) };
    await out.write(JSON.stringify(row) + '\n'); await out.sync();
    rows.push(row); keys.add(`${row.scope}:${row.key}`); return row;
  }
  return {
    /** Idempotent per (scope, key): a retry of the same attempt is not a new strategy. */
    async record({ scope, key, sourceId, lineageGroup = null, datasetFamily = null }) {
      requireThat(SCOPES.includes(scope) && scope !== 'blind_selection' && HASH.test(key) && ID.test(sourceId), 'invalid_exposure_entry');
      if (keys.has(`${scope}:${key}`)) return null;
      return append({ scope, key, source_id: sourceId, lineage_group: lineageGroup, dataset_family: datasetFamily });
    },
    /** One blind selection against a frozen experiment. The fourth is refused, whatever the id. */
    async recordBlindSelection({ experimentConfig, sourceId }) {
      const key = experimentKey(experimentConfig);
      const used = rows.filter(r => r.scope === 'blind_selection' && r.experiment_key === key).length;
      requireThat(used < MAX_BLIND_SELECTIONS, 'blind_selection_cap');
      return append({ scope: 'blind_selection', key: digest({ key, n: used }), experiment_key: key, source_id: sourceId, lineage_group: null, dataset_family: null });
    },
    globalN: () => globalSearchSize(rows),
    rows: () => rows.slice(),
    async close() { await out.close(); await lock.close(); await unlink(lockPath); }
  };
}
