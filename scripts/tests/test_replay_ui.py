"""Replay controls use full saved datasets, never generated market history."""
from pathlib import Path
import subprocess
import unittest

PAGE = Path(__file__).resolve().parents[1] / 'status.html'


class ReplayUITests(unittest.TestCase):
    def run_js(self, assertions):
        page = PAGE.read_text()
        self.assertIn('// REPLAY_CONTROLLER_START', page, 'Real saved-data replay controller is missing')
        source = page.split('// REPLAY_CONTROLLER_START', 1)[1].split('// REPLAY_CONTROLLER_END', 1)[0]
        harness = "const assert=require('node:assert/strict');\n" + source + '\n(async()=>{\n' + assertions + '\n})().catch(e=>{console.error(e);process.exitCode=1});'
        result = subprocess.run(['node', '-e', harness], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_replay_steps_through_entire_dataset_and_stops_at_end(self):
        self.run_js('''
const replay=createReplayController();
replay.load({bars:Array.from({length:1950},(_,i)=>({timestamp:new Date(60000*i).toISOString()})),fills:[]});
assert.equal(replay.cursor,0);
replay.seek(1948); replay.step(1); assert.equal(replay.cursor,1949);
replay.step(1); assert.equal(replay.cursor,1949);
replay.step(-1); assert.equal(replay.cursor,1948);
replay.seek(-5); assert.equal(replay.cursor,0);
''')


    def test_playback_uses_real_bar_duration_and_changes_speed_without_skipping(self):
        self.run_js('''
let callback,delay,cleared=0,changes=0;
const replay=createReplayController({schedule:(fn,ms)=>{callback=fn;delay=ms;return 1},cancel:()=>cleared++,change:()=>changes++});
replay.load({bars:[{timestamp:'2026-10-20T13:30:00Z'},{timestamp:'2026-10-20T13:31:00Z'},{timestamp:'2026-10-21T13:30:00Z'}],fills:[]});
replay.speed=1; replay.play(); assert.equal(delay,60000);
callback(); assert.equal(replay.cursor,1); assert.equal(delay,60000);
replay.setSpeed(120); assert.equal(delay,500);
callback(); assert.equal(replay.cursor,2); assert.equal(replay.playing,false);
replay.play(); assert.equal(replay.cursor,0);
replay.pause(); assert.equal(replay.playing,false); assert.ok(cleared>0);
assert.ok(changes>0);
''')


    def test_apply_commits_valid_settings_only_after_success(self):
        self.run_js('''
const calls=[];
const state=createReplaySession(async payload=>{calls.push(payload);if(payload.capital===999)throw Error('Rejected');return {bars:[{timestamp:'2026-10-20T13:30:00Z'}],fills:[],settings:payload};});
state.draft={strategy_id:'saved-sma',dataset_id:'spy-saved',capital:'25000',slippage_bps:'12'};
await state.apply(); assert.equal(calls[0].capital,25000); assert.equal(calls[0].slippage_bps,12);
assert.equal(state.applied.capital,25000);
state.draft.capital='30000'; assert.equal(state.applied.capital,25000);assert.equal(state.dirty(),true);
state.draft.slippage_bps='-1'; await assert.rejects(()=>state.apply());assert.equal(calls.length,1);
state.draft.slippage_bps='12';state.draft.capital='999';await assert.rejects(()=>state.apply());
assert.equal(state.applied.capital,25000);assert.equal(state.busy,false);
state.draft.capital='';await assert.rejects(()=>state.apply());
''')


    def test_view_shows_only_cursor_time_fills_and_accounts_for_costs(self):
        self.run_js('''
const data={settings:{capital:10000},bars:[{equity:10000,cash:10000,position:0},{equity:9998,cash:8998,position:10},{equity:10020,cash:10020,position:0}],fills:[{bar_index:1,slippage_cost:2,fee:0},{bar_index:2,slippage_cost:3,fee:1}]};
let view=replaySnapshot(data,0);assert.equal(view.fills.length,0);assert.equal(view.pnl,0);
view=replaySnapshot(data,1);assert.equal(view.fills.length,1);assert.equal(view.pnl,-2);assert.equal(view.slippage,2);assert.equal(view.fees,0);
view=replaySnapshot(data,2);assert.equal(view.fills.length,2);assert.equal(view.slippage,5);assert.equal(view.fees,1);
''')

    def test_replay_ui_has_sidebar_settings_transport_and_no_generated_results(self):
        page=PAGE.read_text()
        for marker in ['bt-strategy-list', 'bt-capital', 'bt-slippage', 'bt-apply', 'bt-play', 'bt-prev', 'bt-next', 'bt-speed', 'bt-seek', 'bt-applied', 'bt-error']:
            self.assertTrue(marker in page, marker+' is missing')
        self.assertNotIn('const OT_PREVIOUS_ALGOS=',page)
        self.assertNotIn('function buildMinuteSessionData',page)
        self.assertIn('/api/backtest/library',page)
        self.assertIn('/api/backtest/run',page)


    def test_rendered_controls_apply_step_filter_and_preserve_unapplied_settings(self):
        harness=r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const elements=new Map();
function element(tag='div'){return {tagName:tag.toUpperCase(),textContent:'',value:'',hidden:false,disabled:false,style:{},children:[],append(...nodes){this.children.push(...nodes)},replaceChildren(...nodes){this.children=[...nodes]},setAttribute(k,v){this[k]=v}};}
const get=id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id)};
const strategies=[{id:'sma',name:'Saved SMA',family:'sma_cross_long',available:true},{id:'unavailable',name:'Unavailable options',family:'options',available:false,reason:'No options data'}];
let requests=[];
const context={document:{body:{},getElementById:get,createElement:element},AbortController,URL,console,setTimeout(){return 1},clearTimeout(){},
 fetch:async(url,options)=>({ok:true,json:async()=>{
 if(url==='/api/backtest/library')return {strategies,datasets:[{id:'spy',symbol:'SPY',bar_count:3,start:'2026-10-20',end:'2026-10-20'}]};
 if(url==='/api/backtest/run'){const settings=JSON.parse(options.body);requests.push(settings);return {settings,bars:[0,1,2].map(i=>({timestamp:'2026-10-20T13:3'+i+':00Z',bar_close_at:'2026-10-20T13:3'+(i+1)+':00Z',equity:settings.capital-i,cash:settings.capital-i,position:0,open:100,high:101,low:99,close:100,volume:1000,decision:'hold'})),fills:[]};}
 if(url==='/api/replay')return {state:'unavailable'};
 return {runner:{state:'running',mode:'shadow'},settings:{ai:{provider:'none'}},observed_at:new Date().toISOString()};
 }})};
