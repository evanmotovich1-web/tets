import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, writeFile, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { spawnSync } from 'node:child_process';
import { mkdir } from 'node:fs/promises';
import { openExecutionService, FakeBroker, DisabledRobinhoodOrderAdapter, DEFAULT_LIMITS, policyFingerprint,
  idempotencyKey, sealRecord } from '../../runtime/execution/index.mjs';
import { digest } from '../../runtime/execution/contracts.mjs';

const A = 'a'.repeat(64), B = 'b'.repeat(64), C = 'c'.repeat(64);
const ACCOUNT = 'FAKE-ACCOUNT-1234';
async function setup(t, options = {}) {
  const root = await mkdtemp(join(tmpdir(), 'execution-2d-'));
  const time = { value: options.time ?? '2024-01-08T15:00:00.000Z' }, clock = () => time.value;
  const broker = new FakeBroker({ accountId: ACCOUNT, clock, cash: options.cash ?? '1000.00', quotes: options.quotes,
    session: options.session });
  const limits = options.limits ?? {}, allocation = options.allocations ?? Object.fromEntries([A, B, C].map(key => [key, limits.per_strategy_max_exposure_usd ?? '100.00']));
  const policy = policyFingerprint({ accountId: ACCOUNT, limits, allocations: allocation });
  const records = Object.fromEntries([A, B, C].map(strategy => [strategy, {
    promotion: sealRecord({ kind: 'promotion', stage: 'eligible_for_live', author_role: 'referee', strategy_sha256: strategy,
      symbol: 'SPY', created_at: '2024-01-01T13:00:00.000Z', revoked: false }),
    approval: sealRecord({ kind: 'evan_approval', approved_by: 'Evan', strategy_sha256: strategy, account_sha256: digest(ACCOUNT),
      policy_sha256: policy, created_at: '2024-01-01T13:00:00.000Z', expires_at: '2024-01-12T00:00:00.000Z', revoked: false })
  }]));
  const env = { ALGO_DESK_ALLOW_LIVE_ORDERS: options.live ?? '1' };
  const config = { stateDir: root, accountId: ACCOUNT, broker, clock, env, limits, allocations: allocation,
    authorizationSource: async strategy => records[strategy], faultHooks: options.faultHooks ?? {} };
  const services = [];
  t.after(async () => { for (const service of services) await service.close(); await rm(root, { recursive: true, force: true }); });
  const open = async extra => { const service = await openExecutionService({ ...config, ...extra }); services.push(service); return service; };
  const service = await open();
  const intent = (strategy = A, extra = {}) => ({ strategy_sha256: strategy,
    decision_bar_close_time: new Date(Date.parse(time.value) - 60000).toISOString(), side: 'buy', intent_seq: 0,
    account_id: ACCOUNT, symbol: 'SPY', quantity: '2', limit_price: '10.00', ...extra });
  return { root, broker, service, config, records, env, time, intent, open };
}

test('execution refuses without the exact live flag and records a dry-run intent', async t => {
  const f = await setup(t, { live: '0' });
  const result = await f.service.submit(f.intent());
  assert.equal(result.code, 'live_disabled'); assert.equal(result.checks.dry_run, true);
  assert.equal(f.broker.placeCalls, 0);
  assert.equal(f.service.journalEvents().some(e => e.type === 'intent_prepared'), true);
  assert.equal(f.env.ALGO_DESK_ALLOW_LIVE_ORDERS, '0');
});

test('promotion and separate current human approval must match hash, account and caps', async t => {
  const f = await setup(t);
  const original = structuredClone(f.records[A]);
  const mutations = [
    record => { delete record.promotion; }, record => { delete record.approval; },
    record => { record.promotion = sealRecord({ ...record.promotion, record_sha256: undefined, stage: 'research_only' }); },
    record => { const { record_sha256, ...body } = record.approval; record.approval = sealRecord({ ...body, revoked: true }); },
    record => { const { record_sha256, ...body } = record.approval; record.approval = sealRecord({ ...body, expires_at: f.time.value }); },
    record => { const { record_sha256, ...body } = record.approval; record.approval = sealRecord({ ...body, policy_sha256: '0'.repeat(64) }); }
  ];
  let seq = 0;
  for (const mutate of mutations) {
    const record = structuredClone(original); mutate(record); f.records[A] = record;
    assert.equal((await f.service.submit(f.intent(A, { intent_seq: seq++ }))).code, 'not_approved');
  }
  assert.equal(f.broker.placeCalls, 0);
});

