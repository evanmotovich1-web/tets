// Daily leaderboard. Shadow testing is retired. With paper disabled (the default) the
// ranking is backtest-only: it uses validation and holdout walk-forward verdicts, never
// development results alone. Paper results rank first only when a campaign enables paper.
import { mkdir, readFile, writeFile, rename } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { requireThat } from './contracts.mjs';
import { STAGES, PAPER_DISABLED, readJsonl, runReferee, judgeReadOnly } from './referee.mjs';

const DEPTH = { validation: 1, holdout: 2 };
const pvalue = v => v.backtest?.adjusted_pvalue === null || v.backtest?.adjusted_pvalue === undefined ? 1 : Number(v.backtest.adjusted_pvalue);

export function rank(verdicts) {
  const row = v => ({
    strategy_sha256: v.strategy_sha256, symbol: v.symbol, family: v.family, timeframe: v.timeframe,
    author: `${v.author.role} (${v.author.model_id})`, stage: v.stage, failing: v.failing, backtest: v.backtest, paper: v.paper
  });
  const rankable = verdicts.filter(v => DEPTH[v.backtest?.partition]);
  const ranked = rankable
    .sort((a, b) => STAGES.indexOf(b.stage) - STAGES.indexOf(a.stage) || DEPTH[b.backtest.partition] - DEPTH[a.backtest.partition]
      || Number(b.backtest.passed) - Number(a.backtest.passed) || pvalue(a) - pvalue(b) || a.strategy_sha256.localeCompare(b.strategy_sha256))
    .map((v, i) => ({ rank: i + 1, ...row(v) }));
  const unranked = verdicts.filter(v => !rankable.includes(v))
    .sort((a, b) => STAGES.indexOf(b.stage) - STAGES.indexOf(a.stage) || a.strategy_sha256.localeCompare(b.strategy_sha256))
    .map(v => ({ rank: null, reason: v.backtest ? 'development_only' : 'no_backtest_verdict', ...row(v) }));
  return { ranked, unranked };
}

const short = sha => sha.slice(0, 10);

export function renderMarkdown(board) {
  const lines = [
    `# Strategy leaderboard: ${board.campaign_id}, ${board.date}`, '',
    board.paper_enabled
      ? `Research only. Paper money is enabled for this campaign; ranked by stage, then validation and holdout verdicts. N = ${board.N} strategies tried.`
      : `Research only. Backtest-only ranking from validation and holdout verdicts (paper disabled, no shadow testing). N = ${board.N} strategies tried.`,
    `Eligible for live: ${board.eligible.length}. None of them trades without Evan's separate written approval.`, ''
  ];
  if (board.promotions.length) lines.push(`New promotion records: ${board.promotions.map(p => short(p.strategy_sha256)).join(', ')}.`, '');
  if (board.revocations.length) lines.push(`Revoked: ${board.revocations.map(p => short(p.strategy_sha256)).join(', ')}.`, '');
  lines.push('| Rank | Strategy | Author | Stage | Deepest backtest | Holm-adjusted p | Blocked by |', '|---|---|---|---|---|---|---|');
  for (const r of [...board.ranked, ...board.unranked]) {
    lines.push(`| ${r.rank ?? '-'} | ${short(r.strategy_sha256)} ${r.family} ${r.symbol} ${r.timeframe} | ${r.author} | ${r.stage} | ${r.backtest ? `${r.backtest.partition} ${r.backtest.passed ? 'pass' : 'fail'}` : '-'} | ${r.backtest?.adjusted_pvalue ?? 'n/a'} | ${r.failing ? `${r.failing.stage}: ${r.failing.criteria.join(', ')}` : '-'} |`);
  }
  return lines.join('\n') + '\n';
}

async function readInputs(paths) {
  const out = [];
  for (const path of paths) out.push(...readJsonl(await readFile(path, 'utf8')));
  return out;
}

