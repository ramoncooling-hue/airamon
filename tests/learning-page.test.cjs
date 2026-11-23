const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const API_SLICE = html.slice(
  html.indexOf('async function api(path, params){'),
  html.indexOf('/* T1.12/T1.10/T1.11/T1.13')
);
const LEARNING_SLICE = html.slice(
  html.indexOf('const LEARNING_PAGE_SIZE = 20;'),
  html.indexOf('\nfunction render(id){')
);
assert.ok(API_SLICE.includes('async function apiPost'), 'api helpers found');
assert.ok(LEARNING_SLICE.includes('async function learningFeedback'), 'learning module found');

const SUMMARY = {
  today:{duration_minutes:95, events:4, notes:2, candidates_confirmed:1, reviews:3},
  streak_days:5,
  recent_materials:[
    {path:'/data/会计学/第一章.pdf', last_event_at:'2026-10-17 09:20', kind:'read', source_id:12},
    {path:'/data/会计学/第二章.pdf', last_event_at:'2026-10-16 21:05', kind:'read'}
  ]
};
const QUEUE = {items:[
  {kp_id:5, name:'权责发生制', course:'会计学', chapter:'第一章', mastery:'读过', reason:'due', priority:1},
  {kp_id:6, name:'借贷记账法 <b>x</b>', course:'会计学', chapter:'第二章', mastery:'未读', reason:'unread'}
], total:2, exam_dates:[
  {course_name:'会计学', date:'2026-10-17', days_left:0, title:'期中'},
  {course:'组织行为学', date:'2026-10-20', days_left:3},
  {course_name:'管理经济学', days_left:-2}
]};

function pad2(value){ return value < 10 ? '0' + value : String(value); }
function localDay(offset){
  const date = new Date();
  date.setDate(date.getDate() + offset);
  return date.getFullYear() + '-' + pad2(date.getMonth() + 1) + '-' + pad2(date.getDate());
}
const TIMELINE = {events:[
  {kind:'review_item', kp_id:5, title:'权责发生制', course:'会计学', occurred_at:localDay(0) + ' 09:20:00'},
  {kind:'excerpt', source_id:12, title:'摘录 · 第一章.pdf', occurred_at:localDay(0) + ' 08:10:00'},
  {kind:'note_saved', document_id:7, title:'笔记《会计基础》', occurred_at:localDay(-1) + ' 21:05:00'},
  {kind:'read', title:'旧事件', occurred_at:'2020-05-06 10:00:00'}
]};

