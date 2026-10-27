import test from 'node:test';
import assert from 'node:assert/strict';
import { validateStrategyShape } from '../../runtime/schemas.mjs';

const cases = {
  sma_cross_long: { fast_window: 1, slow_window: 2 },
  momentum_long: { lookback_bars: 2, enter_return_bps: '500', exit_return_bps: '0' },
  mean_reversion_long: { lookback_bars: 5, enter_z: '-2', exit_z: '0' },
  breakout_long: { entry_channel_bars: 2, exit_channel_bars: 2 },
  post_earnings_drift_long: { min_surprise_pct: '5', entry_delay_bars: 1, hold_bars: 2 }
};
const spec = (family, parameters = cases[family]) => ({ schema_version: 2, family, symbol: 'SPY',
  parameters, position: { quantity: '1' }, rationale: 'Synthetic test.',
  data_requirements: family === 'post_earnings_drift_long' ? ['ohlcv_pti_v1', 'earnings_events_pti_v1'] : ['ohlcv_pti_v1'] });

test('shared schema accepts all five v2 families and the unchanged v1 shape', () => {
  for (const family of Object.keys(cases)) assert.equal(validateStrategyShape(spec(family)).family, family);
  assert.ok(validateStrategyShape({ symbol: 'SYNTH', parameters: cases.sma_cross_long, position: { quantity: '1' } }));
});

test('shared schema rejects short sales, multiple legs, code and same-bar execution', () => {
  const base = spec('momentum_long');
  for (const invalid of [
    { ...base, position: { quantity: '-1' } }, { ...base, legs: [] },
    { ...base, direction: 'short' }, { ...base, account: 'forbidden' },
    { ...base, parameters: { ...base.parameters, code: 'execute()' } },
    { ...base, execution: { timing: 'same_bar_close' } }
  ]) assert.throws(() => validateStrategyShape(invalid), { code: 'invalid_strategy_spec' });
});

test('shared schema enforces family parameter shapes and earnings requirements', () => {
  assert.throws(() => validateStrategyShape(spec('momentum_long', cases.breakout_long)));
  assert.throws(() => validateStrategyShape({ ...spec('post_earnings_drift_long'), data_requirements: ['ohlcv_pti_v1'] }));
  assert.throws(() => validateStrategyShape({ ...spec('post_earnings_drift_long'), timeframe: '1m' }));
  assert.throws(() => validateStrategyShape({ ...spec('momentum_long'), timeframe: '1s' }));
  assert.throws(() => validateStrategyShape({ ...spec('momentum_long'), position: { sizing: 'fixed_notional', quantity: '1', notional_usd: '25' } }));
});