test('per-order, strategy, allocation, total and settled-cash caps are enforced in the service', async t => {
  for (const [options, intents, expected] of [
    [{}, f => [f.intent(A, { quantity: '3' })], 'order_cap'],
    [{ limits: { per_strategy_max_exposure_usd: '15.00' } }, f => [f.intent()], 'strategy_exposure_cap'],
    [{ allocations: { [A]: '15.00' } }, f => [f.intent()], 'strategy_allocation_cap'],
    [{ limits: { total_max_exposure_usd: '30.00' } }, f => [f.intent(A), f.intent(B)], 'total_exposure_cap'],
    [{ cash: '25.00' }, f => [f.intent(A), f.intent(B)], 'total_exposure_cap']
  ]) await t.test(expected, async t => {
    const f = await setup(t, options); const results = [];
    for (const value of intents(f)) results.push(await f.service.submit(value));
    assert.equal(results.at(-1).code, expected);
    assert.ok(f.broker.placeCalls <= 1);
  });
});

test('central allocation sorts batch decisions and reserves open-order capital', async t => {
  const f = await setup(t, { limits: { total_max_exposure_usd: '25.00' } });
  const earlier = f.intent(B, { decision_bar_close_time: '2024-01-08T14:58:00.000Z' });
  const later = f.intent(A);
  const [first, second] = await f.service.submitBatch([later, earlier]);
  assert.equal(first.intent.strategy_sha256, B); assert.equal(first.status, 'open');
  assert.equal(second.code, 'total_exposure_cap'); assert.equal(f.broker.placeCalls, 1);
});

test('a later-arriving older signal cannot take previously allocated capital', async t => {
  const f = await setup(t);
  await f.service.submit(f.intent(A));
  const result = await f.service.submit(f.intent(B, { decision_bar_close_time: '2024-01-08T14:58:00.000Z' }));
  assert.equal(result.code, 'out_of_order_intent'); assert.equal(f.broker.placeCalls, 1);
});

test('duplicate and concurrent identical signals submit exactly once', async t => {
  const f = await setup(t), intent = f.intent();
  const results = await Promise.all([f.service.submit(intent), f.service.submit(intent), f.service.submit(intent)]);
  assert.equal(new Set(results.map(r => r.key)).size, 1); assert.equal(f.broker.placeCalls, 1);
  assert.equal(results[0].key, idempotencyKey(intent));
  const changed = await f.service.submit({ ...intent, quantity: '1' });
  assert.equal(changed.code, 'idempotency_conflict'); assert.equal(f.broker.placeCalls, 1);
});

test('only the configured account and long-only owned shares are accepted', async t => {
  const f = await setup(t);
  assert.equal((await f.service.submit(f.intent(A, { account_id: 'OTHER-ACCOUNT-9999' }))).code, 'wrong_account');
  assert.equal((await f.service.submit(f.intent(A, { side: 'sell' }))).code, 'short_not_allowed');
  f.broker.queueFault({ type: 'fill' });
  const buy = await f.service.submit(f.intent(A, { intent_seq: 1 })); assert.equal(buy.status, 'filled');
  assert.equal((await f.service.submit(f.intent(B, { side: 'sell' }))).code, 'short_not_allowed');
  assert.equal((await f.service.submit(f.intent(A, { side: 'sell', quantity: '3', limit_price: '5.00', intent_seq: 1 }))).code, 'short_not_allowed');
  assert.equal((await f.service.submit(f.intent(A, { intent_seq: 2 }))).code, 'short_not_allowed');
  assert.equal(f.broker.placeCalls, 1);
});

test('partial fills update owned quantity only from reconciliation', async t => {
  const f = await setup(t); f.broker.queueFault({ type: 'partial_fill', quantity: '1' });
  const order = await f.service.submit(f.intent());
  assert.equal(order.status, 'partially_filled'); assert.equal(order.filled_quantity, '1');
  assert.equal(f.service.snapshot().positions[A].quantity, '1');
  assert.equal((await f.service.submit(f.intent(A, { side: 'sell', intent_seq: 1, quantity: '2' }))).code, 'short_not_allowed');
  f.broker.fill(order.key, '1', '10.00'); await f.service.reconcile();
  assert.equal(f.service.snapshot().positions[A].quantity, '2');
});

