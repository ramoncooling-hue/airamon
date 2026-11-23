const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(require('node:path').join(__dirname, '../web/index.html'), 'utf8');
const block = html.slice(html.indexOf('let chatSending = false;'), html.indexOf('/** Safe Markdown'));

function setup(result){
  const posts=[], requests=[];
  const elements={chatGo:{disabled:false},chatIn:{value:'问题'}};
  const state={msgs:[{who:'me',text:'旧问题'},{who:'ai',text:'<p>旧答案</p>',rawText:'原始回答',webResults:[{title:'来源',url:'https://example.com'}]}],role:{name:'小思 Sense',key:'laoba',model:'fast'},scope:'*',webMode:'auto',mcpServerId:'auto',convId:1,page:'chat'};
  const context=vm.createContext({S:state,SCOPE_ALL:'*',API:'',TextDecoder,Uint8Array,JSON,
    el:id=>elements[id],ensureConv:async()=>1,persistMsg:async(id,p)=>posts.push(p),
    renderMessages(){},renderConvList(){},loadConvs:async()=>{},toast(){},
    messageRefs:message=>[...(message.cites||[]),...(message.manualResults||[]),...(message.webResults||[])],
    esc:s=>s,stripHtml:s=>s.replace(/<[^>]+>/g,''),mdToHtml:s=>'<p>'+s+'</p>',
    fetch:async(url,opts)=>{
      requests.push(JSON.parse(opts.body));
      const data='data: '+JSON.stringify({type:'tool',tool:'web_search',query:'Astra',status:'done',count:1})+'\n\n'+'data: '+JSON.stringify({type:'result',data:result})+'\n\n';
      const bytes=new TextEncoder().encode(data);
      return {ok:true,body:new ReadableStream({start(c){c.enqueue(bytes.slice(0,37));c.enqueue(bytes.slice(37));c.close();}})};
    }
  });
  vm.runInContext(block,context);
  return {context,state,posts,requests,elements};
}

test('chat preserves selected role and raw context, cites only backend evidence and saves sources', async()=>{
  const x=setup({engine:'model',text:'查到了 [网页1]',hits:[{id:5,name:'无关会计知识',citation:'记忆1'}],web_results:[{url:'https://example.com/astra',title:'Astra'}],trace:[]});
  await vm.runInContext("chatSend('新问题')",x.context);
  assert.equal(x.requests[0].role_key,'laoba');
  assert.equal(x.requests[0].history.length,2);
  assert.match(x.requests[0].history[1].content,/原始回答/);
  assert.match(x.requests[0].history[1].content,/https:\/\/example.com/);
  assert.equal(x.state.msgs.at(-1).cites.length,0);
  assert.doesNotMatch(x.state.msgs.at(-1).text,/依据上面/);
  assert.equal(x.posts[1].metadata.web_results[0].title,'Astra');
  assert.equal(x.elements.chatGo.disabled,false);
});

test('double Enter does not create duplicate model requests',async()=>{
  const x=setup({engine:'model',text:'回答'});
  await Promise.all([vm.runInContext("chatSend('一')",x.context),vm.runInContext("chatSend('二')",x.context)]);
  assert.equal(x.requests.length,1);
});

test('model failure is explicit rather than a fake locally composed answer',async()=>{
  const x=setup({engine:'local',text:'',error:'模型连接失败'});
  await vm.runInContext("chatSend('问题')",x.context);
  assert.equal(x.state.msgs.at(-1).engine,'error');
  assert.match(x.state.msgs.at(-1).text,/模型连接失败/);
});

test('chat sends the default automatic MCP selection and preserves explicit narrowing', async()=>{
  const x=setup({engine:'model',text:'回答'});
  await vm.runInContext("chatSend('自动')",x.context);
  assert.equal(x.requests[0].mcp_server_id,'auto');

  const y=setup({engine:'model',text:'回答'});
  y.state.mcpServerId='none';
  await vm.runInContext("chatSend('禁用扩展')",y.context);
  assert.equal(y.requests[0].mcp_server_id,'none');

  const z=setup({engine:'model',text:'回答'});
  z.state.mcpServerId='docs';
  await vm.runInContext("chatSend('指定服务')",z.context);
  assert.equal(z.requests[0].mcp_server_id,'docs');
});

test('MCP selector keeps an unavailable choice across course, workspace and network changes',()=>{
  const x=setup({});
  const meta={attempted:true,loaded:true,sdk:true,error:'',missingEnv:{},servers:[{id:'docs',label:'Documents',workspace:'studypal',course:'Accounting',network:true,enabled:true,trusted:true,tools:{read:{read_only:true}}}]};
  x.context.mcpSettingsState=()=>meta;
  x.elements.chatMcpSelect={value:'',innerHTML:''};x.elements.chatMcpStatus={textContent:''};
  x.state.workspace='studypal';x.state.scope='Accounting';x.state.mcpServerId='docs';
  vm.runInContext('renderChatMcpSelection()',x.context);
  assert.match(x.elements.chatMcpStatus.textContent,/当前范围可用/);
  x.state.scope='Economics';vm.runInContext('renderChatMcpSelection()',x.context);
  assert.equal(x.state.mcpServerId,'docs');assert.equal(x.elements.chatMcpSelect.value,'docs');
  assert.match(x.elements.chatMcpStatus.textContent,/不可用.*不调用其他/);
  x.state.scope='Accounting';x.state.webMode='off';vm.runInContext('renderChatMcpSelection()',x.context);
  assert.match(x.elements.chatMcpStatus.textContent,/联网已关闭/);
  x.state.webMode='auto';x.state.workspace='work';vm.runInContext('renderChatMcpSelection()',x.context);
  assert.match(x.elements.chatMcpStatus.textContent,/当前工作区/);
  meta.servers=[];vm.runInContext('renderChatMcpSelection()',x.context);
  assert.match(x.elements.chatMcpSelect.innerHTML,/docs.*已失效/);assert.equal(x.state.mcpServerId,'docs');
});

