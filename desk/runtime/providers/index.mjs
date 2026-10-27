// Data-provider contract:
//   getBars({ symbol, timeframe, start, end_exclusive, signal? }) -> bar_snapshot
//   getQuote({ symbol, signal? }) -> quote_snapshot
// Snapshot checksums cover normalized data and receipt-time metadata. Raw MCP
// payloads and client sessions never leave the provider. Missing historical
// availability is receipt time, never backdated to the candle close. These
// snapshots do not bypass host-registry admission or establish execution quality.
export { createRecordedProvider } from './recorded.mjs';
export { createRobinhoodProvider } from './robinhood.mjs';
export { createLiveQuoteAdapter } from './quotes.mjs';
export { createWalkForwardBridge } from './walkforward.mjs';
export { ROBINHOOD_MCP_URL, READ_TOOLS, ProviderError } from './contracts.mjs';