function boardFrom(result, campaignId, day) {
  return { schema_version: 2, kind: 'leaderboard', campaign_id: campaignId, date: day, decided_at: result.decided_at, N: result.N,
    ranking_basis: result.paper_enabled ? 'stage_then_backtest' : 'backtest_only', paper_enabled: result.paper_enabled,
    ...rank(result.verdicts), eligible: result.verdicts.filter(v => v.stage === 'eligible_for_live').map(v => v.strategy_sha256),
    promotions: result.promotions, revocations: result.revocations, qualification: 'research_only', promoting: false };
}

/** Read-only export (task 1.b item 5). Writes nothing: no referee folder, verdict log, promotion or
 * leaderboard file. This is what Hermes and any author-facing league view must call. */
export async function exportLeaderboard({ stateDir, campaignId, verdictPaths = [], paperPaths = [], paper = PAPER_DISABLED, date, now }) {
  const root = resolve(stateDir ?? fileURLToPath(new URL('./.state', import.meta.url)));
  requireThat(paper.enabled === true || paperPaths.length === 0, 'paper_disabled');
  const result = await judgeReadOnly({ stateDir: root, campaignId, verdicts: await readInputs(verdictPaths), paperVerdicts: await readInputs(paperPaths), paper, now });
  const day = date ?? result.decided_at.slice(0, 10);
  requireThat(/^\d{4}-\d\d-\d\d$/.test(day), 'invalid_date');
  const board = { ...boardFrom(result, campaignId, day), read_only: true, would_promote: result.would_promote };
  return { board, markdown: renderMarkdown(board), paths: null };
}

export async function buildLeaderboard({ stateDir, campaignId, verdictPaths = [], paperPaths = [], paper = PAPER_DISABLED, date, now }) {
  const root = resolve(stateDir ?? fileURLToPath(new URL('./.state', import.meta.url)));
  const verdicts = await readInputs(verdictPaths);
  requireThat(paper.enabled === true || paperPaths.length === 0, 'paper_disabled');
  const paperVerdicts = await readInputs(paperPaths);
  const result = await runReferee({ stateDir: root, campaignId, verdicts, paperVerdicts, paper, now });
  const day = date ?? result.decided_at.slice(0, 10);
  requireThat(/^\d{4}-\d\d-\d\d$/.test(day), 'invalid_date');
  const board = boardFrom(result, campaignId, day);
  const dir = join(root, 'leaderboard', campaignId);
  await mkdir(dir, { recursive: true, mode: 0o700 });
  const markdown = renderMarkdown(board);
  for (const [name, body] of [[`${day}.json`, JSON.stringify(board, null, 2) + '\n'], [`${day}.md`, markdown]]) {
    const tmp = join(dir, `${name}.${process.pid}.tmp`); await writeFile(tmp, body, { mode: 0o600 }); await rename(tmp, join(dir, name));
  }
  return { board, markdown, paths: { json: join(dir, `${day}.json`), markdown: join(dir, `${day}.md`) } };
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? '').href) {
  const argv = process.argv.slice(2); const multi = { verdicts: [], 'paper-verdicts': [] }; const single = {};
  for (let i = 0; i < argv.length; i += 2) {
    const key = argv[i]?.replace(/^--/, ''), value = argv[i + 1];
    if (key in multi) multi[key].push(value); else single[key] = value;
  }
  // --read-only yes: export without writing anything (Hermes uses this). Default stays the operator command.
  const build = single['read-only'] === 'yes' ? exportLeaderboard : buildLeaderboard;
  build({ stateDir: single['state-dir'], campaignId: single['campaign-id'] ?? 'fleet-fixture-v1', verdictPaths: multi.verdicts, paperPaths: multi['paper-verdicts'],
    paper: single.paper === 'enabled' ? { enabled: true } : PAPER_DISABLED, date: single.date })
    .then(({ board, markdown, paths }) => process.stdout.write(JSON.stringify({ ok: true, read_only: board.read_only === true, campaign_id: board.campaign_id, date: board.date, N: board.N, ranking_basis: board.ranking_basis,
      ranked: board.ranked.length, unranked: board.unranked.length, eligible_for_live: board.eligible.length, paths, markdown }) + '\n'))
    .catch(error => { process.stdout.write(JSON.stringify({ ok: false, error: error.code ?? 'leaderboard_failure' }) + '\n'); process.exitCode = 1; });
}
