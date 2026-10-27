// One foreground phase of a bounded real-author research proof. No orders or paper path.
import { readFile, writeFile, mkdir, unlink } from 'node:fs/promises';
import { resolve, join, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { DEFAULT_LANES, runAuthorGeneration } from './authors.mjs';
import { readRegistry } from '../runtime/author-fleet.mjs';
import { validateGeneration } from '../runtime/models.mjs';
import { digest, requireThat } from '../runtime/contracts.mjs';
import { workerBridge } from '../runtime/worker.mjs';
import { runCampaign, candidatesFromFleet, campaignScreen } from '../runtime/campaign.mjs';
import { openLedger, ingestFleetAttempts, exportEpisodes } from '../runtime/episodes.mjs';
import { readExposure, globalSearchSize } from '../runtime/exposure.mjs';

const APP = fileURLToPath(new URL('..', import.meta.url));
const read = async path => JSON.parse(await readFile(path, 'utf8'));

export function scopedFixtureAuthor() {
  return { provider:'fixture', model_id:'fixture-fleet-v1', async generate({role,messages}) {
    const context=JSON.parse(messages.at(-1).content),s=context.research_scope;
    requireThat(s?.symbol==='SPY' && s.family==='sma_cross_long' && s.order_type==='market','invalid_research_scope');
    const windows=[[2,9],[4,13],[6,17],[8,21]][context.round];
    requireThat(windows,'fixture_attempt_cap');
    const tool_calls=[{name:'submit_strategy',arguments:{strategy:{schema_version:2,family:s.family,symbol:s.symbol,timeframe:s.timeframe,
      parameters:{fast_window:windows[0],slow_window:windows[1]},position:{quantity:s.quantity},execution:{order_type:'market',limit_offset_bps:'0'},
      rationale:`Fixture research mechanics ${context.round}; host template, never model-written.`}}},
      {name:'submit_preregistration',arguments:{preregistration:context.preregistration_template}}];
    return validateGeneration({provider:'fixture',model_id:'fixture-fleet-v1',tool_calls,usage:{input_tokens:1,output_tokens:1},
      provenance:{transport:'fixture',agent_written:false,model_version:null,raw_response_sha256:digest(tool_calls)}},role);
  } };
}

export async function proofPhase({phase,stateDir,stopAfterFirst=false}) {
  const root=resolve(stateDir),allowed=join(APP,'runtime/.state');
  requireThat(root.startsWith(allowed+sep),'gitignored_state_required');
  const setup=await read(join(root,'setup.json')),campaignId=setup.campaign_id;
  if(phase==='authors') {
    const lanes=JSON.parse(JSON.stringify(DEFAULT_LANES));lanes.max_attempts_per_author=4;lanes.lanes.author_hermes.timeout_ms=50000;
    const worker=workerBridge({stateDir:join(root,'author-worker'),registry:setup.registry});
    let generation;
    try { generation=await runAuthorGeneration({stateDir:root,campaignId,lanes,rounds:1,worker,
      datasetIds:[setup.development.dataset_id],datasetBindings:setup.dataset_bindings,researchScope:setup.research_scope,
      factories:{fixture:()=>scopedFixtureAuthor()}}); }
    finally { await worker.close(); }
    const entries=await readRegistry(join(root,'candidates',campaignId+'.jsonl'));
    const ledger=await openLedger({stateDir:root,campaignId});
    try { await ingestFleetAttempts({ledger,stateDir:root,campaignId,budget:{model_calls:16,backtests:16}}); }
    finally { await ledger.close(); }
    const episodes=await exportEpisodes({stateDir:root,campaignId});
    return {qualification:'research_only',promoting:false,lanes:generation.lanes,attempts:entries.length,episodes,
      authors:Object.fromEntries(Object.keys(lanes.lanes).map(role=>[role,entries.filter(e=>e.author.role===role).length])),
      global_distinct_search_N:globalSearchSize(await readExposure(root))};
  }
  requireThat(phase==='campaign','unknown_proof_phase');
  const candidates=await candidatesFromFleet({stateDir:root,campaignId});requireThat(candidates.length>0,'no_candidates');
  const plan={schema_version:1,kind:'backtest_campaign',campaign_id:campaignId,registry:setup.registry,dataset_id:setup.development.dataset_id,
    evaluation_window:setup.evaluation_window,initial_cash:'10000.000000',cost_model:{commission_bps:'0',slippage_bps_per_side:'5',rounding:'half_even'},
    walkforward:{initial_train_bars:16,test_bars:16},screen:campaignScreen(),limits:{concurrency:1,max_attempts:16,max_wall_ms:30000,job_timeout_ms:10000},candidates};
  const stop=join(root,'campaigns',campaignId,'STOP');
  if(!stopAfterFirst)await unlink(stop).catch(e=>{if(e.code!=='ENOENT')throw e;});
  const summary=await runCampaign({plan,stateDir:root,stateStore:join(root,'campaign.sqlite'),onCheckpoint:async point=>{
    if(stopAfterFirst && point.kind==='result_committed' && point.attempt===0)await writeFile(stop,'operator proof STOP\n');
  }});
  return summary;
}

if(import.meta.url===pathToFileURL(process.argv[1]??'').href) {
  const [phase,stateDir,stop]=process.argv.slice(2);
  proofPhase({phase,stateDir,stopAfterFirst:stop==='stop'}).then(r=>console.log(JSON.stringify(r)))
    .catch(e=>{console.log(JSON.stringify({ok:false,error:e.code??'proof_phase_failed'}));process.exitCode=1;});
}
