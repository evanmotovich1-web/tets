import { DatabaseSync } from 'node:sqlite';
import { mkdir, open, readFile, unlink, chmod } from 'node:fs/promises';
import { join, isAbsolute } from 'node:path';
import { randomUUID } from 'node:crypto';
import canonicalize from 'canonicalize';
import { copy, digest, requireThat } from './contracts.mjs';

export async function openJournal(stateDir, identity) {
  requireThat(typeof stateDir === 'string' && isAbsolute(stateDir), 'state_directory_required');
  await mkdir(stateDir, { recursive: true, mode: 0o700 });
  const lockPath = join(stateDir, 'writer.lock'), token = randomUUID();
  let lock;
  for (let attempt = 0; attempt < 2; attempt++) {
    try { lock = await open(lockPath, 'wx', 0o600); break; } catch (error) {
      if (error.code !== 'EEXIST') throw error;
      let previous;
      try { previous = JSON.parse(await readFile(lockPath, 'utf8')); } catch { requireThat(false, 'execution_writer_locked'); }
      requireThat(Number.isSafeInteger(previous.pid) && previous.pid > 0, 'execution_writer_locked');
      try { process.kill(previous.pid, 0); requireThat(false, 'execution_writer_locked'); }
      catch (probe) { requireThat(probe.code === 'ESRCH', 'execution_writer_locked'); }
      await unlink(lockPath);
    }
  }
  requireThat(lock, 'execution_writer_locked');
  await lock.writeFile(JSON.stringify({ pid: process.pid, token })); await lock.sync(); await lock.close();
  let db;
  async function unlock() {
    try { const value = JSON.parse(await readFile(lockPath, 'utf8')); if (value.token === token) await unlink(lockPath); } catch (error) { if (error.code !== 'ENOENT') throw error; }
  }
  try {
    const dbPath = join(stateDir, 'orders.sqlite');
    db = new DatabaseSync(dbPath);
    await chmod(dbPath, 0o600);
    db.exec('PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA busy_timeout=1000; CREATE TABLE IF NOT EXISTS execution_state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL); CREATE TABLE IF NOT EXISTS execution_events (sequence INTEGER PRIMARY KEY, payload TEXT NOT NULL, event_sha256 TEXT NOT NULL);');
    const stored = db.prepare('SELECT payload FROM execution_state WHERE id=1').get();
    let state = stored ? JSON.parse(stored.payload) : { schema_version: 1, kind: 'fake_execution_state', ...identity,
      orders: {}, positions: {}, daily_pnl: {}, daily_counts: {}, strategy_halts: {}, global_halt: null,
      reconcile_failures: 0, last_decision_time: null, last_reconciled_at: null, sequence: 0, event_sha256: null };
    requireThat(state.account_sha256 === identity.account_sha256 && state.policy_sha256 === identity.policy_sha256, 'execution_policy_conflict');
    if (stored) {
      let last = null, sequence = 0;
      for (const row of db.prepare('SELECT * FROM execution_events ORDER BY sequence').all()) {
        const event = JSON.parse(row.payload);
        requireThat(row.sequence === ++sequence && event.previous_event_sha256 === last && digest(event) === row.event_sha256, 'journal_integrity_failure');
        last = row.event_sha256;
      }
      requireThat(sequence === state.sequence && last === state.event_sha256, 'journal_integrity_failure');
      const tail = db.prepare('SELECT payload FROM execution_events ORDER BY sequence DESC LIMIT 1').get();
      const { sequence: _seq, event_sha256: _sha, ...body } = state;
      requireThat(tail && JSON.parse(tail.payload).state_sha256 === digest(body), 'journal_integrity_failure');
    }
    return {
      get state() { return copy(state); },
      commit(next, event) {
        const { sequence: _seq, event_sha256: _sha, ...body } = copy(next);
        const payload = { sequence: state.sequence + 1, previous_event_sha256: state.event_sha256, ...event, state_sha256: digest(body) };
        const eventHash = digest(payload);
        next = { ...body, sequence: payload.sequence, event_sha256: eventHash };
        db.exec('BEGIN IMMEDIATE');
        try {
          db.prepare('INSERT INTO execution_events VALUES (?, ?, ?)').run(payload.sequence, canonicalize(payload), eventHash);
          db.prepare('INSERT INTO execution_state VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload').run(canonicalize(next));
          db.exec('COMMIT'); state = next;
        } catch (error) { db.exec('ROLLBACK'); throw error; }
      },
      events() { return db.prepare('SELECT payload, event_sha256 FROM execution_events ORDER BY sequence').all().map(row => ({ ...JSON.parse(row.payload), event_sha256: row.event_sha256 })); },
      async close() { db.close(); await unlock(); }
    };
  } catch (error) { db?.close(); await unlock(); throw error; }
}
