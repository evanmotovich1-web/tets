import { cents, money, quantity, digest, copy, currentTime, fakeSession, requireThat } from './contracts.mjs';

/** Deterministic test venue. No HTTP, MCP, OAuth or real account access. Sells
 * settle immediately in this fake; this is not a model of real settlement. */
export class FakeBroker {
  #orders = new Map(); #fills = []; #faults = []; #connected = true; #quotes; #clock; #cash;
  #pnl = {}; #approvals = true; #extraPositions = []; #session;
  constructor({ accountId = 'FAKE-ACCOUNT-1234', cash = '1000.00', quotes = { SPY: { bid: '10.00', ask: '10.00' } },
    clock = () => new Date(), session } = {}) {
    this.accountId = accountId; this.#cash = cents(cash); this.#quotes = copy(quotes); this.#clock = clock; this.#session = session;
    this.placeCalls = 0; this.cancelCalls = 0; this.approvalReads = 0; this.snapshotReads = 0;
  }
  #ready(account) { requireThat(this.#connected, 'fake_disconnected'); requireThat(account === this.accountId, 'fake_wrong_account'); }
  disconnect() { this.#connected = false; }
  reconnect() { this.#connected = true; }
  queueFault(fault) { this.#faults.push(copy(fault)); }
  setQuote(symbol, quote) { this.#quotes[symbol] = copy(quote); }
  setApprovals(enabled) { this.#approvals = enabled; }
  setDailyPnl(strategy, value) { cents(value, true); this.#pnl[strategy] = value; }
  addUntrackedPosition(position) { this.#extraPositions.push(copy(position)); }
  getSession(now) { return this.#session ? copy(this.#session) : fakeSession(now); }
  async getTradeApprovalSetting(account) { this.#ready(account); this.approvalReads++; return this.#approvals; }
  async getQuote(account, symbol) {
    this.#ready(account); const value = this.#quotes[symbol]; requireThat(value, 'fake_missing_quote');
    const now = currentTime(this.#clock);
    return { symbol, bid: value.bid, ask: value.ask, quote_at: value.quote_at ?? now, available_at: value.available_at ?? now };
  }
  async placeOrder(account, order) {
    this.placeCalls++; this.#ready(account);
    const fault = this.#faults.shift();
    if (fault?.type === 'disconnect_before_accept') throw new Error('fake disconnected before acceptance');
    if (this.#orders.has(order.client_reference)) return copy(this.#orders.get(order.client_reference));
    const stored = { ...copy(order), broker_order_id: `fake-order-${this.#orders.size + 1}`, status: 'open',
      filled_quantity: '0', filled_notional: '0.00', submitted_at: currentTime(this.#clock) };
    this.#orders.set(order.client_reference, stored);
    if (fault?.type === 'fill' || fault?.type === 'timeout_after_fill') this.fill(order.client_reference, order.quantity, fault.price ?? order.limit_price);
    if (fault?.type === 'partial_fill') this.fill(order.client_reference, fault.quantity ?? '1', fault.price ?? order.limit_price);
    if (fault?.type === 'timeout_after_accept' || fault?.type === 'timeout_after_fill') throw new Error('fake submission timeout');
    if (fault?.type === 'unknown_response') return {};
    return { broker_order_id: stored.broker_order_id };
  }
  fill(key, amount, price) {
    const order = this.#orders.get(key); requireThat(order && ['open', 'partially_filled'].includes(order.status), 'fake_order_not_open');
    const count = quantity(amount), before = quantity(order.filled_quantity, true), total = quantity(order.quantity);
    requireThat(before + count <= total, 'fake_overfill');
    const notional = count * cents(price);
    order.filled_quantity = String(before + count); order.filled_notional = money(cents(order.filled_notional) + notional);
    order.status = before + count === total ? 'filled' : 'partially_filled';
    this.#fills.push({ key, strategy_sha256: order.strategy_sha256, symbol: order.symbol, side: order.side,
      quantity: amount, price, notional: money(notional), filled_at: currentTime(this.#clock) });
  }
  async cancelOrder(account, id) {
    this.#ready(account); this.cancelCalls++;
    const order = [...this.#orders.values()].find(order => order.broker_order_id === id);
    requireThat(order, 'fake_order_missing');
    if (['open', 'partially_filled'].includes(order.status)) order.status = 'cancelled';
    return { broker_order_id: id, status: order.status };
  }
  async getAccountSnapshot(account) {
    this.#ready(account); this.snapshotReads++;
    const now = currentTime(this.#clock), session = this.getSession(now);
    let cash = this.#cash;
    const positions = {}, byStrategy = {}, dayCash = {}, openingQty = {};
    for (const fill of this.#fills) {
      const amount = quantity(fill.quantity), signed = fill.side === 'buy' ? amount : -amount;
      const flow = fill.side === 'buy' ? -cents(fill.notional) : cents(fill.notional);
      positions[fill.symbol] = (positions[fill.symbol] ?? 0n) + signed;
      byStrategy[fill.strategy_sha256] ??= { symbol: fill.symbol, quantity: 0n };
      byStrategy[fill.strategy_sha256].quantity += signed; cash += flow;
      if (fill.filled_at >= session.open_at) dayCash[fill.strategy_sha256] = (dayCash[fill.strategy_sha256] ?? 0n) + flow;
      else openingQty[fill.strategy_sha256] = (openingQty[fill.strategy_sha256] ?? 0n) + signed;
    }
    const pnl = {};
    for (const [strategy, position] of Object.entries(byStrategy)) {
      const quote = this.#quotes[position.symbol]; requireThat(quote, 'fake_missing_quote');
      const mark = (cents(quote.bid) + cents(quote.ask)) / 2n;
      const openMark = cents(quote.day_open_price ?? quote.bid);
      pnl[strategy] = money(position.quantity * mark - (openingQty[strategy] ?? 0n) * openMark + (dayCash[strategy] ?? 0n));
    }
    Object.assign(pnl, this.#pnl);
    return { account_sha256: digest(account), as_of: now, market_day: session.market_day, settled_cash: money(cash),
      orders: copy([...this.#orders.values()]), daily_pnl_by_strategy: pnl,
      positions: [...Object.entries(positions).filter(([, qty]) => qty !== 0n).map(([symbol, qty]) => ({ symbol, quantity: String(qty),
        mark_price: money((cents(this.#quotes[symbol].bid) + cents(this.#quotes[symbol].ask)) / 2n) })), ...copy(this.#extraPositions)] };
  }
}
