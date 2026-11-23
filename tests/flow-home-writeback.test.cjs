const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');

function slice(startAnchor, endAnchor){
  const start = html.indexOf(startAnchor);
  assert.ok(start >= 0, 'missing start anchor: ' + startAnchor);
  const end = html.indexOf(endAnchor, start);
  assert.ok(end > start, 'missing end anchor: ' + endAnchor);
  return html.slice(start, end);
}

function run(source, context){
  vm.createContext(context);
  vm.runInContext(source, context);
  return context;
}

function flowContext(){
  const context = {
    S: {workspace:'studypal', courses:[], _ingestJobs:[], _events:[], personalAgent:{today:null, attention:null}},
    esc: (value) => String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'),
  };
  run(slice('/* T1.12/T1.10/T1.11/T1.13', '\nfunction pathActions(path){'), context);
  return context;
}

const baseCourses = [{id:1, name:'生物', kp_count:12}];

test('flow bar builds five ordered steps with active, done and blocked states', () => {
  const ctx = flowContext();
  const steps = ctx.buildFlowSteps({courses:baseCourses, jobs:[], attention:{source_candidates:[]}, kpTotal:12, examCount:0});
  assert.equal(steps.map((step) => step.label).join('|'), '投放|拆录|确认|图谱/掌握度|复习');
  assert.equal(steps.map((step) => step.page).join('|'), 'course|course|course|graph|calendar');
  assert.equal(steps[0].state, 'active');
  assert.equal(steps[1].state, 'todo');
  assert.equal(steps[2].state, 'done');
  assert.equal(steps[3].state, 'done');
  assert.equal(steps[4].state, 'todo');

  const waiting = ctx.buildFlowSteps({
    courses:baseCourses,
    jobs:[{id:1, status:'waiting_review', path:'a.pdf'}],
    attention:{source_candidates:[{status:'pending'}]},
    kpTotal:3, examCount:2,
  });
  assert.equal(waiting[0].state, 'done');
  assert.equal(waiting[1].state, 'done');
  assert.equal(waiting[2].state, 'active');
  assert.equal(waiting[2].badge, 1);
  assert.equal(waiting[4].state, 'active');

  const blocked = ctx.buildFlowSteps({
    courses:baseCourses, jobs:[{id:2, status:'failed', path:'b.pdf'}],
    attention:{source_candidates:[]}, kpTotal:0, examCount:0,
  });
  assert.equal(blocked[1].state, 'blocked');
  assert.equal(blocked[1].badge, 1);
});

test('next step priority follows confirm, exam, goal, course, intake, graph', () => {
  const ctx = flowContext();
  assert.equal(ctx.nextStepFor({courses:[], jobs:[], attention:{}, examCount:0}).kind, 'course');
  assert.equal(ctx.nextStepFor({courses:baseCourses, jobs:[], attention:{}, examCount:0}).kind, 'intake');
  assert.equal(ctx.nextStepFor({courses:baseCourses, jobs:[], attention:{goals:[{title:'期末', status:'active'}]}, examCount:0}).kind, 'goal');
  assert.equal(ctx.nextStepFor({courses:baseCourses, jobs:[{id:1,status:'waiting_review'}], attention:{}, examCount:0}).kind, 'confirm');
  const exam = ctx.nextStepFor({courses:baseCourses, jobs:[{id:1,status:'succeeded'}], attention:{}, examCount:2});
  assert.equal(exam.kind, 'exam');
  assert.equal(exam.action, '看日历');
  const graph = ctx.nextStepFor({courses:baseCourses, jobs:[{id:1,status:'succeeded'}], attention:{}, examCount:0});
  assert.equal(graph.kind, 'graph');
});

test('error helper separates retry, config and manual levels', () => {
  const ctx = flowContext();
  assert.equal(ctx.classifyError('Failed to fetch'), 'retry');
  assert.equal(ctx.classifyError('接口返回 401 token 失效'), 'config');
  assert.equal(ctx.classifyError('unexpected boom'), 'manual');
  const retry = ctx.errorHtml('page', 'retry', '<b>连接超时</b>', {title:'读取失败'});
  assert.match(retry, /data-error-level="page"/);
  assert.match(retry, /data-error-kind="retry"/);
  assert.match(retry, /&lt;b&gt;连接超时/);
  assert.match(retry, /data-error-action="detail"/);
  const config = ctx.errorHtml('toast', 'config', '配置缺失');
  assert.match(config, /data-error-action="settings"/);
  assert.match(config, /data-error-action="close"/);
});

