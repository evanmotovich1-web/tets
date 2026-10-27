import Ajv from 'ajv';
import { ROBINHOOD_MCP_URL, READ_TOOLS, INTERVALS, ProviderError, barQuery, quoteQuery,
  check, clockTime, freeze, normalizeBars, normalizeQuote } from './contracts.mjs';

/** Adapter over an already connected MCP SDK Client. The host owns transport,
 * OAuth, refresh, and shutdown. This module never reads credentials, opens login,
 * connects a transport, writes files, or exposes a general-purpose tool call.
 * SDK methods used: listTools({}, options), callTool(params, undefined, options).
 * Live argument/response compatibility remains unverified until human OAuth.
 */
export function createRobinhoodProvider({ client, serverUrl,
  clock = () => new Date(), timeoutMs = 10000, resolveBarBounds } = {}) {
  check((serverUrl ?? (client === undefined ? ROBINHOOD_MCP_URL : null)) === ROBINHOOD_MCP_URL, 'untrusted_mcp_endpoint');
  check(Number.isSafeInteger(timeoutMs) && timeoutMs >= 1 && timeoutMs <= 15000, 'invalid_provider_timeout');
  check(client === undefined || (typeof client?.listTools === 'function' && typeof client?.callTool === 'function'), 'invalid_mcp_client');
  let tools;
  async function bounded(invoke, externalSignal) {
    check(client !== undefined, 'robinhood_oauth_required');
    check(!externalSignal?.aborted, 'provider_aborted');
    const controller = new AbortController();
    let timer, onAbort;
    try {
      return await Promise.race([
        Promise.resolve().then(() => invoke({ signal: controller.signal, timeout: timeoutMs })),
        new Promise((_resolve, reject) => {
          timer = setTimeout(() => { controller.abort(); reject(new ProviderError('provider_timeout')); }, timeoutMs);
          onAbort = () => { controller.abort(); reject(new ProviderError('provider_aborted')); };
          externalSignal?.addEventListener('abort', onAbort, { once: true });
        })
      ]);
    } catch (error) {
      if (error instanceof ProviderError) throw error;
      if (error?.status === 401 || error?.statusCode === 401) throw new ProviderError('robinhood_oauth_required');
      throw new ProviderError('provider_unavailable');
    } finally { clearTimeout(timer); externalSignal?.removeEventListener('abort', onAbort); }
  }
  async function discover(signal) {
    if (tools) return tools;
    const result = await bounded(options => client.listTools({}, options), signal);
    try {
      check(result && Array.isArray(result.tools) && result.tools.length <= 512 && !result.nextCursor, 'unsupported_tool_catalog');
      const selected = {};
      for (const name of READ_TOOLS) {
        const found = result.tools.filter(tool => tool?.name === name);
        check(found.length === 1 && found[0].annotations?.readOnlyHint !== false, 'read_tool_unavailable');
        const schema = found[0].inputSchema;
        check(schema?.type === 'object' && schema.properties && Buffer.byteLength(JSON.stringify(schema)) <= 65536, 'unsupported_tool_schema');
        const validator = new Ajv({ strict: false, validateFormats: false }).compile(schema);
        selected[name] = { schema, validator };
      }
      tools = selected;
      return tools;
    } catch (error) { throw error instanceof ProviderError ? error : new ProviderError('unsupported_tool_schema'); }
  }
  function argumentsFor(name, tool, query) {
    const props = tool.schema.properties, args = {};
    if (props.symbols) args.symbols = props.symbols.type === 'string' ? query.symbol : [query.symbol];
    else if (props.symbol) args.symbol = query.symbol;
    else throw new ProviderError('unsupported_tool_schema');
    if (name === 'get_equity_historicals') {
      const intervalKey = props.interval ? 'interval' : props.timeframe ? 'timeframe' : null;
      check(intervalKey !== null, 'unsupported_tool_schema');
      const enumValues = props[intervalKey].enum;
      const candidates = intervalKey === 'timeframe' ? [query.timeframe, INTERVALS[query.timeframe]] : [INTERVALS[query.timeframe], query.timeframe];
      const value = candidates.find(value => !enumValues || enumValues.includes(value));
      check(value !== undefined, 'unsupported_timeframe'); args[intervalKey] = value;
      if (props.bounds) args.bounds = 'regular';
      const starts = ['start', 'start_time', 'start_date', 'start_at'].filter(key => props[key]);
      const ends = ['end', 'end_time', 'end_date', 'end_at', 'end_exclusive'].filter(key => props[key]);
      for (const key of starts) args[key] = props[key].format === 'date' ? query.start.slice(0, 10) : query.start;
      for (const key of ends) args[key] = props[key].format === 'date' ? query.end_exclusive.slice(0, 10) : query.end_exclusive;
      if (props.span) {
        const days = Math.max(1, (Date.parse(clockTime(clock)) - Date.parse(query.start)) / 86400000);
        const spans = [['day', 1], ['week', 7], ['month', 28], ['3month', 84], ['year', 365], ['5year', 1825], ['all', Infinity]];
        args.span = spans.find(([value, coverage]) => coverage >= days && (!props.span.enum || props.span.enum.includes(value)))?.[0];
        check(args.span !== undefined, 'unsupported_history_window');
      } else check(starts.length > 0 && ends.length > 0, 'unsupported_history_window');
    }
    check(tool.validator(args), 'unsupported_tool_schema');
    return args;
  }
  async function read(name, query, signal) {
    check(READ_TOOLS.includes(name), 'read_only_provider');
    const catalog = await discover(signal);
    const args = argumentsFor(name, catalog[name], query);
    return bounded(options => client.callTool({ name, arguments: args }, undefined, options), signal);
  }
  return freeze({ id: 'robinhood_mcp', endpoint: ROBINHOOD_MCP_URL, readOnly: true,
    async getBars(input) {
      const query = barQuery(input);
      const response = await read('get_equity_historicals', query, input.signal);
      const received_at = clockTime(clock);
      return normalizeBars(response, query, { provider: 'robinhood_mcp', received_at, resolveBarBounds });
    },
    async getQuote(input) {
      const query = quoteQuery(input);
      const response = await read('get_equity_quotes', query, input.signal);
      const received_at = clockTime(clock);
      return normalizeQuote(response, query, { provider: 'robinhood_mcp', received_at, as_of: received_at });
    }
  });
}
