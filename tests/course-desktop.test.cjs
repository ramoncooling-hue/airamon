const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const scriptPath = path.join(__dirname, '../web/assets/course-desktop.js');
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));

function element() {
  return {dataset:{}, hidden:false, disabled:false, value:'', textContent:'', innerHTML:'',
    classList:{add(){}, toggle(){}}, setAttribute(name, value){this[name]=String(value);},
    querySelectorAll(){return [];}, querySelector(){return null;}, focus(){}};
}

function runtime(overrides = {}) {
  assert.ok(fs.existsSync(scriptPath), 'course desktop enhancement must exist');
  const elements = new Map();
  const el = id => {if(!elements.has(id)) elements.set(id, element()); return elements.get(id);};
  el('p-course').dataset.courseDesktop = 'true';
  const requests = [], posts = [], messages = [], originals = [];
  const S = {page:'course', workspace:'study-a', chatCourse:'Accounting',
    courses:[{name:'Accounting',workspace_key:'study-a',kp_count:1}], kp:[]};
  const context = vm.createContext({S,el,esc:escapeHtml,console,setTimeout,clearTimeout,
    document:{getElementById:el,createElement:element},
    isStudyWorkspace:()=>true, M_ORDER:['熟练','会用','读过','未读'],
    renderCourse:()=>originals.push('course'),
    renderSourceStatus:async(...args)=>originals.push(args),
    api:async(endpoint, params)=>{requests.push({endpoint,params});return [];},
    apiPost:async(endpoint, body)=>{posts.push({endpoint,body});return {ok:true,changed:true};},
    toast:text=>messages.push(text), go:page=>{S.page=page;},
    ...overrides});
  vm.runInContext(fs.readFileSync(scriptPath,'utf8'), context, {filename:scriptPath});
  return {context,S,el,requests,posts,messages,originals};
}

function deferred() {let resolve;const promise=new Promise(done=>{resolve=done;});return {promise,resolve};}
const point = (id=7, extra={}) => ({id,name:'Balance sheet',course_name:'Accounting',chapter:'第2章',mastery:'未读',source:'textbook.pdf',...extra});

test('course knowledge requests explicit course and workspace, rendering only API rows', async () => {
  const env=runtime();
  env.context.api=async(endpoint,params)=>{env.requests.push({endpoint,params});return [point()];};
  await env.context.courseDesktopRenderKnowledge();
  assert.equal(env.requests[0].endpoint,'/api/kp');
  assert.equal(env.requests[0].params.course,'Accounting');
  assert.equal(env.requests[0].params.workspace,'study-a');
  assert.match(env.el('courseKnowledgeList').innerHTML,/Balance sheet/);
  assert.match(env.el('courseKnowledgeList').innerHTML,/data-course-mastery="7"/);
});

test('empty workspace performs no fallback course or source request', async () => {
  const env=runtime();env.S.courses=[];
  await env.context.renderCourse();
  await env.context.courseDesktopRenderKnowledge();
  assert.equal(env.requests.length,0);
  assert.equal(env.originals.length,0);
  assert.equal(env.S.chatCourse,'');
  assert.match(env.el('courseKnowledgeList').innerHTML,/暂无/);
  assert.equal(env.el('courseUploadToggle').disabled,true);
});

test('knowledge load discards responses from a previous workspace', async () => {
  const pending=deferred();const env=runtime({api:()=>pending.promise});
  const loading=env.context.courseDesktopRenderKnowledge();
  env.S.workspace='study-b';env.S.chatCourse='Other';env.S.courses=[{name:'Other',workspace_key:'study-b'}];
  pending.resolve([point()]);await loading;
  assert.doesNotMatch(env.el('courseKnowledgeList').innerHTML,/Balance sheet/);
});

test('only the newest refresh may replace knowledge rows', async () => {
  const first=deferred(),second=deferred();let count=0;
  const env=runtime({api:()=>++count===1?first.promise:second.promise});
  const old=env.context.courseDesktopRenderKnowledge();const fresh=env.context.courseDesktopRenderKnowledge();
  second.resolve([point(8,{name:'Latest'})]);await fresh;
  first.resolve([point(7,{name:'Stale'})]);await old;
  assert.match(env.el('courseKnowledgeList').innerHTML,/Latest/);
  assert.doesNotMatch(env.el('courseKnowledgeList').innerHTML,/Stale/);
});

test('mastery is persisted and remains pending until the API acknowledges it', async () => {
  const pending=deferred();const env=runtime({api:async()=>[point()]});
  await env.context.courseDesktopRenderKnowledge();
  env.context.apiPost=async(endpoint,body)=>{env.posts.push({endpoint,body});return pending.promise;};
  const control={value:'会用',disabled:false,dataset:{courseMastery:'7'}};
  const saving=env.context.courseDesktopSaveMastery(control);
  assert.equal(control.disabled,true);
  assert.equal(env.posts[0].endpoint,'/api/kp/7/mastery');
  assert.equal(env.posts[0].body.mastery,'会用');
  assert.equal(env.posts[0].body.origin,'course-page');
  pending.resolve({ok:true,changed:true});await saving;
  assert.equal(control.disabled,false);
  assert.match(env.el('courseKnowledgeList').innerHTML,/value="会用" selected/);
});

test('mastery failure rolls the control back without claiming success', async () => {
  const env=runtime({api:async()=>[point()],apiPost:async()=>{throw Error('offline');}});
  await env.context.courseDesktopRenderKnowledge();
  const control={value:'熟练',disabled:false,dataset:{courseMastery:'7'}};
  await env.context.courseDesktopSaveMastery(control);
  assert.equal(control.value,'未读');assert.equal(control.disabled,false);
  assert.match(env.messages.at(-1),/保存失败/);
});

