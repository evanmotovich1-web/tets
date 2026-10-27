import { spawn } from 'node:child_process';
import { isAbsolute, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { ProviderError, check } from './contracts.mjs';
import { parseStrictJSON } from '../contracts.mjs';

/** Host-only bridge. No broker/author tools are registered here. Captures are
 * pinned before preregistration; repeated evaluation reuses those exact bytes.
 * State and executable paths come from host configuration, never strategy JSON.
 * The CLI has no holdout operation and cannot open a holdout ledger or dataset.
 */
export function createWalkForwardBridge({ statePath, python, timeoutMs = 55000 } = {}) {
  check(typeof statePath === 'string' && isAbsolute(statePath), 'walkforward_state_required');
  check(Number.isSafeInteger(timeoutMs) && timeoutMs > 0 && timeoutMs <= 55000, 'invalid_provider_timeout');
  const cwd = fileURLToPath(new URL('../../', import.meta.url));
  const executable = python ?? join(cwd, 'backtest/.venv/bin/python');
  async function invoke(message, signal) {
    check(!signal?.aborted, 'provider_aborted');
    check(Buffer.byteLength(JSON.stringify(message)) <= 8 * 1024 * 1024, 'walkforward_input_too_large');
    return new Promise((resolve, reject) => {
      const child = spawn(executable, ['-m', 'backtest.walkforward'], { cwd, shell: false, stdio: ['pipe', 'pipe', 'pipe'],
        env: { PATH: process.env.PATH ?? '/usr/bin:/bin', PYTHONNOUSERSITE: '1', PYTHONDONTWRITEBYTECODE: '1',
          OMP_NUM_THREADS: '1', OPENBLAS_NUM_THREADS: '1', TRADING_DESK_WALKFORWARD_STATE: statePath } });
      const chunks = []; let bytes = 0, errorBytes = 0, failure;
      const stop = code => { failure ??= new ProviderError(code); child.kill('SIGKILL'); };
      const timer = setTimeout(() => stop('walkforward_timeout'), timeoutMs);
      const onAbort = () => stop('provider_aborted'); signal?.addEventListener('abort', onAbort, { once: true });
      if (signal?.aborted) onAbort();
      child.stdout.on('data', chunk => { bytes += chunk.length; if (bytes > 8 * 1024 * 1024) stop('walkforward_output_too_large'); else chunks.push(chunk); });
      child.stderr.on('data', chunk => { errorBytes += chunk.length; if (errorBytes > 1024 * 1024) stop('walkforward_output_too_large'); });
      child.stdin.on('error', () => {});
      child.on('error', () => { failure = new ProviderError('walkforward_unavailable'); });
      child.on('close', code => {
        clearTimeout(timer); signal?.removeEventListener('abort', onAbort);
        if (failure) return reject(failure);
        try {
          const result = parseStrictJSON(Buffer.concat(chunks).toString('utf8'), 8 * 1024 * 1024);
          if (!result.ok) {
            check(typeof result.error === 'string' && /^[a-z_]{1,80}$/.test(result.error), 'walkforward_invalid_response');
            return reject(new ProviderError(result.error));
          }
          check(code === 0 && result.value && typeof result.value === 'object', 'walkforward_invalid_response');
          resolve(result.value);
        } catch (error) { reject(error instanceof ProviderError ? error : new ProviderError('walkforward_invalid_response')); }
      });
      child.stdin.end(JSON.stringify(message));
    });
  }
  return Object.freeze({
    async captureProviderDataset({ provider, query, partition, developmentManifestSha256, signal }) {
      check(['development', 'validation'].includes(partition), 'dataset_access_denied');
      check(developmentManifestSha256 === undefined || partition === 'validation' && /^[0-9a-f]{64}$/.test(developmentManifestSha256), 'invalid_development_reference');
      check(typeof provider?.getBars === 'function', 'provider_required');
      const snapshot = await provider.getBars({ ...query, signal });
      return invoke({ op: 'import_provider', snapshot, partition,
        ...(developmentManifestSha256 === undefined ? {} : { development_manifest_sha256: developmentManifestSha256 }) }, signal);
    },
    createCampaign: (campaign, signal) => invoke({ op: 'create_campaign', campaign }, signal),
    register: ({ campaign_id, spec, preregistration, requested_at }, signal) => invoke({ op: 'register', campaign_id, spec, preregistration, requested_at }, signal),
    // minimum_search_n: the desk-wide global N (runtime/exposure.mjs); Holm then counts every strategy tried.
    evaluate: ({ registration_sha256, partition, requested_at, minimum_search_n }, signal) => {
      check(['development', 'validation'].includes(partition), 'dataset_access_denied');
      return invoke({ op: 'evaluate', registration_sha256, partition, requested_at, ...(minimum_search_n === undefined ? {} : { minimum_search_n }) }, signal);
    }
  });
}
