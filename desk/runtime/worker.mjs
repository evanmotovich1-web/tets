import { spawn } from 'node:child_process';
import { readFile, readdir, mkdir, open, link, unlink, stat } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { join, resolve, dirname } from 'node:path';
import canonicalize from 'canonicalize';
import { APP, DeskError, digest, requireThat, verifyReceipt, parseStrictJSON } from './contracts.mjs';

async function publish(path, value) {
  const bytes = canonicalize(value);
  const staging = `${path}.${process.pid}.tmp`;
  const fd = await open(staging, 'wx', 0o600);
  try { await fd.writeFile(bytes); await fd.sync(); } finally { await fd.close(); }
  try { await link(staging, path); } catch (e) {
    if (e.code !== 'EEXIST') throw e;
    requireThat(await readFile(path, 'utf8') === bytes, 'operation_cache_integrity_failure');
  } finally { await unlink(staging); }
}
export function workerBridge({ stateDir, registry, python, operationMs = 30000 } = {}) {
  const cwd = fileURLToPath(APP);
  const executable = python ?? join(cwd, 'backtest/.venv/bin/python');
  const registryPath = resolve(registry ?? join(cwd, 'backtest/fixtures/registry.json'));
  const artifacts = join(resolve(stateDir), 'artifacts');
  const replies = join(resolve(stateDir), 'worker-replies');
  const inflight = new Set();
  async function invoke(message, signal) {
    requireThat(Buffer.byteLength(JSON.stringify(message)) <= 65536, 'worker_input_too_large');
    await mkdir(artifacts, { recursive: true });
    return new Promise((resolvePromise, reject) => {
      const child = spawn(executable, ['-m', 'backtest.worker'], { cwd, shell: false, stdio: ['pipe', 'pipe', 'pipe'], env: { PATH: `${dirname(executable)}:/usr/bin:/bin`, PYTHONNOUSERSITE: '1', OMP_NUM_THREADS: '1', OPENBLAS_NUM_THREADS: '1', VECLIB_MAXIMUM_THREADS: '1', TRADING_DESK_REGISTRY: registryPath, TRADING_DESK_ARTIFACT_DIR: artifacts } });
      inflight.add(child);
      const chunks = []; let stdout = 0, stderr = 0, failed;
      const stop = code => { failed ??= new DeskError(code, { uncertain: true }); child.kill('SIGKILL'); };
      const timer = setTimeout(() => stop('worker_timeout_unknown'), operationMs);
      const onAbort = () => stop('worker_interrupted_unknown');
      signal?.addEventListener('abort', onAbort, { once: true });
      if (signal?.aborted) onAbort();
      child.stdout.on('data', bytes => { stdout += bytes.length; if (stdout > 1048576) stop('worker_output_too_large'); else chunks.push(bytes); });
      child.stderr.on('data', bytes => { stderr += bytes.length; if (stderr > 1048576) stop('worker_output_too_large'); });
      child.stdin.on('error', () => {});
      child.once('error', () => { failed = new DeskError('worker_unavailable'); });
      child.once('close', code => {
        clearTimeout(timer); signal?.removeEventListener('abort', onAbort); inflight.delete(child);
        if (failed) return reject(failed);
        try {
          const response = parseStrictJSON(Buffer.concat(chunks).toString('utf8'), 1048576);
          requireThat(response.schema_version === 1 && response.kind === 'worker_response' && typeof response.ok === 'boolean' && Array.isArray(response.events), 'invalid_worker_response');
          requireThat((code === 0) === response.ok, 'invalid_worker_exit');
          if (response.result) verifyReceipt(response.result);
          resolvePromise(response);
        } catch (error) { reject(error instanceof DeskError ? error : new DeskError('worker_result_unknown', { uncertain: true })); }
      });
      child.stdin.end(JSON.stringify(message));
    });
  }
  async function cached(operationId) {
    requireThat(/^[0-9a-f]{64}$/.test(operationId), 'invalid_operation_id');
    try {
      const stored = parseStrictJSON(await readFile(join(replies, `${operationId}.json`), 'utf8'), 1048576);
      const { response_sha256, ...body } = stored;
      requireThat(digest(body) === response_sha256 && body.operation_id === operationId, 'operation_cache_integrity_failure');
      if (stored.response.result) verifyReceipt(stored.response.result);
      return stored;
    } catch (e) { if (e.code === 'ENOENT') return null; throw e; }
  }
  async function save(operationId, message, response) {
    await mkdir(replies, { recursive: true });
    const body = { operation_id: operationId, message_sha256: digest(message), response };
    await publish(join(replies, `${operationId}.json`), { ...body, response_sha256: digest(body) });
    return response;
  }
  async function call(message, { operationId, signal } = {}) {
    // Receipt verification must inspect current referenced artifact bytes, not an old read cache.
    if (message.op === 'read_receipt') return invoke(message, signal);
    const id = operationId ?? digest(message);
    const stored = await cached(id);
    if (stored) { requireThat(stored.message_sha256 === digest(message), 'operation_payload_conflict'); return stored.response; }
    return save(id, message, await invoke(message, signal));
  }
  async function reconcile(message, { operationId, signal, manifestHash }) {
    const known = await cached(operationId);
    if (known) { requireThat(known.message_sha256 === digest(message), 'operation_payload_conflict'); return known.response; }
    if (message.op !== 'run_backtest') return null;
    let files;
    try { files = await readdir(artifacts); } catch (e) { if (e.code === 'ENOENT') return null; throw e; }
    requireThat(files.length <= 5000, 'artifact_reconciliation_limit');
    const matches = []; const deadline = Date.now() + operationMs;
    const norm = s => s.includes('.') ? s.replace(/0+$/, '').replace(/\.$/, '') : s;
    for (const file of files) {
      if (Date.now() >= deadline) throw new DeskError('reconciliation_budget_exhausted', { uncertain: true });
      if (!/^[0-9a-f]{64}\.json$/.test(file)) continue;
      // Large orders/equity artifacts need not be loaded to discover a small receipt.
      if ((await stat(join(artifacts, file))).size > 1048576) continue;
      const bytes = await readFile(join(artifacts, file));
      const receipt = parseStrictJSON(bytes.toString('utf8'), 1048576);
      if (receipt.kind !== 'backtest_result' || receipt.status !== 'completed') continue;
      verifyReceipt(receipt);
      const request = parseStrictJSON(await readFile(join(artifacts, `${receipt.request_sha256}.json`), 'utf8'), 1048576);
      if (request.strategy_sha256 !== message.strategy.strategy_sha256 || request.manifest_sha256 !== manifestHash || digest(request.evaluation_window) !== digest(message.evaluation_window) || request.initial_cash !== norm(message.initial_cash) || Object.keys(message.cost_model).some(k => request.cost_model[k] !== norm(message.cost_model[k]))) continue;
      matches.push({ request, receipt });
    }
    requireThat(matches.length <= 1, 'ambiguous_reconciliation');
    if (!matches.length) return null;
    const { request, receipt } = matches[0];
    const checked = await invoke({ op: 'read_receipt', receipt_sha256: receipt.receipt_sha256 }, signal);
    requireThat(checked.ok && checked.result.receipt_sha256 === receipt.receipt_sha256, 'receipt_integrity_failure');
    return save(operationId, message, { ...checked, request });
  }
  return { call, reconcile, cached, artifacts, registryPath, async close() { const waits = [...inflight].map(child => new Promise(done => { child.once('close', done); child.kill('SIGKILL'); })); await Promise.all(waits); } };
}
