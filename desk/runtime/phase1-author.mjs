// One CLI coding-model author. Local Ollama is not used: 8GB RAM cannot load qwen3.5:2b
// while nomic-embed is resident. Fixture adapters are refused.
import { mkdir, writeFile } from 'node:fs/promises';
import { createCliAuthorAdapter } from './models.mjs';
import { roleTools, dispatch } from './roles.mjs';
import { digest } from './contracts.mjs';
import { workerBridge } from './worker.mjs';

const proof = process.env.PHASE1_PROOF_DIR;
if (!proof) throw new Error('PHASE1_PROOF_DIR required');
await mkdir(proof, { recursive: true, mode: 0o700 });
const messages = [
  { role: 'system', content: 'You are a research strategy author. Submit one original declarative long-only SPY daily sma_cross_long via submit_strategy. Research only. No commands, files, or profitability claims.' },
  { role: 'user', content: 'Choose your own integers: fast_window in 1..5 and slow_window in (fast_window+1)..10. Do not copy a template. schema_version 2, family sma_cross_long, symbol SPY, timeframe 1d, position {sizing:fixed_quantity, quantity:"1"}, execution {timing:next_bar_open, order_type:market, pending_on_end:cancel, terminal_position:mark_to_market}. Rationale must state the hypothesis in one sentence. Return exactly one submit_strategy tool call. Do not include hashes.' }
];
const env = {
  TRADING_DESK_ALLOW_CLI_AUTHOR: '1',
  TRADING_DESK_MODEL_ID: process.env.TRADING_DESK_MODEL_ID || 'gpt-6.1-sol'
};
const started = Date.now();
const prompt_sha256 = digest(messages);
await writeFile(`${proof}/author-prompt.json`, JSON.stringify({ messages, prompt_sha256, model: env.TRADING_DESK_MODEL_ID }, null, 2));
let bridge;
try {
  const model = createCliAuthorAdapter('codex', env, { timeoutMs: 180000 });
  const generation = await model.generate({
    role: 'author_hermes',
    messages,
    tools: roleTools('author_hermes').filter(t => t.name === 'submit_strategy'),
    budget: { max_output_tokens: 1024 }
  });
  await writeFile(`${proof}/generation.json`, JSON.stringify(generation, null, 2));
  if (generation.provenance?.agent_written !== true || generation.provenance?.transport !== 'cli') {
    throw new Error('not_agent_written');
  }
  let submitted;
  for (const call of generation.tool_calls) {
    await dispatch('author_hermes', call, {
      submit_strategy: args => {
        if (submitted) throw new Error('duplicate');
        submitted = args.strategy;
      }
    });
  }
  bridge = workerBridge({ stateDir: `${proof}/author-worker`, operationMs: 5000 });
  const checked = await bridge.call({ op: 'validate_strategy', strategy: submitted });
  if (!checked.ok) throw new Error(checked.error?.code || 'validate_failed');
  await writeFile(`${proof}/authored-spec.json`, JSON.stringify(checked.strategy, null, 2));
  const status = {
    available: true,
    agent_written: true,
    elapsed_ms: Date.now() - started,
    provenance: generation.provenance,
    prompt_sha256,
    strategy_sha256: checked.strategy.strategy_sha256,
    provider: generation.provider,
    model_id: generation.model_id
  };
  await writeFile(`${proof}/author-status.json`, JSON.stringify(status, null, 2));
  console.log(JSON.stringify({ available: true, strategy_sha256: checked.strategy.strategy_sha256, provenance: generation.provenance }));
} catch (error) {
  const status = {
    available: false,
    agent_written: false,
    error: error.code || error.message,
    elapsed_ms: Date.now() - started
  };
  await writeFile(`${proof}/author-status.json`, JSON.stringify(status, null, 2));
  console.log(JSON.stringify({ available: false, error: status.error }));
  process.exitCode = 1;
} finally {
  await bridge?.close();
}
