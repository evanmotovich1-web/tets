import { check, checksum, clockTime, freeze } from './contracts.mjs';

export function createLiveQuoteAdapter({ provider, clock = () => new Date(), maxAgeMs = 2000 } = {}) {
  check(provider && typeof provider.getQuote === 'function', 'provider_required');
  check(Number.isSafeInteger(maxAgeMs) && maxAgeMs >= 0 && maxAgeMs <= 60000, 'invalid_quote_age_limit');
  return freeze({ async sample(input) {
    const source = await provider.getQuote(input);
    const { snapshot_sha256, ...body } = source;
    check(snapshot_sha256 === checksum(body), 'provider_checksum_mismatch');
    const observed_at = clockTime(clock);
    check(observed_at >= source.received_at && observed_at >= source.as_of, 'clock_regression');
    const quote_age_ms = source.quote.quote_at === null ? null : Date.parse(observed_at) - Date.parse(source.quote.quote_at);
    const record = { schema_version: 1, kind: 'quote_observation', symbol: source.symbol, provider: source.provider,
      observed_at, available_at: source.quote.available_at, quote_at: source.quote.quote_at,
      bid: source.quote.bid, ask: source.quote.ask, bid_size: source.quote.bid_size, ask_size: source.quote.ask_size,
      quote_age_ms, age_status: quote_age_ms === null ? 'timestamp_unavailable' : quote_age_ms > maxAgeMs ? 'stale' : 'fresh',
      stale: quote_age_ms === null || quote_age_ms > maxAgeMs, source_snapshot_sha256: snapshot_sha256,
      execution_eligible: false };
    return freeze({ ...record, observation_sha256: checksum(record) });
  } });
}