test('pending sells reserve held shares and cannot create a short', async t => {
  const f = await setup(t); f.broker.queueFault({ type: 'fill' });
  await f.service.submit(f.intent());
  const sell = await f.service.submit(f.intent(A, { side: 'sell', intent_seq: 1, quantity: '1' }));
  assert.equal(sell.status, 'open');
  assert.equal((await f.service.submit(f.intent(A, { side: 'sell', intent_seq: 2, quantity: '2' }))).code, 'short_not_allowed');
});

test('stale, missing and future quote timestamps block submission', async t => {
  for (const quote_at of ['2024-01-08T14:59:57.000Z', null, '2024-01-08T15:00:01.000Z']) await t.test(String(quote_at), async t => {
    const f = await setup(t);
    if (quote_at === null) f.broker.getQuote = async () => ({ symbol: 'SPY', bid: '10.00', ask: '10.00', quote_at: null, available_at: f.time.value });
    else f.broker.setQuote('SPY', { bid: '10.00', ask: '10.00', quote_at });
    assert.equal((await f.service.submit(f.intent())).code, 'stale_quote'); assert.equal(f.broker.placeCalls, 0);
  });
});

test('quote age is rechecked after durable reservation delays', async t => {
  const f = await setup(t);
  const delayed = await f.open({ stateDir: join(f.root, 'delayed'), faultHooks: { afterReservation() { f.time.value = '2024-01-08T15:00:03.000Z'; } } });
  assert.equal((await delayed.submit(f.intent())).code, 'stale_quote'); assert.equal(f.broker.placeCalls, 0);
});

test('market session, first/last five minutes and approval setting are checked', async t => {
  for (const time of ['2024-01-08T14:34:00.000Z', '2024-01-08T20:55:00.000Z', '2024-01-06T15:00:00.000Z']) await t.test(time, async t => {
    const f = await setup(t, { time });
    assert.equal((await f.service.submit(f.intent())).code, 'market_closed'); assert.equal(f.broker.placeCalls, 0);
  });
  await t.test('approvals off', async t => {
    const f = await setup(t); f.broker.setApprovals(false);
    assert.equal((await f.service.submit(f.intent())).code, 'approvals_not_verified'); assert.equal(f.broker.placeCalls, 0);
  });
});

test('price floor and limit collar cannot be changed by an intent', async t => {
  const f = await setup(t);
  f.broker.setQuote('SPY', { bid: '4.99', ask: '4.99' });
  assert.equal((await f.service.submit(f.intent(A, { limit_price: '5.00', intent_seq: 1 }))).code, 'price_floor');
  f.broker.setQuote('SPY', { bid: '10.00', ask: '10.00' });
  assert.equal((await f.service.submit(f.intent(A, { limit_price: '10.06', intent_seq: 2 }))).code, 'collar');
  assert.equal((await f.service.submit({ ...f.intent(A, { intent_seq: 3 }), limits: { per_order_max_usd: '1000.00' } })).code, 'invalid_intent');
  await assert.rejects(f.open({ stateDir: join(f.root, 'raised'), limits: { per_order_max_usd: '26.00' } }), { code: 'limit_raise_not_approved' });
  assert.equal(f.broker.placeCalls, 0);
});

test('per-strategy daily order count persists through filled buy/sell cycles', async t => {
  const f = await setup(t, { limits: { max_orders_per_strategy_per_day: 2 } });
  f.broker.queueFault({ type: 'fill' }); await f.service.submit(f.intent());
  f.broker.queueFault({ type: 'fill' }); await f.service.submit(f.intent(A, { side: 'sell' }));
  assert.equal((await f.service.submit(f.intent(A, { intent_seq: 1 }))).code, 'order_count_cap');
  assert.equal(f.broker.placeCalls, 2);
});

