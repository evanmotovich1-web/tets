import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { DeskError, exact, requireThat, parseStrictJSON, digest } from './contracts.mjs';
import { roles, beforeTool } from './roles.mjs';

const HASH = /^[0-9a-f]{64}$/;
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');
// Provenance says who actually wrote a response. Only a real model transport sets
// agent_written true; fixture adapters always set it false.
function validateProvenance(p) {
  exact(p, ['transport', 'agent_written', 'model_version', 'raw_response_sha256']);
  requireThat(['http', 'cli', 'fixture'].includes(p.transport) && typeof p.agent_written === 'boolean' &&
    (p.transport !== 'fixture' || p.agent_written === false) && HASH.test(p.raw_response_sha256) &&
    (p.model_version === null || (typeof p.model_version === 'string' && p.model_version.length <= 256)), 'malformed_model_output');
}
export function validateGeneration(value, role) {
  exact(value, Object.hasOwn(value ?? {}, 'provenance') ? ['tool_calls', 'usage', 'provider', 'model_id', 'provenance'] : ['tool_calls', 'usage', 'provider', 'model_id']);
  if (value.provenance !== undefined) validateProvenance(value.provenance);
  requireThat(typeof value.provider === 'string' && value.provider.length <= 128 && typeof value.model_id === 'string' && value.model_id.length <= 128, 'malformed_model_output');
  requireThat(Array.isArray(value.tool_calls) && value.tool_calls.length >= 1 && value.tool_calls.length <= 4, 'malformed_model_output');
  for (const call of value.tool_calls) {
    exact(call, ['name', 'arguments']);
    requireThat(!beforeTool(role, call), 'forbidden_tool');
    requireThat(call.arguments && typeof call.arguments === 'object' && !Array.isArray(call.arguments), 'malformed_model_output');
  }
  exact(value.usage, ['input_tokens', 'output_tokens']);
  requireThat(Object.values(value.usage).every(n => Number.isSafeInteger(n) && n >= 0 && n <= 1000000), 'unknown_provider_usage');
  requireThat(Buffer.byteLength(JSON.stringify(value)) <= 65536, 'model_output_too_large');
  return value;
}
export function fixtureAdapter() {
  return {
    provider: 'fixture', model_id: 'fixture-sma-v1',
    async generate({ role, messages, signal }) {
      signal?.throwIfAborted();
      const c = JSON.parse(messages.at(-1).content);
      const calls = {
        desk: { name: 'enqueue_research', arguments: { campaign_id: c.campaign_id, event_id: c.event_id } },
        scout: { name: 'submit_hypothesis', arguments: { dataset_id: c.dataset_ids[0], text: 'Synthetic SMA timing and cost accounting fixture. This is not evidence of market edge.' } },
        author: { name: 'submit_strategy', arguments: { strategy: { symbol: 'SYNTH', parameters: { fast_window: 1, slow_window: 2 }, position: { quantity: '1' } } } },
        runner: { name: 'run_backtest', arguments: { trial_id: c.trial_id } },
        referee: { name: 'record_review', arguments: { trial_id: c.trial_id, verdict: 'research_valid', reason_codes: ['synthetic_fixture', 'research_only'], summary: 'Verified synthetic accounting receipt. No promotion or profitability conclusion.' } }
      };
      requireThat(Boolean(calls[role]), 'invalid_role');
      return validateGeneration({ tool_calls: [calls[role]], usage: { input_tokens: 1, output_tokens: 1 }, provider: 'fixture', model_id: 'fixture-sma-v1' }, role);
    }
  };
}
export function createModelAdapter(provider = 'fixture', env = process.env, fetcher = fetch) {
  if (provider === 'fixture') return fixtureAdapter();
  requireThat(provider === 'localhost' || provider === 'deepseek', 'unsupported_provider');
  const model = env.TRADING_DESK_MODEL_ID;
  requireThat(typeof model === 'string' && model.length > 0 && model.length <= 128, 'model_id_required');
  let base, key;
  if (provider === 'localhost') {
    requireThat(Boolean(env.TRADING_DESK_LOCAL_BASE_URL), 'local_endpoint_required');
    base = new URL(env.TRADING_DESK_LOCAL_BASE_URL);
    requireThat(['http:', 'https:'].includes(base.protocol) && ['localhost', '127.0.0.1', '[::1]'].includes(base.hostname), 'non_loopback_endpoint');
    key = env.TRADING_DESK_LOCAL_API_KEY;
  } else {
    requireThat(env.TRADING_DESK_ALLOW_PAID_INFERENCE === '1', 'paid_inference_disabled');
    requireThat(Boolean(env.DEEPSEEK_API_KEY), 'deepseek_key_required');
    base = new URL(env.DEEPSEEK_BASE_URL || 'https://api.deepseek.com');
    requireThat(base.protocol === 'https:' && base.hostname === 'api.deepseek.com' && !base.port && ['', '/', '/v1', '/v1/'].includes(base.pathname), 'not_direct_deepseek_endpoint');
    key = env.DEEPSEEK_API_KEY;
  }
  requireThat(!base.username && !base.password && !base.search && !base.hash, 'invalid_endpoint');
  const url = new URL(base.href.replace(/\/$/, '') + '/chat/completions');
  // Local models can be slow to load; allow up to 5 minutes when the operator asks for it.
  const timeoutMs = env.TRADING_DESK_MODEL_TIMEOUT_MS === undefined ? 30000 : Number(env.TRADING_DESK_MODEL_TIMEOUT_MS);
  requireThat(Number.isSafeInteger(timeoutMs) && timeoutMs >= 1000 && timeoutMs <= 300000, 'invalid_model_timeout');
  return {
    provider, model_id: model, route_sha256: digest({ provider, endpoint: url.href, model_id: model }),
    async generate({ role, messages, tools, signal, budget }) {
      requireThat(Boolean(roles[role]) && budget.max_output_tokens > 0, 'invalid_model_request');
      let response;
      try {
        response = await fetcher(url, { method: 'POST', redirect: 'error', signal: AbortSignal.any([signal ?? new AbortController().signal, AbortSignal.timeout(timeoutMs)]), headers: { 'Content-Type': 'application/json', ...(key ? { Authorization: `Bearer ${key}` } : {}) }, body: JSON.stringify({ model, temperature: 0, max_tokens: budget.max_output_tokens, messages, tools: tools.map(tool => ({ type: 'function', function: tool })), tool_choice: 'required', stream: false }) });
      } catch (error) {
        // A local model server that is not running is an unavailable author, never a silent fallback.
        if (provider === 'localhost' && ['ECONNREFUSED', 'ENOTFOUND', 'EHOSTUNREACH'].includes(error?.cause?.code ?? error?.code)) throw new DeskError('author_unavailable');
        throw new DeskError('provider_result_unknown', { uncertain: true });
      }
      if (provider === 'localhost' && response.status === 404) throw new DeskError('author_unavailable'); // model not pulled
      if (!response.ok) {
        // Never persist response bodies, endpoints, keys, or upstream error text.
        throw new DeskError(response.status === 429 ? 'provider_rate_limited' : 'provider_unavailable', { transient: response.status === 429 || response.status >= 500 });
      }
      const reader = response.body.getReader(); let size = 0; const chunks = [];
      try {
        while (true) { const { done, value } = await reader.read(); if (done) break; size += value.length; requireThat(size <= 65536, 'model_output_too_large'); chunks.push(value); }
      } finally { await reader.cancel().catch(() => {}); }
      const raw = Buffer.concat(chunks);
      let data;
      try { data = parseStrictJSON(raw.toString('utf8')); } catch { throw new DeskError('malformed_model_output'); }
      const calls = data.choices?.[0]?.message?.tool_calls;
      requireThat(Array.isArray(calls), 'malformed_model_output');
      let parsed;
      try { parsed = calls.map(call => ({ name: call.function.name, arguments: parseStrictJSON(call.function.arguments) })); } catch { throw new DeskError('malformed_model_output'); }
      const reported = [data.model, data.system_fingerprint].filter(v => typeof v === 'string' && v.length).join(' ');
      return validateGeneration({ tool_calls: parsed, usage: { input_tokens: data.usage?.prompt_tokens, output_tokens: data.usage?.completion_tokens }, provider, model_id: model,
        provenance: { transport: 'http', agent_written: true, model_version: reported ? reported.slice(0, 256) : null, raw_response_sha256: sha256(raw) } }, role);
    }
  };
}

