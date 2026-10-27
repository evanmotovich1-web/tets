#!/usr/bin/env node
// Research-only terminal HUD. Dense black/amber panels inspired by the local
// OpenTerminal workspace. Replays the packaged SYNTH fixture. Not live, not
// Robinhood, not OpenTerminal, not the retired bot.
import { existsSync } from 'node:fs';
import { readdir, readFile, writeFile, mkdir } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { stdin, stdout } from 'node:process';
import { digest, verifyReceipt } from './contracts.mjs';

const DESK = join(dirname(fileURLToPath(import.meta.url)), '..');
const BARS_PATH = join(DESK, 'backtest/fixtures/sma-four-bars.jsonl');
const PASTE = 'hermes mcp add robinhood-trading --url https://agent.robinhood.com/mcp/trading --auth oauth';

const C = {
  reset: '\x1b[0m',
  hide: '\x1b[?25l',
  show: '\x1b[?25h',
  alt: '\x1b[?1049h',
  unalt: '\x1b[?1049l',
  clear: '\x1b[2J\x1b[H',
  amber: (s) => `\x1b[38;2;255;153;0m${s}\x1b[0m`,
  amberDim: (s) => `\x1b[38;2;179;107;0m${s}\x1b[0m`,
  up: (s) => `\x1b[38;2;0;200;83m${s}\x1b[0m`,
  down: (s) => `\x1b[38;2;255;61;61m${s}\x1b[0m`,
  text: (s) => `\x1b[38;2;217;217;217m${s}\x1b[0m`,
  dim: (s) => `\x1b[38;2;128;128;128m${s}\x1b[0m`,
  purple: (s) => `\x1b[38;2;196;160;255m${s}\x1b[0m`,
  bg: (s) => `\x1b[48;2;0;0;0m${s}\x1b[0m`,
};

const GLYPH = {
  A: [' ███ ', '█   █', '█████', '█   █', '█   █'],
  L: ['█    ', '█    ', '█    ', '█    ', '█████'],
  G: [' ████', '█    ', '█ ███', '█   █', ' ███ '],
  O: [' ███ ', '█   █', '█   █', '█   █', ' ███ '],
};

function args(argv) {
  const out = { once: false, html: false, replay: true, stateDir: null };
  for (let i = 2; i < argv.length; i += 1) {
    const flag = argv[i];
    if (flag === '--once') out.once = true;
    else if (flag === '--html') out.html = true;
    else if (flag === '--no-replay') out.replay = false;
    else if (flag === '--state-dir') out.stateDir = argv[i + 1], i += 1;
  }
  return out;
}

function parseJsonl(text) {
  return text.split(/\n/).filter(Boolean).map((line) => JSON.parse(line));
}

async function loadKind(stateDir, kind) {
  if (!stateDir || !existsSync(stateDir)) return null;
  const artifacts = join(stateDir, 'artifacts');
  if (!existsSync(artifacts)) return null;
  for (const name of await readdir(artifacts)) {
    if (!name.endsWith('.json')) continue;
    const data = JSON.parse(await readFile(join(artifacts, name), 'utf8'));
    if (data.kind === kind) return data;
  }
  return null;
}

async function loadSnapshot(stateDir) {
  const bars = parseJsonl(await readFile(BARS_PATH, 'utf8'));
  const result = await loadKind(stateDir, 'backtest_result');
  if (result) verifyReceipt(result);
  async function linked(kind) {
    if (!result) return null;
    const sha = result.artifacts[`${kind}_sha256`];
    if (!/^[a-f0-9]{64}$/.test(sha)) throw new Error('Invalid artifact reference');
    const artifact = JSON.parse(await readFile(join(stateDir, 'artifacts', `${sha}.json`), 'utf8'));
    if (digest(artifact) !== sha || artifact.kind !== kind || artifact.request_sha256 !== result.request_sha256) throw new Error('Artifact integrity failure');
    return artifact;
  }
  const [equity, fills] = await Promise.all([linked('equity'), linked('fills')]);
  return {
    symbol: 'SYNTH',
    qualification: 'research_only',
    promoting: false,
    live: false,
    robinhood: 'not_connected',
    bars,
    equityRows: equity?.rows || [],
    fills: fills?.rows || [],
    metrics: result?.metrics || null,
    receiptSha: result?.receipt_sha256 ?? null,
  };
}

