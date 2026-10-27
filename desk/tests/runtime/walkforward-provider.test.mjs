import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createRecordedProvider, createWalkForwardBridge } from '../../runtime/providers/index.mjs';
import { digest } from '../../runtime/contracts.mjs';
import { fileURLToPath } from 'node:url';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const seal = (value, field) => ({ ...value, [field]: digest(value) });
function provider(day) {
  const historicals = [100, 101, 99, 102, 100, 104, 98, 100].map((price, i) => {
    const begins_at = new Date(Date.parse(`${day}T14:30:00.000Z`) + i * 60000).toISOString();
    const ends_at = new Date(Date.parse(begins_at) + 60000).toISOString();
    return { begins_at, ends_at, available_at: ends_at, open_price: String(price), close_price: String(price),
      high_price: String(price + 1), low_price: String(price - 1), volume: 1000, session: 'reg' };
  });
  return createRecordedProvider({ recording: { classification: 'synthetic', received_at: '2024-01-10T15:00:00.000Z',
    bars: { structuredContent: { symbol: 'SPY', interval: 'minute', historicals } }, quotes: {} } });
}
const query = day => ({ symbol: 'SPY', timeframe: '1m', start: `${day}T14:30:00.000Z`, end_exclusive: `${day}T14:39:00.000Z` });

test('2.b provider captures run through pinned Python walk-forward evaluation', { timeout: 90000 }, async t => {
  const root = await mkdtemp(join(tmpdir(), 'walkforward-provider-')); t.after(() => rm(root, { recursive: true, force: true }));
  const bridge = createWalkForwardBridge({ statePath: join(root, 'campaign.sqlite'), timeoutMs: 30000 });
  const development = await bridge.captureProviderDataset({ provider: provider('2024-01-08'), query: query('2024-01-08'), partition: 'development' });
  const validation = await bridge.captureProviderDataset({ provider: provider('2024-01-09'), query: query('2024-01-09'), partition: 'validation' });
  const campaign = seal({ schema_version: 1, kind: 'walkforward_campaign', campaign_id: 'provider-smoke', created_at: '2024-01-10T16:00:00.000Z',
    max_candidates: 50, datasets: { development: development.manifest_sha256, validation: validation.manifest_sha256, holdout_id: 'sealed:test' },
    holdout_window: { start: '2024-01-11T00:00:00.000Z', end_exclusive: '2024-01-12T00:00:00.000Z' }, initial_cash: '1000.00',
    walkforward: { initial_train_bars: 3, test_bars: 2 } }, 'campaign_sha256');
  await bridge.createCampaign(campaign);
  // Python normalizes and seals this v2 strategy before it is preregistered.
  const spec = { schema_version: 2, family: 'sma_cross_long', symbol: 'SPY', timeframe: '1m',
    parameters: { fast_window: 1, slow_window: 2 }, position: { quantity: '1' }, rationale: 'Synthetic provider integration.' };
  const normalized = { ...spec, kind: 'strategy_spec', position: { quantity: '1', sizing: 'fixed_quantity', max_positions: 1 },
    exits: { stop_loss_bps: null, take_profit_bps: null, max_hold_bars: null },
    execution: { timing: 'next_bar_open', order_type: 'limit', limit_offset_bps: '10', pending_on_end: 'cancel', terminal_position: 'mark_to_market' },
    data_requirements: ['ohlcv_pti_v1'] };
  const strategy_sha256 = digest(normalized);
  const preregistration = seal({ schema_version: 2, kind: 'preregistration', campaign_id: campaign.campaign_id,
    created_at: '2024-01-10T17:00:00.000Z', author: { slot: 'unit', model: 'synthetic', role: 'author' }, strategy_sha256,
    datasets: campaign.datasets, cost_model: { commission_bps: '0', slippage_bps_per_side: '10', regulatory_fee_bps_on_sells: '1', rounding: 'half_even' },
    baselines: ['buy_and_hold', 'cash'], pass_criteria: { min_independent_trades: 30, max_drawdown_pct: '15', min_excess_return_vs_buy_and_hold_pct: '0',
      alpha: '0.05', test: 'stationary_block_bootstrap_excess_daily_returns', bootstrap: { block_bars: 10, resamples: 1000, seed: 0 } },
    paper_criteria: null }, 'preregistration_sha256');
  const registration = await bridge.register({ campaign_id: campaign.campaign_id, spec, preregistration, requested_at: '2024-01-10T18:00:00.000Z' });
  assert.equal(registration.status, 'registered');
  const request = { registration_sha256: registration.registration_sha256, partition: 'development', requested_at: '2024-01-10T18:00:00.000Z' };
  const first = await bridge.evaluate(request), second = await bridge.evaluate(request);
  assert.deepEqual(first, second); assert.equal(first.verdict.N, 1); assert.equal(first.verdict.passed, false);
  assert.equal(first.result.qualification, 'research_only'); assert.equal(first.result.data_classification, 'synthetic');
  assert.equal(first.result.evaluated_rows, 5); assert.equal(first.result.baselines.cash.total_return, '0');
  assert.equal(first.result.baselines.buy_and_hold.equity.length, 5);
});

test('provider bridge rejects holdout before calling a provider or starting Python', async () => {
  const bridge = createWalkForwardBridge({ statePath: '/tmp/unused-walkforward-holdout.sqlite' }); let called = false;
  await assert.rejects(bridge.captureProviderDataset({ provider: { getBars() { called = true; } }, query: {}, partition: 'holdout' }), { code: 'dataset_access_denied' });
  assert.equal(called, false);
  assert.throws(() => bridge.evaluate({ registration_sha256: '0'.repeat(64), partition: 'holdout', requested_at: '2024-01-01T00:00:00.000Z' }), { code: 'dataset_access_denied' });
});

test('Maverick validation provider import binds the known development manifest', { timeout: 90000 }, async t => {
  const app = fileURLToPath(new URL('../..', import.meta.url));
  const root = await mkdtemp(join(app, 'runtime/.state/validation-provider-test-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const { stdout } = await promisify(execFile)(join(app, 'backtest/.venv/bin/python'), ['-c',
    "import sys,json;sys.path.insert(0,'tests/backtest');from test_maverick_import import capture;from test_maverick_validation import later_capture;from backtest.maverick_import import build_snapshot;print(json.dumps([build_snapshot(capture()),build_snapshot(later_capture())]))"],
    { cwd: app, timeout: 30000, maxBuffer: 1024 * 1024 });
  const [development, validation] = JSON.parse(stdout);
  const bridge = createWalkForwardBridge({ statePath: join(root, 'campaign.sqlite'), timeoutMs: 30000 });
  const dev = await bridge.captureProviderDataset({ provider: { getBars: async () => development }, query: {}, partition: 'development' });
  await assert.rejects(bridge.captureProviderDataset({ provider: { getBars: async () => validation }, query: {}, partition: 'validation' }), { code: 'development_reference_required' });
  const val = await bridge.captureProviderDataset({ provider: { getBars: async () => validation }, query: {}, partition: 'validation', developmentManifestSha256: dev.manifest_sha256 });
  assert.equal(val.partition, 'validation');
  assert.equal(val.classification, 'historical_unverified');
  assert.equal(val.provenance.development_manifest_sha256, dev.manifest_sha256);
  assert.equal(val.provenance.point_in_time_verified, false);
  assert.equal(val.provenance.historical_eligible, false);
});
