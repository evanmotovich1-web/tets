import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, readFile, writeFile, readdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { runCampaign } from '../../runtime/campaign.mjs';
import { planFor, proposal } from '../fixtures/campaign.mjs';

async function temp(t) { const root = await mkdtemp(join(tmpdir(), 'campaign-integration-')); t.after(() => rm(root, { recursive: true, force: true })); return root; }

test('native candidate workers produce real receipts, reject invalid specs and count duplicate attempts', { timeout: 25000 }, async t => {
  const root = await temp(t);
  const plan = await planFor(root, [proposal('first'), proposal('duplicate', { rationale: 'same rules, reworded fixture' }),
    proposal('invalid', { parameters: { fast_window: 2, slow_window: 1 } }), proposal('variant', { position: { quantity: '2' } })]);
  const summary = await runCampaign({ plan, stateDir: root });
  assert.equal(summary.N, 4); assert.equal(summary.ledger_decision_count, 4); assert.equal(summary.ledger_outcome_count, 4);
  assert.deepEqual(summary.candidates.map(c => c.status), ['completed', 'duplicate', 'rejected', 'completed']);
  assert.notEqual(summary.candidates[0].receipt_sha256, summary.candidates[3].receipt_sha256);
  assert.ok(summary.candidates.every(c => c.verdict.N === 4 && !c.verdict.passed));
  const dir = join(root, 'campaigns', plan.campaign_id);
  assert.equal((await readdir(join(dir, 'jobs'))).length, 4);
  const done = JSON.parse(await readFile(join(dir, 'DONE.json'), 'utf8'));
  assert.equal(done.summary_sha256, summary.summary_sha256);
  const [run] = await readdir(join(dir, 'jobs', '0000'));
  const jobDir = join(dir, 'jobs', '0000', run);
  const output = JSON.parse(await readFile(join(jobDir, 'result.json'), 'utf8'));
  await rm(join(jobDir, 'artifacts', `${output.receipt.artifacts.fills_sha256}.json`));
  await assert.rejects(runCampaign({ plan, stateDir: root }), { code: 'ENOENT' });
});

async function crash(root, plan, phase) {
  const module = fileURLToPath(new URL('../../runtime/campaign.mjs', import.meta.url));
  await writeFile(join(root, 'plan.json'), JSON.stringify(plan));
  await writeFile(join(root, 'crash.mjs'), `import {runCampaign} from ${JSON.stringify(module)};\nimport{readFile}from'node:fs/promises';\nconst plan=JSON.parse(await readFile(${JSON.stringify(join(root,'plan.json'))},'utf8'));\nawait runCampaign({plan,stateDir:${JSON.stringify(root)},onCheckpoint:async e=>{if(e.kind===${JSON.stringify(phase)}&&e.attempt===0)process.kill(process.pid,'SIGKILL');}});`);
  return new Promise((resolveRun, reject) => {
    const child = spawn(process.execPath, [join(root, 'crash.mjs')], { shell: false, stdio: ['ignore', 'pipe', 'pipe'] });
    let stderr = ''; child.stdout.resume(); child.stderr.on('data', c => { stderr += c; });
    const timer = setTimeout(() => child.kill('SIGKILL'), 20000);
    child.on('error', reject); child.on('close', (code, signal) => { clearTimeout(timer); resolveRun({ code, signal, stderr }); });
  });
}

test('SIGKILL after worker output but before the registry commit recovers it without rerunning', { timeout: 25000 }, async t => {
  const root = await temp(t); const plan = await planFor(root); plan.limits.concurrency = 1;
  const killed = await crash(root, plan, 'job_finished'); assert.equal(killed.signal, 'SIGKILL', killed.stderr);
  const firstDir = join(root, 'campaigns', plan.campaign_id, 'jobs', '0000');
  const before = await readdir(firstDir);
  const summary = await runCampaign({ plan, stateDir: root });
  assert.equal(summary.status, 'completed'); assert.equal(summary.N, 2);
  assert.equal(summary.registry_attempt_count, summary.ledger_decision_count); assert.equal(summary.ledger_outcome_count, 2);
  assert.deepEqual(await readdir(firstDir), before);
  assert.ok(summary.candidates.every(c => c.status === 'completed'));
});

test('SIGKILL after admission keeps the missing result unknown and never resubmits it', { timeout: 25000 }, async t => {
  const root = await temp(t); const plan = await planFor(root); plan.limits.concurrency = 1;
  const killed = await crash(root, plan, 'attempt_committed'); assert.equal(killed.signal, 'SIGKILL', killed.stderr);
  const summary = await runCampaign({ plan, stateDir: root });
  assert.equal(summary.N, 2); assert.equal(summary.ledger_decision_count, 2); assert.equal(summary.ledger_outcome_count, 2);
  assert.equal(summary.candidates[0].status, 'unknown'); assert.equal(summary.candidates[0].code, 'interrupted_result_unknown');
  assert.equal(summary.candidates[1].status, 'completed');
  await assert.rejects(readdir(join(root, 'campaigns', plan.campaign_id, 'jobs', '0000')), { code: 'ENOENT' });
});
