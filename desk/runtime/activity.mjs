// Explicit public projection: never publish prompts, provider config or raw payloads.
import { readFile, mkdir, rename, writeFile } from 'node:fs/promises';
import { join, dirname } from 'node:path';
import { digest, verifyReceipt } from './contracts.mjs';

const metrics = ['initial_cash', 'final_equity', 'final_cash', 'realized_net_pnl', 'total_return', 'max_drawdown', 'commission_total', 'completed_trades'];
const select = (value, keys) => Object.fromEntries(keys.filter(k => Object.hasOwn(value, k)).map(k => [k, value[k]]));

export async function publicActivity(job, stateDir) {
  const receipt = job.receipt ? verifyReceipt(job.receipt) : null;
  async function rows(kind) {
    if (!receipt) return [];
    const sha = receipt.artifacts[`${kind}_sha256`];
    if (!/^[a-f0-9]{64}$/.test(sha)) throw new Error('invalid_artifact_hash');
    const artifact = JSON.parse(await readFile(join(stateDir, 'artifacts', `${sha}.json`), 'utf8'));
    if (digest(artifact) !== sha || artifact.kind !== kind || artifact.request_sha256 !== receipt.request_sha256) throw new Error('activity_artifact_integrity_failure');
    const keys = kind === 'equity' ? ['at', 'equity', 'cash', 'quantity'] : ['executed_at', 'side', 'price', 'quantity', 'commission'];
    return artifact.rows.slice(-100).map(r => select(r, keys));
  }
  const [equity, fills] = await Promise.all([rows('equity'), rows('fills')]);
  return {
    schema_version: 1, kind: 'algo_activity', observed_at: new Date().toISOString(),
    job_id: job.job_id, state: job.state, phase: job.phase, revision: job.checkpoint_revision,
    provider: job.policy.provider, model_id: job.policy.model_id,
    dataset_id: job.dataset_id ?? null, symbol: job.strategy?.symbol ?? null,
    qualification: 'research_only', promoting: false, live_orders: false,
    broker_connection: 'not_checked', receipt_sha256: receipt?.receipt_sha256 ?? null,
    metrics: receipt ? select(receipt.metrics, metrics) : null, equity, fills,
    events: job.events.slice(-60).map(e => ({
      sequence: e.sequence, event_type: e.event_type, actor_role: e.actor_role,
      occurred_at: e.occurred_at,
      tool: e.event_type === 'tool_recorded' ? e.payload.tool : null,
    })),
  };
}

export async function publishActivity(job, stateDir, path) {
  const data = await publicActivity(job, stateDir);
  await mkdir(dirname(path), { recursive: true, mode: 0o700 });
  const temp = `${path}.${process.pid}.tmp`;
  await writeFile(temp, JSON.stringify(data) + '\n', { mode: 0o600 });
  await rename(temp, path);
  return data;
}
