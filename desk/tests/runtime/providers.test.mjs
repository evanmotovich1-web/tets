import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { isAbsolute } from 'node:path';
import { pathToFileURL } from 'node:url';
import { createHash } from 'node:crypto';
import canonicalize from 'canonicalize';
import { createRecordedProvider, createRobinhoodProvider, createLiveQuoteAdapter, ROBINHOOD_MCP_URL, READ_TOOLS } from '../../runtime/providers/index.mjs';

const original = JSON.parse(await readFile(new URL('./fixtures/providers/robinhood-shaped.json', import.meta.url), 'utf8'));
const recording = () => structuredClone(original);
const now = () => original.received_at;
const query = () => ({ symbol: 'SPY', timeframe: '1m', start: '2024-01-08T14:30:00.000Z', end_exclusive: '2024-01-08T14:34:00.000Z' });
const quote = data => data.quotes.structuredContent.results[0];
const rows = data => data.bars.structuredContent.historicals;
const hash = value => createHash('sha256').update(canonicalize(value)).digest('hex');
function mockClient(data = recording()) {
  const calls = [];
  return { calls, data,
    async listTools(_params, options) { assert.ok(options.signal instanceof AbortSignal); calls.push('tools/list'); return { tools: data.tools }; },
    async callTool(params, resultSchema, options) {
      assert.equal(resultSchema, undefined); assert.ok(options.signal instanceof AbortSignal);
      assert.ok(READ_TOOLS.includes(params.name)); calls.push(structuredClone(params));
      return params.name === 'get_equity_historicals' ? data.bars : data.quotes;
    }
  };
}
const robinhood = (client = mockClient(), extras = {}) => createRobinhoodProvider({ client, serverUrl: ROBINHOOD_MCP_URL, clock: now, ...extras });
function assertChecksum(snapshot) {
  const { snapshot_sha256, ...body } = snapshot;
  assert.equal(snapshot_sha256, hash(body));
}

test('recorded provider returns compatible bars, availability and JSONL checksums', async () => {
  const provider = createRecordedProvider({ recording: recording(), clock: now });
  const first = await provider.getBars(query()), second = await provider.getBars(query());
  assert.deepEqual(first, second); assertChecksum(first);
  assert.equal(first.rows.length, 3); assert.equal(first.rows[0].close, '101');
  assert.equal(first.rows[0].available_at, '2024-01-08T14:31:00.000Z');
  assert.equal(first.rows[2].available_at, original.received_at);
  assert.equal(first.availability_basis, 'mixed'); assert.equal(first.classification, 'synthetic');
  assert.equal(first.historical_eligible, false);
  const jsonl = first.rows.map(row => canonicalize(row) + '\n').join('');
  assert.equal(first.data_sha256, createHash('sha256').update(jsonl).digest('hex'));
  assert.throws(() => first.rows.push({}), TypeError);
});

test('Robinhood provider uses discovered schemas and calls only the two read tools', async () => {
  const client = mockClient(), provider = robinhood(client);
  const bars = await provider.getBars(query()), q = await provider.getQuote({ symbol: 'SPY' });
  assert.deepEqual(client.calls, ['tools/list',
    { name: 'get_equity_historicals', arguments: { symbols: ['SPY'], interval: 'minute', start: query().start, end: query().end_exclusive, bounds: 'regular' } },
    { name: 'get_equity_quotes', arguments: { symbols: ['SPY'] } }]);
  assert.equal(provider.endpoint, ROBINHOOD_MCP_URL); assert.equal(provider.readOnly, true);
  assert.equal(provider.callTool, undefined); assert.equal(provider.placeOrder, undefined);
  assert.equal(bars.classification, 'unqualified_market_data');
  assert.equal(q.quote.quote_age_ms, 500); assertChecksum(bars); assertChecksum(q);
});

