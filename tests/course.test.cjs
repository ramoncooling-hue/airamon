const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');

test('course page exposes absolute storage path and local folder actions', () => {
  assert.match(html, /id="runtimeDataPath"/);
  assert.match(html, /data-copy-path/);
  assert.match(html, /data-reveal-path/);
  assert.match(html, /\/api\/runtime\/reveal/);
});

test('course page shows material inventory and extraction quality state', () => {
  assert.match(html, /id="materialInventory"/);
  assert.match(html, /materials\|\|\[\]/);
  assert.match(html, /中文 OCR/);
  assert.match(html, /待识别/);
});

test('course actions use a stable right-aligned action rail', () => {
  assert.match(html, /\.source-row\{[^}]*display:grid/);
  assert.match(html, /source-row-actions/);
  assert.match(html, /grid-template-columns:repeat\(4/);
});

test('course upload sends the raw File body and enforces the 200 MB client guard', () => {
  assert.match(html, /\/api\/intake\/upload/);
  assert.match(html, /body:file/);
  assert.match(html, /file\.size > 200 \* 1024 \* 1024/);
  assert.doesNotMatch(html, /const dataUrl = String\(reader\.result/);
});

test('course page explains the material-to-graph and calendar handoff', () => {
  assert.match(html, /id="courseFlowNotice"/);
  assert.match(html, /待确认知识点/);
  assert.match(html, /data-candidate-batch-all/);
  assert.match(html, /\/api\/sources\/candidates\/review-batch/);
  assert.match(html, /已进入图谱和掌握度/);
  assert.match(html, /待转为日历事项/);
  assert.match(html, /data-go-calendar/);
});

function functionSource(name) {
  const start = html.indexOf(`async function ${name}(`);
  const end = html.indexOf('\nasync function ', start + 1);
  return html.slice(start, end);
}

test('workspace boots from durable profile even on an origin with no local storage', async () => {
  const state = {workspace:'studypal'}; let restoredWorkspace;
  const context = vm.createContext({S:state,
    localStorage:{getItem:()=>null}, renderWorkspacePicker:()=>{}, toast:()=>{}, esc:x=>x,
    restoreChatMcpChoice:()=>{restoredWorkspace=state.workspace;},
    api:async endpoint=>endpoint==='/api/workspaces' ? [{key:'studypal'},{key:'work'}] : {active_workspace_key:'work'}
  });
  vm.runInContext(functionSource('loadWorkspaces'), context);
  await context.loadWorkspaces();
  assert.equal(state.workspace, 'work');
  assert.equal(restoredWorkspace, 'work');
});

test('workspace save failure preserves selection and sends no unrelated profile fields', async () => {
  let sent;
  const state = {workspace:'studypal',workspaces:[{key:'work'}]};
  const context = vm.createContext({S:state,chatSending:false,toast:()=>{},esc:x=>x,
    apiPut:async (endpoint, body)=>{sent={endpoint,body};throw Error('offline');}
  });
  vm.runInContext(functionSource('switchWorkspace'), context);
  await context.switchWorkspace('work');
  assert.equal(sent.endpoint, '/api/agent/profile');
  assert.deepEqual(Object.keys(sent.body), ['active_workspace_key']);
  assert.equal(state.workspace, 'studypal');
  assert.equal(state.workspaceSwitching, false);
});

test('course task polling distinguishes archived, running and review states', () => {
  const start = html.indexOf('function ingestStatusText(');
  const end = html.indexOf('\nasync function renderIngestJobs', start);
  const context = vm.createContext({});
  vm.runInContext(html.slice(start,end),context);
  assert.match(context.ingestStatusText({status:'running',stage:'ocr',pages_done:3,pages_total:10}), /3\/10 页/);
  assert.match(context.ingestStatusText({status:'waiting_review'}), /待确认候选/);
  assert.match(context.ingestStatusText({status:'interrupted'}), /可重试/);
  assert.match(html, /data-ingest-action="cancel"/);
  assert.match(html, /data-ingest-action="retry"/);
  assert.match(html, /canonical_path/);
});

test('first terminal poll refreshes materials once without starting a polling loop', async () => {
  const start = html.indexOf('let ingestPollTimer = null;');
  const end = html.indexOf('\nasync function renderSourceStatus', start);
  const refreshes = [];
  let timers = 0;
  const box = {innerHTML:'',querySelectorAll:()=>[]};
  const context = vm.createContext({S:{chatCourse:'course'},el:()=>box,esc:x=>x,
    clearTimeout:()=>{},setTimeout:()=>{timers++;},
    api:async()=>[{id:1,status:'waiting_review',path:'/tmp/notes.md'}],
    renderSourceStatus:(...args)=>refreshes.push(args)
  });
  vm.runInContext(html.slice(start,end),context);
  await context.renderIngestJobs('course');
  assert.deepEqual(refreshes, [['course', false]]);
  await context.renderIngestJobs('course');
  assert.equal(refreshes.length, 1);
  assert.equal(timers, 0);
});
