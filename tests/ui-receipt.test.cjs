const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
// 与 tests/chat-persist.test.cjs 相同的切片：只加载 chatSend 及其前面的状态变量。
const chatBlock = html.slice(html.indexOf('let chatSending = false;'), html.indexOf('/** Safe Markdown'));

function chatSetup({result = {}, fail = false} = {}){
  const posts = [];
  const state = {msgs:[], role:{name:'小思 Sense', key:'laoba', model:'fast'}, scope:'*',
    webMode:'auto', mcpServerId:'auto', convId:7, page:'chat'};
  const elements = {chatGo:{disabled:false}, chatIn:{value:'问题'}};
  const context = vm.createContext({S:state, SCOPE_ALL:'*', API:'', TextDecoder:globalThis.TextDecoder, Uint8Array, JSON, TextEncoder:globalThis.TextEncoder, ReadableStream:globalThis.ReadableStream,
    el:id=>elements[id], ensureConv:async()=>7, persistMsg:async(_id,payload)=>posts.push(payload),
    renderMessages(){}, renderConvList(){}, loadConvs:async()=>{}, toast(){},
    messageRefs:message=>[...(message.cites||[]),...(message.manualResults||[]),...(message.webResults||[])],
    esc:s=>s, stripHtml:s=>s.replace(/<[^>]+>/g,''), mdToHtml:s=>'<p>'+s+'</p>',
    fetch:async()=>{
      if(fail) throw new Error('offline');
      const data = 'data: ' + JSON.stringify({type:'result', data:result}) + '\n\n';
      return {ok:true, body:new globalThis.ReadableStream({start(controller){
        controller.enqueue(new globalThis.TextEncoder().encode(data));
        controller.close();
      }})};
    }});
  vm.runInContext(chatBlock, context);
  return {context, posts};
}

async function send(options){
  const x = chatSetup(options);
  await vm.runInContext("chatSend('问题')", x.context);
  return x;
}

test('user_saved=true/persisted=false only补写 assistant 消息', async()=>{
  const x = await send({result:{engine:'model', text:'回答', user_saved:true, persisted:false}});
  assert.equal(x.posts.length, 1);
  assert.equal(x.posts[0].role, 'assistant');
});

test('user_saved=false/persisted=true only补写 user 消息', async()=>{
  const x = await send({result:{engine:'model', text:'回答', user_saved:false, persisted:true}});
  assert.equal(x.posts.length, 1);
  assert.equal(x.posts[0].role, 'user');
  assert.equal(x.posts[0].content, '问题');
});

test('user_saved=true/persisted=true 时不补写任何消息', async()=>{
  const x = await send({result:{engine:'model', text:'回答', user_saved:true, persisted:true}});
  assert.deepEqual(x.posts, []);
});

test('user_saved=false/persisted=false 时补写两条消息', async()=>{
  const x = await send({result:{engine:'model', text:'回答', user_saved:false, persisted:false}});
  assert.deepEqual(x.posts.map(item=>item.role), ['user', 'assistant']);
});

test('旧回执只有 persisted=false 时保持原行为补写两条', async()=>{
  const x = await send({result:{engine:'model', text:'回答', persisted:false}});
  assert.deepEqual(x.posts.map(item=>item.role), ['user', 'assistant']);
});

test('完全缺字段的旧服务端成功响应保持原行为补写两条', async()=>{
  const x = await send({result:{engine:'model', text:'回答'}});
  assert.deepEqual(x.posts.map(item=>item.role), ['user', 'assistant']);
});

const decideSlice = html.slice(html.indexOf('async function decideAgentAction('), html.indexOf('\nfunction renderChat('));

function decideSetup(apiImplementation){
  const toasts = [];
  let renderCalls = 0;
  const button = {disabled:false, textContent:'接受'};
  const context = vm.createContext({
    S:{workspace:'studypal'},
    CSS:{escape:value=>String(value)},
    document:{querySelector:()=>button},
    apiPost:apiImplementation,
    renderPersonalAgent:async()=>{ renderCalls += 1; },
    toast:message=>toasts.push(message),
    esc:value=>value,
  });
  vm.runInContext(decideSlice, context);
  return {context, toasts, button, renders:()=>renderCalls};
}

test('approve 返回 failed 时展示后端错误、不报成功并保留可重试按钮', async()=>{
  let current = {status:'failed', error:'执行租约超时，未完成执行（可重试）'};
  const x = decideSetup(async()=>current);
  await x.context.decideAgentAction(5, 'approve');
  assert.equal(x.button.disabled, false, '失败后按钮必须恢复可用');
  assert.equal(x.button.textContent, '重试执行');
  assert.match(x.toasts.at(-1), /执行租约超时/);
  assert.ok(!x.toasts.some(item=>item.includes('动作已执行')), 'failed 不能当成功');
  assert.equal(x.renders(), 0, '失败时不刷新列表，保留重试入口');

  current = {status:'applied'};
  await x.context.decideAgentAction(5, 'approve');
  assert.match(x.toasts.at(-1), /动作已执行/);
  assert.equal(x.renders(), 1);
});

test('approve 被后端 400 拒绝时保留按钮并展示 detail', async()=>{
  const x = decideSetup(async()=>{ throw new Error('该动作执行失败且不可自动重试，请重新发起动作'); });
  await x.context.decideAgentAction(5, 'approve');
  assert.equal(x.button.disabled, false);
  assert.equal(x.button.textContent, '接受');
  assert.match(x.toasts.at(-1), /不可自动重试/);
});

test('attention 的 failed 行渲染「重试执行」并复用 data-action-approve', ()=>{
  assert.match(html, /const failed = action\.status === 'failed'/);
  assert.match(html, /const approveLabel = failed \? '重试执行' : '接受'/);
  assert.match(html, /data-action-approve="\$\{action\.id\}"\)?\>?\$\{approveLabel\}|data-action-approve="\$\{action\.id\}">\$\{approveLabel\}/);
});
