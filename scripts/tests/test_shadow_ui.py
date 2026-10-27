"""Execute the actual terminal chart code with Safari-era APIs and observed fixtures."""
import json
from pathlib import Path
import subprocess
import unittest

ROOT=Path(__file__).resolve().parents[2]
HARNESS=r'''
import fs from 'node:fs';
import vm from 'node:vm';
const elements=new Map();
function element(tag='div'){return {tag,textContent:'',children:[],attributes:{},value:'',checked:false,
 append(...nodes){this.children.push(...nodes)},replaceChildren(...nodes){this.children=nodes},
 setAttribute(key,value){this.attributes[key]=value}}}
const get=id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id)};
get('chart-mode').value='candles';get('chart-window').value='60';get('follow-scanner').checked=true;
const context={document:{body:{},getElementById:get,createElement:element,createElementNS:(_,tag)=>element(tag)},
 fetch:async url=>({ok:true,json:async()=>url==='/api/replay'?{state:'available',message:'Historical fixture replay'}:{runner:{state:'not_started',mode:'shadow'},settings:{},activity:null}}),
 AbortController,AbortSignal:undefined,setTimeout(){return 1},clearTimeout(){},URL};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1],'utf8').split('<script>')[1].split('</script>')[0],context);
await new Promise(resolve=>setImmediate(resolve));
const home={initialVisible:!get('home-panel').hidden,marketInitiallyHidden:get('live-layout').hidden,title:get('view-title').textContent};
get('home-live').onclick();home.marketOpened=!get('live-layout').hidden&&get('home-panel').hidden;
const at=new Date().toISOString();
const company=symbol=>({symbol,timeframe:'1m',family:'sma_cross_long',state:'shadow_watching',feed_status:'fresh_indicative',
 observed_at:at,last_bar_at:at,last_checked_at:at,quote:{price:'101',quote_at:at},
 quote_history:[{at,price:'101'}],chart_bars:[{bar_close_at:at,open:'100',high:'102',low:'99',close:'101',volume:'1000'}],
 decisions:[{side:'buy',decision_at:at,bar_close_at:at,reason:'fixture rule met'}],fills:[]});
const payload={runner:{state:'running',mode:'shadow'},settings:{},activity:{
 companies:[company('SPY'),company('AAPL')],scanner:{active_symbol:'AAPL',last_scanned_at:at},
 portfolio:{initial_cash:'1000',virtual_equity:'1000'},events:[]}};
payload.market={companies:[{symbol:'AAPL',name:'Apple fixture',quote_type:'EQUITY',shares_outstanding:'1000',previous_close:'100',observed_at:at}]};
payload.news={observed_at:at,companies:['SPY','AAPL'].map(symbol=>({symbol,sources:[{source:'fixture',state:'connected',items:[{title:symbol+' fixture headline',url:'https://example.org/'+symbol,published_at:at}]}]})),broad_sources:[]};
context.payload=payload;vm.runInContext('render(payload)',context);
const initial={title:get('chart-title').textContent,buttons:get('companies').children.length,
 chartNodes:get('stock-chart').children.length,signal:get('signal-summary').textContent};
const capBefore=get('market-cap').textContent;
payload.activity.companies[1].quote.price='102';vm.runInContext('render(payload)',context);
const market={name:get('company-name').textContent,capBefore,capAfter:get('market-cap').textContent,label:get('cap-label').textContent,news:get('news-heading').textContent};
payload.activity.companies[1].quote.quote_at=new Date(Date.now()-60000).toISOString();vm.runInContext('render(payload)',context);
market.staleLabel=get('cap-label').textContent;
get('companies').children[0].onclick();
const selected={title:get('chart-title').textContent,follow:get('follow-scanner').checked,news:get('news-heading').textContent};
get('chart-mode').value='quotes';get('chart-mode').onchange();
const quotes={title:get('chart-title').textContent,nodes:get('stock-chart').children.length};
payload.activity.companies[0].quote_history=[];vm.runInContext('render(payload)',context);
const empty={text:get('chart-empty').textContent,nodes:get('stock-chart').children.length};
payload.runner.mode='paper';vm.runInContext('render(payload)',context);
get('home-replay').onclick();await new Promise(resolve=>setImmediate(resolve));
const replay={src:get('replay-frame').src,hidden:get('replay-frame').hidden,liveHidden:get('live-layout').hidden,state:get('replay-state').textContent};
get('home-live').onclick();replay.returnedToLive=!get('live-layout').hidden;
context.terminalFixture={research:{state:'saved_simulation',net_pnl:'-12.34',accounts:[{instrument_mode:'long_call',net_pnl:'-12.34',completed_trades:1}],phases:[{gate_passed:false,gate_check:'fixture failed gate'}]},library:{state:'unavailable',message:'No catalog'},models:{state:'saved',providers:['codex'],defaults:{codex:'test'},roster:[{agent_id:'trader_price_structure',primary_model:'codex:test'}]}};
vm.runInContext('renderTerminal(terminalFixture)',context);
const research={pnl:get('research-pnl').textContent,className:get('research-pnl').className,accounts:get('research-accounts').textContent,steps:get('research-phases').children.length,roles:get('model-roster').children.length,model:get('model-id').value};
vm.runInContext("renderTerminal({research:{state:'unavailable',message:'No saved run'},models:{},library:{}})",context);
research.emptyPnl=get('research-pnl').textContent;research.emptySteps=get('research-phases').children.length;
get('home-models').onclick();research.modelsVisible=!get('models-panel').hidden&&get('live-layout').hidden&&get('replay-panel').hidden;
home.destinations={};for(const view of ['research','models','replay']){get('brand-home').onclick();get('home-'+view).onclick();await new Promise(resolve=>setImmediate(resolve));home.destinations[view]=!get(view+'-panel').hidden&&get('home-panel').hidden}
get('back-action').onclick();home.backWorks=!get('home-panel').hidden;get('home-tab').onclick();home.returned=!get('home-panel').hidden&&get('live-layout').hidden&&get('replay-panel').hidden&&get('models-panel').hidden&&get('research-panel').hidden;
console.log(JSON.stringify({initial,selected,quotes,empty,market,replay,research,home,paperHidden:get('shadow-scan').hidden}));
'''


