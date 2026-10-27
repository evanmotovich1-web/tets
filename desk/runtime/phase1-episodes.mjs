// Bind the Phase 1 agent-written trial into the episode ledger. Research only.
import { readFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { ruleHash } from './author-fleet.mjs';
import { requireThat } from './contracts.mjs';
import { exportEpisodes, lineageGroup, openLedger } from './episodes.mjs';

async function json(path) {
  return JSON.parse(await readFile(path, 'utf8'));
}

function candidateOf(spec) {
  return {
    family: spec.family,
    timeframe: spec.timeframe ?? '1d',
    symbol: spec.symbol,
    strategy_sha256: spec.strategy_sha256,
    rule_sha256: ruleHash(spec)
  };
}

export async function recordPhase1Episodes({ proofDir, stateDir, campaignId, now = () => new Date().toISOString() }) {
  const proof = resolve(proofDir);
  const summary = await json(join(proof, 'verification.json'));
  requireThat(summary.agent_written_proven === true && summary.research_only === true && summary.promoting === false, 'phase1_not_proven');
  requireThat(summary.author_status?.agent_written === true && ['cli', 'http'].includes(summary.author_status?.provenance?.transport), 'author_not_agent_written');
  const authored = await json(join(proof, 'authored-spec.json'));
  const mutated = await json(join(proof, 'window_mutation-spec.json'));
  requireThat(authored.strategy_sha256 === summary.trials.baseline.strategy_sha256, 'baseline_spec_mismatch');
  requireThat(mutated.strategy_sha256 === summary.trials.window_mutation.strategy_sha256, 'mutation_spec_mismatch');

  const lineage = lineageGroup({ family: authored.family, timeframe: authored.timeframe ?? '1d' });
  const ledger = await openLedger({ stateDir, campaignId, now });
  try {
    const baselineState = {
      stage: 'not_registered',
      attempts_in_lineage: 0,
      prior_errors: [],
      candidate: candidateOf(authored),
      budget: { model_calls_spent: 1, backtests_spent: 0, model_calls_remaining: 49, backtests_remaining: 50 }
    };
    const authoredDecision = await ledger.recordDecision({
      episodeId: `p1:${summary.trials.baseline.strategy_sha256.slice(0, 24)}`,
      lineage,
      state: baselineState,
      offeredActions: ['test_candidate', 'reject', 'abstain'],
      chosenAction: 'test_candidate',
      chooser: { kind: 'rules', id: 'phase1_host', version: '1' }, labelKind: 'retrospective',
      worker: {
        role: 'author_hermes', slot: 'hermes', provider: summary.author_status.provider,
        model_id: summary.author_status.model_id, agent_written: true
      }
    });
    await ledger.recordOutcome({
      decisionId: authoredDecision.decision_id,
      status: 'completed',
      code: 'historical_unverified_hindsight',
      stageReached: 'registered',
      receipts: [summary.trials.baseline.request_sha256, summary.trials.baseline.receipt_sha256],
      cost: { model_calls: 1, backtests: 1, wall_ms: summary.author_status.elapsed_ms ?? 0 }
    });

    const mutationState = {
      stage: 'not_registered',
      attempts_in_lineage: 1,
      prior_errors: [],
      candidate: candidateOf(mutated),
      budget: { model_calls_spent: 1, backtests_spent: 1, model_calls_remaining: 49, backtests_remaining: 49 }
    };
    const mutationDecision = await ledger.recordDecision({
      episodeId: `p1:${summary.trials.window_mutation.strategy_sha256.slice(0, 24)}`,
      lineage,
      state: mutationState,
      offeredActions: ['test_candidate', 'reject', 'abstain'],
      chosenAction: 'test_candidate',
      chooser: { kind: 'rules', id: 'phase1_window_mutation', version: '1' }, labelKind: 'retrospective',
      worker: { role: 'runner', slot: 'host', provider: 'host_mutation', model_id: 'none', agent_written: false }
    });
    await ledger.recordOutcome({
      decisionId: mutationDecision.decision_id,
      status: 'completed',
      code: 'host_window_mutation',
      stageReached: 'registered',
      receipts: [summary.trials.window_mutation.request_sha256, summary.trials.window_mutation.receipt_sha256],
      cost: { model_calls: 0, backtests: 1, wall_ms: 0 }
    });
  } finally {
    await ledger.close();
  }

  const exported = await exportEpisodes({ stateDir, campaignId });
  return { ...exported, promoting: false, qualification: 'research_only', agent_written_proven: true };
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? '').href) {
  const proofDir = process.env.PHASE1_PROOF_DIR;
  const stateDir = process.env.PHASE1_STATE_DIR || proofDir;
  const campaignId = process.env.PHASE1_CAMPAIGN_ID || 'phase1-hermes-20261025';
  recordPhase1Episodes({ proofDir, stateDir, campaignId })
    .then(r => process.stdout.write(JSON.stringify(r) + '\n'))
    .catch(e => {
      process.stdout.write(JSON.stringify({ ok: false, error: e.code ?? e.message }) + '\n');
      process.exitCode = 1;
    });
}
