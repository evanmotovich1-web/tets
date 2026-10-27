// One bounded research tick, with a public activity projection for OpenTerminal.
import { resolve } from 'node:path';
import { randomUUID } from 'node:crypto';
import { runTick } from './tick.mjs';
import { publishActivity } from './activity.mjs';

const [state, activity] = process.argv.slice(2);
if (!state || !activity || process.argv.length !== 4) throw new Error('usage: observed-tick.mjs STATE ACTIVITY');
const stateDir = resolve(state);
const result = await runTick({
  eventId: `fixture:${randomUUID()}`, provider: 'fixture', stateDir,
  onSnapshot: job => publishActivity(job, stateDir, resolve(activity)),
});
await publishActivity(result.job, stateDir, resolve(activity));
console.log(JSON.stringify({ state: result.job.state, activity: resolve(activity), research_only: true }));
if (result.job.state !== 'completed') process.exitCode = 1;