function escapeHtml(value){
  return String(value == null ? '' : value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
}
function fakeElement(id){
  const listeners = {};
  return {id, hidden:false, innerHTML:'', textContent:'', value:'', checked:false, disabled:false, src:'',
    dataset:{}, onclick:null, listeners,
    classList:{add(){}, remove(){}, toggle(){}},
    addEventListener(type, handler){ (listeners[type] = listeners[type] || []).push(handler); },
    querySelectorAll(){ return []; }, querySelector(){ return null; }, closest(){ return null; },
    appendChild(){}, removeChild(){}, focus(){ this.focused = true; }, setAttribute(){}, removeAttribute(){},
    style:{}, scrollIntoView(){}};
}
function fakeTarget(map){
  return {closest(selector){
    if(!(selector in map)) return null;
    const attrs = map[selector];
    return {getAttribute(name){ return attrs[name] == null ? null : attrs[name]; }};
  }};
}
function learningRuntime(overrides = {}){
  const elements = new Map();
  const calls = [];
  const toasts = [];
  const goCalls = [];
  const readerCalls = [];
  const docCalls = [];
  const focusCalls = [];
  const element = id => { if(!elements.has(id)) elements.set(id, fakeElement(id)); return elements.get(id); };
  const state = {fail:overrides.fail || ''};
  const context = vm.createContext({
    S:{workspace:'studypal', page:'learning'}, API:'', URL:URL, location:{href:'http://127.0.0.1/'},
    esc:escapeHtml,
    document:{getElementById:id => element(id), createElement:id => fakeElement(id), body:{appendChild(){}}},
    el:id => element(id),
    toast:text => toasts.push(String(text)),
    go:page => goCalls.push(page),
    openReader:options => { readerCalls.push(options); return Promise.resolve({}); },
    openKnowledgeDoc:id => docCalls.push(id),
    focusGraphNode:id => focusCalls.push(id),
    setTimeout, clearTimeout,
    fetch:async(url, options) => {
      const textUrl = String(url);
      const method = (options && options.method) || 'GET';
      calls.push({url:textUrl, method, body:options && options.body ? JSON.parse(options.body) : null});
      const ok = (json, status) => ({ok:(status || 200) >= 200 && (status || 200) < 300, status:status || 200, headers:{get:() => 'application/json'}, json:async() => json});
      const clone = value => JSON.parse(JSON.stringify(value));
      if(textUrl.includes('/api/learning/summary')){
        return state.fail === 'summary' ? ok({detail:'summary boom'}, 500) : ok(clone(SUMMARY));
      }
      if(textUrl.includes('/api/review/due')){
        return state.fail === 'queue' ? ok({detail:'queue boom'}, 500) : ok(clone(QUEUE));
      }
      if(textUrl.includes('/api/learning/timeline')){
        return state.fail === 'timeline' ? ok({detail:'timeline boom'}, 500) : ok(clone(TIMELINE));
      }
      if(method === 'POST' && textUrl === '/api/review/feedback'){
        if(state.fail === 'feedback') return ok({detail:'反馈失败'}, 500);
        return ok({ok:true, kp:'权责发生制', kp_id:5, mastery:'读过', changed:true, event:{id:1}});
      }
      return ok({});
    }
  });
  vm.runInContext(API_SLICE + '\n' + LEARNING_SLICE, context);
  const stateRef = vm.runInContext('learningState', context);
  return {context, calls, toasts, goCalls, readerCalls, docCalls, focusCalls, element, state:() => stateRef};
}
async function settle(){
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
}

test('learning page markup, nav and dispatch hooks exist', () => {
  assert.match(html, /<section class="page" id="p-learning">/);
  assert.match(html, /id="learningTodayBody"/);
  assert.match(html, /id="reviewQueueBody"/);
  assert.match(html, /id="reviewQueueExam"/);
  assert.match(html, /id="learningTimeline"/);
  assert.match(html, /id="learningRecent"/);
  assert.match(html, /\{id:'learning', ic:'🌱', name:'学习', badge:null\}/);
  assert.match(html, /\['workspace','chat','knowledge','learning','graph','calendar','settings'\]/);
  assert.match(html, /else if\(id==='learning'\) renderLearningPage\(\);/);
  assert.match(html, /if\(S\.page === 'learning'\)\{ renderLearningContextSidebar\(\); return; \}/);
  assert.match(html, /\.learning-review-item\{/);
  assert.match(html, /\/api\/review\/due/);
  assert.match(html, /\/api\/learning\/timeline/);
});

test('today card renders six stats and the streak line', () => {
  const rt = learningRuntime();
  const markup = rt.context.learningTodayHtml(SUMMARY);
  assert.match(markup, /learning-today-stats/);
  assert.match(markup, /1 小时 35 分钟/);
  assert.match(markup, /4 条/);
  assert.match(markup, /2 篇/);
  assert.match(markup, /1 个/);
  assert.match(markup, /3 条/);
  assert.match(markup, /5 天/);
  assert.match(markup, /连续学习 5 天/);
  assert.ok(!/今天还没有学习记录/.test(markup));
});

test('empty today card points at the graph and formats helpers', () => {
  const rt = learningRuntime();
  const markup = rt.context.learningTodayHtml({today:{duration_minutes:0, events:0, notes:0, reviews:0}, streak_days:0});
  assert.match(markup, /今天还没有学习记录/);
  assert.match(markup, /data-learning-goto="graph"/);
  assert.equal(rt.context.learningMinutesText(0), '0 分钟');
  assert.equal(rt.context.learningMinutesText(120), '2 小时');
  assert.equal(rt.context.learningMinutesText(135), '2 小时 15 分钟');
  assert.equal(rt.context.learningMasteryLabel('can_use'), '会用');
  assert.equal(rt.context.learningReasonLabel('exam'), '考试临近');
  assert.equal(rt.context.learningKindIcon('review_item'), '🔁');
});

test('review items escape names and carry mastery plus three feedback buttons', () => {
  const rt = learningRuntime();
  const markup = rt.context.learningReviewItemHtml(QUEUE.items[1]);
  assert.match(markup, /借贷记账法/);
  assert.ok(!/<b>/.test(markup), 'raw html in kp name is escaped');
  assert.match(markup, /&lt;b&gt;/);
  assert.match(markup, /掌握度 未读/);
  assert.match(markup, /reason-new/);
  assert.match(markup, /data-review-kp="6"/);
  assert.match(markup, /data-review-result="again"/);
  assert.match(markup, /data-review-result="hard"/);
  assert.match(markup, /data-review-result="good"/);
});

test('exam banner uses course_name, filters past exams and caps at three', () => {
  const rt = learningRuntime();
  const markup = rt.context.learningExamHtml(QUEUE);
  assert.match(markup, /今天考试：会计学/);
  assert.match(markup, /距离 组织行为学 考试还有 3 天/);
  assert.ok(!/管理经济学/.test(markup), 'negative days_left is filtered out');
  const many = rt.context.learningExamHtml({exam_dates:[1,2,3,4,5].map(n => ({course_name:'课' + n, days_left:n}))});
  assert.equal((many.match(/考试还有/g) || []).length, 3);
});

test('summary loads with range and workspace params and renders recent materials', async()=>{
  const rt = learningRuntime();
  await rt.context.loadLearningSummary();
  await settle();
  const call = rt.calls.find(item => item.url.includes('/api/learning/summary'));
  assert.ok(call, 'summary request issued');
  assert.ok(call.url.includes('range=today'));
  assert.ok(call.url.includes('workspace_key=studypal'), 'summary must use workspace_key (backend contract)');
  assert.ok(!/(^|[?&])workspace=/.test(call.url), 'bare workspace param is ignored by the backend');
  assert.match(rt.element('learningTodayBody').innerHTML, /learning-today-stats/);
  assert.match(rt.element('learningTodaySub').textContent, /1 小时 35 分钟/);
  assert.match(rt.element('learningRecent').innerHTML, /第一章\.pdf/);
  assert.match(rt.element('learningRecent').innerHTML, /data-learning-recent="0"/);
  assert.equal(rt.state().summaryError, '');
});

test('review queue loads with limit and renders items plus the exam banner', async()=>{
  const rt = learningRuntime();
  await rt.context.loadReviewQueue();
  await settle();
  const call = rt.calls.find(item => item.url.includes('/api/review/due'));
  assert.ok(call.url.includes('limit=20'));
  assert.ok(call.url.includes('workspace=studypal'));
  assert.match(rt.element('reviewQueueBody').innerHTML, /权责发生制/);
  assert.match(rt.element('reviewQueueExam').innerHTML, /今天考试：会计学/);
  assert.match(rt.element('reviewQueueSub').textContent, /剩余 2 条/);
});

test('timeline groups events into today, yesterday and older days', async()=>{
  const rt = learningRuntime();
  await rt.context.loadLearningTimeline();
  await settle();
  const call = rt.calls.find(item => item.url.includes('/api/learning/timeline'));
  assert.ok(call.url.includes('limit=50'));
  assert.ok(call.url.includes('workspace_key=studypal'), 'timeline must use workspace_key (backend contract)');
  const markup = rt.element('learningTimeline').innerHTML;
  assert.match(markup, /今天/);
  assert.match(markup, /昨天/);
  assert.match(markup, /5月6日/);
  assert.match(markup, /data-learning-graph="5"/);
  assert.match(markup, /data-learning-reader="12"/);
  assert.match(markup, /data-learning-doc="7"/);
  assert.match(rt.element('learningTimelineSub').textContent, /最近 4 条事件/);
});

test('feedback posts the result, removes the item and counts the completion', async()=>{
  const rt = learningRuntime();
  await rt.context.loadReviewQueue();
  await settle();
  const response = await rt.context.learningFeedback(5, 'again');
  await settle();
  assert.ok(response && response.ok);
  const post = rt.calls.find(item => item.method === 'POST');
  assert.equal(post.url, '/api/review/feedback');
  assert.deepEqual(post.body, {kp_id:5, result:'again', workspace:'studypal'});
  assert.equal(rt.state().completed, 1);
  assert.equal(rt.state().queue.total, 1);
  assert.ok(!rt.state().queue.items.some(item => item.kp_id === 5));
  assert.ok(rt.toasts.some(text => text.includes('已标记忘了')));
  assert.ok(rt.toasts.some(text => text.includes('掌握度降为读过')));
  const markup = rt.element('reviewQueueBody').innerHTML;
  assert.ok(!/权责发生制/.test(markup), 'answered kp removed from the list');
  assert.match(markup, /借贷记账法/);
});

test('feedback failures keep the queue intact and surface a toast', async()=>{
  const rt = learningRuntime({fail:'feedback'});
  await rt.context.loadReviewQueue();
  await settle();
  const response = await rt.context.learningFeedback(5, 'good');
  await settle();
  assert.equal(response, null);
  assert.equal(rt.state().completed, 0);
  assert.equal(rt.state().queue.total, 2);
  assert.ok(rt.state().queue.items.some(item => item.kp_id === 5));
  assert.ok(rt.toasts.some(text => text.includes('复习反馈失败')));
  assert.ok(rt.toasts.some(text => text.includes('反馈失败')));
  assert.equal(rt.context.learningFeedbackToast('hard', {name:'权责发生制'}), '「权责发生制」已标记模糊，掌握度记为会用');
});

test('draining the queue shows the completed state', async()=>{
  const rt = learningRuntime();
  await rt.context.loadReviewQueue();
  await settle();
  await rt.context.learningFeedback(5, 'good');
  await settle();
  await rt.context.learningFeedback(6, 'good');
  await settle();
  const markup = rt.element('reviewQueueBody').innerHTML;
  assert.match(markup, /今日复习完成/);
  assert.match(markup, /今日完成 2 条/);
  assert.match(rt.element('reviewQueueSub').textContent, /今日完成 2 条/);
});

test('load failures render retry affordances wired to the right loader', async()=>{
  const rt = learningRuntime({fail:'queue'});
  await rt.context.loadReviewQueue();
  await settle();
  assert.match(rt.element('reviewQueueBody').innerHTML, /data-learning-retry="queue"/);
  assert.match(rt.element('reviewQueueBody').innerHTML, /读取失败/);
  assert.match(rt.state().queueError, /\/api\/review\/due → 500/);
  const before = rt.calls.filter(item => item.url.includes('/api/review/due')).length;
  rt.context.learningClick({target:fakeTarget({'[data-learning-retry]':{'data-learning-retry':'queue'}})});
  await settle();
  const after = rt.calls.filter(item => item.url.includes('/api/review/due')).length;
  assert.equal(after, before + 1);
});

test('click delegation routes review, graph, doc, reader and recent targets', async()=>{
  const rt = learningRuntime();
  await rt.context.loadLearningSummary();
  await settle();
  rt.context.learningClick({target:fakeTarget({'[data-review-kp]':{'data-review-kp':'5','data-review-result':'hard'}})});
  await settle();
  assert.ok(rt.calls.some(item => item.method === 'POST' && item.body && item.body.result === 'hard'));
  rt.context.learningClick({target:fakeTarget({'[data-learning-graph]':{'data-learning-graph':'42'}})});
  await settle();
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.ok(rt.goCalls.includes('graph'));
  assert.ok(rt.focusCalls.includes(42));
  rt.context.learningClick({target:fakeTarget({'[data-learning-doc]':{'data-learning-doc':'7'}})});
  await settle();
  assert.ok(rt.goCalls.includes('knowledge'));
  assert.deepEqual(rt.docCalls, [7]);
  rt.context.learningClick({target:fakeTarget({'[data-learning-reader]':{'data-learning-reader':'12'}})});
  await settle();
  assert.equal(rt.readerCalls.length, 1);
  assert.equal(rt.readerCalls[0].sourceId, 12);
  rt.context.learningClick({target:fakeTarget({'[data-learning-recent]':{'data-learning-recent':'0'}})});
  await settle();
  assert.equal(rt.readerCalls.length, 2);
  assert.equal(rt.readerCalls[1].sourceId, 12);
  rt.context.learningClick({target:fakeTarget({'[data-learning-goto]':{'data-learning-goto':'graph'}})});
  assert.equal(rt.goCalls[rt.goCalls.length - 1], 'graph');
  rt.context.learningClick({target:fakeTarget({})});
  rt.context.learningClick({target:{closest:'nope'}});
  await settle();
});

test('recent material without a source falls back to the course page', async()=>{
  const rt = learningRuntime();
  await rt.context.loadLearningSummary();
  await settle();
  await rt.context.learningOpenMaterial(1);
  await settle();
  assert.ok(rt.toasts.some(text => text.includes('暂无原文索引')));
  assert.equal(rt.goCalls[rt.goCalls.length - 1], 'course');
  assert.equal(rt.readerCalls.length, 0);
});

test('page binding loads all three sources once and renders the sidebar count', async()=>{
  const rt = learningRuntime();
  rt.context.renderLearningPage();
  await settle();
  assert.equal(rt.calls.filter(item => item.url.includes('/api/learning/summary')).length, 1);
  assert.equal(rt.calls.filter(item => item.url.includes('/api/review/due')).length, 1);
  assert.equal(rt.calls.filter(item => item.url.includes('/api/learning/timeline')).length, 1);
  const refresh = rt.element('learningRefreshBtn').listeners.click || [];
  assert.equal(refresh.length, 1);
  assert.equal((rt.element('p-learning').listeners.click || []).length, 1);
  rt.context.renderLearningPage();
  await settle();
  assert.equal((rt.element('p-learning').listeners.click || []).length, 1, 'bindings are not duplicated');
  const sidebar = rt.element('contextNav').innerHTML;
  assert.match(sidebar, /待复习/);
  assert.match(sidebar, /context-action/);
  assert.match(rt.element('contextTitle').textContent, /学习/);
});