// Command-line authors (Codex or Claude). They run on Evan's subscription, so they need
// TRADING_DESK_ALLOW_CLI_AUTHOR=1. No shell, read-only sandbox, no tools, prompt on stdin.
const CLI = {
  codex: { bin: 'TRADING_DESK_CODEX_BIN', fallback: 'codex',
    args: model => ['exec', '--sandbox', 'read-only', '--skip-git-repo-check', '--ephemeral', '-m', model, '-'] },
  claude: { bin: 'TRADING_DESK_CLAUDE_BIN', fallback: 'claude',
    args: model => ['-p', '--output-format', 'json', '--model', model, '--tools', '', '--strict-mcp-config', '--no-session-persistence'] }
};
const CLI_OUTPUT_LIMIT = 1 << 20;

export function runCli(spawnFn, bin, args, input, timeoutMs) {
  return new Promise((resolve, reject) => {
    let child;
    try { child = spawnFn(bin, args, { shell: false, stdio: ['pipe', 'pipe', 'pipe'] }); }
    catch (error) { return reject(new DeskError(error?.code === 'ENOENT' ? 'author_unavailable' : 'provider_unavailable')); }
    const out = []; let size = 0, failure = null;
    const timer = setTimeout(() => { failure ??= new DeskError('provider_result_unknown', { uncertain: true }); child.kill('SIGKILL'); }, timeoutMs);
    child.stdout.on('data', chunk => { size += chunk.length; if (size > CLI_OUTPUT_LIMIT) { failure ??= new DeskError('model_output_too_large'); child.kill('SIGKILL'); } else out.push(chunk); });
    child.stderr?.on('data', () => {}); // never persisted: may echo prompts or account details
    child.stdin.on('error', () => {});
    let settled = false;
    const settle = fn => { if (!settled) { settled = true; clearTimeout(timer); fn(); } };
    // A spawn failure (e.g. ENOENT) may emit 'error' without a later 'close'; settle on either.
    child.once('error', error => settle(() => reject(failure ?? new DeskError(error?.code === 'ENOENT' ? 'author_unavailable' : 'provider_unavailable'))));
    child.once('close', code => settle(() => {
      if (failure) return reject(failure);
      if (code !== 0) return reject(new DeskError('provider_unavailable'));
      resolve(Buffer.concat(out));
    }));
    child.stdin.end(input);
  });
}

