import { createHash } from 'node:crypto';
import canonicalize from 'canonicalize';
import { parseStrictJSON } from '../contracts.mjs';

export const ROBINHOOD_MCP_URL = 'https://agent.robinhood.com/mcp/trading';
export const READ_TOOLS = Object.freeze(['get_equity_historicals', 'get_equity_quotes']);
export const INTERVALS = Object.freeze({ '1m': 'minute', '5m': '5minute', '15m': '15minute', '1h': 'hour', '1d': 'day' });
const MAX_BYTES = 1024 * 1024;
export class ProviderError extends Error {
  constructor(code) { super(code); this.name = 'ProviderError'; this.code = code; }
}
export function check(condition, code) { if (!condition) throw new ProviderError(code); }
export function checksum(value) { return createHash('sha256').update(canonicalize(value)).digest('hex'); }
export function freeze(value) {
  if (value && typeof value === 'object') { for (const item of Object.values(value)) freeze(item); Object.freeze(value); }
  return value;
}
export function timestamp(value, ceil = false) {
  check(typeof value === 'string' && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|[+-]\d\d:\d\d)$/.test(value), 'invalid_timestamp');
  const year = Number(value.slice(0, 4)), month = Number(value.slice(5, 7)), day = Number(value.slice(8, 10));
  check(year >= 1900 && month >= 1 && month <= 12 && day >= 1 && day <= new Date(Date.UTC(year, month, 0)).getUTCDate(), 'invalid_timestamp');
  let ms = Date.parse(value);
  check(Number.isFinite(ms), 'invalid_timestamp');
  const fraction = value.match(/\.(\d+)/)?.[1] ?? '';
  if (ceil && /[1-9]/.test(fraction.slice(3))) ms++;
  return new Date(ms).toISOString();
}
export function clockTime(clock) {
  const value = clock();
  return timestamp(value instanceof Date ? value.toISOString() : value);
}
export function symbol(value) { check(typeof value === 'string' && /^[A-Z][A-Z0-9]{0,4}(?:[.-][A-Z])?$/.test(value), 'symbol_out_of_scope'); return value; }
export function decimal(value, positive = false) {
  if (typeof value === 'number' && Number.isSafeInteger(value)) value = String(value);
  check(typeof value === 'string' && value.length <= 40 && /^(0|[1-9][0-9]*)(\.[0-9]{1,12})?$/.test(value), 'invalid_market_decimal');
  const [whole, fraction = ''] = value.split('.');
  const result = whole + (fraction.replace(/0+$/, '') ? '.' + fraction.replace(/0+$/, '') : '');
  check(!positive || units(result) > 0n, 'invalid_market_decimal');
  return result;
}
export function units(value) { const [whole, fraction = ''] = value.split('.'); return BigInt(whole) * 1000000000000n + BigInt(fraction.padEnd(12, '0')); }
export function barQuery(input) {
  check(input && typeof input === 'object' && !Array.isArray(input), 'invalid_provider_query');
  check(Object.keys(input).every(k => ['symbol', 'timeframe', 'start', 'end_exclusive', 'signal'].includes(k)), 'invalid_provider_query');
  const result = { symbol: symbol(input.symbol), timeframe: input.timeframe, start: timestamp(input.start), end_exclusive: timestamp(input.end_exclusive) };
  check(Object.hasOwn(INTERVALS, result.timeframe), 'unsupported_timeframe');
  check(result.start < result.end_exclusive, 'invalid_provider_window');
  return result;
}
export function quoteQuery(input) {
  check(input && typeof input === 'object' && !Array.isArray(input) && Object.keys(input).every(k => ['symbol', 'signal'].includes(k)), 'invalid_provider_query');
  return { symbol: symbol(input.symbol) };
}
export function toolPayload(response) {
  try {
    check(response && typeof response === 'object' && response.isError !== true, 'provider_tool_error');
    const text = JSON.stringify(response);
    check(Buffer.byteLength(text) <= MAX_BYTES, 'provider_response_too_large');
    let payload;
    if (response.structuredContent !== undefined) payload = response.structuredContent;
    else {
      check(Array.isArray(response.content) && response.content.length <= 8 && response.content.every(c => c.type === 'text' && typeof c.text === 'string'), 'unsupported_provider_response');
      payload = parseStrictJSON(response.content.map(c => c.text).join('\n'), MAX_BYTES);
    }
    check(payload && typeof payload === 'object', 'unsupported_provider_response');
    if (payload.result !== undefined) payload = payload.result;
    check(payload && typeof payload === 'object' && !payload.next && !payload.next_cursor && !payload.has_more, 'provider_pagination_required');
    return payload;
  } catch (error) { throw error instanceof ProviderError ? error : new ProviderError('unsupported_provider_response'); }
}
function snapshot(body) { return freeze({ ...body, snapshot_sha256: checksum(body) }); }
const ny = new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit', weekday: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' });
function regular(open, close) {
  const parts = value => Object.fromEntries(ny.formatToParts(new Date(value)).map(p => [p.type, p.value]));
  const a = parts(open), b = parts(close);
  const day = p => `${p.year}-${p.month}-${p.day}`;
  const time = p => `${p.hour}:${p.minute}:${p.second}`;
  return !['Sat', 'Sun'].includes(a.weekday) && day(a) === day(b) && time(a) >= '09:30:00' && time(a) < time(b) && time(b) <= '16:00:00';
}

