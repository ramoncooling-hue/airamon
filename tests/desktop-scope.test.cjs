const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname,'../web/index.html'),'utf8');
const shell = fs.readFileSync(path.join(__dirname,'../web/assets/desktop-shell.js'),'utf8');
const apiSource = html.slice(html.indexOf('async function api(path, params)'),html.indexOf('async function apiPost('));
function harness(fetch){
  const c={S:{workspace:'workpal',workspaceEpoch:0},API:'',URL,location:{href:'http://localhost:8899/'},fetch};
  vm.createContext(c);vm.runInContext(apiSource,c);return c;
}
test('knowledge requests carry the active workspace even without caller params',async()=>{
  let requested;
  const c=harness(async url=>{requested=new URL(url);return {ok:true,json:async()=>[]};});
  await c.api('/api/kp');
  assert.equal(requested.searchParams.get('workspace'),'workpal');
  await c.api('/api/graph',{course:'产品设计'});
  assert.equal(requested.searchParams.get('workspace'),'workpal');
  assert.equal(requested.searchParams.get('course'),'产品设计');
});
test('late JSON body from a previous workspace cannot update the active workspace',async()=>{
  let resolve,started;
  const ready=new Promise(r=>{started=r;});
  const c=harness(async()=>({ok:true,json:()=>new Promise(r=>{resolve=r;started();})}));
  const pending=c.api('/api/kp');await ready;
  c.S.workspace='lifepal';c.S.workspaceEpoch++;
  resolve([{id:123,name:'old workspace'}]);
  await assert.rejects(pending,/工作区已切换/);
});
test('switching away and back still rejects the old workspace generation',async()=>{
  let resolve,started;
  const ready=new Promise(r=>{started=r;});
  const c=harness(async()=>({ok:true,json:()=>new Promise(r=>{resolve=r;started();})}));
  const pending=c.api('/api/graph',{course:'产品设计'});await ready;
  c.S.workspaceEpoch+=2;resolve({nodes:[{id:123}]});
  await assert.rejects(pending,/工作区已切换/);
});
test('course and mastery navigation depend on workspace type, not just the seed name',()=>{
  const source=shell.slice(shell.indexOf('function isStudyWorkspace(){'),shell.indexOf('for(const [id,name,ic]'));
  const navSource=shell.match(/visibleNav = function\(\)\{[^\n]+/)[0];
  const c={S:{workspace:'custom-study'},activeWorkspace:()=>({kind:'study'}),NAV:['course','mastery','graph','agent','skill','mcp'].map(id=>({id}))};
  vm.createContext(c);vm.runInContext(source+navSource,c);
  assert.equal(c.visibleNav().length,6);
  c.activeWorkspace=()=>({kind:'life'});
  assert.deepEqual(Array.from(c.visibleNav(),n=>n.id),['graph','agent','skill','mcp']);
});