test('MCP availability requires trust, enabled, readonly, dependency and environment gates',()=>{
  const x=setup({});x.state.workspace='studypal';
  const meta={loaded:true,sdk:true,error:'',missingEnv:{}};
  const server={id:'docs',enabled:true,trusted:true,workspace:'studypal',course:null,network:false,tools:{read:{read_only:true}}};
  x.context.server=server;x.context.meta=meta;
  const reason=()=>vm.runInContext('chatMcpAvailability(server,meta)',x.context);
  assert.equal(reason(),'');server.enabled=false;assert.match(reason(),/停用/);server.enabled=true;
  server.trusted=false;assert.match(reason(),/信任/);server.trusted=true;
  meta.sdk=false;assert.match(reason(),/依赖/);meta.sdk=true;
  meta.missingEnv.docs=['DOCS_TOKEN'];assert.match(reason(),/环境变量/);meta.missingEnv={};
  server.tools={write:{read_only:false}};assert.match(reason(),/只读/);
});

test('MCP choice survives reload per workspace and invalid stored choice fails closed',()=>{
  const x=setup({}), values=new Map();
  x.context.localStorage={getItem:key=>values.get(key)||null,setItem:(key,value)=>values.set(key,value)};
  x.state.workspace='studypal';x.state.mcpServerId='none';
  vm.runInContext('persistChatMcpChoice()',x.context);
  x.state.mcpServerId='auto';vm.runInContext('restoreChatMcpChoice()',x.context);
  assert.equal(x.state.mcpServerId,'none');
  x.state.workspace='work';x.state.mcpServerId='removed_service';
  vm.runInContext('persistChatMcpChoice()',x.context);
  x.state.workspace='studypal';vm.runInContext('restoreChatMcpChoice()',x.context);
  assert.equal(x.state.mcpServerId,'none');
  x.state.workspace='work';vm.runInContext('restoreChatMcpChoice()',x.context);
  assert.equal(x.state.mcpServerId,'removed_service');
  values.set('mindock-mcp-choice:work','bad/id');vm.runInContext('restoreChatMcpChoice()',x.context);
  assert.equal(x.state.mcpServerId,'none');
});

test('MCP response separates available tools from actual calls and persists status',async()=>{
  const available=setup({engine:'model',text:'回答',mcp_selection:{requested:'docs',selected:['docs'],status:'available'},trace:[{type:'mcp_selection',requested:'docs',selected:['docs'],status:'available'}]});
  await vm.runInContext("chatSend('问题')",available.context);
  assert.match(available.state.msgs.at(-1).mcpStatus,/可用.*未调用/);assert.equal(available.state.msgs.at(-1).steps.length,0);
  assert.equal(available.posts.at(-1).metadata.mcp_selection.status,'available');
  const called=setup({engine:'model',text:'回答',mcp_selection:{status:'available'},trace:[{type:'tool',tool:'mcp_docs_read',server_id:'docs',remote_tool:'read',status:'done',count:1}]});
  await vm.runInContext("chatSend('问题')",called.context);assert.match(called.state.msgs.at(-1).mcpStatus,/实际调用 1 次/);
  const failed=setup({engine:'model',text:'回答',mcp_selection:{requested:'docs',selected:[],status:'unavailable'},mcp_errors:['指定服务不在当前范围'],trace:[]});
  await vm.runInContext("chatSend('问题')",failed.context);assert.match(failed.state.msgs.at(-1).mcpStatus,/不可用.*未调用.*不在当前范围/);
});

test('the turn snapshots MCP choice and workspace before conversation creation resolves',async()=>{
  const x=setup({engine:'model',text:'回答'});let release;
  x.state.workspace='studypal';x.state.mcpServerId='docs';
  x.context.ensureConv=()=>new Promise(resolve=>{release=resolve;});
  const pending=vm.runInContext("chatSend('问题')",x.context);
  x.state.workspace='work';x.state.mcpServerId='none';release(1);await pending;
  assert.equal(x.requests[0].workspace,'studypal');assert.equal(x.requests[0].mcp_server_id,'docs');
});

test('stream failure retains an observed MCP tool call instead of reporting unused tools',async()=>{
  const x=setup({});
  x.context.fetch=async()=>{
    const events=[{type:'mcp_selection',requested:'docs',selected:['docs'],status:'available'},
      {type:'tool',tool:'mcp_docs_read',server_id:'docs',remote_tool:'read',status:'done',count:1},
      {type:'error',message:'stream interrupted'}];
    const bytes=new TextEncoder().encode(events.map(e=>'data: '+JSON.stringify(e)+'\n\n').join(''));
    return {ok:true,body:new ReadableStream({start(c){c.enqueue(bytes);c.close();}})};
  };
  await vm.runInContext("chatSend('问题')",x.context);
  assert.match(x.state.msgs.at(-1).mcpStatus,/实际调用 1 次/);
  assert.equal(x.posts.at(-1).metadata.trace[0].remote_tool,'read');
});