export function normalizeBars(response, query, { provider, received_at, resolveBarBounds, synthetic = false }) {
  const payload = toolPayload(response);
  let parent = payload;
  if (Array.isArray(payload.results)) {
    const containers = payload.results.filter(item => item?.symbol === query.symbol && Array.isArray(item.historicals));
    check(containers.length === 1, 'unsupported_provider_response');
    parent = containers[0];
  }
  check(parent.symbol === query.symbol && Array.isArray(parent.historicals), 'provider_symbol_mismatch');
  if (parent.interval !== undefined) check([query.timeframe, INTERVALS[query.timeframe]].includes(parent.interval), 'provider_timeframe_mismatch');
  check(parent.historicals.length <= 10000, 'provider_row_limit');
  const rows = [], bases = new Set(), excluded = { outside_window: 0, incomplete: 0, extended_hours: 0 };
  let previous = null;
  for (const raw of parent.historicals) {
    check(raw && typeof raw === 'object' && (raw.symbol === undefined || raw.symbol === query.symbol), 'provider_symbol_mismatch');
    check(raw.interpolated !== true, 'interpolated_bar');
    if (raw.session !== undefined && !['reg', 'regular'].includes(raw.session)) { excluded.extended_hours++; continue; }
    let open = raw.bar_open_at ?? raw.begins_at;
    let close = raw.bar_close_at ?? raw.ends_at;
    if (close === undefined && resolveBarBounds) {
      const resolved = resolveBarBounds({ symbol: query.symbol, timeframe: query.timeframe, begins_at: timestamp(open) });
      check(resolved && typeof resolved === 'object', 'bar_bounds_unavailable');
      open = resolved.bar_open_at; close = resolved.bar_close_at;
    }
    // Daily bucket labels do not establish an exchange session close. A host
    // calendar must resolve missing bounds, including holidays and early closes.
    check(close !== undefined, 'bar_bounds_unavailable');
    open = timestamp(open); close = timestamp(close);
    check(open < close && (previous === null || open >= previous), 'duplicate_or_unordered_bars');
    const duration = Date.parse(close) - Date.parse(open);
    const expectedDuration = { '1m': 60000, '5m': 300000, '15m': 900000, '1h': 3600000 }[query.timeframe];
    if (expectedDuration !== undefined)
      check(duration === expectedDuration || (query.timeframe === '1h' && duration === 1800000), 'provider_timeframe_mismatch');
    previous = close;
    if (open < query.start || close >= query.end_exclusive) { excluded.outside_window++; continue; }
    if (close > received_at) { excluded.incomplete++; continue; }
    if (!regular(open, close)) { excluded.extended_hours++; continue; }
    const basis = raw.available_at === undefined ? 'observed_at_fetch' : 'source';
    const available = raw.available_at === undefined ? received_at : timestamp(raw.available_at, true);
    check(available >= close && available <= received_at, 'future_data');
    const row = { symbol: query.symbol, bar_open_at: open, bar_close_at: close, available_at: available,
      open: decimal(raw.open_price ?? raw.open, true), high: decimal(raw.high_price ?? raw.high, true),
      low: decimal(raw.low_price ?? raw.low, true), close: decimal(raw.close_price ?? raw.close, true), volume: decimal(raw.volume) };
    check(units(row.low) <= units(row.open) && units(row.low) <= units(row.close) && units(row.high) >= units(row.open) && units(row.high) >= units(row.close), 'invalid_ohlc');
    bases.add(basis); rows.push(row);
  }
  const jsonl = rows.map(row => canonicalize(row) + '\n').join('');
  return snapshot({ schema_version: 1, kind: 'bar_snapshot', provider, classification: synthetic ? 'synthetic' : 'unqualified_market_data',
    symbol: query.symbol, timeframe: query.timeframe, received_at, rows,
    availability_basis: bases.size === 1 ? [...bases][0] : bases.size ? 'mixed' : 'unavailable',
    point_in_time_verified: false, historical_eligible: false,
    counts: { raw_rows: parent.historicals.length, returned_rows: rows.length, excluded },
    raw_response_sha256: checksum(response), data_sha256: createHash('sha256').update(jsonl).digest('hex') });
}

