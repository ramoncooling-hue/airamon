const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const code = html.slice(html.indexOf('function renderIfacePanel(){'), html.indexOf('async function putSettings('));
const escapeCode = html.slice(html.indexOf('function esc(s){'), html.indexOf('function toast(msg){'));
const clone = value => JSON.parse(JSON.stringify(value));
const server = (extra = {}) => ({id:'docs',label:'Documents',transport:'stdio',command:'/usr/bin/node',args:['server.js'],env_names:[],workspace:'studypal',course:null,network:false,public_data:false,timeout_seconds:5,enabled:true,trusted:true,tools:{read:{read_only:true,description:'Read documents',fixed_arguments:{limit:3}}},...extra});
const config = (servers = [], extra = {}) => ({servers,revision:'revision-1',sdk_installed:true,capabilities:{transports:['stdio','streamable_http'],oauth:false,read_only:true},error:'',...extra});

// A bounded DOM fixture parses rendered controls and dispatches their real handlers.
// Browser layout and native fieldset behavior are covered by integration QA.
function setup(initial = config()) {
  const elements = {}, requests = [], replies = [], confirms = [];
  let saved = clone(initial), confirmResult = true, children = new Set();
  const decode = text => String(text||'').replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&');
  function element(id) {
    let markup = '';
    return {id,value:'',checked:false,disabled:false,textContent:'',
      get innerHTML(){ return markup; },
      set innerHTML(value){
        markup = value;
        if(id !== 'mcpSettings') return;
        for(const key of children) delete elements[key];
        children = new Set();
        for(const match of value.matchAll(/<(input|button|select|textarea)\b([^>]*\bid="([^"]+)"[^>]*)>(?:([\s\S]*?)<\/\1>)?/g)) {
          const [,tag,attrs,key,body] = match;
          const node = element(key);
          node.disabled = /\bdisabled\b/.test(attrs);
          node.checked = /\bchecked\b/.test(attrs);
          node.value = decode((attrs.match(/\bvalue="([^"]*)"/)||[])[1]);
          if(tag==='textarea') node.value = decode(body);
          if(tag==='select') {
            const opts = [...(body||'').matchAll(/<option value="([^"]*)"([^>]*)>/g)];
            node.value = decode((opts.find(opt=>opt[2].includes('selected'))||opts[0]||[])[1]);
          }
          elements[key]=node; children.add(key);
        }
      }};
  }
  elements.mcpSettings=element('mcpSettings');
  elements.ifaceBox=element('ifaceBox');
  elements.refreshExtensions=element('refreshExtensions');
  elements.saveCh=element('saveCh');
  const state={workspace:'studypal',workspaces:[{key:'studypal',name:'Study'},{key:'work',name:'Work'}],courses:[{name:'Accounting'}],health:{},_extensions:{mcp:{sdk_installed:true,servers:[]}}};
  const context=vm.createContext({S:state,API:'',URL,location:{origin:'http://127.0.0.1'},el:id=>elements[id]||null,window:{confirm:message=>{confirms.push(message);return confirmResult;}},fetch:async(url,options={})=>{
    const request={url,method:options.method||'GET',body:options.body?JSON.parse(options.body):undefined}; requests.push(request);
    if(replies.length) {
      const reply=await replies.shift();
      if(reply instanceof Error) throw reply;
      return {ok:reply.status<400,status:reply.status,json:async()=>reply.body};
    }
    if(url.startsWith('/api/courses?')) return {ok:true,status:200,json:async()=>[{name:'Accounting',workspace_key:'work'}]};
    if(request.method==='PUT') saved={...saved,servers:request.body.servers,revision:'revision-2'};
    return {ok:true,status:200,json:async()=>clone(saved)};
  }});
  vm.runInContext(escapeCode+code,context);
  const run=source=>vm.runInContext(source,context);
  const input=(id,value)=>{const node=elements[id];assert.ok(node,id+' exists');if(typeof value==='boolean')node.checked=value;else node.value=value;return (node.oninput||node.onchange)();};
  const click=id=>{assert.ok(elements[id],id+' exists');assert.equal(elements[id].disabled,false,id+' enabled');return elements[id].onclick();};
  return {state,elements,requests,replies,confirms,run,input,click,confirm:value=>{confirmResult=value;},load:()=>run('loadMcpSettings()'),get view(){return elements.mcpSettings.innerHTML;},get draft(){return state._mcpSettings.draft;}};
}

test('opening interface loads configuration once and never probes or executes a tool',async()=>{
  const x=setup(); x.run('renderIfacePanel()');
  await new Promise(resolve=>setImmediate(resolve));
  x.run('renderIfacePanel()');
  assert.deepEqual(x.requests.map(r=>[r.method,r.url]),[['GET','/api/extensions/mcp']]);
  assert.match(x.view,/暂无 MCP 服务/);
  assert.ok(x.elements.refreshExtensions.onclick);
  assert.match(x.elements.ifaceBox.innerHTML,/未接入，仅保留配置，不会自动收信/);
  assert.doesNotMatch(x.elements.ifaceBox.innerHTML,/自动拉取课程通知与作业/);
});

test('course and public scope labels distinguish global conversations from cross-course access',async()=>{
  const x=setup(config([server()]));await x.load();await x.click('mcpEdit0');
  assert.match(x.view,/<option value="" selected>仅全部课程对话<\/option>/);
  assert.match(x.view,/公共资料（可跨课程）/);
  assert.doesNotMatch(x.view,/不限课程|公共数据范围/);
});

test('add, edit and delete send complete revisioned CRUD requests without a connection',async()=>{
  const x=setup(); await x.load(); await x.click('mcpAdd');
  x.input('mcpId','local_docs');x.input('mcpLabel','Local docs');x.input('mcpCommand','/usr/bin/node');
  x.input('mcpArgs','["/tmp/server.js", "--readonly"]');x.input('mcpEnv','DOCS_TOKEN, DOCS_ROOT');
  await x.input('mcpWorkspace','work');x.input('mcpCourse','Accounting');x.input('mcpTimeout','8');
  x.input('mcpEnabled',true);x.input('mcpPublic',true);x.input('mcpNetwork',true);x.input('mcpTrusted',true);
  await x.click('mcpSave');
  assert.deepEqual(x.requests[2],{url:'/api/extensions/mcp',method:'PUT',body:{revision:'revision-1',servers:[{id:'local_docs',label:'Local docs',transport:'stdio',command:'/usr/bin/node',args:['/tmp/server.js','--readonly'],env_names:['DOCS_TOKEN','DOCS_ROOT'],workspace:'work',course:'Accounting',network:true,public_data:true,timeout_seconds:8,enabled:true,trusted:true,tools:{}}]}});
  await x.click('mcpEdit0');x.input('mcpLabel','Renamed');await x.click('mcpSave');
  assert.equal(x.requests[3].body.revision,'revision-2');
  assert.equal(x.requests[3].body.servers[0].label,'Renamed');
  await x.click('mcpDelete0');
  assert.deepEqual(x.requests[4].body,{servers:[],revision:'revision-2'});
  assert.equal(x.draft,null);
  assert.ok(x.requests.every(r=>r.url==='/api/extensions/mcp'||r.url==='/api/courses?workspace=work'));
});

test('saving an MCP server returns to the list and keeps stable edit and add entry points',async()=>{
  const x=setup(); await x.load(); await x.click('mcpAdd');
  x.input('mcpId','first');x.input('mcpLabel','First');x.input('mcpCommand','/usr/bin/node');
  await x.click('mcpSave');
  assert.equal(x.draft,null);
  assert.match(x.view,/First/);
  assert.doesNotMatch(x.view,/mcpEditor/);
  assert.equal(x.elements.mcpAdd.disabled,false);
  await x.click('mcpEdit0');
  assert.equal(x.draft.config.id,'first');
  await x.click('mcpCancel');
  await x.click('mcpAdd');
  assert.equal(x.draft.index,-1);
});

test('workspace course loading clears scope, filters workspace_key and ignores stale responses',async()=>{
  const x=setup(config([server({course:'Accounting'})]));await x.load();await x.click('mcpEdit0');
  let resolve;
  x.replies.push(new Promise(done=>{resolve=done;}));
  const pending=x.input('mcpWorkspace','work');
  assert.equal(x.requests.at(-1).url,'/api/courses?workspace=work');
  assert.equal(x.draft.config.course,null);assert.equal(x.elements.mcpCourse.disabled,true);
  assert.doesNotMatch(x.view,/>Accounting<\/option>/);
  await x.input('mcpWorkspace','studypal');
  resolve({status:200,body:[{name:'Projects',workspace_key:'work'},{name:'Wrong course',workspace_key:'studypal'}]});await pending;
  assert.equal(x.elements.mcpWorkspace.value,'studypal');assert.match(x.view,/>Accounting<\/option>/);assert.doesNotMatch(x.view,/>Projects<\/option>/);
  const count=x.requests.length;await x.input('mcpWorkspace','work');
  assert.equal(x.requests.length,count);assert.match(x.view,/>Projects<\/option>/);assert.doesNotMatch(x.view,/>Wrong course<\/option>/);
  assert.equal(x.draft.config.course,null);assert.equal(x.elements.mcpCourse.disabled,false);
});

test('course errors preserve draft and retry fetches that workspace; opening other workspace loads its courses',async()=>{
  const x=setup(config([server({workspace:'work',course:'Projects'})]));await x.load();
  x.replies.push({status:503,body:{}});await x.click('mcpEdit0');
  assert.match(x.view,/课程载入失败/);assert.equal(x.draft.config.course,'Projects');
  x.input('mcpLabel','Keep edits');x.replies.push({status:200,body:[{name:'Projects',workspace_key:'work'}]});
  await x.click('mcpRetryCourses');assert.equal(x.draft.config.label,'Keep edits');assert.doesNotMatch(x.view,/课程载入失败/);
  assert.equal(x.requests.filter(r=>r.url==='/api/courses?workspace=work').length,2);
});

test('repeated workspace selection shares pending fetch and a closed draft stays closed',async()=>{
  const x=setup(config([server()]));await x.load();await x.click('mcpEdit0');
  let resolve;x.replies.push(new Promise(done=>{resolve=done;}));
  const first=x.input('mcpWorkspace','work');await x.input('mcpWorkspace','studypal');
  const second=x.input('mcpWorkspace','work');
  assert.equal(x.requests.filter(r=>r.url==='/api/courses?workspace=work').length,1);
  await x.click('mcpCancel');
  resolve({status:200,body:[{name:'Projects',workspace_key:'work'}]});await Promise.all([first,second]);
  assert.equal(x.draft,null);assert.equal(x.elements.mcpCourse,undefined);
});

test('missing environment names are shown per server and escaped',async()=>{
  const x=setup(config([server(),server({id:'other',label:'Other'})],{missing_env:{docs:['DOCS_TOKEN','<img src=x onerror=alert(1)>'],other:['OTHER_TOKEN']}}));
  await x.load();assert.match(x.view,/缺少环境变量：DOCS_TOKEN/);assert.match(x.view,/缺少环境变量：OTHER_TOKEN/);
  assert.doesNotMatch(x.view,/<img/);assert.match(x.view,/&lt;img/);
});

test('HTTP configuration omits local fields and only sends the bearer environment name',async()=>{
  const x=setup(); await x.load(); await x.click('mcpAdd');
  x.input('mcpId','remote');x.input('mcpLabel','Remote');x.input('mcpTransport','streamable_http');
  x.input('mcpUrl','https://example.com/mcp');x.input('mcpBearer','MCP_TOKEN');x.input('mcpNetwork',true);
  await x.click('mcpSave');
  const sent=x.requests.at(-1).body.servers[0];
  assert.equal(sent.transport,'streamable_http');assert.equal(sent.bearer_env,'MCP_TOKEN');
  assert.equal(sent.url,'https://example.com/mcp');assert.ok(!('command' in sent));assert.ok(!('args' in sent));
  assert.match(x.view,/远程 OAuth 暂不支持/);await x.click('mcpEdit0');assert.match(x.view,/桌面应用的运行环境/);
});

for(const failure of [{status:409,body:{detail:'stale'}},{status:422,body:{detail:'invalid'}},new Error('Connection refused')]) {
  test('failed save preserves draft, JSON text and revision: '+(failure.status||'network'),async()=>{
    const x=setup(config([server()]));await x.load();await x.click('mcpEdit0');
    x.input('mcpLabel','Unsaved');x.input('mcpFixed0','{"limit": 11}');const draft=x.draft;
    x.replies.push(failure);await x.click('mcpSave');
    assert.equal(x.draft,draft);assert.equal(x.draft.config.label,'Unsaved');assert.equal(x.elements.mcpFixed0.value,'{"limit": 11}');
    assert.equal(x.state._mcpSettings.revision,'revision-1');assert.equal(x.elements.mcpSave.disabled,false);
    assert.match(x.view,/role="alert"/);
    if(failure.status===409)assert.match(x.view,/409.*草稿已保留/);
  });
}

test('explicit trust gates discovery and connection failure preserves configured tools',async()=>{
  const x=setup(config([server({trusted:false})]));await x.load();await x.click('mcpEdit0');
  assert.equal(x.elements.mcpProbe.disabled,true);await x.run('probeMcpServer()');assert.equal(x.requests.length,1);
  x.input('mcpTrusted',true);x.input('mcpFixed0','{"limit": 7}');
  x.replies.push({status:200,body:{ok:false,error:'Connection failed'}});await x.click('mcpProbe');
  assert.equal(x.requests.at(-1).url,'/api/extensions/mcp/probe');assert.equal(x.requests.at(-1).body.server.trusted,true);
  assert.equal(x.requests.at(-1).body.server.tools.read.fixed_arguments.limit,7);
  assert.match(x.view,/Connection failed/);assert.match(x.view,/已配置，尚未重新发现/);
  assert.equal(x.elements.mcpFixed0.value,'{"limit": 7}');assert.equal(x.draft.dirty,true);
  x.input('mcpCommand','/usr/bin/other');assert.equal(x.draft.config.trusted,false);assert.equal(x.elements.mcpProbe.disabled,true);
});

test('discovery allows only eligible readonly tools, preserves unseen configured tools and fixed JSON',async()=>{
  const x=setup(config([server()]));await x.load();await x.click('mcpEdit0');
  x.replies.push({status:200,body:{ok:true,tools:[
    {name:'find',description:'Find',input_schema:{type:'object'},read_only:true,eligible:true},
    {name:'write',description:'Write',read_only:false,eligible:true,reason:'Writes data'},
    {name:'blocked',read_only:true,eligible:false,reason:'Unavailable'}]}});
  await x.click('mcpProbe');
  assert.equal(x.elements.mcpTool0.disabled,false);assert.equal(x.elements.mcpTool1.disabled,true);assert.equal(x.elements.mcpTool2.disabled,true);
  assert.match(x.view,/已配置，尚未重新发现/);assert.ok(x.draft.config.tools.read);
  x.input('mcpTool1',true);assert.ok(!x.draft.config.tools.write);
  x.input('mcpTool0',true);x.input('mcpFixed0','{"query":"example"}');await x.click('mcpSave');
  assert.deepEqual(x.requests.at(-1).body.servers[0].tools.find,{read_only:true,description:'Find',fixed_arguments:{query:'example'}});
});

test('discovered descriptions normalize control characters and whitespace before the 400 character limit',async()=>{
  const x=setup(config([server()]));await x.load();await x.click('mcpEdit0');
  const description='  Look\n\tup\u0000documents\u007f\u0085 now  '+ 'x'.repeat(500);
  x.replies.push({status:200,body:{ok:true,tools:[{name:'find',description,read_only:true,eligible:true}]}});
  await x.click('mcpProbe');x.input('mcpTool0',true);await x.click('mcpSave');
  const saved=x.requests.at(-1).body.servers[0].tools.find.description;
  assert.equal(saved,('Look up documents now '+'x'.repeat(500)).slice(0,400));
  assert.equal(saved.length,400);assert.doesNotMatch(saved,/[\u0000-\u001f\u007f-\u009f]/);
  await x.click('mcpEdit0');x.draft.config.tools.find.description='Existing\n\t description';
  await x.click('mcpSave');assert.equal(x.requests.at(-1).body.servers[0].tools.find.description,'Existing description');
});

test('invalid fixed arguments remain editable, block save and execution, and recover after valid reprobe',async()=>{
  const x=setup(config([server()]));await x.load();await x.click('mcpEdit0');
  x.input('mcpFixed0','{"limit":"wrong"}');
  const invalid={name:'read',read_only:true,eligible:false,reason:'固定参数不符合工具定义',input_schema:{type:'object',properties:{limit:{type:'integer'}}}};
  x.replies.push({status:200,body:{ok:true,tools:[invalid]}});await x.click('mcpProbe');
  assert.ok(x.draft.config.tools.read);assert.equal(x.elements.mcpTool0.checked,true);
  assert.equal(x.elements.mcpTool0.disabled,true);assert.equal(x.elements.mcpFixed0.value,'{"limit":"wrong"}');
  assert.match(x.view,/固定参数不符合工具定义/);assert.ok(x.elements.mcpRemoveTool0);
  assert.equal(x.elements.mcpTest.disabled,true);
  x.input('mcpFixed0','{"limit":7}');const count=x.requests.length;
  await x.click('mcpSave');await x.run('testMcpTool()');assert.equal(x.requests.length,count);
  x.replies.push({status:200,body:{ok:false,error:'Connection failed'}});await x.click('mcpProbe');
  assert.equal(x.elements.mcpFixed0.value,'{"limit":7}');assert.equal(x.elements.mcpTest.disabled,true);
  x.replies.push({status:200,body:{ok:true,tools:[]}});await x.click('mcpProbe');
  assert.equal(x.elements.mcpTool0.disabled,true);assert.equal(x.elements.mcpFixed0.value,'{"limit":7}');
  const missingCount=x.requests.length;await x.click('mcpSave');assert.equal(x.requests.length,missingCount);
  x.replies.push({status:200,body:{ok:true,tools:[{...invalid,eligible:true,reason:''}]}});await x.click('mcpProbe');
  assert.equal(x.requests.at(-1).body.server.tools.read.fixed_arguments.limit,7);
  assert.equal(x.elements.mcpTest.disabled,true); // Corrected draft must still be saved.
  await x.click('mcpSave');assert.equal(x.requests.at(-1).body.servers[0].tools.read.fixed_arguments.limit,7);
  assert.equal(x.draft,null);await x.click('mcpEdit0');assert.equal(x.elements.mcpTest.disabled,false);
});

test('probe preserves a selected tool that is no longer readonly and permits explicit removal',async()=>{
  const x=setup(config([server()]));await x.load();await x.click('mcpEdit0');
  x.replies.push({status:200,body:{ok:true,tools:[{name:'read',read_only:false,eligible:false,reason:'服务未声明只读'}]}});await x.click('mcpProbe');
  assert.ok(x.draft.config.tools.read);assert.equal(x.elements.mcpTest.disabled,true);
  const count=x.requests.length;await x.run('testMcpTool()');assert.equal(x.requests.length,count);
  await x.click('mcpRemoveTool0');assert.ok(!x.draft.config.tools.read);
  await x.click('mcpSave');assert.deepEqual(x.requests.at(-1).body.servers[0].tools,{});
});

test('direct tool test sends saved scope and optional arguments without a model request',async()=>{
  const x=setup(config([server({course:'Accounting'})]));await x.load();await x.click('mcpEdit0');
  x.input('mcpTestArgs','{"query":"invoice"}');
  x.replies.push({status:200,body:{ok:true,result:{text:'Found document'}}});await x.click('mcpTest');
  assert.deepEqual(x.requests.at(-1),{url:'/api/extensions/mcp/test',method:'POST',body:{id:'docs',tool:'read',arguments:{query:'invoice'},workspace:'studypal',course:'Accounting',web_mode:'auto'}});
  assert.match(x.view,/Found document/);assert.match(x.view,/未调用模型/);
  x.input('mcpFixed0','{"limit":4}');assert.equal(x.elements.mcpTest.disabled,true);
  await x.run('testMcpTool()');assert.equal(x.requests.length,2);
});

test('server names, descriptions, schemas, errors and results are escaped',async()=>{
  const attack='\"><img src=x onerror="alert(1)"></textarea><script>alert(1)</script>';
  const x=setup(config([server({label:attack,tools:{read:{read_only:true,description:attack,fixed_arguments:{query:attack}}}})]));
  x.state.workspaces.push({key:attack,name:attack});x.state.courses.push({name:attack});
  await x.load();await x.click('mcpEdit0');
  assert.doesNotMatch(x.view,/<img|<script>/);assert.match(x.view,/&lt;img/);
  x.replies.push({status:200,body:{ok:true,tools:[{name:'read',description:attack,input_schema:{description:attack},read_only:true,eligible:true}]}});await x.click('mcpProbe');
  assert.doesNotMatch(x.view,/<img|<script>/);
  x.replies.push({status:200,body:{ok:true,result:{text:attack}}});await x.click('mcpTest');
  assert.doesNotMatch(x.view,/<img|<script>/);assert.match(x.view,/&lt;script&gt;/);
  x.replies.push({status:422,body:{detail:attack}});await x.click('mcpProbe');assert.doesNotMatch(x.view,/<img|<script>/);
});

test('SDK unavailable and endpoint failures are visible and do not discard unsaved edits',async()=>{
  const x=setup(config([server()],{sdk_installed:false}));await x.load();await x.click('mcpEdit0');
  assert.match(x.view,/依赖未安装/);assert.equal(x.elements.mcpProbe.disabled,true);assert.equal(x.elements.mcpTest.disabled,true);
  x.input('mcpLabel','Keep me');x.replies.push({status:503,body:{detail:'MCP unavailable'}});await x.click('mcpReload');
  assert.equal(x.draft.config.label,'Keep me');assert.match(x.view,/MCP unavailable/);
  const y=setup();y.replies.push({status:404,body:{detail:'Not Found'}});await y.load();
  assert.match(y.view,/404/);assert.equal(y.elements.mcpAdd.disabled,true);assert.equal(y.elements.mcpReload.disabled,false);
});

test('invalid JSON and secret-shaped environment entries fail locally without losing text',async()=>{
  const x=setup(config([server()]));await x.load();await x.click('mcpEdit0');
  x.input('mcpArgs','[invalid');await x.click('mcpSave');assert.equal(x.requests.length,1);assert.equal(x.elements.mcpArgs.value,'[invalid');
  x.input('mcpArgs','[]');x.input('mcpEnv','MCP_TOKEN=not-a-real-token');await x.click('mcpSave');
  assert.equal(x.requests.length,1);assert.match(x.view,/不可填写密钥值/);
  x.input('mcpEnv','MCP_TOKEN');x.input('mcpFixed0','[]');await x.click('mcpSave');assert.equal(x.requests.length,1);assert.match(x.view,/JSON 对象/);
});

test('cancelled reload, deletion and editor switch do not lose a dirty draft',async()=>{
  const x=setup(config([server()]));await x.load();await x.click('mcpEdit0');x.input('mcpLabel','Keep');x.confirm(false);
  const draft=x.draft;await x.click('mcpReload');await x.click('mcpDelete0');await x.click('mcpAdd');await x.click('mcpCancel');
  assert.equal(x.draft,draft);assert.equal(x.requests.length,1);assert.equal(x.confirms.length,4);
});

test('four services remain independently editable and add entry survives repeated saves',async()=>{
  const x=setup();await x.load();
  for(let i=0;i<4;i++){
    await x.click('mcpAdd');x.input('mcpId','service_'+i);x.input('mcpLabel','Service '+i);x.input('mcpCommand','/usr/bin/node');await x.click('mcpSave');
    assert.equal(x.draft,null);assert.equal(x.state._mcpSettings.servers.length,i+1);assert.equal(x.elements.mcpAdd.disabled,false);
  }
  for(let i=0;i<4;i++){
    await x.click('mcpEdit'+i);assert.equal(x.draft.config.id,'service_'+i);x.input('mcpLabel','Renamed '+i);await x.click('mcpSave');
  }
  assert.deepEqual(Array.from(x.state._mcpSettings.servers,s=>s.label),['Renamed 0','Renamed 1','Renamed 2','Renamed 3']);
  await x.load();assert.equal(x.state._mcpSettings.servers.length,4);
});
