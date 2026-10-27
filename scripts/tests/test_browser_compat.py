"""The status page must load in older Safari without AbortSignal.timeout."""
from pathlib import Path
import json
import subprocess
import unittest


class BrowserCompatibilityTests(unittest.TestCase):
    def test_status_refresh_without_new_abortsignal_helpers(self):
        page=Path(__file__).resolve().parents[1]/'status.html'
        harness=r'''
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(process.argv[1],'utf8').split('<script>')[1].split('</script>')[0];
const elements=new Map();
const element=()=>({textContent:'',append(){},replaceChildren(){}});
let calls=0;
const context={
 document:{getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id)},createElement:element},
 fetch:async()=>{calls++;return {ok:true,json:async()=>({runner:{state:'running',mode:'shadow'},settings:{ai:{provider:'none'}},observed_at:new Date().toISOString(),activity:null})}},
 AbortController,AbortSignal:undefined,setTimeout(){return 1},clearTimeout(){},URL,
};
vm.runInNewContext(source,context);
await new Promise(resolve=>setImmediate(resolve));
console.log(JSON.stringify({calls,health:elements.get('health').textContent}));
'''
        result=subprocess.run(['node','--input-type=module','-e',harness,str(page)],capture_output=True,text=True,timeout=30,check=True)
        data=json.loads(result.stdout)
        self.assertEqual(data['calls'],1)
        self.assertIn('RUNNING',data['health'])
        self.assertNotIn('UNAVAILABLE',data['health'])


if __name__=='__main__':unittest.main()