export function normalizeQuote(response, query, { provider, received_at, as_of, synthetic = false }) {
  const payload = toolPayload(response);
  const values = Array.isArray(payload) ? payload : payload.results ?? payload.quotes ?? [payload];
  check(Array.isArray(values) && values.length <= 20, 'unsupported_provider_response');
  const matches = values.filter(row => row?.symbol === query.symbol);
  check(matches.length === 1, 'provider_symbol_mismatch');
  const raw = matches[0];
  const bid = decimal(raw.bid_price ?? raw.bid, true), ask = decimal(raw.ask_price ?? raw.ask, true);
  check(units(bid) <= units(ask), 'crossed_quote');
  let quote_at = null;
  const sourceTimes = {};
  for (const key of ['quote_at', 'bid_price_updated_at', 'ask_price_updated_at']) if (raw[key] !== undefined) {
    sourceTimes[key] = timestamp(raw[key]);
    check(sourceTimes[key] <= received_at, 'future_data');
  }
  if (sourceTimes.quote_at !== undefined) quote_at = sourceTimes.quote_at;
  if (sourceTimes.bid_price_updated_at !== undefined && sourceTimes.ask_price_updated_at !== undefined)
    quote_at = [sourceTimes.bid_price_updated_at, sourceTimes.ask_price_updated_at, ...(quote_at ? [quote_at] : [])].sort()[0];
  check(quote_at === null || quote_at <= received_at, 'future_data');
  const source_available_at = raw.available_at === undefined ? null : timestamp(raw.available_at, true);
  check(source_available_at === null || (source_available_at <= received_at && (quote_at === null || source_available_at >= quote_at)), 'future_data');
  check(as_of >= received_at, 'clock_regression');
  const quote_age_ms = quote_at === null ? null : Date.parse(as_of) - Date.parse(quote_at);
  return snapshot({ schema_version: 1, kind: 'quote_snapshot', provider, classification: synthetic ? 'synthetic' : 'unqualified_market_data',
    symbol: query.symbol, received_at, as_of, quote: { symbol: query.symbol, bid, ask,
      bid_size: raw.bid_size === undefined ? null : decimal(raw.bid_size), ask_size: raw.ask_size === undefined ? null : decimal(raw.ask_size),
      quote_at, available_at: received_at, source_available_at, quote_age_ms, age_status: quote_at === null ? 'timestamp_unavailable' : 'known' },
    raw_response_sha256: checksum(response), execution_eligible: false });
}
