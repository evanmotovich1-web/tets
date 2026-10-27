import { mkdir, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { join } from 'node:path';
import canonicalize from 'canonicalize';
import { digest } from '../../runtime/contracts.mjs';
import { campaignScreen } from '../../runtime/campaign.mjs';

export const proposal = (id, changes = {}) => ({ id, source: { kind: 'fixture' }, strategy: {
  schema_version: 2, family: 'sma_cross_long', symbol: 'SPY', timeframe: '1m',
  parameters: { fast_window: 1, slow_window: 2 }, position: { quantity: '1' },
  execution: { timing: 'next_bar_open', order_type: 'market', pending_on_end: 'cancel', terminal_position: 'mark_to_market' },
  rationale: 'Synthetic campaign mechanics only.', ...changes } });
const sealed = (v, field) => ({ ...v, [field]: digest(v) });
export function failedJob(message, status = 'failed', code = 'fixture_job_failed') {
  return sealed({ schema_version: 1, kind: 'campaign_job_result', input_sha256: digest(message), status, code,
    qualification: 'research_only', promoting: false }, 'job_result_sha256');
}
export async function planFor(root, candidates = [proposal('one'), proposal('two', { parameters: { fast_window: 1, slow_window: 3 } })]) {
  const rows = [100, 101, 99, 102, 100, 104, 98, 100].map((p, i) => {
    const open = new Date(Date.UTC(2024, 0, 8, 14, 30 + i)).toISOString();
    const close = new Date(Date.parse(open) + 60000).toISOString();
    return { symbol: 'SPY', bar_open_at: open, bar_close_at: close, available_at: close,
      open: String(p), high: String(p + 1), low: String(p - 1), close: String(p), volume: '1000' };
  });
  const bytes = rows.map(r => canonicalize(r) + '\n').join('');
  const manifest = sealed({ schema_version: 1, kind: 'dataset_manifest', dataset_id: 'synthetic:campaign:v1', classification: 'synthetic', partition: 'development',
    source: { provider: 'internal-fixture', source_reference: 'campaign-mechanics', license_reference: 'synthetic_fixture', retrieved_at: '2024-01-09T00:00:00.000Z', snapshot_sha256: 'a'.repeat(64) },
    format: 'jsonl', row_schema: 'ohlcv_pti_v1', timeframe: '1m', calendar_id: 'nyse_regular', price_basis: 'raw', currency: 'USD', price_scale: 2, cash_scale: 2, quantity_scale: 0,
    symbols: ['SPY'], coverage: { first_bar_open: rows[0].bar_open_at, last_bar_close: rows.at(-1).bar_close_at }, row_count: rows.length,
    data_sha256: createHash('sha256').update(bytes).digest('hex') }, 'manifest_sha256');
  await mkdir(root, { recursive: true });
  await writeFile(join(root, 'data.jsonl'), bytes); await writeFile(join(root, 'manifest.json'), JSON.stringify(manifest));
  const registry = join(root, 'registry.json');
  await writeFile(registry, JSON.stringify({ schema_version: 1, kind: 'dataset_registry', datasets: {
    [manifest.dataset_id]: { classification: 'synthetic', partition: 'development', manifest: 'manifest.json', data: 'data.jsonl', manifest_sha256: manifest.manifest_sha256 } } }));
  return { schema_version: 1, kind: 'backtest_campaign', campaign_id: 'bounded-test', registry, dataset_id: manifest.dataset_id,
    evaluation_window: { start: rows[0].bar_open_at, end_exclusive: '2024-01-08T14:39:00.000Z' }, initial_cash: '10000',
    cost_model: { commission_bps: '10', slippage_bps_per_side: '10', rounding: 'half_even' },
    walkforward: { initial_train_bars: 4, test_bars: 2 }, screen: campaignScreen(),
    limits: { concurrency: 3, max_attempts: 50, max_wall_ms: 30000, job_timeout_ms: 10000 }, candidates };
}
