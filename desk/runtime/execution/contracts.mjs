import { createHash } from 'node:crypto';
import canonicalize from 'canonicalize';

export class ExecutionError extends Error {
  constructor(code) { super(code); this.name = 'ExecutionError'; this.code = code; }
}
export function requireThat(condition, code) { if (!condition) throw new ExecutionError(code); }
export function digest(value) { return createHash('sha256').update(canonicalize(value)).digest('hex'); }
export function copy(value) { return JSON.parse(JSON.stringify(value)); }
export function exact(value, required, optional = []) {
  requireThat(value && typeof value === 'object' && !Array.isArray(value) && required.every(k => Object.hasOwn(value, k)) && Object.keys(value).every(k => [...required, ...optional].includes(k)), 'invalid_intent');
}
export function stamp(value) {
  requireThat(typeof value === 'string' && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$/.test(value) && Number.isFinite(Date.parse(value)) && new Date(value).toISOString() === value, 'invalid_timestamp');
  return value;
}
export function currentTime(clock) { const value = clock(); return stamp(value instanceof Date ? value.toISOString() : value); }
export function cents(value, signed = false) {
  requireThat(typeof value === 'string' && value.length <= 18 && (signed ? /^-?(0|[1-9][0-9]*)(\.\d{1,2})?$/ : /^(0|[1-9][0-9]*)(\.\d{1,2})?$/).test(value), 'invalid_money');
  const negative = value.startsWith('-'), [whole, fraction = ''] = value.replace('-', '').split('.');
  return (BigInt(whole) * 100n + BigInt(fraction.padEnd(2, '0'))) * (negative ? -1n : 1n);
}
export function money(value) {
  const negative = value < 0n; if (negative) value = -value;
  return `${negative ? '-' : ''}${value / 100n}.${String(value % 100n).padStart(2, '0')}`;
}
export function quantity(value, zero = false) {
  requireThat(typeof value === 'string' && /^(0|[1-9][0-9]{0,6})$/.test(value) && (zero || value !== '0'), 'invalid_quantity');
  return BigInt(value);
}
export function hash(value) { requireThat(typeof value === 'string' && /^[a-f0-9]{64}$/.test(value), 'invalid_strategy_hash'); return value; }
export const DEFAULT_LIMITS = Object.freeze({ per_order_max_usd: '25.00', per_strategy_max_exposure_usd: '100.00',
  total_max_exposure_usd: '250.00', max_orders_per_strategy_per_day: 10, strategy_daily_loss_usd: '10.00',
  daily_loss_kill_usd: '25.00', quote_max_age_ms: 2000, limit_collar_bps: 50, min_share_price_usd: '5.00' });
export function limits(input = {}) {
  requireThat(input && typeof input === 'object' && Object.keys(input).every(k => Object.hasOwn(DEFAULT_LIMITS, k)), 'invalid_limits');
  const result = { ...DEFAULT_LIMITS, ...input };
  for (const [key, value] of Object.entries(result)) {
    const numeric = typeof DEFAULT_LIMITS[key] === 'number';
    requireThat(numeric ? Number.isSafeInteger(value) && value >= 0 && value <= DEFAULT_LIMITS[key] : cents(value) > 0n && cents(value) <= cents(DEFAULT_LIMITS[key]), 'limit_raise_not_approved');
  }
  // Raising a default is deliberately unsupported in this fake-only service.
  return Object.freeze(result);
}
export function normalizeIntent(value, accountId) {
  exact(value, ['strategy_sha256', 'decision_bar_close_time', 'side', 'intent_seq', 'account_id', 'symbol', 'quantity', 'limit_price']);
  hash(value.strategy_sha256); stamp(value.decision_bar_close_time);
  requireThat(['buy', 'sell'].includes(value.side), 'short_not_allowed');
  requireThat(Number.isSafeInteger(value.intent_seq) && value.intent_seq >= 0 && value.intent_seq <= 1000000, 'invalid_intent');
  requireThat(value.account_id === accountId, 'wrong_account');
  requireThat(typeof value.symbol === 'string' && /^[A-Z][A-Z0-9]{0,4}([.-][A-Z])?$/.test(value.symbol), 'symbol_out_of_scope');
  quantity(value.quantity); requireThat(cents(value.limit_price) > 0n, 'invalid_money');
  const { account_id, ...safe } = value;
  return { ...safe, limit_price: money(cents(value.limit_price)), account_sha256: digest(account_id), account_last4: account_id.slice(-4) };
}
export function idempotencyKey(intent) {
  return digest(Object.fromEntries(['strategy_sha256', 'decision_bar_close_time', 'side', 'intent_seq'].map(key => [key, intent[key]])));
}
export function sealRecord(value) { return { ...value, record_sha256: digest(value) }; }
export function verifyRecord(value) {
  if (!value || typeof value !== 'object') return false;
  const { record_sha256, ...body } = value;
  return typeof record_sha256 === 'string' && digest(body) === record_sha256;
}
const formatter = new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit', weekday: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' });
export function fakeSession(now) {
  const p = Object.fromEntries(formatter.formatToParts(new Date(now)).map(part => [part.type, part.value]));
  const midnight = Date.parse(now) - (Number(p.hour) * 3600 + Number(p.minute) * 60 + Number(p.second)) * 1000 - new Date(now).getUTCMilliseconds();
  return { market_day: `${p.year}-${p.month}-${p.day}`, open_at: new Date(midnight + 34200000).toISOString(),
    close_at: new Date(midnight + 57600000).toISOString(), regular_session: !['Sat', 'Sun'].includes(p.weekday) };
}
