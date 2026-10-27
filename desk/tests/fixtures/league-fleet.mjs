import { runFleet, fixtureFleetAdapter } from '../../runtime/author-fleet.mjs';
const [stateDir, raw] = process.argv.slice(2);
const base = fixtureFleetAdapter();
const adapter = { ...base, async generate(input) {
  const output = await base.generate(input);
  if (input.role === 'author_pi') output.tool_calls[0].arguments.strategy = JSON.parse(raw);
  return output;
} };
console.log(JSON.stringify(await runFleet({ stateDir, campaignId: 'league-fleet-proof', rounds: 1, adapter })));
