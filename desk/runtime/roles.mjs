import { readFileSync } from 'node:fs';
import { defineExtension, defineTool, hook, section, ToolTask } from '@earendil-works/pi-durable';
import { requireThat, schemas, validateArguments } from './contracts.mjs';
const source = JSON.parse(readFileSync(new URL('../config/roles.json', import.meta.url)));
export const roles = Object.freeze(Object.fromEntries(Object.entries(source.roles).map(([name, role]) => [name, Object.freeze({ ...role, tools: Object.freeze(role.tools) })])));
export function beforeTool(role, call) {
  return Object.hasOwn(roles, role) && roles[role].tools.includes(call.name) ? undefined : { block: `Tool ${call.name} is not allowed for ${role}` };
}
export async function dispatch(role, call, handlers) {
  requireThat(!beforeTool(role, call), 'forbidden_tool');
  validateArguments(call.name, call.arguments);
  requireThat(typeof handlers[call.name] === 'function', 'missing_handler');
  return handlers[call.name](call.arguments);
}
export function roleTools(role) {
  requireThat(Object.hasOwn(roles, role), 'invalid_role');
  return roles[role].tools.map(name => ({ name, description: `${name}: research-only, campaign-scoped`, parameters: schemas[name] }));
}
export function roleExtensions(handlers = {}) {
  return Object.entries(roles).map(([role, policy]) => defineExtension({
    name: `trading-${role}`,
    sections: [section(`trading-${role}`, () => policy.prompt, { tag: false })],
    hooks: [hook(ToolTask, { beforeTool: call => beforeTool(role, call) })],
    tools: roleTools(role).map(tool => defineTool({ ...tool, replay: 'safe', execute: async args => ({ content: [{ type: 'text', text: JSON.stringify(await dispatch(role, { name: tool.name, arguments: args }, handlers)) }] }) }))
  }));
}
