import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { resolve } from 'node:path';
import { mkdtemp, rm, readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { runTick } from '../../runtime/tick.mjs';
import { publicActivity, publishActivity } from '../../runtime/activity.mjs';

function run(args) {
  return new Promise((resolveChild, reject) => {
    const child = spawn(process.execPath, ['runtime/tui.mjs', ...args], {
      cwd: resolve('.'),
      env: { ...process.env, TERM: 'dumb' },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let output = '';
    child.stdout.on('data', (chunk) => { output += chunk; });
    child.stderr.on('data', (chunk) => { output += chunk; });
    child.on('error', reject);
    child.on('close', (code) => resolveChild({ code, output }));
  });
}

test('tui --once is a research fixture HUD, not a live terminal', async () => {
  const { code, output } = await run(['--once']);
  assert.equal(code, 0, output);
  assert.match(output, /RESEARCH DESK/);
  assert.match(output, /research_only/);
  assert.match(output, /SYNTH/);
  assert.match(output, /not_connected/);
  assert.match(output, /promoting/);
  assert.doesNotMatch(output, /NYSE OPEN/);
  assert.doesNotMatch(output, /GOOGL/);
  assert.doesNotMatch(output, /robin_stocks/);
});

test('tui reads real fixture metrics in a clean install and keeps ANSI columns intact', async t => {
  const state = await mkdtemp(join(tmpdir(), 'algo-tui-'));
  t.after(() => rm(state, { recursive: true, force: true }));
  const observations = [];
  const result = await runTick({ eventId: 'tui:001', stateDir: state,
    onSnapshot: job => { observations.push({ state: job.state, revision: job.checkpoint_revision, phase: job.phase }); },
  });
  assert.equal(result.job.state, 'completed');
  assert.ok(observations.some(s => s.state === 'running'));
  assert.equal(observations.at(-1).state, 'completed');
  assert.ok(observations.length > 3);
  const { code, output } = await run(['--once', '--state-dir', state]);
  assert.equal(code, 0, output);
  assert.match(output, /1005\.40/);
  assert.match(output, /2\.30/);
  assert.match(output, /BUY /);
  assert.match(output, /SELL/);
  const plain = output.replace(/\x1b\[[0-9;?]*[A-Za-z]/g, '');
  assert.doesNotMatch(plain, /\x1b/);
  assert.match(plain, /111\.10/);
  assert.match(plain, /118\.80/);
  const job = structuredClone(result.job);
  job.events[0].payload.private_prompt = 'PRIVATE-CONTEXT-MUST-NOT-LEAVE';
  job.policy.private_token = 'PRIVATE-CONTEXT-MUST-NOT-LEAVE';
  const path = join(state, 'public/activity.json');
  const activity = await publishActivity(job, state, path);
  assert.equal(activity.metrics.final_equity, '1005.40');
  assert.equal(activity.metrics.commission_total, '2.30');
  assert.equal(activity.fills.length, 2);
  assert.equal(activity.equity.length, 4);
  assert.equal(activity.provider, 'fixture');
  assert.equal(activity.live_orders, false);
  assert.equal(activity.broker_connection, 'not_checked');
  assert.ok(activity.events.some(e => e.actor_role === 'author'));
  assert.ok(activity.events.some(e => e.tool === 'run_backtest'));
  assert.equal(activity.receipt_sha256, result.job.receipt.receipt_sha256);
  assert.doesNotMatch(await readFile(path, 'utf8'), /PRIVATE-CONTEXT-MUST-NOT-LEAVE|prompt_sha256|output_sha256|arguments/);
  const artifact = join(state, 'artifacts', `${job.receipt.artifacts.fills_sha256}.json`);
  const changed = JSON.parse(await readFile(artifact, 'utf8'));
  changed.rows[0].price = '99999';
  await writeFile(artifact, JSON.stringify(changed));
  await assert.rejects(publicActivity(job, state), /integrity_failure/);
  const tampered = await run(['--once', '--state-dir', state]);
  assert.notEqual(tampered.code, 0);
});
