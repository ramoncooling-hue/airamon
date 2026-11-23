const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const block = html.slice(html.indexOf('let chatSending = false;'), html.indexOf('/** Safe Markdown'));

function setup({result = {}, fail = false} = {}){
  const posts = [], requests = [];
  const elements = {chatGo:{disabled:false}, chatIn:{value:'问题'}};
  const state = {msgs:[], role:{name:'小思 Sense', key:'laoba', model:'fast'}, scope:'*',
    webMode:'auto', mcpServerId:'auto', convId:7, page:'chat'};
  const context = vm.createContext({S:state, SCOPE_ALL:'*', API:'', TextDecoder, Uint8Array, JSON, TextEncoder, ReadableStream,
    el:id=>elements[id], ensureConv:async()=>7, persistMsg:async(id,p)=>posts.push(p),
    renderMessages(){}, renderConvList(){}, loadConvs:async()=>{}, toast(){},
    messageRefs:message=>[...(message.cites||[]),...(message.manualResults||[]),...(message.webResults||[])],
    esc:s=>s, stripHtml:s=>s.replace(/<[^>]+>/g,''), mdToHtml:s=>'<p>'+s+'</p>',
    fetch:async(url,opts)=>{
      requests.push({url, body:JSON.parse(opts.body)});
      if(fail) throw new Error('offline');
      const payload = {type:'result', data:result};
      const data = 'data: ' + JSON.stringify(payload) + '\n\n';
      const bytes = new TextEncoder().encode(data);
      return {ok:true, body:new ReadableStream({start(c){c.enqueue(bytes);c.close();}})};
    }
  });
  vm.runInContext(block, context);
  return {context, state, posts, requests};
}

test('conversation_id is sent with the ask stream request', async()=>{
  const x = setup({result:{engine:'model', text:'回答', persisted:true}});
  await vm.runInContext("chatSend('新问题')", x.context);
  assert.equal(x.requests.length, 1);
  assert.equal(x.requests[0].body.conversation_id, 7);
  assert.equal(x.requests[0].body.question, '新问题');
});

test('server-persisted turn skips both client writes to avoid duplicates', async()=>{
  const x = setup({result:{engine:'model', text:'回答', persisted:true}});
  await vm.runInContext("chatSend('已落库的问题')", x.context);
  assert.deepEqual(x.posts, [], 'client must not re-persist a server-stored pair');
});

test('old responses without a persisted receipt keep the legacy client writes', async()=>{
  const x = setup({result:{engine:'model', text:'回答 [网页1]',
    web_results:[{url:'https://example.com', title:'Astra'}]}});
  await vm.runInContext("chatSend('旧服务端')", x.context);
  assert.equal(x.posts.length, 2);
  assert.equal(x.posts[0].role, 'user');
  assert.equal(x.posts[0].content, '旧服务端');
  assert.equal(x.posts[1].role, 'assistant');
  assert.equal(x.posts[1].metadata.web_results[0].title, 'Astra');
});

test('failed requests keep the error reply without duplicating a possibly saved user message', async()=>{
  // P2-6a：传输失败时没有 user_saved 回执，服务端可能已先保存 user 消息；
  // 客户端不再补写 user，只补写本地 error 回复，避免刷新后出现重复 user 行。
  const x = setup({fail:true});
  await vm.runInContext("chatSend('断网问题')", x.context);
  assert.equal(x.posts.length, 1);
  assert.equal(x.posts[0].role, 'assistant');
  assert.equal(x.posts[0].engine, 'error');
});