test('SDK client owns OAuth without exposing tokens to provider properties or output', async () => {
  const client = mockClient();
  assert.throws(() => createRobinhoodProvider({ client }), { code: 'untrusted_mcp_endpoint' });
  Object.defineProperty(client, 'accessToken', { get() { assert.fail('provider must never read tokens'); } });
  Object.defineProperty(client, 'oauthSession', { get() { assert.fail('provider must never inspect session secrets'); } });
  const provider = robinhood(client);
  const result = await provider.getQuote({ symbol: 'SPY' });
  assert.equal(JSON.stringify(provider).includes('oauth'), false);
  assert.equal(result.quote.bid, '103');
});

test('missing client returns human OAuth requirement without starting login', async () => {
  const provider = createRobinhoodProvider();
  await assert.rejects(provider.getQuote({ symbol: 'SPY' }), { code: 'robinhood_oauth_required' });
});

test('endpoint override, order arguments and out-of-scope symbols reject before transport', async () => {
  const client = mockClient();
  assert.throws(() => robinhood(client, { serverUrl: 'https://example.invalid/mcp' }), { code: 'untrusted_mcp_endpoint' });
  const provider = robinhood(client);
  await assert.rejects(provider.getQuote({ symbol: 'SPY', tool: 'place_equity_order' }), { code: 'invalid_provider_query' });
  await assert.rejects(provider.getBars({ ...query(), symbol: 'BTC-USD' }), { code: 'symbol_out_of_scope' });
  assert.deepEqual(client.calls, []);
});

test('changed tool schemas and write annotations fail closed', async () => {
  for (const mutate of [
    data => { data.tools[0].inputSchema.required.push('account_number'); },
    data => { data.tools[1].annotations.readOnlyHint = false; },
    data => { data.tools = data.tools.filter(tool => tool.name !== 'get_equity_historicals'); }
  ]) {
    const data = recording(); mutate(data); const client = mockClient(data);
    await assert.rejects(robinhood(client).getBars(query()), error => ['read_tool_unavailable', 'unsupported_tool_schema'].includes(error.code));
    assert.deepEqual(client.calls, ['tools/list']);
  }
});

test('native span schemas are mapped using their enum and results filtered to requested window', async () => {
  const data = recording(), schema = data.tools[0].inputSchema;
  delete schema.properties.start; delete schema.properties.end;
  schema.properties.span = { enum: ['day', 'week'] };
  schema.required = ['symbols', 'interval', 'bounds', 'span'];
  const client = mockClient(data);
  const result = await robinhood(client).getBars({ ...query(), end_exclusive: '2024-01-08T14:32:00.000Z' });
  assert.equal(client.calls[1].arguments.span, 'day');
  assert.equal(result.counts.raw_rows, 3); assert.equal(result.counts.returned_rows, 1);
  assert.equal(result.counts.excluded.outside_window, 2);
});

test('structured and text MCP result envelopes normalize identically', async () => {
  const data = recording();
  const direct = await robinhood(mockClient(data)).getBars(query());
  data.bars = { content: [{ type: 'text', text: JSON.stringify(data.bars.structuredContent) }] };
  const text = await robinhood(mockClient(data)).getBars(query());
  assert.deepEqual(direct.rows, text.rows); assert.equal(direct.data_sha256, text.data_sha256);
  assert.notEqual(direct.raw_response_sha256, text.raw_response_sha256);
});

test('batch historical symbol containers are checked before normalization', async () => {
  const data = recording(); data.bars.structuredContent = { results: [data.bars.structuredContent] };
  assert.equal((await robinhood(mockClient(data)).getBars(query())).rows.length, 3);
  data.bars.structuredContent.results[0].symbol = 'MSFT';
  await assert.rejects(robinhood(mockClient(data)).getBars(query()), { code: 'unsupported_provider_response' });
});

test('missing historical availability stays fetch time, never candle close', async () => {
  const data = recording(); for (const row of rows(data)) delete row.available_at;
  const result = await robinhood(mockClient(data)).getBars(query());
  assert.equal(result.availability_basis, 'observed_at_fetch');
  assert.ok(result.rows.every(row => row.available_at === original.received_at));
  assert.equal(result.point_in_time_verified, false); assert.equal(result.historical_eligible, false);
});