test('strategy loss kill cancels that strategy and persists after loss improves', async t => {
  const f = await setup(t); await f.service.submit(f.intent());
  f.broker.setDailyPnl(A, '-10.00'); await f.service.reconcile();
  assert.equal(f.service.snapshot().strategy_halts[A], 'strategy_loss_kill');
  assert.equal(f.broker.cancelCalls, 1); f.broker.setDailyPnl(A, '0.00');
  assert.equal((await f.service.submit(f.intent(A, { intent_seq: 1 }))).code, 'strategy_loss_kill');
  assert.equal((await f.service.submit(f.intent(B))).status, 'open');
});

test('loss checks include fake venue marked losses and realized sale losses', async t => {
  await t.test('unrealized', async t => {
    const f = await setup(t); f.broker.queueFault({ type: 'fill' }); await f.service.submit(f.intent());
    f.broker.setQuote('SPY', { bid: '5.00', ask: '5.00' }); await f.service.reconcile();
    assert.equal(f.service.snapshot().daily_pnl[A], '-10.00');
    assert.equal(f.service.snapshot().strategy_halts[A], 'strategy_loss_kill');
  });
  await t.test('realized', async t => {
    const f = await setup(t); f.broker.queueFault({ type: 'fill' }); await f.service.submit(f.intent(A, { quantity: '1' }));
    f.broker.setQuote('SPY', { bid: '7.00', ask: '7.00' });
    f.broker.queueFault({ type: 'fill' }); await f.service.submit(f.intent(A, { side: 'sell', quantity: '1', limit_price: '7.00' }));
    assert.equal(f.service.snapshot().daily_pnl[A], '-3.00'); assert.equal(f.service.snapshot().positions[A], undefined);
  });
});

test('global daily loss kill cancels open orders and never flattens held positions', async t => {
  const f = await setup(t); f.broker.queueFault({ type: 'fill' }); await f.service.submit(f.intent());
  await f.service.submit(f.intent(B)); f.broker.setDailyPnl(A, '-13.00'); f.broker.setDailyPnl(B, '-12.00');
  await f.service.reconcile();
  assert.equal(f.service.snapshot().global_halt, 'daily_loss_kill');
  assert.equal(f.broker.cancelCalls, 1); assert.equal(f.service.snapshot().positions[A].quantity, '2');
  assert.equal(f.broker.placeCalls, 2);
  f.broker.setDailyPnl(A, '0.00'); f.broker.setDailyPnl(B, '0.00');
  await f.service.close(); const restarted = await f.open();
  assert.equal((await restarted.submit(f.intent(C))).code, 'daily_loss_kill');
  assert.equal(f.broker.placeCalls, 2);
});

test('HALT file cancels open orders and remains latched after file removal', async t => {
  const f = await setup(t); await f.service.submit(f.intent());
  await writeFile(join(f.root, 'HALT'), 'operator halt\n');
  assert.equal((await f.service.submit(f.intent(B))).code, 'manual_halt');
  assert.equal(f.broker.cancelCalls, 1);
  await rm(join(f.root, 'HALT'));
  assert.equal((await f.service.submit(f.intent(C))).code, 'manual_halt');
});

test('HALT is checked even when the incoming signal is a duplicate', async t => {
  const f = await setup(t), value = f.intent(); await f.service.submit(value);
  await writeFile(join(f.root, 'HALT'), 'operator halt\n');
  assert.equal((await f.service.submit(value)).status, 'cancelled');
  assert.equal(f.service.snapshot().global_halt, 'manual_halt'); assert.equal(f.broker.cancelCalls, 1);
});

test('submission timeout retains reservation and duplicate keys never resubmit', async t => {
  const f = await setup(t); f.broker.queueFault({ type: 'timeout_after_accept' });
  const result = await f.service.submit(f.intent());
  assert.equal(result.status, 'unknown'); assert.equal(f.broker.placeCalls, 1);
  assert.equal((await f.service.submit(f.intent())).status, 'unknown');
  assert.equal(f.broker.placeCalls, 1);
  await f.service.reconcile();
  assert.equal(f.service.snapshot().orders[result.key].status, 'open');
  assert.equal((await f.service.submit(f.intent())).status, 'open'); assert.equal(f.broker.placeCalls, 1);
});

