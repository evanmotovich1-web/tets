import { ExecutionError } from './contracts.mjs';

/** No MCP client is accepted. This stub cannot be enabled by an environment
 * flag, a promotion record, or an intent. Real broker integration is unbuilt. */
export class DisabledRobinhoodOrderAdapter {
  constructor() { this.enabled = false; this.reconciliation_method = 'unverified_disabled'; Object.freeze(this); }
  async placeOrder() { throw new ExecutionError('robinhood_execution_disabled'); }
  async cancelOrder() { throw new ExecutionError('robinhood_execution_disabled'); }
}
