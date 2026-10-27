import test from 'node:test';
import assert from 'node:assert/strict';
import {reviewAI} from '../../runtime/paper-agent.mjs';
const config={provider:'local',consent:true,model:'test-model',endpoint:'http://127.0.0.1:11434/v1'};

test('local AI uses a bounded loopback request and reports actual provider output',async()=>{
  const result=await reviewAI(config,{symbol:'SPY',technical_signal:'buy'},{fetcher:async(url,request)=>{
    assert.equal(url,'http://127.0.0.1:11434/v1/chat/completions');assert.equal(request.redirect,'error');
    const body=JSON.parse(request.body);assert.equal(body.max_tokens,256);assert.equal(body.model,'test-model');
    return new Response(JSON.stringify({model:'reported-test',choices:[{message:{content:JSON.stringify({decision:'approve',reason:'Test review'})}}],usage:{prompt_tokens:12,completion_tokens:4}}));
  }});
  assert.equal(result.status,'reviewed');assert.equal(result.decision,'approve');assert.equal(result.reported_model,'reported-test');
  assert.equal(result.usage.input_tokens,12);
});
test('disabled AI never contacts a provider',async()=>{
  const r=await reviewAI({...config,consent:false},{},{fetcher:()=>assert.fail('should not fetch')});
  assert.equal(r.status,'disabled');
});
test('remote endpoints, malformed replies and HTTP errors never approve an entry',async()=>{
  const r=await reviewAI({...config,endpoint:'https://example.org/v1'},{},{fetcher:()=>assert.fail('should not fetch')});
  assert.equal(r.status,'unavailable');assert.equal(r.decision,null);
  for(const body of [{choices:[{message:{content:'not JSON'}}]},{choices:[{message:{content:'{"decision":"buy","reason":"override"}'}}]}]){
    const reply=await reviewAI(config,{},{fetcher:async()=>new Response(JSON.stringify(body))});
    assert.equal(reply.status,'unavailable');assert.equal(reply.decision,null);
  }
  assert.equal((await reviewAI(config,{},{fetcher:async()=>new Response('',{status:429})})).decision,null);
});
test('CLI review has no broker tools, uses bounded invocation and honors the selected model',async()=>{
  const calls=[];
  const result=await reviewAI({provider:'codex',consent:true,model:'selected-model'},{symbol:'SPY'},
    {exec:async(...args)=>{calls.push(args);return Buffer.from('{"decision":"hold","reason":"test"}');}});
  assert.equal(result.decision,'hold');assert.equal(calls[0][1],'codex');assert.ok(calls[0][2].includes('read-only'));
  assert.ok(calls[0][2].includes('--ignore-user-config'));assert.ok(calls[0][2].includes('shell_tool'));assert.ok(calls[0][2].includes('apps'));
  assert.ok(calls[0][2].includes('selected-model'));assert.equal(calls[0][4],30000);
});
test('Claude envelope errors and provider failures fail closed',async()=>{
  const c={provider:'claude',consent:true,model:''};
  assert.equal((await reviewAI(c,{},{exec:async()=>Buffer.from('{"is_error":true,"result":""}')})).decision,null);
  assert.equal((await reviewAI(c,{},{exec:async()=>{throw new Error('private upstream text');}})).reason,'AI response unavailable or invalid; entry skipped');
});