test('ingest inbox renders status, four step badges, progress and grouped retry', () => {
  const box = {innerHTML:'', querySelectorAll:() => []};
  const context = {
    S:{chatCourse:'生物的图谱'}, el:() => box, esc:(value) => String(value),
    clearTimeout:() => {}, setTimeout:() => 0,
    api: async () => [
      {id:1, status:'waiting_review', path:'/tmp/生物 第 3 章.pdf', pages_done:3, pages_total:10},
      {id:2, status:'failed', path:'/tmp/坏文件.pdf', error:'PDF 解析失败'},
      {id:3, status:'running', path:'/tmp/大书.pdf', pages_done:1, pages_total:40},
    ],
    renderSourceStatus:() => {},
    renderFlowBarSafe:() => {},
  };
  run(slice('let ingestPollTimer = null;', '\nasync function renderSourceStatus'), context);
  assert.ok(context.ingestStepIndex({status:'succeeded'}) === 3);
  assert.ok(context.ingestStepIndex({status:'waiting_review'}) === 2);
  assert.equal(context.ingestFileName('/tmp/a/b.pdf'), 'b.pdf');
  assert.equal(context.ingestFailReason({error:'PDF 解析失败'}), '解析失败');
  assert.equal(context.ingestFailReason({error:'network timeout'}), '连接超时');
  return context.renderIngestJobs('生物的图谱').then(() => {
    assert.match(box.innerHTML, /class="ingest-job" data-ingest-status="waiting_review"/);
    assert.match(box.innerHTML, /生物 第 3 章\.pdf/);
    assert.equal((box.innerHTML.match(/class="ingest-step"/g) || []).length, 12);
    assert.match(box.innerHTML, /data-step="active"/);
    assert.match(box.innerHTML, /data-step="blocked"/);
    assert.match(box.innerHTML, /3 \/ 10 页/);
    assert.match(box.innerHTML, /失败分组/);
    assert.match(box.innerHTML, /data-ingest-retry-group="1"/);
    assert.match(box.innerHTML, /data-ingest-action="cancel"/);
    assert.match(box.innerHTML, /data-ingest-action="retry"/);
  });
});

test('writeback flows keep confirmation, receipt, revisions and compensating undo', () => {
  assert.ok(html.includes("openWritebackConfirm({id:id, name:name, from:from, to:val, msg:msg})"));
  assert.ok(html.includes("origin:'chat'"), 'chat writeback tags origin');
  const undo = slice('async function undoWriteReceipt(node)', '\nfunction openRevisionDialog(');
  assert.match(undo, /origin:'undo'/);
  assert.match(undo, /补偿写入/);
  assert.ok(html.includes("origin:'undo'"));
  const revisions = slice('function openRevisionDialog(kpId)', '\nfunction openWritebackConfirm(');
  assert.match(revisions, /\/revisions/);
  assert.match(revisions, /rv-main/);
  const receipt = slice('function showWriteReceipt(options)', '\nasync function undoWriteReceipt(');
  assert.match(receipt, /data-receipt-action="undo"/);
  assert.match(receipt, /data-receipt-action="revisions"/);
  assert.match(receipt, /receiptStack/);
});

test('tophead mounts flow bar and home next card in study workspaces only', () => {
  assert.match(html, /<div class="flow-bar" id="flowBar" hidden/);
  assert.match(html, /<div class="tophead-mid">/);
  assert.match(html, /<div id="homeNext"><\/div>/);
  assert.match(html, /id="intakeExit"/);
  const render = slice('function renderHomeNext()', '\nfunction renderFlowBar()');
  assert.match(render, /今日下一步/);
  assert.match(render, /data-next-go/);
  assert.match(render, /isStudyMode\(\)/);
  const bar = slice('function renderFlowBar()', '\nfunction renderFlowBarSafe()');
  assert.match(bar, /data-flow-page/);
  assert.match(bar, /flow-step/);
});


test('today home renders three summaries, empty CTAs and actionable error state', () => {
  const host = {innerHTML:'', querySelectorAll:() => []};
  const calls = [];
  const context = {
    S:{workspace:'studypal', courses:[], _ingestJobs:[], _events:[], personalAgent:{attention:null}, _homeError:null},
    esc:(value) => String(value),
    document:{getElementById:() => host},
    go:(page) => calls.push(page),
    isStudyMode:() => true,
    renderWorkspace:() => {},
    openAppDialog:() => {},
    flowData:() => ({courses:[], jobs:[], attention:{}, kpTotal:0, examCount:0}),
    nextStepFor:() => ({title:'先建一门课程', desc:'先建课再投放', action:'去课程', page:'course'}),
  };
  run(slice('function homeSummaries(data){', '\nfunction renderFlowBar(){'), context);
  context.renderHomeNext();
  assert.match(host.innerHTML, /data-home-sum="confirm"/);
  assert.match(host.innerHTML, /data-home-sum="materials"/);
  assert.match(host.innerHTML, /data-home-sum="exams"/);
  assert.match(host.innerHTML, /待确认候选/);
  assert.match(host.innerHTML, /还没有投放材料/);
  assert.match(host.innerHTML, /投放第一份材料/);
  assert.match(host.innerHTML, /先问一句/);
  assert.match(host.innerHTML, /今日下一步/);

  context.S._homeError = '本机服务连接失败';
  context.flowData = () => ({courses:[{id:1, kp_count:5}], jobs:[{id:1, status:'waiting_review', path:'/tmp/a.pdf'}], attention:{source_candidates:[{status:'pending', name:'细胞膜'}]}, kpTotal:5, examCount:2});
  host.innerHTML = '';
  context.renderHomeNext();
  assert.match(host.innerHTML, /本机服务未就绪/);
  assert.match(host.innerHTML, /data-home-retry="1"/);
  assert.match(host.innerHTML, /data-home-log="1"/);
  assert.match(host.innerHTML, /data-home-sum="confirm"[^]*?细胞膜/);

  context.S.workspace = 'workpal';
  context.isStudyMode = () => false;
  host.innerHTML = 'stale';
  context.renderHomeNext();
  assert.equal(host.innerHTML, '');
});