function lastJsonObject(text) {
  text = text.slice(-65536); // bounded search; a valid answer is far smaller
  for (let end = text.lastIndexOf('}'); end >= 0; end = text.lastIndexOf('}', end - 1)) {
    for (let start = text.lastIndexOf('{', end); start >= 0; start = text.lastIndexOf('{', start - 1)) {
      try { const value = parseStrictJSON(text.slice(start, end + 1)); if (value && Array.isArray(value.tool_calls)) return value; } catch {}
    }
  }
  throw new DeskError('malformed_model_output');
}

export function createCliAuthorAdapter(cli, env = process.env, { spawnFn = spawn, timeoutMs = 180000 } = {}) {
  requireThat(Object.hasOwn(CLI, cli), 'unsupported_provider');
  requireThat(env.TRADING_DESK_ALLOW_CLI_AUTHOR === '1', 'cli_author_disabled');
  const model = env.TRADING_DESK_MODEL_ID;
  requireThat(typeof model === 'string' && /^[A-Za-z0-9][A-Za-z0-9._:\/-]{0,127}$/.test(model), 'model_id_required');
  const bin = env[CLI[cli].bin] || CLI[cli].fallback;
  const provider = `cli_${cli}`;
  let version;
  return {
    provider, model_id: model,
    async generate({ role, messages, tools, budget }) {
      requireThat(Boolean(roles[role]) && budget.max_output_tokens > 0, 'invalid_model_request');
      version ??= (await runCli(spawnFn, bin, ['--version'], '', 15000)).toString('utf8').trim().split('\n')[0].slice(0, 120);
      const prompt = [
        ...messages.map(m => `${m.role.toUpperCase()}:\n${m.content}`),
        `TOOLS (JSON Schema): ${JSON.stringify(tools)}`,
        'Reply with exactly one JSON object and nothing else: {"tool_calls":[{"name":"<tool>","arguments":{...}}]}. Do not run commands or edit files.'
      ].join('\n\n');
      const raw = await runCli(spawnFn, bin, CLI[cli].args(model), prompt, timeoutMs);
      let text = raw.toString('utf8'), usage = { input_tokens: 0, output_tokens: 0 };
      if (cli === 'claude') {
        let envelope;
        try { envelope = parseStrictJSON(text, CLI_OUTPUT_LIMIT); } catch { throw new DeskError('malformed_model_output'); }
        requireThat(envelope.is_error !== true && typeof envelope.result === 'string', 'malformed_model_output');
        text = envelope.result;
        usage = { input_tokens: envelope.usage?.input_tokens ?? 0, output_tokens: envelope.usage?.output_tokens ?? 0 };
      }
      const body = lastJsonObject(text);
      exact(body, ['tool_calls']);
      return validateGeneration({ tool_calls: body.tool_calls, usage, provider, model_id: model,
        provenance: { transport: 'cli', agent_written: true, model_version: `${model} via ${version}`.slice(0, 256), raw_response_sha256: sha256(raw) } }, role);
    }
  };
}
