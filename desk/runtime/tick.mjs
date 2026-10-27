import { mkdir, readFile, readdir, unlink } from 'node:fs/promises';
import { openSync, writeFileSync, fsyncSync, closeSync } from 'node:fs';
import { resolve, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { hostname } from 'node:os';
import { randomUUID } from 'node:crypto';
import { BACKGROUND_CONTEXT as ctx } from '@earendil-works/chord/context';
import { createModels } from '@earendil-works/pi-ai/models';
import { Harness, createRegistry, defineExtension } from '@earendil-works/pi-durable';
import { openNodeSqliteStorage } from '@earendil-works/pi-durable/storage/sqlite/node';
import { APP, config, copy, digest, requireThat, DeskError } from './contracts.mjs';
import { createModelAdapter } from './models.mjs';
import { roles, roleExtensions } from './roles.mjs';
import { workerBridge } from './worker.mjs';
import { DeskState, makeWorkflow, ledger, doneStates } from './workflow.mjs';
import { validateJob } from './schemas.mjs';

async function acquireLock(dir) {
  await mkdir(dir, { recursive: true, mode: 0o700 });
  const path = join(dir, 'writer.lock'); const token = randomUUID();
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const fd = openSync(path, 'wx', 0o600);
      try { writeFileSync(fd, JSON.stringify({ pid: process.pid, host: hostname(), token })); fsyncSync(fd); } finally { closeSync(fd); }
      return async () => { const owner = JSON.parse(await readFile(path, 'utf8')); requireThat(owner.token === token, 'writer_lock_changed'); await unlink(path); };
    } catch (error) {
      if (error.code !== 'EEXIST') throw error;
      // Serialize stale-lock reclamation. Without this guard, a second reclaimer
      // could unlink the fresh lock that replaced the dead PID's lock.
      const reclaimPath = `${path}.reclaim`; let reclaim;
      try { reclaim = openSync(reclaimPath, 'wx', 0o600); } catch (e) { if (e.code === 'EEXIST') throw new DeskError('writer_recovery_active'); throw e; }
      try {
        let owner;
        try { owner = JSON.parse(await readFile(path, 'utf8')); } catch (e) { if (e.code === 'ENOENT') continue; throw new DeskError('invalid_writer_lock'); }
        requireThat(owner.host === hostname() && Number.isSafeInteger(owner.pid) && owner.pid > 0, 'invalid_writer_lock');
        try { process.kill(owner.pid, 0); throw new DeskError('writer_already_active'); } catch (e) { if (e.code !== 'ESRCH') throw e; }
        await unlink(path);
      } finally { closeSync(reclaim); await unlink(reclaimPath); }
    }
  }
  throw new DeskError('writer_already_active');
}
async function fingerprint(worker) {
  const root = fileURLToPath(APP); const files = {};
  for (const name of (await readdir(join(root, 'backtest'))).filter(n => n.endsWith('.py')).sort()) files[`backtest/${name}`] = digest((await readFile(join(root, 'backtest', name))).toString());
  for (const name of (await readdir(join(root, 'backtest/contracts'))).filter(n => n.endsWith('.json')).sort()) files[`contracts/${name}`] = digest((await readFile(join(root, 'backtest/contracts', name))).toString());
  files.lock = digest((await readFile(join(root, 'requirements.lock'))).toString());
  files.runtime_lock = digest((await readFile(join(root, 'package-lock.json'))).toString());
  files.registry = digest((await readFile(worker.registryPath)).toString());
  return digest(files);
}
export async function runTick({ eventId, provider = 'fixture', stateDir = fileURLToPath(new URL('./.state', import.meta.url)), payload = { kind: 'scheduled_research' }, source = 'foreground', model: suppliedModel, worker: suppliedWorker, registry: datasetRegistry, maxPhases = Infinity, tickMs = config.budget.tick_ms, budget: budgetOverride, afterPhase, onSnapshot } = {}) {
  requireThat(typeof eventId === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(eventId), 'invalid_event_id');
  requireThat(Number.isInteger(tickMs) && tickMs > 0 && tickMs <= 60000, 'invalid_tick_budget');
  const dir = resolve(stateDir); const unlock = await acquireLock(dir);
  let harness, worker;
  try {
    const model = suppliedModel ?? createModelAdapter(provider);
    worker = suppliedWorker ?? workerBridge({ stateDir: dir, registry: datasetRegistry });
    const budget = { ...config.budget, ...budgetOverride };
    for (const [key, value] of Object.entries(budget)) requireThat(Number.isSafeInteger(value) && value > 0 && value <= config.budget[key], 'invalid_budget_override');
    let phaseCount = 0;
    const policy = { ...copy(config), budget, provider: model.provider, model_id: model.model_id, provider_route_sha256: model.route_sha256 ?? null, roles_sha256: digest(roles), worker_sha256: suppliedWorker ? 'test-worker' : await fingerprint(worker) };
    const policyHash = digest(policy);
    const registry = createRegistry();
    for (const extension of roleExtensions()) registry.install(extension);
    const readState = async () => copy(await harness.snapshot(DeskState, ctx));
    const task = makeWorkflow({ readJob: async id => (await readState()).jobs[id], model, worker, budget, shouldYield: () => phaseCount >= maxPhases, onPhase: phase => { phaseCount++; afterPhase?.(phase); } });
    const coordinator = defineExtension({ name: 'trading-workflow', tasks: [task] });
    registry.install(coordinator);
    harness = await Harness.open(await openNodeSqliteStorage(join(dir, 'agent.sqlite')), { models: createModels(), registry, settings: { extensions: [coordinator], retry: { enabled: false }, compaction: { enabled: false }, stream: { maxRetries: 0, timeoutMs: 30000 } } }, ctx);
    const root = await harness.root(ctx, { agent: { extensions: [coordinator] } });
    const triggerHash = digest({ source, event_id: eventId, campaign_id: config.campaign_id });
    const payloadHash = digest(payload);
    const jobId = digest({ workflow_version: 1, trigger_source: source, trigger_event_id: eventId, trigger_payload_sha256: payloadHash, campaign_id: config.campaign_id });
    await root.commit(async tx => {
      const state = await tx.doc(DeskState);
      const existing = state.triggers[triggerHash];
      if (existing) {
        requireThat(existing.payload_sha256 === payloadHash && existing.job_id === jobId, 'trigger_payload_conflict');
        const job = state.jobs[jobId];
        requireThat(job.policy_sha256 === policyHash, 'execution_policy_conflict');
        if (job.state === 'unknown' && job.inflight?.type === 'worker') {
          job.state = 'running';
          job.task_id = await tx.createTask(task, { job_id: jobId, resume: copy(job.checkpoint) }, { ownership: { kind: 'conversation' } });
          ledger(job, 'reconciliation_started', { operation_id: job.inflight.operation_id });
        }
        return;
      }
      requireThat(Object.keys(state.jobs).length < 200, 'job_limit');
      requireThat(!Object.values(state.jobs).some(j => !doneStates.includes(j.state)), 'another_job_active');
      const job = { schema_version: 1, kind: 'durable_job', job_id: jobId, workflow_version: 1, campaign_id: config.campaign_id, event_id: eventId, trigger: { source, event_id: eventId, payload_sha256: payloadHash }, trial_id: digest({ job_id: jobId, proposal: 1 }), policy, policy_sha256: policyHash, role_policy_sha256: digest(roles), budget_policy_sha256: digest(budget), state: 'running', phase: 'admit', checkpoint_revision: 0, checkpoint: { phase: 'admit', step: 'generate', round: 0, index: 0 }, attempts_by_phase: {}, next_attempt_at: null, last_error_code: null, budget: { model_calls: 0, tool_calls: 0, tokens: 0 }, outputs: [], events: [], accepted: null, inflight: null, hypothesis: null, strategy: null, manifest: null, receipt: null, review: null, card: null, last_tool_result: null };
      ledger(job, 'trial_registered', { policy_sha256: policyHash, model: { provider: model.provider, model_id: model.model_id }, training_cutoff: 'unknown' });
      const taskId = await tx.createTask(task, { job_id: jobId }, { ownership: { kind: 'conversation' } });
      state.jobs[jobId] = validateJob({ ...job, task_id: taskId });
      state.triggers[triggerHash] = { payload_sha256: payloadHash, job_id: jobId };
    }, ctx);
    const start = Date.now(); let publishedRevision = null;
    harness.resume();
    while (true) {
      const state = await readState(); const job = state.jobs[jobId];
      const revision = `${job.checkpoint_revision}:${job.state}`;
      if (onSnapshot && revision !== publishedRevision) {
        await onSnapshot(copy(job)); publishedRevision = revision;
      }
      if (doneStates.includes(job.state) || phaseCount >= maxPhases || (job.state === 'retry_wait' && Date.now() < job.next_attempt_at) || Date.now() - start >= tickMs) return { schema_version: 1, kind: 'tick_result', job: copy(job), cards: copy(state.cards) };
      await new Promise(done => setTimeout(done, 10));
    }
  } finally {
    try { await harness?.close(ctx); } finally { try { await worker?.close?.(); } finally { await unlock(); } }
  }
}
async function main() {
  const options = {}; const names = { '--event-id': 'eventId', '--provider': 'provider', '--state-dir': 'stateDir' };
  const args = process.argv.slice(2);
  for (let i = 0; i < args.length; i += 2) { requireThat(names[args[i]] && typeof args[i + 1] === 'string' && !Object.hasOwn(options, names[args[i]]), 'invalid_cli_arguments'); options[names[args[i]]] = args[i + 1]; }
  const result = await runTick(options);
  // Compact public receipt: prompts, hypotheses, and provider configuration stay in controlled state.
  console.log(JSON.stringify({ schema_version: 1, job_id: result.job.job_id, state: result.job.state, phase: result.job.phase, error: result.job.last_error_code, card: result.cards[result.job.job_id] ?? null }));
  if (['failed', 'rejected', 'unknown'].includes(result.job.state)) process.exitCode = 1;
}
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) main().catch(error => { console.error(JSON.stringify({ error: error instanceof DeskError ? error.code : 'tick_failed' })); process.exitCode = 1; });