test('future availability, invalid OHLC, duplicate and interpolated bars reject', async () => {
  for (const [mutate, code] of [
    [data => { rows(data)[0].available_at = '2024-01-09T00:00:00Z'; }, 'future_data'],
    [data => { rows(data)[0].low_price = '105'; }, 'invalid_ohlc'],
    [data => { rows(data)[1] = structuredClone(rows(data)[0]); }, 'duplicate_or_unordered_bars'],
    [data => { rows(data)[0].interpolated = true; }, 'interpolated_bar'],
    [data => { rows(data)[0].open_price = 'NaN'; }, 'invalid_market_decimal']
  ]) {
    const data = recording(); mutate(data);
    await assert.rejects(robinhood(mockClient(data)).getBars(query()), { code });
  }
});

test('incomplete and extended-hours rows have explicit exclusion counts', async () => {
  const data = recording();
  rows(data)[1].session = 'post';
  rows(data)[2].begins_at = '2024-01-08T14:34:00Z';
  rows(data)[2].ends_at = '2024-01-08T14:35:00Z';
  const result = await robinhood(mockClient(data)).getBars({ ...query(), end_exclusive: '2024-01-08T14:36:00.000Z' });
  assert.deepEqual(result.counts, { raw_rows: 3, returned_rows: 1, excluded: { outside_window: 0, incomplete: 1, extended_hours: 1 } });
});

test('bar duration must match the requested timeframe even without an interval label', async () => {
  const data = recording();
  delete data.bars.structuredContent.interval;
  rows(data)[0].ends_at = '2024-01-08T14:32:00Z';
  await assert.rejects(robinhood(mockClient(data)).getBars(query()), { code: 'provider_timeframe_mismatch' });
});

test('missing bar bounds require a trusted host calendar resolver', async () => {
  const data = recording(); for (const row of rows(data)) delete row.ends_at;
  await assert.rejects(robinhood(mockClient(data)).getBars(query()), { code: 'bar_bounds_unavailable' });
  const resolveBarBounds = ({ begins_at }) => ({ bar_open_at: begins_at, bar_close_at: new Date(Date.parse(begins_at) + 60000).toISOString() });
  const result = await robinhood(mockClient(data), { resolveBarBounds }).getBars(query());
  assert.equal(result.rows.length, 3);
});

test('paginated responses and oversized batches cannot masquerade as complete history', async () => {
  const data = recording(); data.bars.structuredContent.next = 'unread-page';
  await assert.rejects(robinhood(mockClient(data)).getBars(query()), { code: 'provider_pagination_required' });
  delete data.bars.structuredContent.next;
  data.bars.structuredContent.historicals = Array(10001).fill(rows(original)[0]);
  await assert.rejects(robinhood(mockClient(data)).getBars(query()), error => ['provider_row_limit', 'provider_response_too_large'].includes(error.code));
});

test('quote age uses the older side timestamp and includes adapter delay', async () => {
  const provider = robinhood();
  const adapter = createLiveQuoteAdapter({ provider, clock: () => '2024-01-08T14:34:01.000Z' });
  const result = await adapter.sample({ symbol: 'SPY' });
  assert.equal(result.quote_age_ms, 1000); assert.equal(result.age_status, 'fresh');
  assert.equal(result.quote_at, '2024-01-08T14:34:00.000Z');
  const { observation_sha256, ...body } = result; assert.equal(observation_sha256, hash(body));
});

test('live quote availability is its receipt time, with source availability kept separately', async () => {
  const data = recording(); quote(data).available_at = '2024-01-08T14:34:00.200Z';
  const result = await robinhood(mockClient(data)).getQuote({ symbol: 'SPY' });
  assert.equal(result.quote.available_at, original.received_at);
  assert.equal(result.quote.source_available_at, '2024-01-08T14:34:00.200Z');
});

