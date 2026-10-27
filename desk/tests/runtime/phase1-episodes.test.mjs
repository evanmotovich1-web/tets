import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, readFile, writeFile, mkdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { readLedger, toRows, exportEpisodes } from '../../runtime/episodes.mjs';
import { recordPhase1Episodes } from '../../runtime/phase1-episodes.mjs';

async function temp(t) {
  const d = await mkdtemp(join(tmpdir(), 'p1ep-'));
  t.after(() => rm(d, { recursive: true, force: true }));
  return d;
}

const authored = {
  schema_version: 2, kind: 'strategy_spec', family: 'sma_cross_long', symbol: 'SPY', timeframe: '1d',
  parameters: { fast_window: 3, slow_window: 8 },
  position: { sizing: 'fixed_quantity', quantity: '1', max_positions: 1 },
  execution: { timing: 'next_bar_open', order_type: 'market', pending_on_end: 'cancel', terminal_position: 'mark_to_market', limit_offset_bps: '10' },
  exits: { max_hold_bars: null, stop_loss_bps: null, take_profit_bps: null },
  data_requirements: ['ohlcv_pti_v1'],
  rationale: 'test',
  strategy_sha256: 'd'.repeat(64)
};

function verification() {
  return {
    schema_version: 1, kind: 'phase1_proof', agent_written_proven: true, authorship: 'cli_codex',
    research_only: true, promoting: false,
    author_status: {
      available: true, agent_written: true, provider: 'cli_codex', model_id: 'gpt-6.1-sol',
      strategy_sha256: 'd'.repeat(64), prompt_sha256: 'e'.repeat(64),
      provenance: { transport: 'cli', agent_written: true, model_version: 'gpt-6.1-sol via codex-cli 0.164.0', raw_response_sha256: 'f'.repeat(64) }
    },
    data: {
      bars: 61, classification: 'historical_unverified', partition: 'development', point_in_time_verified: false,
      data_sha256: 'a'.repeat(64), manifest_sha256: 'b'.repeat(64), dataset_id: 'maverick:test'
    },
    trials: {
      baseline: { strategy_sha256: 'd'.repeat(64), request_sha256: '1'.repeat(64), receipt_sha256: '2'.repeat(64), fills: 9, independent_match: true, request_binding_match: true },
      window_mutation: { strategy_sha256: 'c'.repeat(64), request_sha256: '3'.repeat(64), receipt_sha256: '4'.repeat(64), fills: 28, independent_match: true, request_binding_match: true }
    }
  };
}

test('phase1 episodes export two resolved decisions, keep mutation host-written, and never leak outcomes into state', async t => {
  const dir = await temp(t);
  const proof = join(dir, 'proof');
  await mkdir(proof, { recursive: true });
  await writeFile(join(proof, 'verification.json'), JSON.stringify(verification()));
  await writeFile(join(proof, 'authored-spec.json'), JSON.stringify(authored));
  await writeFile(join(proof, 'window_mutation-spec.json'), JSON.stringify({ ...authored, parameters: { fast_window: 1, slow_window: 2 }, strategy_sha256: 'c'.repeat(64) }));
  let clock = 0;
  const now = () => new Date(Date.UTC(2026, 9, 25, 18, 0, 0) + clock++ * 1000).toISOString().replace(/\.\d{3}Z$/, '.000Z').replace(/Z$/, '.000Z');
  // ISO from Date already has ms; keep the helper honest:
  const stamps = ['2026-10-25T18:00:00.000Z', '2026-10-25T18:00:01.000Z', '2026-10-25T18:00:02.000Z', '2026-10-25T18:00:03.000Z'];
  let i = 0;
  const summary = await recordPhase1Episodes({ proofDir: proof, stateDir: dir, campaignId: 'phase1-test', now: () => stamps[i++] });
  assert.equal(summary.ok, true);
  assert.equal(summary.qualification, 'research_only');
  assert.equal(summary.promoting, false);
  assert.equal(summary.decisions, 2);
  assert.equal(summary.resolved, 2);
  assert.equal(summary.unresolved, 0);
  const rows = toRows(await readLedger(join(dir, 'episodes', 'phase1-test.jsonl')));
  assert.equal(rows[0].worker.agent_written, true);
  assert.equal(rows[0].worker.provider, 'cli_codex');
  assert.equal(rows[1].worker.agent_written, false);
  assert.equal(rows[0].lineage_group, rows[1].lineage_group);
  assert.equal(rows[0].chosen_action, 'test_candidate');
  assert.equal(rows[1].chosen_action, 'test_candidate');
  assert.ok(!JSON.stringify(rows[0].state).includes('"outcome"'));
  assert.equal(rows[0].outcome.status, 'completed');
  assert.equal(rows[0].outcome.receipts.length, 2);
  const one = await exportEpisodes({ stateDir: dir, campaignId: 'phase1-test', out: join(dir, 'one.jsonl') });
  const two = await exportEpisodes({ stateDir: dir, campaignId: 'phase1-test', out: join(dir, 'two.jsonl') });
  assert.equal(one.export_sha256, two.export_sha256);
  assert.equal(await readFile(join(dir, 'one.jsonl'), 'utf8'), await readFile(join(dir, 'two.jsonl'), 'utf8'));
});

test('phase1 episodes accept a local-model (http) author and refuse fixture authorship', async t => {
  const stamps = ['2026-10-25T19:00:00.000Z', '2026-10-25T19:00:01.000Z', '2026-10-25T19:00:02.000Z', '2026-10-25T19:00:03.000Z'];
  for (const [transport, ok] of [['http', true], ['fixture', false]]) {
    const dir = await temp(t); const proof = join(dir, 'proof'); await mkdir(proof, { recursive: true });
    const v = verification();
    v.author_status.provider = 'localhost'; v.author_status.provenance = { ...v.author_status.provenance, transport, model_version: 'qwen3.5:2b' };
    await writeFile(join(proof, 'verification.json'), JSON.stringify(v));
    await writeFile(join(proof, 'authored-spec.json'), JSON.stringify(authored));
    await writeFile(join(proof, 'window_mutation-spec.json'), JSON.stringify({ ...authored, parameters: { fast_window: 1, slow_window: 2 }, strategy_sha256: 'c'.repeat(64) }));
    let i = 0;
    const run = recordPhase1Episodes({ proofDir: proof, stateDir: dir, campaignId: `p1-${transport}`, now: () => stamps[i++] });
    if (ok) assert.equal((await run).decisions, 2); else await assert.rejects(run, /author_not_agent_written/);
  }
});