function pad(value, width, align = 'left') {
  const text = String(value ?? '');
  // Escape sequences occupy no terminal columns. Never cut one in half.
  let count = 0, clipped = '';
  for (const token of text.match(/\x1b\[[0-9;?]*[A-Za-z]|[^\x1b]/gu) ?? []) {
    if (token.startsWith('\x1b')) clipped += token;
    else if (count < width) { clipped += token; count++; }
  }
  const space = ' '.repeat(Math.max(0, width - count));
  return align === 'right' ? space + clipped + C.reset : clipped + C.reset + space;
}

function wordmark(frame) {
  const word = 'ALGO';
  const rows = [0, 1, 2, 3, 4].map((r) => word.split('').map((ch) => GLYPH[ch][r]).join('  '));
  return rows.map((row, r) => {
    let out = '';
    for (let i = 0; i < row.length; i += 1) {
      const ch = row[i];
      if (ch === ' ') {
        out += ' ';
        continue;
      }
      const sweep = (i + frame + r) % 12 < 6;
      out += sweep ? C.amber(ch) : C.purple(ch);
    }
    return out;
  });
}

function candlePanel(bars, visible) {
  const slice = bars.slice(0, visible);
  const lows = slice.map((b) => Number(b.low));
  const highs = slice.map((b) => Number(b.high));
  const lo = Math.min(...lows);
  const hi = Math.max(...highs);
  const height = 8;
  const span = Math.max(hi - lo, 0.01);
  const y = (price) => Math.round(((price - lo) / span) * (height - 1));
  const lines = [];
  for (let row = height - 1; row >= 0; row -= 1) {
    let line = ' ';
    for (const bar of slice) {
      const open = y(Number(bar.open));
      const close = y(Number(bar.close));
      const high = y(Number(bar.high));
      const low = y(Number(bar.low));
      const bodyLo = Math.min(open, close);
      const bodyHi = Math.max(open, close);
      let ch = ' ';
      if (row === high || row === low) ch = '│';
      if (row >= bodyLo && row <= bodyHi) ch = close >= open ? '█' : '▓';
      const paint = close >= open ? C.up : C.down;
      line += paint(ch) + '  ';
    }
    lines.push(line);
  }
  const axis = ' ' + slice.map((b) => pad(Number(b.close).toFixed(0), 3)).join(' ');
  lines.push(C.dim(axis));
  return lines;
}

function spark(rows) {
  const glyphs = '▁▂▃▄▅▆▇█';
  if (!rows.length) return C.dim('equity unavailable until fixture tick');
  const values = rows.map((r) => Number(r.equity));
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const span = Math.max(hi - lo, 0.01);
  return values.map((v, i) => {
    const idx = Math.round(((v - lo) / span) * (glyphs.length - 1));
    const ch = glyphs[idx];
    const prev = i ? values[i - 1] : v;
    return v >= prev ? C.up(ch) : C.down(ch);
  }).join('');
}

function frameText(snap, visible, frame) {
  const bars = snap.bars;
  const last = bars[Math.max(0, visible - 1)];
  const prev = bars[Math.max(0, visible - 2)];
  const chg = last && prev ? Number(last.close) - Number(prev.close) : 0;
  const chgPct = prev ? (chg / Number(prev.close)) * 100 : 0;
  const chgText = `${chg >= 0 ? '+' : ''}${chg.toFixed(2)} (${chgPct.toFixed(2)}%)`;
  const m = snap.metrics;
  const lines = [];
  lines.push(C.dim('━'.repeat(92)));
  wordmark(frame).forEach((row) => lines.push('  ' + row));
  lines.push('  ' + C.amber('RESEARCH DESK') + '  ' + C.purple('SYNTH') + '  ' + C.dim('fixture replay · not live · not Robinhood · not edge'));
  lines.push(C.dim('━'.repeat(92)));
  lines.push(
    C.text(' STATUS ') +
    C.amber('FIXTURE') + C.dim(' · ') +
    C.up('research_only') + C.dim(' · ') +
    C.dim('promoting ') + C.down('false') + C.dim(' · ') +
    C.dim('Robinhood ') + C.down('not_connected') + C.dim(' · ') +
    C.dim(last ? last.bar_close_at : 'no bars'),
  );
  lines.push('');
  lines.push(C.amber(' CHART  SYNTH') + C.dim('                    ') + C.amber('QUOTE  SYNTH'));
  const chart = candlePanel(bars, visible);
  const quote = [
    C.text('Last  ') + C.amber(last ? last.close : '—') + '  ' + (chg >= 0 ? C.up(chgText) : C.down(chgText)),
    C.dim(`O ${last?.open}  H ${last?.high}  L ${last?.low}  C ${last?.close}  V ${last?.volume}`),
    C.dim('equity  ') + (m ? C.up(m.final_equity) : C.dim('unavailable')),
    C.dim('cash    ') + (m ? C.text(m.final_cash) : C.dim('unavailable')),
    C.dim('comm    ') + (m ? C.text(m.commission_total) : C.dim('unavailable')),
    C.dim('trades  ') + (m ? C.text(String(m.completed_trades)) : C.dim('unavailable')),
    C.dim('path    ') + spark(snap.equityRows.slice(0, visible)),
    C.dim('fake broker only · next-bar-open SMA fixture'),
  ];
  for (let i = 0; i < Math.max(chart.length, quote.length); i += 1) {
    const left = pad(chart[i] || '', 36);
    const right = quote[i] || '';
    lines.push(' ' + left + '  ' + right);
  }
  lines.push('');
  lines.push(C.amber(' FILLS') + C.dim('  (synthetic accounting, not broker executions)'));
  if (!snap.fills.length) {
    lines.push(C.dim('  no fills in this state dir — run the fixture tick first'));
  } else {
    snap.fills.filter(fill => fill.executed_at <= last.bar_close_at).forEach((fill) => {
      const side = fill.side === 'buy' ? C.up('BUY ') : C.down('SELL');
      lines.push(`  ${side}  ${C.text(fill.quantity)} @ ${C.amber(fill.price)}  ${C.dim(fill.executed_at)}  comm ${fill.commission}`);
    });
  }
  lines.push('');
  lines.push(C.amber(' ROBINHOOD') + C.dim('  human gate — this HUD will not run OAuth'));
  lines.push(C.dim('  paste yourself if you want the official MCP:'));
  lines.push('  ' + C.text(PASTE));
  lines.push('');
  lines.push(C.dim(' q quit · four packaged SYNTH bars · make start opens OpenTerminal + ALGO activity'));
  return lines.join('\n');
}

function htmlBoard(snap) {
  const last = snap.bars.at(-1) || {};
  const m = snap.metrics || {};
  const fills = (snap.fills || []).map((f) => `<tr><td>${f.side}</td><td>${f.quantity}</td><td>${f.price}</td><td>${f.executed_at}</td></tr>`).join('');
  const bars = snap.bars.map((b) => Number(b.close));
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>ALGO research desk — SYNTH fixture</title>
<style>
:root{--bg:#000;--panel:#0a0a0a;--panel-2:#111;--border:#262626;--amber:#ff9900;--up:#00c853;--down:#ff3d3d;--text:#d9d9d9;--dim:#808080}
html,body{margin:0;background:var(--bg);color:var(--text);font:12px/1.4 "SF Mono",Menlo,Consolas,monospace;min-height:100%}
header{display:flex;justify-content:space-between;align-items:center;padding:10px 14px;border-bottom:1px solid var(--border);background:var(--panel-2)}
.brand{color:var(--amber);letter-spacing:.18em}
.pill{color:var(--up);border:1px solid #143;padding:2px 8px;margin-left:8px}
.warn{color:var(--down)}
main{display:grid;grid-template-columns:1.4fr .9fr;grid-template-rows:auto auto;gap:1px;background:var(--border);min-height:calc(100vh - 42px)}
section{background:var(--panel);padding:12px 14px}
h2{margin:0 0 8px;font-size:11px;color:var(--amber);letter-spacing:.12em}
.quote{font-size:28px;color:var(--amber)}
.dim{color:var(--dim)}
table{width:100%;border-collapse:collapse}
td{padding:4px 0;border-bottom:1px solid #1a1a1a}
canvas{width:100%;height:180px;background:#050505;border:1px solid var(--border)}
</style></head>
<body>
<header>
  <div><span class="brand">ALGO RESEARCH DESK</span><span class="pill">research_only</span></div>
  <div class="dim">SYNTH fixture · promoting false · Robinhood not_connected · not edge</div>
</header>
<main>
  <section>
    <h2>CHART SYNTH</h2>
    <canvas id="c" width="720" height="180"></canvas>
    <p class="dim">O ${last.open}  H ${last.high}  L ${last.low}  C ${last.close}  V ${last.volume}</p>
  </section>
  <section>
    <h2>QUOTE SYNTH</h2>
    <div class="quote">${last.close || '—'}</div>
    <p>equity ${m.final_equity || 'unavailable'} · cash ${m.final_cash || 'unavailable'}</p>
    <p>commissions ${m.commission_total || 'unavailable'} · trades ${m.completed_trades ?? 'unavailable'}</p>
    <p class="warn">Not live. Fake broker. Four packaged bars only.</p>
  </section>
  <section>
    <h2>FILLS</h2>
    <table>${fills || '<tr><td class="dim">no fills</td></tr>'}</table>
  </section>
  <section>
    <h2>ROBINHOOD</h2>
    <p class="warn">not_connected — paste yourself, this page does not OAuth</p>
    <p class="dim">${PASTE}</p>
  </section>
</main>
<script>
const bars = ${JSON.stringify(bars)};
const ctx = document.getElementById('c').getContext('2d');
const w = 720, h = 180, lo = Math.min(...bars), hi = Math.max(...bars);
ctx.strokeStyle = '#ff9900'; ctx.beginPath();
bars.forEach((v,i) => {
  const x = 20 + i * ((w-40)/(bars.length-1||1));
  const y = h-20 - ((v-lo)/Math.max(hi-lo,.01))*(h-40);
  i ? ctx.lineTo(x,y) : ctx.moveTo(x,y);
});
ctx.stroke();
</script>
</body></html>`;
}

async function writeHtml(snap, stateDir) {
  const dir = stateDir ? join(stateDir, '..') : join(DESK, 'runtime/.state/one-action-install');
  await mkdir(dir, { recursive: true });
  const path = join(dir, 'board.html');
  await writeFile(path, htmlBoard(snap));
  return path;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function live(snap, opts) {
  stdout.write(C.alt + C.hide + C.clear);
  const restore = () => {
    if (stdin.isTTY) stdin.setRawMode(false);
    stdin.pause();
    stdout.write(C.show + C.unalt);
  };
  process.on('SIGINT', () => {
    restore();
    process.exit(0);
  });
  process.on('SIGTERM', () => { restore(); process.exit(0); });
  if (stdin.isTTY) stdin.setRawMode(true);
  stdin.resume();
  let quit = false;
  stdin.on('data', (chunk) => {
    if (String(chunk) === 'q' || String(chunk) === '\u0003') quit = true;
  });
  const max = snap.bars.length;
  let visible = opts.replay ? 1 : max;
  let frame = 0;
  while (!quit) {
    stdout.write('\x1b[H' + frameText(snap, visible, frame));
    if (visible < max) visible += 1;
    frame += 1;
    await sleep(visible < max ? 420 : 80);
  }
  restore();
}

const opts = args(process.argv);
const snap = await loadSnapshot(opts.stateDir);
if (opts.html) {
  const path = await writeHtml(snap, opts.stateDir);
  stdout.write(path + '\n');
}
if (opts.once || !stdout.isTTY) {
  stdout.write(frameText(snap, snap.bars.length, 0) + '\n');
} else {
  await live(snap, opts);
}