test('stale quotes and unknown timestamps stay explicit and execution-ineligible', async () => {
  const data = recording();
  const freshProvider = robinhood(mockClient(data));
  const stale = await createLiveQuoteAdapter({ provider: freshProvider, clock: () => '2024-01-08T14:34:03.000Z' }).sample({ symbol: 'SPY' });
  assert.equal(stale.stale, true); assert.equal(stale.quote_age_ms, 3000); assert.equal(stale.age_status, 'stale');
  delete quote(data).bid_price_updated_at; delete quote(data).ask_price_updated_at;
  // A recent trade is deliberately not accepted as the bid/ask timestamp.
  const unknown = await createLiveQuoteAdapter({ provider: robinhood(mockClient(data)), clock: now }).sample({ symbol: 'SPY' });
  assert.equal(unknown.quote_age_ms, null); assert.equal(unknown.age_status, 'timestamp_unavailable');
  assert.equal(unknown.execution_eligible, false); assert.equal(unknown.stale, true);
});

test('each future quote side, crossed prices and mismatched symbols reject', async () => {
  for (const [mutate, code] of [
    [data => { quote(data).ask_price_updated_at = '2024-01-09T00:00:00Z'; }, 'future_data'],
    [data => { quote(data).bid_price_updated_at = '2024-01-09T00:00:00Z'; }, 'future_data'],
    [data => { quote(data).available_at = '2024-01-09T00:00:00Z'; }, 'future_data'],
    [data => { quote(data).ask_price = '100'; }, 'crossed_quote'],
    [data => { quote(data).symbol = 'MSFT'; }, 'provider_symbol_mismatch']
  ]) {
    const data = recording(); mutate(data);
    await assert.rejects(robinhood(mockClient(data)).getQuote({ symbol: 'SPY' }), { code });
  }
});

test('sub-millisecond availability rounds forward and quote age rounds conservatively', async () => {
  const data = recording();
  rows(data)[0].available_at = '2024-01-08T14:31:00.000999Z';
  quote(data).quote_at = '2024-01-08T14:34:00.000999Z';
  const provider = robinhood(mockClient(data));
  assert.equal((await provider.getBars(query())).rows[0].available_at, '2024-01-08T14:31:00.001Z');
  assert.equal((await provider.getQuote({ symbol: 'SPY' })).quote.quote_age_ms, 500);
});

test('malformed timestamps and non-JSON tool content reject without echoing payloads', async () => {
  const data = recording(); rows(data)[0].begins_at = '2024-02-30T14:30:00Z';
  await assert.rejects(robinhood(mockClient(data)).getBars(query()), { code: 'invalid_timestamp' });
  data.quotes = { content: [{ type: 'text', text: 'Bearer TEST_ONLY_SENSITIVE_SENTINEL' }] };
  await assert.rejects(robinhood(mockClient(data)).getQuote({ symbol: 'SPY' }), error => error.code === 'unsupported_provider_response' && !String(error).includes('SENTINEL'));
});

test('transport errors and MCP tool errors expose stable codes only', async () => {
  const client = mockClient(); client.callTool = async () => { throw new Error('Bearer TEST_ONLY_SENSITIVE_SENTINEL'); };
  await assert.rejects(robinhood(client).getQuote({ symbol: 'SPY' }), error => error.code === 'provider_unavailable' && !String(error).includes('SENTINEL') && error.cause === undefined);
  client.callTool = async () => { throw Object.assign(new Error('TEST_ONLY_SENSITIVE_SENTINEL'), { status: 401 }); };
  await assert.rejects(robinhood(client).getQuote({ symbol: 'SPY' }), { code: 'robinhood_oauth_required' });
  client.callTool = async () => ({ isError: true, content: [{ type: 'text', text: 'TEST_ONLY_SENSITIVE_SENTINEL' }] });
  await assert.rejects(robinhood(client).getQuote({ symbol: 'SPY' }), { code: 'provider_tool_error' });
});