test('timeout after fill is recovered from actual broker positions', async t => {
  const f = await setup(t); f.broker.queueFault({ type: 'timeout_after_fill' });
  const result = await f.service.submit(f.intent()); assert.equal(result.status, 'unknown');
  assert.equal(f.service.snapshot().positions[A], undefined);
  await f.service.reconcile(); assert.equal(f.service.snapshot().positions[A].quantity, '2');
  assert.equal(f.broker.placeCalls, 1);
});

test('an actual bounded response timeout leaves the key unknown without resubmission', async t => {
  const f = await setup(t), place = f.broker.placeOrder.bind(f.broker);
  f.broker.placeOrder = async (...args) => { await place(...args); return new Promise(() => {}); };
  const service = await f.open({ stateDir: join(f.root, 'timeout'), timeoutMs: 20 });
  const result = await service.submit(f.intent()); assert.equal(result.status, 'unknown');
  assert.equal(f.broker.placeCalls, 1); await service.reconcile();
  assert.equal((await service.submit(f.intent())).status, 'open'); assert.equal(f.broker.placeCalls, 1);
});

test('an ambiguous submission response is unknown until reconciled', async t => {
  const f = await setup(t); f.broker.queueFault({ type: 'unknown_response' });
  assert.equal((await f.service.submit(f.intent())).status, 'unknown');
  assert.equal((await f.service.submit(f.intent())).status, 'unknown'); assert.equal(f.broker.placeCalls, 1);
  await f.service.reconcile(); assert.equal((await f.service.submit(f.intent())).status, 'open');
  assert.equal(f.broker.placeCalls, 1);
});

test('unknown missing order blocks its strategy and two unresolved reconciliations halt all', async t => {
  const f = await setup(t); f.broker.queueFault({ type: 'disconnect_before_accept' });
  const order = await f.service.submit(f.intent()); assert.equal(order.status, 'unknown');
  await f.service.reconcile(); assert.equal(f.service.snapshot().strategy_halts[A], 'unknown_order');
  await f.service.reconcile(); assert.equal(f.service.snapshot().global_halt, 'reconcile_mismatch');
  assert.equal((await f.service.submit(f.intent(A))).status, 'unknown');
  assert.equal((await f.service.submit(f.intent(B))).code, 'reconcile_mismatch'); assert.equal(f.broker.placeCalls, 1);
});

test('disconnect during reconciliation fails closed and two failures latch a halt', async t => {
  const f = await setup(t); await f.service.submit(f.intent()); f.broker.disconnect();
  assert.equal(await f.service.reconcile(), false); assert.equal(await f.service.reconcile(), false);
  assert.equal(f.service.snapshot().global_halt, 'reconcile_mismatch');
  f.broker.reconnect(); await f.service.reconcile(); assert.equal(f.broker.cancelCalls, 1);
  assert.equal((await f.service.submit(f.intent(B))).code, 'reconcile_mismatch');
});

test('an unknown account order or position halts all strategies', async t => {
  for (const kind of ['order', 'position']) await t.test(kind, async t => {
    const f = await setup(t);
    if (kind === 'position') f.broker.addUntrackedPosition({ symbol: 'MSFT', quantity: '1', mark_price: '10.00' });
    else await f.broker.placeOrder(ACCOUNT, { client_reference: 'f'.repeat(64), strategy_sha256: A, symbol: 'SPY', side: 'buy', quantity: '1', limit_price: '10.00' });
    await f.service.reconcile(); assert.equal(f.service.snapshot().global_halt, 'reconcile_mismatch');
    assert.equal((await f.service.submit(f.intent())).code, 'reconcile_mismatch');
  });
});

test('intent and capital reservation are committed before broker submission', async t => {
  const f = await setup(t);
  const guarded = await f.open({ stateDir: join(f.root, 'guarded'), faultHooks: { afterReservation(key) {
    assert.equal(f.broker.placeCalls, 0);
    const db = new DatabaseSync(join(f.root, 'guarded/orders.sqlite'));
    const state = JSON.parse(db.prepare('SELECT payload FROM execution_state WHERE id=1').get().payload); db.close();
    assert.equal(state.orders[key].status, 'submitting'); assert.equal(state.orders[key].checks.order_usd, '20.00');
  } } });
  assert.equal((await guarded.submit(f.intent())).status, 'open');
});

