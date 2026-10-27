import { barQuery, quoteQuery, check, clockTime, freeze, normalizeBars, normalizeQuote, timestamp } from './contracts.mjs';

export function createRecordedProvider({ recording, clock = () => new Date(), resolveBarBounds } = {}) {
  // Only host-provided recordings. Provider calls accept no filesystem paths.
  const data = JSON.parse(JSON.stringify(recording));
  check(data?.classification === 'synthetic' && data.bars && data.quotes, 'invalid_recording');
  const received_at = timestamp(data.received_at, true);
  return freeze({ id: 'recorded_fixture', readOnly: true,
    async getBars(input) {
      check(!input?.signal?.aborted, 'provider_aborted');
      return normalizeBars(data.bars, barQuery(input), { provider: 'recorded_fixture', received_at, resolveBarBounds, synthetic: true });
    },
    async getQuote(input) {
      check(!input?.signal?.aborted, 'provider_aborted');
      return normalizeQuote(data.quotes, quoteQuery(input), { provider: 'recorded_fixture', received_at, as_of: clockTime(clock), synthetic: true });
    }
  });
}