test('bounded reads time out and propagate cancellation without transport retries', async () => {
  const client = mockClient(); let calls = 0;
  client.callTool = async (_params, _schema, options) => {
    calls++; return new Promise((_resolve, reject) => options.signal.addEventListener('abort', () => reject(new Error('cancelled')), { once: true }));
  };
  await assert.rejects(robinhood(client, { timeoutMs: 20 }).getQuote({ symbol: 'SPY' }), { code: 'provider_timeout' });
  assert.equal(calls, 1);
  const controller = new AbortController(); controller.abort('TEST_ONLY_SENSITIVE_SENTINEL');
  await assert.rejects(robinhood(client).getQuote({ symbol: 'SPY', signal: controller.signal }), { code: 'provider_aborted' });
  assert.equal(calls, 1);
});

test('quote adapter verifies snapshot checksums and rejects clock regression', async () => {
  const source = await robinhood().getQuote({ symbol: 'SPY' });
  const corrupted = { ...source, quote: { ...source.quote, bid: '999' } };
  await assert.rejects(createLiveQuoteAdapter({ provider: { getQuote: async () => corrupted }, clock: now }).sample({ symbol: 'SPY' }), { code: 'provider_checksum_mismatch' });
  await assert.rejects(createLiveQuoteAdapter({ provider: { getQuote: async () => source }, clock: () => '2024-01-08T14:34:00.000Z' }).sample({ symbol: 'SPY' }), { code: 'clock_regression' });
});

test('recordings are copied and cannot be changed by callers after construction', async () => {
  const data = recording(); const provider = createRecordedProvider({ recording: data, clock: now });
  rows(data)[0].close_price = '900';
  assert.equal((await provider.getBars(query())).rows[0].close, '101');
  const snapshot = await provider.getQuote({ symbol: 'SPY' }); assertChecksum(snapshot);
});

test('LIVE Robinhood read-only provider requires a human-authenticated MCP session', { timeout: 45000 }, async t => {
  // Trusted host module contract: openRobinhoodSession({ interactive:false,
  // endpoint, allowTools }) -> { client, serverUrl, authenticated, close,
  // resolveBarBounds? }. It must reuse the MCP client's existing OAuth session
  // and never start login. The module path is host configuration, never a spec.
  const modulePath = process.env.TRADING_DESK_MCP_CLIENT_MODULE;
  if (!modulePath) { t.skip('Pending Evan completing Robinhood agent-account OAuth. No authenticated MCP client is attached; no live calls made.'); return; }
  assert.ok(isAbsolute(modulePath), 'The host MCP client module must be an absolute local path');
  let session;
  try {
    const host = await import(pathToFileURL(modulePath).href);
    session = await host.openRobinhoodSession({ interactive: false, endpoint: ROBINHOOD_MCP_URL, allowTools: READ_TOOLS });
    if (!session?.authenticated) { t.skip('Pending human Robinhood agent-account OAuth in the MCP client. No login or live tool call attempted.'); return; }
    const provider = createRobinhoodProvider({ client: session.client, serverUrl: session.serverUrl, resolveBarBounds: session.resolveBarBounds });
    const result = await provider.getQuote({ symbol: 'SPY' }); assertChecksum(result);
    assert.equal(result.provider, 'robinhood_mcp');
    const end = new Date(), start = new Date(end.getTime() - 86400000);
    const history = await provider.getBars({ symbol: 'SPY', timeframe: '5m', start: start.toISOString(), end_exclusive: end.toISOString() });
    assertChecksum(history); assert.equal(history.historical_eligible, false);
  } catch (error) {
    if (error?.code === 'robinhood_oauth_required') { t.skip('Pending human Robinhood OAuth or session renewal in the MCP client.'); return; }
    // Never surface host/SDK error bodies or their credential-bearing causes.
    assert.fail('Live Robinhood read-only provider failed. Check the MCP client session and discovered tool/response compatibility locally.');
  } finally { if (session?.close) { try { await session.close(); } catch { assert.fail('MCP client cleanup failed'); } } }
});
