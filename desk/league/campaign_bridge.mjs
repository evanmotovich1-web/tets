import { readFile } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { runCampaign, campaignScreen } from '../runtime/campaign.mjs';
import { candidatesFromFleet } from '../runtime/campaign.mjs';
import { ruleHash } from '../runtime/author-fleet.mjs';
import { pathToFileURL } from 'node:url';

// A host can supply a frozen fleet attempt. No author is invoked here. The
// fleet registry remains the sole provenance source; genomes cannot claim it.
export async function runLeagueCampaign({ plan, stateDir, fleet = null }) {
  if (fleet !== null) {
    const candidates = await candidatesFromFleet({ stateDir: fleet.stateDir, campaignId: fleet.campaignId });
    const candidate = candidates.find(c => c.id === `fleet:${fleet.attempt}`);
    if (!candidate?.strategy || plan.candidates.length !== 1 ||
        ruleHash(candidate.strategy) !== ruleHash(plan.candidates[0].strategy)) throw new Error('fleet_genome_mismatch');
    plan.candidates = [{ ...candidate, id: plan.candidates[0].id }];
  }
  plan.screen = campaignScreen();
  return runCampaign({ plan, stateDir, stateStore: join(stateDir, 'campaign.sqlite') });
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? '').href) {
const [desk, planPath, stateDir] = process.argv.slice(2);
if (resolve(desk) !== fileURLToPath(new URL('..', import.meta.url)).replace(/\/$/, '')) throw new Error('foreign_desk');
const payload = JSON.parse(await readFile(planPath, 'utf8'));
console.log(JSON.stringify(await runLeagueCampaign({ plan: payload.plan ?? payload, fleet: payload.fleet ?? null, stateDir })));
}
