const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const HELPERS = html.slice(
  html.indexOf('const CANDIDATE_BATCH_DELAY_MS = 5000;'),
  html.indexOf('\nasync function renderSourceStatus(course, refreshJobs=true){')
);
assert.ok(HELPERS.includes('function renderCandidateConsole'), 'candidate console helpers exist');

const pendingSample = [
  {id:11, label:'细胞膜', status:'pending', source:'/tmp/生物 第3章.pdf', kp_type:'concept', chapter:'第3章', confidence:0.92},
  {id:12, label:'细胞核', status:'pending', source:'/tmp/生物 第3章.pdf', kp_type:'concept', chapter:'第3章', confidence:0.7},
  {id:13, label:'有丝分裂', status:'pending', source:'/tmp/习题.pdf', kp_type:'procedure', confidence:null}
];

function escapeHtml(value){
  return String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
}

function consoleRuntime(overrides = {}){
  const timerQueue = [];
  const cleared = [];
  const posts = [];
  const toasts = [];
  const notified = [];
  const apiCalls = [];
  const calls = {renderSourceStatus:[], renderCourse:0, go:[]};
  const domElements = new Map();
  function domElement(id){
    if(!domElements.has(id)) domElements.set(id, {hidden:true, textContent:'', onclick:null, disabled:false});
    return domElements.get(id);
  }
  const state = {workspace:'studypal', chatCourse:'生物', courses:[{name:'生物'}]};
  const context = vm.createContext({
    S:state,
    esc:escapeHtml,
    document:{getElementById:id => domElement(id)},
    setTimeout:(fn, ms) => {timerQueue.push({fn, ms}); return timerQueue.length;},
    clearTimeout:id => {cleared.push(id);},
    api:async(endpoint) => {apiCalls.push(endpoint); return [{name:'生物'}];},
    apiPost:async(endpoint, body) => {
      posts.push({endpoint, body});
      if(overrides.postError) throw new Error(overrides.postError);
      return {ok:true, count:body && body.candidate_ids ? body.candidate_ids.length : 1};
    },
    toast:text => toasts.push(String(text)),
    notifyError:(kind, message) => notified.push({kind, message}),
    renderSourceStatus:async(course) => {calls.renderSourceStatus.push(course);},
    renderCourse:() => {calls.renderCourse += 1;},
    go:page => {calls.go.push(page); state.page = page;}
  });
  vm.runInContext(HELPERS, context);
  return {context, state, timerQueue, cleared, posts, toasts, notified, apiCalls, calls, domElements};
}

async function settle(){
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
}

