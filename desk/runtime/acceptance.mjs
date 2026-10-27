// Frozen research-acceptance contract (task 1.b item 7; specs/jev-algo-research.md Phase 3).
// "research_accepted" is a research-survivor label for comparing research policies. It never
// moves a referee stage, never promotes, and never relaxes the historical_research requirement.
// The contract lives in config/research-acceptance-v1.json; its sha256 is over the file bytes,
// so the JS desk and the Python league compute the same value.
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { requireThat } from './contracts.mjs';

const BYTES = readFileSync(new URL('../config/research-acceptance-v1.json', import.meta.url));
export const RESEARCH_ACCEPTED_V1 = Object.freeze(JSON.parse(BYTES.toString('utf8')));
export const ACCEPTANCE_CONTRACT_SHA256 = createHash('sha256').update(BYTES).digest('hex');

/** Refuse to resume or compare work recorded under a different contract. */
export function requireContract(recordedSha256) {
  requireThat(recordedSha256 === ACCEPTANCE_CONTRACT_SHA256, 'acceptance_contract_changed');
}

const num = v => (typeof v === 'string' || typeof v === 'number') && v !== '' && Number.isFinite(Number(v)) ? Number(v) : null;

/** One candidate's evidence -> { accepted, failures }. Missing evidence fails closed. */
export function evaluateAcceptance(evidence, contract = RESEARCH_ACCEPTED_V1) {
  const r = contract.requires, f = [];
  if (evidence?.valid_spec !== true) f.push('valid_spec');
  if (evidence?.authentic_receipt !== true) f.push('authentic_receipt');
  if (evidence?.independent_replay_match !== true) f.push('independent_replay_match');
  const p = num(evidence?.adjusted_pvalue); if (p === null || p > Number(r.max_adjusted_pvalue)) f.push('adjusted_pvalue');
  if (!Number.isSafeInteger(evidence?.independent_trades) || evidence.independent_trades < r.min_independent_trades) f.push('min_independent_trades');
  const dd = num(evidence?.max_drawdown_pct); if (dd === null || dd > Number(r.max_drawdown_pct)) f.push('max_drawdown_pct');
  const ex = num(evidence?.excess_return_vs_buy_and_hold_pct); if (ex === null || ex < Number(r.min_excess_return_vs_buy_and_hold_pct)) f.push('min_excess_return');
  const st = num(evidence?.excess_under_cost_stress_pct);
  if (evidence?.cost_stress_multiplier !== r.cost_stress_multiplier || st === null || st < Number(r.excess_under_cost_stress_min_pct)) f.push('cost_stress');
  return { accepted: f.length === 0, failures: f, contract_sha256: ACCEPTANCE_CONTRACT_SHA256 };
}

/** Lineage-capped survivors: at most max_survivors_per_lineage per lineage, and a lineage that
 * searched more than max_variants_searched_per_lineage variants contributes none. */
export function acceptedLineages(rows, contract = RESEARCH_ACCEPTED_V1) {
  const byLineage = new Map();
  for (const row of rows) {
    requireThat(typeof row.lineage_group === 'string', 'invalid_lineage');
    const g = byLineage.get(row.lineage_group) ?? { variants: 0, accepted: [] };
    g.variants += 1;
    if (evaluateAcceptance(row.evidence, contract).accepted) g.accepted.push(row);
    byLineage.set(row.lineage_group, g);
  }
  const survivors = [];
  for (const [lineage, g] of [...byLineage].sort(([a], [b]) => a.localeCompare(b))) {
    if (g.variants > contract.lineage.max_variants_searched_per_lineage) continue;
    survivors.push(...g.accepted.slice(0, contract.lineage.max_survivors_per_lineage).map(row => ({ lineage_group: lineage, id: row.id })));
  }
  return { survivors, lineages_searched: byLineage.size, contract_sha256: ACCEPTANCE_CONTRACT_SHA256, authority: contract.authority };
}