test('restart reconciles an accepted order before accepting any new intent', async t => {
  const f = await setup(t);
  const path = join(f.root, 'crash');
  const crashed = await f.open({ stateDir: path, faultHooks: { afterBrokerSubmit() { throw new Error('synthetic process interruption'); } } });
  await assert.rejects(crashed.submit(f.intent()), /synthetic process interruption/);
  assert.equal(crashed.snapshot().orders[idempotencyKey(f.intent())].status, 'submitting');
  await crashed.close();
  const reads = f.broker.snapshotReads, restarted = await f.open({ stateDir: path });
  assert.ok(f.broker.snapshotReads > reads);
  assert.equal(restarted.snapshot().orders[idempotencyKey(f.intent())].status, 'open');
  assert.equal((await restarted.submit(f.intent())).status, 'open'); assert.equal(f.broker.placeCalls, 1);
});

test('restart preserves an unresolved pre-send crash without blindly submitting', async t => {
  const f = await setup(t), path = join(f.root, 'presend-crash');
  const crashed = await f.open({ stateDir: path, faultHooks: { afterReservation() { throw new Error('synthetic interruption'); } } });
  await assert.rejects(crashed.submit(f.intent()), /synthetic interruption/); await crashed.close();
  const restarted = await f.open({ stateDir: path });
  assert.equal((await restarted.submit(f.intent())).status, 'unknown'); assert.equal(f.broker.placeCalls, 0);
});

test('one writer and policy identity are enforced across restart', async t => {
  const f = await setup(t);
  await assert.rejects(f.open(), { code: 'execution_writer_locked' });
  await f.service.close();
  await assert.rejects(f.open({ allocations: { [A]: '90.00' } }), { code: 'execution_policy_conflict' });
});

test('an exited process writer lock is recovered without deleting order state', async t => {
  const f = await setup(t), stateDir = join(f.root, 'stale-lock');
  const child = spawnSync(process.execPath, ['-e', ''], { timeout: 1000 }); assert.equal(child.status, 0);
  await mkdir(stateDir); await writeFile(join(stateDir, 'writer.lock'), JSON.stringify({ pid: child.pid, token: 'exited-process' }));
  const service = await f.open({ stateDir }); assert.equal((await service.submit(f.intent())).status, 'open');
  assert.equal(f.broker.placeCalls, 1);
});

test('journal records checks and broker IDs while redacting account numbers', async t => {
  const f = await setup(t); await f.service.submit(f.intent());
  const events = f.service.journalEvents(), serialized = JSON.stringify({ events, state: f.service.snapshot() });
  assert.equal(serialized.includes(ACCOUNT), false); assert.equal(serialized.includes('FAKE-ACCOUNT'), false);
  assert.ok(serialized.includes('1234')); assert.ok(events.some(e => e.type === 'order_reserved' && e.checks.order_usd === '20.00'));
  const disk = await readFile(join(f.root, 'orders.sqlite')); assert.equal(disk.includes(Buffer.from(ACCOUNT)), false);
});

test('tampered journal state is rejected on reopen', async t => {
  const f = await setup(t); await f.service.submit(f.intent()); await f.service.close();
  const db = new DatabaseSync(join(f.root, 'orders.sqlite'));
  const state = JSON.parse(db.prepare('SELECT payload FROM execution_state WHERE id=1').get().payload);
  state.daily_counts = {}; db.prepare('UPDATE execution_state SET payload=?').run(JSON.stringify(state)); db.close();
  await assert.rejects(f.open(), { code: 'journal_integrity_failure' });
});

test('Robinhood order adapter stays disabled even with approvals and live flag', async t => {
  const f = await setup(t), disabled = new DisabledRobinhoodOrderAdapter({ ALGO_DESK_ALLOW_LIVE_ORDERS: '1' });
  assert.equal(disabled.enabled, false);
  await assert.rejects(disabled.placeOrder(ACCOUNT, f.intent()), { code: 'robinhood_execution_disabled' });
  await assert.rejects(f.open({ stateDir: join(f.root, 'real-denied'), broker: disabled }), { code: 'fake_broker_required' });
  assert.equal(DEFAULT_LIMITS.per_order_max_usd, '25.00');
});
