import Ajv from 'ajv';
import { readFileSync } from 'node:fs';
import { ID, requireThat } from './contracts.mjs';
const hash = { type: 'string', pattern: '^[0-9a-f]{64}$' };
const nullable = schema => ({ anyOf: [schema, { type: 'null' }] });
const object = (properties, required = Object.keys(properties)) => ({ type: 'object', additionalProperties: false, properties, required });
const count = { type: 'integer', minimum: 0 };
// The Python semantic validator owns defaults, numeric bounds and hash sealing.
// Both runtimes consume the same closed structural schema.
export const strategySpecSchema = JSON.parse(readFileSync(new URL('../backtest/contracts/strategy-spec.schema.json', import.meta.url), 'utf8'));
const { $schema: strategyDialect, ...strategyShape } = strategySpecSchema;
const validateStrategy = new Ajv({ strict: true }).compile(strategyShape);
export function validateStrategyShape(spec) {
  requireThat(validateStrategy(spec), 'invalid_strategy_spec');
  return spec;
}
export const generationSchema = object({
  tool_calls: { type: 'array', minItems: 1, maxItems: 4, items: object({ name: ID, arguments: { type: 'object' } }) },
  usage: object({ input_tokens: count, output_tokens: count }), provider: ID, model_id: { type: 'string', minLength: 1, maxLength: 128 },
  provenance: object({ transport: { enum: ['http', 'cli', 'fixture'] }, agent_written: { type: 'boolean' },
    model_version: nullable({ type: 'string', maxLength: 256 }), raw_response_sha256: hash })
}, ['tool_calls', 'usage', 'provider', 'model_id']);
export const experimentEventSchema = object({
  schema_version: { const: 1 }, kind: { const: 'experiment_event' }, campaign_id: ID,
  sequence: { type: 'integer', minimum: 1 }, event_id: ID, event_type: ID, trial_id: hash, job_id: hash,
  actor_role: { enum: ['host', 'desk', 'scout', 'author', 'runner', 'referee'] },
  occurred_at: { type: 'string', pattern: '^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{3}Z$' },
  previous_event_sha256: nullable(hash), payload: { type: 'object' }, event_sha256: hash
});
const properties = {
  schema_version: { const: 1 }, kind: { const: 'durable_job' }, job_id: hash, workflow_version: { const: 1 }, campaign_id: ID, event_id: ID,
  trigger: object({ source: ID, event_id: ID, payload_sha256: hash }), trial_id: hash,
  policy: { type: 'object' }, policy_sha256: hash, role_policy_sha256: hash, budget_policy_sha256: hash,
  state: { enum: ['running', 'retry_wait', 'unknown', 'completed', 'rejected', 'failed', 'cancelled'] },
  phase: { enum: ['admit', 'scout', 'propose', 'validate', 'backtest', 'referee', 'save_card', 'done', 'retry'] },
  checkpoint_revision: count, checkpoint: { $ref: '#/definitions/checkpoint' },
  attempts_by_phase: { type: 'object', additionalProperties: { type: 'integer', minimum: 0, maximum: 3 } },
  next_attempt_at: nullable(count), last_error_code: nullable(ID), budget: object({ model_calls: count, tool_calls: count, tokens: count }),
  outputs: { type: 'array', maxItems: 20, items: object({ phase: ID, round: count, prompt_sha256: hash, output: generationSchema }) },
  events: { type: 'array', maxItems: 1000, items: experimentEventSchema }, accepted: nullable(generationSchema),
  inflight: nullable({ anyOf: [object({ type: { const: 'model' }, prompt_sha256: hash, reservation: count }), object({ type: { const: 'worker' }, operation_id: hash, message: { type: 'object' } })] }),
  hypothesis: nullable(object({ hypothesis_id: hash, text: { type: 'string', maxLength: 2000 } })), strategy: nullable({ type: 'object' }), manifest: nullable({ type: 'object' }), receipt: nullable({ type: 'object' }), review: nullable({ type: 'object' }), card: nullable({ type: 'object' }),
  last_tool_result: {}, task_id: { type: 'integer', minimum: 1 }, dataset_id: ID, request_sha256: hash, worker_events: { type: 'array' }
};
export const durableJobSchema = {
  $schema: 'http://json-schema.org/draft-07/schema#',
  ...object(properties, Object.keys(properties).filter(k => !['dataset_id', 'request_sha256', 'worker_events'].includes(k))),
  definitions: { checkpoint: { anyOf: [
    object({ phase: { enum: ['admit', 'scout', 'propose', 'validate', 'backtest', 'referee', 'save_card', 'done'] }, step: { enum: ['generate', 'execute'] }, round: count, index: count }),
    object({ phase: { const: 'retry' }, resume: { $ref: '#/definitions/checkpoint' }, until: count })
  ] } }
};
const validate = new Ajv({ strict: true }).compile(durableJobSchema);
export function validateJob(job) { requireThat(validate(job), 'invalid_durable_job'); return job; }