class ShadowUITests(unittest.TestCase):
    def test_chart_selection_real_data_and_empty_state_without_new_abortsignal_helpers(self):
        result=subprocess.run(['node','--input-type=module','-e',HARNESS,str(ROOT/'scripts/status.html')],
                              check=True,capture_output=True,text=True,timeout=30)
        data=json.loads(result.stdout)
        self.assertIn('AAPL',data['initial']['title']);self.assertEqual(data['initial']['buttons'],2)
        self.assertGreater(data['initial']['chartNodes'],10)
        self.assertIn('BUY',data['initial']['signal'])
        self.assertIn('SPY',data['selected']['title']);self.assertFalse(data['selected']['follow'])
        self.assertIn('observed quotes',data['quotes']['title']);self.assertGreater(data['quotes']['nodes'],0)
        self.assertEqual(data['empty']['nodes'],0);self.assertIn('not animated or fabricated',data['empty']['text'])
        self.assertTrue(data['paperHidden'])
        self.assertEqual(data['market']['name'],'Apple fixture')
        self.assertNotEqual(data['market']['capBefore'],data['market']['capAfter'])
        self.assertEqual(data['market']['label'],'ESTIMATED MARKET CAP')
        self.assertEqual(data['market']['staleLabel'],'LAST MARKET-CAP ESTIMATE')
        self.assertIn('AAPL',data['market']['news']);self.assertIn('SPY',data['selected']['news'])
        self.assertEqual(data['replay']['src'],'/replay');self.assertFalse(data['replay']['hidden'])
        self.assertTrue(data['replay']['liveHidden']);self.assertTrue(data['replay']['returnedToLive'])
        self.assertIn('Historical',data['replay']['state'])
        self.assertEqual(data['research']['pnl'],'-$12.34');self.assertEqual(data['research']['className'],'negative')
        self.assertEqual(data['research']['accounts'],'1');self.assertEqual(data['research']['steps'],1)
        self.assertEqual(data['research']['roles'],1);self.assertEqual(data['research']['model'],'test')
        self.assertEqual(data['research']['emptyPnl'],'—');self.assertEqual(data['research']['emptySteps'],0)
        self.assertTrue(data['research']['modelsVisible'])
        self.assertTrue(data['home']['initialVisible']);self.assertTrue(data['home']['marketInitiallyHidden'])
        self.assertEqual(data['home']['title'],'Home');self.assertTrue(data['home']['marketOpened'])
        self.assertEqual(data['home']['destinations'],dict(research=True,models=True,replay=True))
        self.assertTrue(data['home']['returned']);self.assertTrue(data['home']['backWorks'])
        page=(ROOT/'scripts/status.html').read_text();self.assertNotIn('<aside',page);self.assertNotIn('app-nav',page)


if __name__=='__main__':unittest.main()
