const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(require('node:path').join(__dirname, '../web/index.html'), 'utf8');
const code = html.slice(html.indexOf('const PRESETS = ['), html.indexOf('async function loadSettings()'));

const profile = (id, extra = {}) => ({id, name:id, provider:'custom', base_url:'https://example.com/v1', model:id, api_key:'sk-...abcd', api_key_set:true, ...extra});

function setup({saveOk = true, models = {profiles:[profile('fast-model'), profile('reasoning-model'), profile('extra-model')], routes:{fast:'fast-model', reasoning:'reasoning-model'}, revision:'models-1'}} = {}) {
  const elements = {}, requests = [], messages = [], replies=[],fields={};
  const decode = text => String(text||'').replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&');
  function element(id) {
    return elements[id] ||= {id, value:'', innerHTML:'', textContent:id, disabled:false, checked:false,
      querySelector: selector => fields[selector] || null,
      querySelectorAll: selector => Object.entries(fields).filter(([key])=>key.startsWith(selector+'[')).map(([,node])=>node)};
  }
  const modelBox = element('modelBox');
  Object.defineProperty(modelBox, 'innerHTML', {get(){return this._html||'';}, set(value){
    this._html=value;
    for(const match of value.matchAll(/<(input|select|button|textarea)\b([^>]*\bid="([^"]+)"[^>]*)>(?:([\s\S]*?)<\/\1>)?/g)){
      const [,tag,attrs,id,body]=match; const node=element(id); node.disabled=/\bdisabled\b/.test(attrs); node.value=decode((attrs.match(/\bvalue="([^"]*)"/)||[])[1]);
      node.checked=/\bchecked\b/.test(attrs); if(tag==='textarea') node.value=decode(body);
      if(tag==='select'){const opts=[...(body||'').matchAll(/<option value="([^"]*)"([^>]*)>/g)];node.value=decode((opts.find(o=>o[2].includes('selected'))||opts[0]||[])[1]);}
    }
    for(const match of value.matchAll(/<(input|select)\b([^>]*\bclass="(m\w+)"[^>]*\bdata-tier="([^"]+)"[^>]*)>(?:([\s\S]*?)<\/\1>)?/g)){
      const [,tag,attrs,cls,tier,body]=match,node={value:decode((attrs.match(/\bvalue="([^"]*)"/)||[])[1]),dataset:{tier}};
      if(tag==='select'){const opts=[...(body||'').matchAll(/<option value="([^"]*)"([^>]*)>/g)];node.value=decode((opts.find(o=>o[2].includes('selected'))||opts[0]||[])[1]);}
      fields[`.${cls}[data-tier="${tier}"]`]=node;
    }
  }});
  const state = {_cfg: {}, _configured: {}, _models:models};
  const context = vm.createContext({S:state, API:'', location:{origin:'http://127.0.0.1'}, window:{confirm:()=>true}, el:element, esc:s=>String(s), toast:s=>messages.push(s),
    fetch: async (url, options={}) => {
      const body=options.body?JSON.parse(options.body):undefined; requests.push({url, method:options.method||'GET', body});
      if(replies.length){const reply=replies.shift();if(reply instanceof Error)throw reply;return {ok:reply.status<400,status:reply.status,json:async()=>reply.body};}
      if(url.endsWith('/models') && !options.method) return {ok:true,status:200,json:async()=>models};
      if(url.endsWith('/models') && options.method==='PUT'){
        if(saveOk){models={profiles:body.profiles.map(p=>({...p,api_key_set:p.clear_api_key?false:true,api_key:p.clear_api_key?'':'****abcd'})),routes:body.routes,revision:'models-2'};}
        return {ok:saveOk,status:saveOk?200:500,json:async()=>models};
      }
      if(url.endsWith('/test'))return {ok:true,status:200,json:async()=>({ok:true,reply:'ok',key_set:true})};
      if(url==='/api/settings')return {ok:saveOk,status:saveOk?200:500,json:async()=>({config:{fast:{model:'legacy',api_key_set:true}},configured:{fast:true,reasoning:false}})};
      return {ok:false,status:404,json:async()=>({detail:'unexpected old settings endpoint'})};
    }});
  vm.runInContext(code, context);
  const run=source=>vm.runInContext(source,context);
  const input=(id,value)=>{const node=element(id);if(typeof value==='boolean')node.checked=value;else node.value=value;return (node.oninput||node.onchange)();};
  return {elements, requests, messages, state, context, models, replies,fields,run,input};
}

test('model list loads through the dedicated API and renders both legacy routes', async () => {
  const x = setup();
  await vm.runInContext('loadModelProfiles()', x.context);
  vm.runInContext('renderModelPanel()', x.context);
  assert.deepEqual(x.requests.map(r=>r.url), ['/api/settings/models']);
  assert.match(x.elements.modelBox.innerHTML, /快速回答/);
  assert.match(x.elements.modelBox.innerHTML, /深度分析/);
  assert.match(x.elements.modelBox.innerHTML, /fast-model/);
  assert.match(x.elements.modelBox.innerHTML, /新增模型/);
});

test('settings categories start collapsed and empty legacy keys are not cleared',async()=>{
  const x=setup();await x.run('loadModelProfiles()');x.run('renderModelPanel()');
  assert.doesNotMatch(x.elements.modelBox.innerHTML, /id="modelSection"\s+open/);
  assert.doesNotMatch(html, /settingsSectionOpen\('mcp',true\)/);
  x.fields['.mKey[data-tier="fast"]'].value='';
  await x.elements.saveModel.onclick();
  const req=x.requests.find(r=>r.url==='/api/settings'&&r.method==='PUT');
  assert.equal(Object.hasOwn(req.body.fast,'api_key'),false);
});

test('model profile save uses PUT models and omits an empty masked key', async () => {
  const x = setup();
  await vm.runInContext('loadModelProfiles()', x.context);
  vm.runInContext('renderModelPanel()', x.context);
  x.elements['modelProfile-name-extra-model'].value = 'Extra renamed';
  x.elements['modelProfile-key-extra-model'].value = '';
  await x.elements['modelProfileSave-extra-model'].onclick();
  const request = x.requests.at(-1);
  assert.equal(request.url, '/api/settings/models');
  assert.equal(request.method, 'PUT');
  assert.equal(request.body.revision, 'models-1');
  assert.equal(request.body.profiles.find(p=>p.id==='extra-model').name, 'Extra renamed');
  assert.ok(!Object.hasOwn(request.body.profiles.find(p=>p.id==='extra-model'), 'api_key'));
  assert.match(x.messages.at(-1), /模型配置已保存/);
});

test('referenced model cannot be deleted while an unreferenced model can be deleted', async () => {
  const x = setup();
  await vm.runInContext('loadModelProfiles()', x.context);
  vm.runInContext('renderModelPanel()', x.context);
  assert.equal(x.elements['modelProfileDelete-fast-model'].disabled, true);
  assert.equal(x.elements['modelProfileDelete-extra-model'].disabled, false);
});

test('model and external interface sections are collapsible while agent cards keep an add entry', () => {
  assert.match(html, /<details[^>]+id="modelSection"/);
  assert.match(html, /<details[^>]+id="mcpSection"/);
  assert.match(html, /<details[^>]+id="emailSection"/);
  assert.match(html, /id="newAgentBtn"/);
  assert.match(html, /data-agent-details/);
});

test('skill orchestration exposes create, edit, revision and live binding paths', () => {
  assert.match(html, />技能编排</);
  assert.match(html, /id="newSkillBtn"/);
  assert.match(html, /id="skillDialogBackdrop"/);
  assert.match(html, /id="skillRevisionBackdrop"/);
  assert.match(html, /data-skill-edit=/);
  assert.match(html, /data-skill-revisions=/);
  assert.match(html, /function openSkillDialog/);
  assert.match(html, /function openSkillRevisions/);
  assert.match(html, /\/api\/hub\/skill\//);
  assert.match(html, /\/api\/hub\/revisions\/restore/);
  assert.match(html, /bound_agent_count/);
  assert.match(html, /renderAgentSkillChoices/);
  assert.match(html, /workflowOptions\('skills'/);
});

test('desktop shell exposes a guarded natural-language configuration assistant', () => {
  const shell = fs.readFileSync(require('node:path').join(__dirname, '../web/assets/desktop-shell.js'), 'utf8');
  assert.match(shell, /\/api\/config-assistant\/preview/);
  assert.match(shell, /\/api\/config-assistant\/confirm/);
  assert.match(shell, /确认创建/);
  assert.match(shell, /模型只生成草稿/);
});

test('desktop settings directory separates model, routing, search and extensions', () => {
  const shell = fs.readFileSync(require('node:path').join(__dirname, '../web/assets/desktop-shell.js'), 'utf8');
  for(const key of ['models','routing','search','extensions']) assert.match(shell, new RegExp(`\\['${key}',`));
  assert.match(shell, /settingsPanelTitle/);
  assert.match(html, /id="settingsModelCard"/);
  assert.match(html, /id="settingsInterfaceCard"/);
});

for(const failure of [{status:409,body:{}},{status:422,body:{}},new Error('Connection refused')]){
  test('model failed write preserves raw draft and revision: '+(failure.status||'network'),async()=>{
    const x=setup();await x.run('loadModelProfiles()');
    x.input('modelProfile-name-extra-model','Draft name');x.input('modelProfile-key-extra-model','synthetic-key');
    x.replies.push(failure);await x.elements['modelProfileSave-extra-model'].onclick();
    assert.equal(x.elements['modelProfile-name-extra-model'].value,'Draft name');
    assert.equal(x.elements['modelProfile-key-extra-model'].value,'synthetic-key');
    assert.equal(x.state._modelSettings.revision,'models-1');
    assert.equal(x.elements['modelProfileSave-extra-model'].disabled,false);
    assert.match(x.elements.modelBox.innerHTML,/草稿已保留/);
  });
}

test('explicit key removal sends clear_api_key, while mask input never overwrites a key',async()=>{
  const x=setup();await x.run('loadModelProfiles()');
  x.input('modelProfile-key-extra-model','****abcd');await x.elements['modelProfileSave-extra-model'].onclick();
  assert.ok(!Object.hasOwn(x.requests.at(-1).body.profiles.find(p=>p.id==='extra-model'),'api_key'));
  x.input('modelProfile-clear-extra-model',true);await x.elements['modelProfileSave-extra-model'].onclick();
  assert.equal(x.requests.at(-1).body.profiles.find(p=>p.id==='extra-model').clear_api_key,true);
});

test('four new profiles save, routes bind both tiers, and reload retains the catalog',async()=>{
  const x=setup({models:{profiles:[],routes:{fast:'',reasoning:''},revision:'models-1'}});await x.run('loadModelProfiles()');
  for(let i=0;i<4;i++){
    x.elements.modelProfileAdd.onclick();
    for(const [name,value]of Object.entries({id:'model'+i,name:'Model '+i,model:'model-'+i,base_url:'https://example.com/v1',key:'synthetic-'+i}))x.input('modelProfile-'+name+'-__new__',value);
    await x.elements['modelProfileSave-__new__'].onclick();assert.equal(x.state._modelSettings.newDraft,null);
  }
  x.elements['modelRoute-fast'].value='model0';x.elements['modelRoute-fast'].onchange();
  x.elements['modelRoute-reasoning'].value='model3';x.elements['modelRoute-reasoning'].onchange();
  await x.elements.modelRoutesSave.onclick();
  assert.deepEqual(x.requests.at(-1).body.routes,{fast:'model0',reasoning:'model3'});
  assert.equal(x.elements['modelProfileDelete-model0'].disabled,true);
  await x.run('loadModelProfiles()');assert.equal(x.state._modelSettings.profiles.length,4);
  assert.ok(x.requests.every(r=>r.url==='/api/settings/models'));
});

test('saving one model preserves unsaved drafts for other models',async()=>{
  const x=setup();await x.run('loadModelProfiles()');
  x.input('modelProfile-name-fast-model','Unsaved elsewhere');
  x.input('modelProfile-name-extra-model','Saved here');await x.elements['modelProfileSave-extra-model'].onclick();
  assert.equal(x.elements['modelProfile-name-fast-model'].value,'Unsaved elsewhere');
  assert.equal(x.requests.at(-1).body.profiles.find(p=>p.id==='fast-model').name,'fast-model');
});

test('legacy render failure after a successful write is not reported as a save failure',async()=>{
  const x=setup();const result=await x.run("putSettings({},()=>{throw new Error('display error');})");
  assert.equal(result.configured.fast,true);assert.match(x.messages.at(-1),/配置已保存.*界面刷新失败/);
});

test('vector experiment and mailbox remain explicitly disconnected',async()=>{
  const x=setup();await x.run('loadModelProfiles()');x.state.health={};x.state._extensions={mcp:{sdk_installed:true,servers:[]},vector:{installed:true,indexes:1}};
  x.run('renderIfacePanel()');
  assert.match(x.elements.modelBox.innerHTML,/id="setVec" disabled/);assert.match(x.elements.modelBox.innerHTML,/尚未参与问答/);
  assert.match(x.elements.ifaceBox.innerHTML,/未接入，仅保留配置，不会自动收信/);assert.match(x.elements.ifaceBox.innerHTML,/未参与正式问答/);
});

test('legacy fields survive profile rendering and remain editable when routed to a catalog model',async()=>{
  const x=setup();await x.run('loadModelProfiles()');
  const field=x.fields['.mModel[data-tier="fast"]'];field.value='draft-legacy';(field.oninput||(()=>{}))();
  x.elements.modelProfileAdd.onclick();
  assert.equal(x.fields['.mModel[data-tier="fast"]'].value,'draft-legacy');
});

test('model testing saves current profile and route before testing without a legacy model write',async()=>{
  const x=setup();await x.run('loadModelProfiles()');
  x.input('modelProfile-model-fast-model','changed-model');
  await x.elements.testFast.onclick();
  const models=x.requests.filter(r=>r.method==='PUT'&&r.url==='/api/settings/models');
  assert.equal(models[0].body.profiles.find(p=>p.id==='fast-model').model,'changed-model');
  assert.equal(x.requests.at(-1).url,'/api/settings/test');
  assert.ok(x.requests.filter(r=>r.url==='/api/settings').every(r=>!Object.hasOwn(r.body,'fast')&&!Object.hasOwn(r.body,'reasoning')));
  assert.match(x.elements.testOut.innerHTML,/通了/);
  assert.equal(x.elements.testFast.disabled,false);
});

test('failed model write stops real model test and preserves changed profile',async()=>{
  const x=setup({saveOk:false});await x.run('loadModelProfiles()');x.input('modelProfile-model-fast-model','unsaved-model');
  await x.elements.testFast.onclick();
  assert.ok(!x.requests.some(r=>r.url==='/api/settings/test'));
  assert.equal(x.elements['modelProfile-model-fast-model'].value,'unsaved-model');
  assert.match(x.elements.testOut.innerHTML,/未测试/);
});

test('legacy settings writes refresh the shared model revision while preserving profile drafts',async()=>{
  const x=setup();await x.run('loadModelProfiles()');x.input('modelProfile-name-extra-model','Keep draft');
  x.replies.push({status:200,body:{config:{fast:{}},configured:{}}},{status:200,body:{profiles:x.models.profiles,routes:x.models.routes,revision:'after-legacy'}});
  await x.run('putSettings({retrieval:{top_k:8}})');
  assert.equal(x.state._modelSettings.revision,'after-legacy');
  x.run('renderModelPanel()');assert.equal(x.elements['modelProfile-name-extra-model'].value,'Keep draft');
});

test('profile IDs that match object properties remain independently editable',async()=>{
  const x=setup({models:{profiles:[profile('constructor')],routes:{fast:'constructor',reasoning:''},revision:'models-1'}});
  await x.run('loadModelProfiles()');
  assert.equal(x.elements['modelProfile-name-constructor'].value,'constructor');
  x.input('modelProfile-name-constructor','Renamed constructor');await x.elements['modelProfileSave-constructor'].onclick();
  assert.equal(x.requests.at(-1).body.profiles[0].name,'Renamed constructor');
});

test('mailbox draft survives interface rerenders and state refreshes',async()=>{
  const x=setup();x.state.health={};x.run('renderIfacePanel()');
  const account=x.run("el('chUser')");account.value='synthetic@example.test';(account.oninput||(()=>{}))();
  x.run('renderIfacePanel()');
  assert.match(x.elements.ifaceBox.innerHTML,/value="synthetic@example.test"/);
});

test('a saved profile named new does not collide with the add form',async()=>{
  const x=setup({models:{profiles:[profile('new')],routes:{fast:'new',reasoning:''},revision:'models-1'}});await x.run('loadModelProfiles()');
  x.elements.modelProfileAdd.onclick();
  const ids=[...x.elements.modelBox.innerHTML.matchAll(/\bid="([^"]+)"/g)].map(match=>match[1]);
  assert.equal(new Set(ids).size,ids.length);
});