test('mastery acknowledgment updates rows refreshed while the save was pending', async () => {
  const pending=deferred();const env=runtime({api:async()=>[point()],apiPost:()=>pending.promise});
  await env.context.courseDesktopRenderKnowledge();
  const control={value:'熟练',disabled:false,dataset:{courseMastery:'7'}};
  const saving=env.context.courseDesktopSaveMastery(control);
  await env.context.courseDesktopRenderKnowledge();
  assert.match(env.el('courseKnowledgeList').innerHTML,/data-course-mastery="7"[^>]*disabled/);
  pending.resolve({ok:true,changed:true});await saving;
  assert.match(env.el('courseKnowledgeList').innerHTML,/value="熟练" selected/);
  assert.doesNotMatch(env.el('courseKnowledgeList').innerHTML,/data-course-mastery="7"[^>]*disabled/);
});

test('non-study knowledge rows offer graph navigation without mastery editing', async () => {
  const env=runtime({isStudyWorkspace:()=>false,api:async()=>[point()]});
  await env.context.courseDesktopRenderKnowledge();
  assert.doesNotMatch(env.el('courseKnowledgeList').innerHTML,/data-course-mastery=/);
  assert.match(env.el('courseKnowledgeList').innerHTML,/data-course-graph="7"/);
});

test('knowledge text is escaped and chapter/search filters use real fields', async () => {
  const env=runtime({api:async()=>[point(7,{name:'<img onerror=bad()>',chapter:'第10章'}),point(8,{name:'Cash',chapter:'第2章'})]});
  await env.context.courseDesktopRenderKnowledge();
  assert.match(env.el('courseKnowledgeList').innerHTML,/&lt;img/);
  assert.ok(env.el('courseKnowledgeChapter').innerHTML.indexOf('第2章')<env.el('courseKnowledgeChapter').innerHTML.indexOf('第10章'));
  env.el('courseKnowledgeSearch').value='Cash';env.context.courseDesktopDrawKnowledge();
  assert.match(env.el('courseKnowledgeList').innerHTML,/Cash/);
  assert.doesNotMatch(env.el('courseKnowledgeList').innerHTML,/onerror/);
});

test('material refresh retains the original operation chain and its polling argument', async () => {
  const env=runtime();
  await env.context.renderSourceStatus('Accounting',false);
  assert.deepEqual(env.originals,[['Accounting',false]]);
});

test('material refreshes run in order so an old response cannot overwrite a newer one', async () => {
  const first=deferred();const paints=[];let calls=0;
  const env=runtime({renderSourceStatus:async()=>{const call=++calls;if(call===1)await first.promise;paints.push(call);}});
  const old=env.context.renderSourceStatus('Accounting');
  await Promise.resolve();await Promise.resolve();
  const fresh=env.context.renderSourceStatus('Accounting');
  await Promise.resolve();await Promise.resolve();
  assert.equal(calls,1);
  first.resolve();await Promise.all([old,fresh]);
  assert.deepEqual(paints,[1,2]);
});

test('knowledge and materials tabs toggle their panels and ARIA state', () => {
  const env=runtime();env.context.courseDesktopSetTab('knowledge');
  assert.equal(env.el('courseMaterialsPanel').hidden,true);
  assert.equal(env.el('courseKnowledgePanel').hidden,false);
  assert.equal(env.el('courseKnowledgeTab')['aria-selected'],'true');
  env.context.courseDesktopSetTab('materials');
  assert.equal(env.el('courseMaterialsPanel').hidden,false);
  assert.equal(env.el('courseKnowledgePanel').hidden,true);
});

test('graph navigation waits for actual graph nodes and uses the existing selection entry', async () => {
  const selected=[];const env=runtime({api:async()=>[point()],graphSelectNode:id=>selected.push(id),
    setTimeout:callback=>{env.S.graph={nodes:[point()]};callback();}});
  await env.context.courseDesktopRenderKnowledge();
  await env.context.courseDesktopLocate(7);
  assert.equal(env.S.page,'graph');assert.equal(env.S.graphCourse,'Accounting');
  assert.deepEqual(selected,[7]);
});

test('ingest rows expose exact job states while retaining all jobs and retry/cancel bindings', async () => {
  const html=fs.readFileSync(path.join(__dirname,'../web/index.html'),'utf8');
  const start=html.indexOf('let ingestPollTimer = null;');
  const end=html.indexOf('\nasync function renderSourceStatus',start);
  const statuses=['queued','running','failed','interrupted','cancelled','succeeded','waiting_review'];
  const jobs=statuses.map((status,index)=>({id:index+1,status,path:`/tmp/job-${index+1}.pdf`}));
  const actions=jobs.slice(0,5).map(job=>({dataset:{taskId:String(job.id),ingestAction:job.status==='queued'||job.status==='running'?'cancel':'retry'}}));
  const box={innerHTML:'',querySelectorAll:()=>actions};
  const posts=[];
  const context=vm.createContext({S:{chatCourse:'Accounting'},el:()=>box,esc:escapeHtml,
    clearTimeout(){},setTimeout(){},api:async()=>jobs,apiPost:async(...args)=>posts.push(args),
    renderSourceStatus(){},toast(){}});
  vm.runInContext(html.slice(start,end),context);
  await context.renderIngestJobs('Accounting');
  for(const status of statuses) assert.match(box.innerHTML,new RegExp(`data-ingest-status="${status}"`));
  assert.equal((box.innerHTML.match(/class="ingest-job"/g)||[]).length,jobs.length);
  await actions[0].onclick();
  await actions[2].onclick();
  assert.equal(posts[0][0],'/api/ingest/jobs/1/cancel');
  assert.equal(posts[1][0],'/api/ingest/jobs/3/retry');
});
