import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import canonicalize from 'canonicalize';
import Ajv from 'ajv';

export const APP = new URL('../', import.meta.url);
export const config = JSON.parse(readFileSync(new URL('../config/research.json', import.meta.url)));
export function digest(value) { return createHash('sha256').update(canonicalize(value)).digest('hex'); }
export function copy(value) { return JSON.parse(JSON.stringify(value)); }
export class DeskError extends Error {
  constructor(code, { transient = false, uncertain = false } = {}) { super(code); this.code = code; this.transient = transient; this.uncertain = uncertain; }
}
export function requireThat(condition, code) { if (!condition) throw new DeskError(code); }
export function exact(value, keys) {
  requireThat(value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).length === keys.length && keys.every(k => Object.hasOwn(value, k)), 'invalid_arguments');
}
export function parseStrictJSON(text, maxBytes = 65536) {
  requireThat(typeof text === 'string' && Buffer.byteLength(text) <= maxBytes, 'json_input_too_large');
  const stack = [];
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (ch === '{' || ch === '[') { stack.push(ch === '{' ? new Set() : null); requireThat(stack.length <= 32, 'json_nesting_limit'); }
    else if (ch === '}' || ch === ']') stack.pop();
    else if (ch === '"') {
      const start = i; let escaped = false;
      for (i++; i < text.length; i++) { if (!escaped && text[i] === '"') break; if (!escaped && text[i] === '\\') escaped = true; else escaped = false; }
      let next = i + 1; while (/\s/.test(text[next] ?? '') && next < text.length) next++;
      if (text[next] === ':') {
        const key = JSON.parse(text.slice(start, i + 1)); const keys = stack.at(-1);
        requireThat(keys instanceof Set && !keys.has(key), 'duplicate_json_key'); keys.add(key);
      }
    }
  }
  return JSON.parse(text, (_key, value) => { requireThat(typeof value !== 'number' || Number.isFinite(value), 'nonfinite_json_number'); return value; });
}
export const ID = { type: 'string', minLength: 1, maxLength: 128, pattern: '^[A-Za-z0-9][A-Za-z0-9_.:-]*$' };
const TEXT = { type: 'string', minLength: 1, maxLength: 2000 };
const HASH = { type: 'string', pattern: '^[0-9a-f]{64}$' };
const object = properties => ({ type: 'object', properties, required: Object.keys(properties), additionalProperties: false });
export const schemas = {
  campaign_status: object({ campaign_id: ID }), enqueue_research: object({ campaign_id: ID, event_id: ID }), read_card: object({ card_id: ID }),
  list_research_datasets: object({}), read_manifest: object({ dataset_id: ID }), submit_hypothesis: object({ dataset_id: ID, text: TEXT }),
  read_hypothesis: object({ hypothesis_id: ID }), read_strategy_schema: object({}), submit_strategy: object({ strategy: { type: 'object', maxProperties: 12 } }), submit_preregistration: object({ preregistration: { type: 'object', maxProperties: 12 } }),
  read_accepted_request: object({ trial_id: ID }), run_backtest: object({ trial_id: ID }), read_receipt: object({ receipt_sha256: HASH }),
  read_trial_bundle: object({ trial_id: ID }), record_review: object({ trial_id: ID, verdict: { enum: ['research_valid', 'reject', 'abstain'] }, reason_codes: { type: 'array', maxItems: 20, uniqueItems: true, items: ID }, summary: TEXT })
};
const ajv = new Ajv({ allErrors: false, strict: true });
const validators = Object.fromEntries(Object.entries(schemas).map(([k, v]) => [k, ajv.compile(v)]));
export function validateArguments(name, args) { requireThat(validators[name]?.(args), 'invalid_arguments'); }
export function verifyReceipt(receipt) {
  exact(receipt, ['schema_version', 'kind', 'request_sha256', 'status', 'qualification', 'promoting', 'reason_codes', 'artifacts', 'metrics', 'receipt_sha256']);
  const { receipt_sha256, ...body } = receipt;
  requireThat(receipt.schema_version === 1 && receipt.kind === 'backtest_result' && receipt.qualification === 'research_only' && receipt.promoting === false && digest(body) === receipt_sha256, 'receipt_integrity_failure');
  requireThat(['completed', 'rejected', 'failed', 'unknown'].includes(receipt.status), 'invalid_receipt');
  exact(receipt.artifacts, ['orders_sha256', 'fills_sha256', 'equity_sha256']);
  requireThat(receipt.status === 'completed' ? /^[0-9a-f]{64}$/.test(receipt.request_sha256) && receipt.metrics && typeof receipt.metrics === 'object' && Object.values(receipt.artifacts).every(x => /^[0-9a-f]{64}$/.test(x)) : receipt.metrics === null, 'invalid_receipt');
  return receipt;
}