test('candidate console renders multi-select, grouping and keeps single confirm/reject entries', () => {
  const rt = consoleRuntime();
  rt.context.renderCandidateConsole(pendingSample, '生物');
  const markup = rt.context.renderCandidateConsole(pendingSample, '生物');
  assert.match(markup, /data-candidate-console="1"/);
  assert.match(markup, /知识点候选 · 3 条待确认/);
  assert.match(markup, /全选本页/);
  assert.equal((markup.match(/data-candidate-check="/g) || []).length, 3);
  assert.equal((markup.match(/data-candidate-select-group="/g) || []).length, 2);
  assert.match(markup, /data-candidate="11"/);
  assert.match(markup, /data-reject-candidate="12"/);
  assert.match(markup, /data-candidate-batch-source="\/tmp\/生物 第3章\.pdf"/);
  assert.match(markup, /细胞膜/);
  assert.match(markup, /第3章/);
  assert.match(markup, /置信度 92%/);
  assert.match(markup, /有丝分裂/);
  assert.match(markup, /待确认知识点/);
});

test('batch bar reflects the current selection and hides when cleared', () => {
  const rt = consoleRuntime();
  rt.context.renderCandidateConsole(pendingSample, '生物');
  rt.context.candidateSetSelection([11, 12]);
  const selected = rt.context.syncCandidateBatchBar(pendingSample);
  assert.deepEqual(Array.from(selected), [11, 12]);
  assert.equal(rt.domElements.get('candidateBatchBar').hidden, false);
  assert.equal(rt.domElements.get('candidateBatchCount').textContent, '已选 2 条');
  assert.equal(rt.domElements.get('candidateBatchConfirm').textContent, '确认 2 条');
  assert.equal(rt.domElements.get('candidateBatchReject').textContent, '拒绝 2 条');
  rt.context.candidateSetSelection([]);
  rt.context.syncCandidateBatchBar(pendingSample);
  assert.equal(rt.domElements.get('candidateBatchBar').hidden, true);
});

test('batch confirm is buffered for five seconds and only sends after the timer', async() => {
  const rt = consoleRuntime();
  rt.context.renderCandidateConsole(pendingSample, '生物');
  rt.context.candidateSetSelection([11, 12]);
  rt.context.queueCandidateBatch('生物', [11, 12], 'confirmed');
  assert.equal(rt.posts.length, 0, 'nothing is sent inside the undo window');
  assert.equal(rt.timerQueue.length, 1);
  assert.equal(rt.timerQueue[0].ms, 5000);
  assert.equal(rt.domElements.get('candidateUndoBar').hidden, false);
  assert.match(rt.domElements.get('candidateUndoText').textContent, /5 秒后自动写入/);
  assert.match(rt.domElements.get('candidateUndoText').textContent, /可撤销/);
  rt.timerQueue.shift().fn();
  await settle();
  assert.equal(rt.posts.length, 1);
  assert.equal(rt.posts[0].endpoint, '/api/sources/candidates/review-batch');
  assert.equal(rt.posts[0].body.action, 'confirmed');
  assert.equal(rt.posts[0].body.candidate_ids.join(','), '11,12');
  assert.ok(rt.toasts.some(text => text.includes('已进入图谱和掌握度')));
  assert.equal(rt.domElements.get('candidateUndoBar').hidden, true);
  assert.equal(rt.calls.renderSourceStatus.join(','), '生物');
});

test('undo clears the pending request before the five-second window fires', () => {
  const rt = consoleRuntime();
  rt.context.renderCandidateConsole(pendingSample, '生物');
  rt.context.queueCandidateBatch('生物', [11], 'confirmed');
  assert.equal(rt.context.undoCandidateBatch(), true);
  assert.equal(rt.cleared.length, 1);
  assert.equal(rt.posts.length, 0);
  assert.ok(rt.toasts.some(text => text.includes('已撤销')));
  assert.equal(rt.domElements.get('candidateUndoBar').hidden, true);
});

test('leaving the course flushes the buffered batch immediately without undo', async() => {
  const rt = consoleRuntime();
  rt.context.renderCandidateConsole(pendingSample, '生物');
  rt.context.queueCandidateBatch('生物', [12], 'rejected');
  assert.equal(rt.context.flushCandidateBatch(), true);
  await settle();
  assert.equal(rt.posts.length, 1);
  assert.equal(rt.posts[0].body.action, 'rejected');
  assert.ok(!rt.toasts.some(text => text.includes('已撤销')));
  assert.ok(rt.toasts.some(text => text.includes('已拒绝 1 条候选')));
});

test('confirm/reject previews show counts, names, sources and five-second undo copy', () => {
  const rt = consoleRuntime();
  rt.context.renderCandidateConsole(pendingSample, '生物');
  const confirm = rt.context.candidateConfirmPreviewBody([11, 12]);
  assert.match(confirm, /将确认 <b>2<\/b> 条知识点候选/);
  assert.match(confirm, /来源材料：/);
  assert.match(confirm, /生物 第3章\.pdf（2 条）/);
  assert.match(confirm, /同名知识点会自动合并/);
  assert.match(confirm, /5 秒/);
  assert.match(confirm, /确认并查看图谱/);
  assert.match(confirm, /确认继续审/);
  assert.match(confirm, /细胞膜/);
  const singleReject = rt.context.candidateRejectPreviewBody([11], true);
  assert.match(singleReject, /data-candidate-note/);
  assert.match(singleReject, /拒绝原因（可选/);
  const batchReject = rt.context.candidateRejectPreviewBody([11, 12], false);
  assert.ok(!batchReject.includes('data-candidate-note'));
  assert.match(batchReject, /5 秒发送，期间可撤销/);
});

test('single reject sends /review with a note body', async() => {
  const rt = consoleRuntime();
  rt.context.renderCandidateConsole(pendingSample, '生物');
  let dialog = null;
  rt.context.openAppDialog = options => {dialog = options;};
  rt.context.appDialogEls = () => ({body:{querySelector:() => ({value:'  与课程无关  '})}});
  rt.context.openCandidateRejectDialog('生物', [11]);
  assert.ok(dialog && dialog.title === '拒绝候选');
  assert.match(dialog.bodyHtml, /data-candidate-note/);
  await dialog.onConfirm();
  await settle();
  assert.equal(rt.posts.length, 1);
  assert.equal(rt.posts[0].endpoint, '/api/sources/candidates/11/review');
  assert.equal(rt.posts[0].body.action, 'rejected');
  assert.equal(rt.posts[0].body.note, '与课程无关');
  assert.ok(rt.toasts.some(text => text.includes('已拒绝候选')));
});

test('failed batch commit keeps the failed candidates selected and reports the reason', async() => {
  const rt = consoleRuntime({postError:'该图谱候选已因材料更新失效，请重新扫描后再审核'});
  rt.context.renderCandidateConsole(pendingSample, '生物');
  rt.context.candidateSetSelection([11, 12]);
  rt.context.queueCandidateBatch('生物', [11, 12], 'confirmed');
  rt.timerQueue.shift().fn();
  await settle();
  assert.equal(rt.context.candidateSelectionIds().join(','), '11,12');
  const lastError = vm.runInContext('candidateLastError', rt.context);
  assert.match(lastError, /批量确认失败/);
  assert.match(lastError, /材料更新失效/);
  assert.match(lastError, /本批 2 条/);
  assert.equal(rt.notified.length, 1);
  assert.match(rt.notified[0].message, /失败/);
  assert.equal(rt.domElements.get('candidateUndoBar').hidden, true);
});

test('switching courses drops the previous selection', () => {
  const rt = consoleRuntime();
  rt.context.renderCandidateConsole(pendingSample, '生物');
  rt.context.candidateSetSelection([11, 12, 13]);
  rt.context.renderCandidateConsole([{id:21, label:'函数', status:'pending', source:'/tmp/数学.pdf'}], '数学');
  assert.equal(rt.context.candidateSelectionIds().length, 0);
});

test('page markup and navigation hooks are wired for the candidate console', () => {
  assert.match(html, /id="candidateBatchBar"/);
  assert.match(html, /id="candidateUndoBar"/);
  assert.match(html, /data-candidate-select-all/);
  assert.match(html, /data-candidate-check/);
  assert.match(html, /data-candidate-batch-filter|data-candidate-batch-source/);
  assert.match(html, /CANDIDATE_BATCH_DELAY_MS = 5000/);
  assert.match(html, /\/api\/sources\/candidates\/review-batch/);
  assert.match(html, /data-candidate-batch-all/);
  assert.match(html, /刷新页面会丢失这 5 秒窗口内的确认/);
  assert.match(html, /function go\(id\)\{\n  try\{ flushCandidateBatch\(\); \}/);
  assert.match(html, /function selectCourse\(name\)\{\n  try\{ flushCandidateBatch\(\); \}/);
});
