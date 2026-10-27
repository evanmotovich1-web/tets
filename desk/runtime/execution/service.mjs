import { access } from 'node:fs/promises';
import { join } from 'node:path';
import { openJournal } from './journal.mjs';
import { FakeBroker } from './fake-broker.mjs';
import { ExecutionError, requireThat, digest, copy, currentTime, stamp, cents, money, quantity, hash,
  limits as normalizeLimits, normalizeIntent, idempotencyKey, verifyRecord } from './contracts.mjs';

const ACTIVE = ['submitting', 'unknown', 'open', 'partially_filled'];
const BROKER_STATES = ['open', 'partially_filled', 'filled', 'cancelled', 'rejected'];
export function policyFingerprint({ accountId, limits = {}, allocations = {} }) {
  return digest({ mode: 'fake_only_v1', account_sha256: digest(accountId), limits: normalizeLimits(limits), allocations });
}

/** Host-only entry point, never a research/author tool. Authorization records
 * are supplied by the host; a checksum proves integrity, not human identity.
 * No record, flag, or option can attach a real broker to this implementation. */
export async function openExecutionService({ stateDir, accountId, broker, env = process.env, clock = () => new Date(),
  limits: overrides = {}, allocations = {}, authorizationSource = async () => null, timeoutMs = 1000, faultHooks = {} } = {}) {
  requireThat(broker instanceof FakeBroker, 'fake_broker_required');
  requireThat(typeof accountId === 'string' && accountId.length >= 4 && accountId.length <= 128 && broker.accountId === accountId, 'wrong_account');
  requireThat(Number.isSafeInteger(timeoutMs) && timeoutMs > 0 && timeoutMs <= 15000, 'invalid_timeout');
  const cfg = normalizeLimits(overrides), budgets = copy(allocations), accountHash = digest(accountId);
  for (const [strategy, amount] of Object.entries(budgets)) { hash(strategy); requireThat(cents(amount) > 0n && cents(amount) <= cents(cfg.per_strategy_max_exposure_usd), 'invalid_allocation'); }
  const policyHash = policyFingerprint({ accountId, limits: cfg, allocations: budgets });
  const journal = await openJournal(stateDir, { account_sha256: accountHash, account_last4: accountId.slice(-4), policy_sha256: policyHash });
  const now = () => currentTime(clock);
  let tail = Promise.resolve(), closing = false;
  const save = (event, mutate = () => {}) => { const next = journal.state; mutate(next); journal.commit(next, { at: now(), ...event }); };
  const serialize = fn => {
    requireThat(!closing, 'execution_closed');
    const result = tail.then(fn); tail = result.catch(() => {}); return result;
  };
  async function call(fn) {
    let timer;
    try { return await Promise.race([Promise.resolve().then(fn), new Promise((_resolve, reject) => { timer = setTimeout(() => reject(new ExecutionError('broker_timeout')), timeoutMs); })]); }
    catch { throw new ExecutionError('broker_unavailable'); }
    finally { clearTimeout(timer); }
  }
  async function manualHalt() {
    try { await access(join(stateDir, 'HALT')); save({ type: 'manual_halt' }, s => { s.global_halt = 'manual_halt'; }); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
  }
  function view(key) { return copy(journal.state.orders[key]); }
  function refuse(key, code, checks = {}) {
    save({ type: 'order_refused', key, code, checks }, state => Object.assign(state.orders[key], { status: 'refused', code, checks }));
    return view(key);
  }
  async function approvals() {
    let enabled = false;
    try { enabled = await call(() => broker.getTradeApprovalSetting(accountId)) === true; } catch { /* fail closed */ }
    save({ type: 'approval_setting_read', enabled }, s => { s.approval_setting = { enabled, checked_at: now() }; });
    return enabled;
  }
  async function authorize(intent) {
    try {
      const records = await authorizationSource(intent.strategy_sha256);
      const promotion = records?.promotion, approval = records?.approval;
      requireThat(verifyRecord(promotion) && verifyRecord(approval), 'not_approved');
      requireThat(promotion.kind === 'promotion' && promotion.stage === 'eligible_for_live' && promotion.author_role === 'referee' &&
        promotion.strategy_sha256 === intent.strategy_sha256 && promotion.symbol === intent.symbol && promotion.revoked !== true && stamp(promotion.created_at) <= now(), 'not_approved');
      requireThat(approval.kind === 'evan_approval' && approval.approved_by === 'Evan' && approval.strategy_sha256 === intent.strategy_sha256 &&
        approval.account_sha256 === accountHash && approval.policy_sha256 === policyHash && approval.revoked === false &&
        stamp(approval.created_at) <= now() && stamp(approval.expires_at) > now(), 'not_approved');
      return { promotion_sha256: promotion.record_sha256, approval_sha256: approval.record_sha256 };
    } catch { throw new ExecutionError('not_approved'); }
  }
  async function cancelHalted() {
    const state = journal.state;
    const orders = Object.values(state.orders).filter(order => ['open', 'partially_filled'].includes(order.status) &&
      (state.global_halt || state.strategy_halts[order.intent.strategy_sha256] === 'strategy_loss_kill'));
    for (const order of orders) {
      try { await call(() => broker.cancelOrder(accountId, order.broker_order_id)); save({ type: 'cancel_requested', key: order.key }); }
      catch { save({ type: 'cancel_unknown', key: order.key }, s => { s.orders[order.key].status = 'unknown'; s.strategy_halts[order.intent.strategy_sha256] = 'unknown_order'; }); }
    }
    if (orders.length) await reconcileInternal({ cancel: false });
  }
  async function reconcileInternal({ startup = false, cancel = true } = {}) {
    await manualHalt();
    let snapshot;
    try {
      snapshot = await call(() => broker.getAccountSnapshot(accountId));
      requireThat(snapshot.account_sha256 === accountHash && stamp(snapshot.as_of) <= now() && Date.parse(now()) - Date.parse(snapshot.as_of) <= cfg.quote_max_age_ms, 'reconcile_mismatch');
      requireThat(Array.isArray(snapshot.orders) && snapshot.orders.length <= 10000 && Array.isArray(snapshot.positions) && snapshot.positions.length <= 1000, 'reconcile_mismatch');
      cents(snapshot.settled_cash);
      const state = journal.state, found = new Set();
      if (startup) for (const order of Object.values(state.orders)) if (order.status === 'prepared') Object.assign(order, { status: 'refused', code: 'interrupted_before_submission' });
      for (const remote of snapshot.orders) {
        const key = remote.client_reference, known = state.orders[key];
        requireThat(known && !found.has(key) && known.status !== 'refused' && BROKER_STATES.includes(remote.status), 'reconcile_mismatch');
        found.add(key);
        const i = known.intent;
        requireThat(remote.strategy_sha256 === i.strategy_sha256 && remote.symbol === i.symbol && remote.side === i.side &&
          remote.quantity === i.quantity && cents(remote.limit_price) === cents(i.limit_price), 'reconcile_mismatch');
        requireThat(typeof remote.broker_order_id === 'string' && /^fake-order-[0-9]+$/.test(remote.broker_order_id), 'reconcile_mismatch');
        const filled = quantity(remote.filled_quantity, true), total = quantity(i.quantity), notional = cents(remote.filled_notional);
        requireThat(filled >= quantity(known.filled_quantity ?? '0', true) && filled <= total &&
          (remote.status !== 'filled' || filled === total) && (remote.status !== 'partially_filled' || (filled > 0n && filled < total)) &&
          (filled === 0n ? notional === 0n : notional > 0n) && (!['open', 'rejected'].includes(remote.status) || filled === 0n) &&
          (i.side === 'buy' ? notional <= filled * cents(i.limit_price) : notional >= filled * cents(i.limit_price)), 'reconcile_mismatch');
        requireThat(known.status !== 'filled' || remote.status === 'filled', 'reconcile_mismatch');
        Object.assign(known, { status: remote.status, broker_order_id: remote.broker_order_id, filled_quantity: String(filled),
          filled_notional: money(notional), reconcile_failures: 0, last_reconciled_at: now() });
      }
      for (const order of Object.values(state.orders)) if (ACTIVE.includes(order.status) && !found.has(order.key)) {
        order.status = 'unknown'; order.reconcile_failures = (order.reconcile_failures ?? 0) + 1;
        state.strategy_halts[order.intent.strategy_sha256] ??= 'unknown_order';
        if (order.reconcile_failures >= 2) state.global_halt ??= 'reconcile_mismatch';
      }
      const owned = {}, aggregate = {}, remotePositions = {};
      for (const order of Object.values(state.orders)) {
        const filled = quantity(order.filled_quantity ?? '0', true); if (!filled) continue;
        const i = order.intent, signed = i.side === 'buy' ? filled : -filled;
        owned[i.strategy_sha256] ??= { symbol: i.symbol, quantity: 0n };
        requireThat(owned[i.strategy_sha256].symbol === i.symbol, 'reconcile_mismatch');
        owned[i.strategy_sha256].quantity += signed;
        aggregate[i.symbol] = (aggregate[i.symbol] ?? 0n) + signed;
      }
      for (const p of snapshot.positions) {
        requireThat(!Object.hasOwn(remotePositions, p.symbol), 'reconcile_mismatch');
        remotePositions[p.symbol] = p;
        requireThat(quantity(p.quantity) === (aggregate[p.symbol] ?? 0n) && cents(p.mark_price) > 0n, 'reconcile_mismatch');
      }
      for (const [symbol, held] of Object.entries(aggregate)) requireThat(held >= 0n && (held === 0n || remotePositions[symbol]), 'reconcile_mismatch');
      state.positions = {};
      for (const [strategy, position] of Object.entries(owned)) {
        requireThat(position.quantity >= 0n, 'reconcile_mismatch');
        if (position.quantity) state.positions[strategy] = { symbol: position.symbol, quantity: String(position.quantity), mark_price: remotePositions[position.symbol].mark_price };
      }
      const session = broker.getSession(now());
      requireThat(snapshot.market_day === session.market_day && snapshot.daily_pnl_by_strategy && typeof snapshot.daily_pnl_by_strategy === 'object', 'reconcile_mismatch');
      let loss = 0n;
      state.daily_pnl = {};
      for (const [strategy, pnl] of Object.entries(snapshot.daily_pnl_by_strategy)) {
        hash(strategy); const value = cents(pnl, true); loss += value; state.daily_pnl[strategy] = money(value);
        if (value <= -cents(cfg.strategy_daily_loss_usd)) state.strategy_halts[strategy] = 'strategy_loss_kill';
      }
      if (loss <= -cents(cfg.daily_loss_kill_usd)) state.global_halt ??= 'daily_loss_kill';
      for (const [strategy, code] of Object.entries(state.strategy_halts)) if (code === 'unknown_order' &&
        !Object.values(state.orders).some(o => o.intent.strategy_sha256 === strategy && ['unknown', 'submitting'].includes(o.status))) delete state.strategy_halts[strategy];
      state.market_day = snapshot.market_day; state.settled_cash = snapshot.settled_cash;
      state.reconcile_failures = 0; state.last_reconciled_at = now();
      journal.commit(state, { at: now(), type: 'account_reconciled', method: 'client_reference_fake' });
    } catch (error) {
      const mismatch = !(error instanceof ExecutionError && error.code === 'broker_unavailable');
      save({ type: 'reconcile_failed', code: mismatch ? 'reconcile_mismatch' : 'broker_unavailable' }, state => {
        state.reconcile_failures++;
        for (const order of Object.values(state.orders)) if (ACTIVE.includes(order.status)) {
          order.status = 'unknown'; state.strategy_halts[order.intent.strategy_sha256] ??= 'unknown_order';
        }
        if (mismatch || state.reconcile_failures >= 2) state.global_halt ??= 'reconcile_mismatch';
      });
      return false;
    }
    if (cancel) await cancelHalted();
    return true;
  }
  function exposures(state) {
    const byStrategy = {};
    for (const [strategy, p] of Object.entries(state.positions)) byStrategy[strategy] = quantity(p.quantity) * cents(p.mark_price);
    for (const order of Object.values(state.orders)) if (ACTIVE.includes(order.status) && order.intent.side === 'buy') {
      const i = order.intent, left = quantity(i.quantity) - quantity(order.filled_quantity ?? '0', true);
      byStrategy[i.strategy_sha256] = (byStrategy[i.strategy_sha256] ?? 0n) + left * cents(i.limit_price);
    }
    return { byStrategy, total: Object.values(byStrategy).reduce((a, b) => a + b, 0n) };
  }
  async function submitInternal(value) {
    await manualHalt();
    if (journal.state.global_halt) await cancelHalted();
    let intent;
    try { intent = normalizeIntent(value, accountId); }
    catch (error) {
      const code = error instanceof ExecutionError ? error.code : 'invalid_intent';
      save({ type: 'invalid_intent_refused', code });
      return { status: 'refused', code };
    }
    const key = idempotencyKey(intent), payloadHash = digest(intent);
    const prior = journal.state.orders[key];
    if (prior) {
      if (prior.intent_sha256 !== payloadHash) { save({ type: 'idempotency_conflict', key }); return { key, status: 'refused', code: 'idempotency_conflict' }; }
      return view(key);
    }
    save({ type: 'intent_prepared', key, intent }, state => {
      state.orders[key] = { key, intent, intent_sha256: payloadHash, status: 'prepared', prepared_at: now(), filled_quantity: '0', filled_notional: '0.00' };
    });
    if (env.ALGO_DESK_ALLOW_LIVE_ORDERS !== '1') return refuse(key, 'live_disabled', { dry_run: true });
    let authorization;
    try { authorization = await authorize(intent); } catch { return refuse(key, 'not_approved'); }
    if (!Object.hasOwn(budgets, intent.strategy_sha256)) return refuse(key, 'not_allocated');
    if (intent.decision_bar_close_time > now()) return refuse(key, 'future_intent');
    const session = broker.getSession(now()), time = Date.parse(now());
    if (!session.regular_session || time < Date.parse(session.open_at) + 300000 || time >= Date.parse(session.close_at) - 300000) return refuse(key, 'market_closed');
    if (!await reconcileInternal()) return refuse(key, journal.state.global_halt ?? 'reconcile_unavailable');
    let state = journal.state;
    if (state.global_halt) return refuse(key, state.global_halt);
    if (state.strategy_halts[intent.strategy_sha256]) return refuse(key, state.strategy_halts[intent.strategy_sha256]);
    if (!await approvals()) return refuse(key, 'approvals_not_verified');
    if (state.last_decision_time && intent.decision_bar_close_time < state.last_decision_time) return refuse(key, 'out_of_order_intent');
    const notional = quantity(intent.quantity) * cents(intent.limit_price), checks = { order_usd: money(notional), ...cfg };
    if (notional > cents(cfg.per_order_max_usd)) return refuse(key, 'order_cap', checks);
    const held = state.positions[intent.strategy_sha256];
    const active = Object.values(state.orders).filter(o => ACTIVE.includes(o.status) && o.intent.strategy_sha256 === intent.strategy_sha256);
    if (intent.side === 'buy' && (held || active.length)) return refuse(key, 'short_not_allowed', checks);
    const reservedSell = active.filter(o => o.intent.side === 'sell').reduce((n, o) => n + quantity(o.intent.quantity) - quantity(o.filled_quantity, true), 0n);
    if (intent.side === 'sell' && (!held || held.symbol !== intent.symbol || quantity(intent.quantity) + reservedSell > quantity(held.quantity))) return refuse(key, 'short_not_allowed', checks);
    const exposure = exposures(state), additional = intent.side === 'buy' ? notional : 0n;
    checks.strategy_exposure_usd = money((exposure.byStrategy[intent.strategy_sha256] ?? 0n) + additional);
    checks.total_exposure_usd = money(exposure.total + additional); checks.allocation_usd = budgets[intent.strategy_sha256]; checks.settled_cash = state.settled_cash;
    if ((exposure.byStrategy[intent.strategy_sha256] ?? 0n) + additional > cents(cfg.per_strategy_max_exposure_usd)) return refuse(key, 'strategy_exposure_cap', checks);
    if ((exposure.byStrategy[intent.strategy_sha256] ?? 0n) + additional > cents(budgets[intent.strategy_sha256])) return refuse(key, 'strategy_allocation_cap', checks);
    if (exposure.total + additional > cents(cfg.total_max_exposure_usd) || (intent.side === 'buy' && exposure.total + additional > cents(state.settled_cash))) return refuse(key, 'total_exposure_cap', checks);
    const count = state.daily_counts[session.market_day]?.[intent.strategy_sha256] ?? 0;
    if (count >= cfg.max_orders_per_strategy_per_day) return refuse(key, 'order_count_cap', checks);
    let quote;
    try { quote = await call(() => broker.getQuote(accountId, intent.symbol)); }
    catch { return refuse(key, 'quote_unavailable', checks); }
    const submitAt = now();
    try {
      requireThat(quote.symbol === intent.symbol && quote.quote_at !== null && stamp(quote.quote_at) <= submitAt && stamp(quote.available_at) <= submitAt, 'stale_quote');
      const age = Date.parse(submitAt) - Date.parse(quote.quote_at); checks.quote_age_ms = age;
      requireThat(age <= cfg.quote_max_age_ms, 'stale_quote');
      const bid = cents(quote.bid), ask = cents(quote.ask), limit = cents(intent.limit_price);
      requireThat(bid > 0n && ask >= bid, 'stale_quote');
      requireThat(bid + ask >= 2n * cents(cfg.min_share_price_usd), 'price_floor');
      const difference = 2n * limit - bid - ask;
      requireThat((difference < 0n ? -difference : difference) * 10000n <= (bid + ask) * BigInt(cfg.limit_collar_bps), 'collar');
    } catch (error) { return refuse(key, ['price_floor', 'collar'].includes(error.code) ? error.code : 'stale_quote', checks); }
    // Reserve capital and record the attempt durably before the broker sees it.
    save({ type: 'order_reserved', key, checks, ...authorization }, s => {
      Object.assign(s.orders[key], { status: 'submitting', checks, ...authorization, submitted_at: submitAt, reconciliation_method: 'client_reference_fake' });
      s.daily_counts[session.market_day] ??= {}; s.daily_counts[session.market_day][intent.strategy_sha256] = count + 1;
      s.last_decision_time = intent.decision_bar_close_time;
    });
    await faultHooks.afterReservation?.(key);
    // Recheck local gates and quote age after disk flush or a delayed handoff.
    await manualHalt();
    if (journal.state.global_halt) { await cancelHalted(); return refuse(key, journal.state.global_halt); }
    if (env.ALGO_DESK_ALLOW_LIVE_ORDERS !== '1') return refuse(key, 'live_disabled', checks);
    try { await authorize(intent); } catch { return refuse(key, 'not_approved', checks); }
    if (Date.parse(now()) - Date.parse(quote.quote_at) > cfg.quote_max_age_ms) return refuse(key, 'stale_quote', checks);
    const finalSession = broker.getSession(now());
    if (!finalSession.regular_session || Date.parse(now()) < Date.parse(finalSession.open_at) + 300000 || Date.parse(now()) >= Date.parse(finalSession.close_at) - 300000) return refuse(key, 'market_closed', checks);
    let response;
    try {
      response = await call(() => broker.placeOrder(accountId, { client_reference: key, strategy_sha256: intent.strategy_sha256,
        symbol: intent.symbol, side: intent.side, quantity: intent.quantity, limit_price: intent.limit_price, order_type: 'limit' }));
    } catch { response = null; }
    await faultHooks.afterBrokerSubmit?.(key);
    if (!response || typeof response.broker_order_id !== 'string') {
      save({ type: 'submission_unknown', key }, s => { s.orders[key].status = 'unknown'; s.strategy_halts[intent.strategy_sha256] ??= 'unknown_order'; });
      return view(key);
    }
    // An acknowledgement is never used as a fill or held-quantity update.
    await reconcileInternal();
    return view(key);
  }
  try {
    save({ type: 'service_opened', mode: 'fake_only' });
    await approvals(); await reconcileInternal({ startup: true });
  } catch (error) { await journal.close(); throw error; }
  return Object.freeze({ mode: 'fake_only', policy_sha256: policyHash,
    submit: intent => serialize(() => submitInternal(intent)),
    submitBatch: intents => serialize(async () => {
      requireThat(Array.isArray(intents) && intents.length <= 100, 'invalid_batch');
      const ordered = [...intents].sort((a, b) => String(a?.decision_bar_close_time).localeCompare(String(b?.decision_bar_close_time)) || String(a?.strategy_sha256).localeCompare(String(b?.strategy_sha256)) || (a.intent_seq ?? 0) - (b.intent_seq ?? 0));
      const results = []; for (const intent of ordered) results.push(await submitInternal(intent)); return results;
    }),
    reconcile: () => serialize(() => reconcileInternal()),
    snapshot: () => journal.state, journalEvents: () => journal.events(),
    async close() { if (closing) return; closing = true; await tail; await journal.close(); }
  });
}