const source=fs.readFileSync(process.argv[1],'utf8').split('<script>')[1].split('</script>')[0];
vm.createContext(context);vm.runInContext(source,context);
(async()=>{
 await vm.runInContext("switchView('replay')",context);
 for(let i=0;i<8;i++)await new Promise(r=>setImmediate(r));
 assert.equal(requests.length,1);assert.match(get('bt-progress').textContent,/Bar 1 \/ 3/);
 assert.match(get('bt-cursor-time').textContent,/13:31:00 UTC.*bar close/);
 get('bt-next').onclick();assert.match(get('bt-progress').textContent,/Bar 2 \/ 3/);
 get('bt-prev').onclick();assert.match(get('bt-progress').textContent,/Bar 1 \/ 3/);
 get('bt-capital').value='25000';get('bt-slippage').value='20';get('bt-dataset').value='spy';get('bt-capital').oninput();
 assert.match(get('bt-applied').textContent,/10,000/);assert.equal(get('bt-dirty').textContent,'Unapplied settings');
 await vm.runInContext('applyReplaySettings()',context);
 assert.equal(requests[1].capital,25000);assert.equal(requests[1].slippage_bps,20);assert.match(get('bt-applied').textContent,/25,000/);
 get('bt-end').onclick();assert.match(get('bt-progress').textContent,/Complete.*Bar 3 \/ 3/);
 get('bt-search').value='options';get('bt-search').oninput();assert.equal(get('bt-strategy-list').children.length,1);assert.equal(get('bt-strategy-list').children[0].disabled,true);
 await vm.runInContext("switchView('home')",context);assert.equal(vm.runInContext('replayUI.controller.playing',context),false);
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        result=subprocess.run(['node','-e',harness,str(PAGE)],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)


if __name__ == '__main__':
    unittest.main()
