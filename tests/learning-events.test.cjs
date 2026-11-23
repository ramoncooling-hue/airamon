const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// B3 复核 P1-1：前端学习事件采集（learningTrack）的行为与埋点位置。
const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const LEARNING_SLICE = html.slice(
  html.indexOf('const LEARNING_PAGE_SIZE = 20;'),
  html.indexOf('\nfunction render(id){')
);
assert.ok(LEARNING_SLICE.includes('function learningTrack('), 'learning module found');

function runtime(){
  const calls = [];
  const debugLog = [];
  const state = {rejectNext:false};
  const context = vm.createContext({
    S:{workspace:'studypal', page:'learning'},
    console:{debug:function(){ debugLog.push(Array.prototype.slice.call(arguments)); }},
    apiPost:function(url, body){
      calls.push({url, body});
      if(state.rejectNext){
        state.rejectNext = false;
        return Promise.reject(new Error('offline'));
      }
      return Promise.resolve({ok:true, duplicated:false});
    }
  });
  vm.runInContext(LEARNING_SLICE, context);
  return {context, calls, debugLog, state};
}

async function settle(){
  for(let i = 0; i < 3; i++) await new Promise(resolve => setImmediate(resolve));
}

test('learningTrack posts one learning event with workspace and idempotency key', async () => {
  const rt = runtime();
  rt.context.learningTrack('open_kp', {kp_id:7, payload:{name:'资产'}}, 'kp:7:1');
  await settle();
  assert.equal(rt.calls.length, 1);
  assert.equal(rt.calls[0].url, '/api/learning/events');
  const body = rt.calls[0].body;
  assert.equal(body.kind, 'open_kp');
  assert.equal(body.workspace_key, 'studypal');
  assert.equal(body.kp_id, 7);
  assert.equal(body.idempotency_key, 'kp:7:1');
  assert.deepEqual(body.payload, {name:'资产'});
  assert.equal(rt.debugLog.length, 0, '成功时不应有 console.debug');
});

test('learningTrack uses the current workspace', async () => {
  const rt = runtime();
  rt.context.S.workspace = 'workpal';
  rt.context.learningTrack('open_material', {source_id:9}, 'material:9:x');
  await settle();
  assert.equal(rt.calls[0].body.workspace_key, 'workpal');
});

test('learningTrack dedupes by idempotency key but keeps keyless events', async () => {
  const rt = runtime();
  rt.context.learningTrack('open_kp', {kp_id:7}, 'kp:7:1');
  rt.context.learningTrack('open_kp', {kp_id:7}, 'kp:7:1');
  rt.context.learningTrack('open_kp', {kp_id:8}, 'kp:8:1');
  rt.context.learningTrack('candidate_confirmed', {course:'会计学'});
  rt.context.learningTrack('candidate_confirmed', {course:'会计学'});
  await settle();
  assert.deepEqual(rt.calls.map(call => call.body.kind),
    ['open_kp', 'open_kp', 'candidate_confirmed', 'candidate_confirmed']);
  assert.deepEqual(rt.calls.map(call => call.body.kp_id), [7, 8, undefined, undefined]);
});

test('learningTrack stays silent when the API rejects', async () => {
  const rt = runtime();
  rt.state.rejectNext = true;
  assert.doesNotThrow(() => rt.context.learningTrack(
    'open_material', {source_id:3}, 'material:3:2026-01-01'));
  await settle();
  assert.equal(rt.calls.length, 1);
  assert.ok(rt.debugLog.some(args => String(args[0]).includes('learning event')),
    'console.debug 记录失败');
  rt.context.learningTrack('open_material', {source_id:3}, 'material:3:2026-01-01');
  await settle();
  assert.equal(rt.calls.length, 1, '失败的幂等键不重试，避免刷屏');
});

function sliceBetween(start, end){
  const from = html.indexOf(start);
  assert.ok(from >= 0, 'missing ' + start);
  const to = html.indexOf(end, from);
  assert.ok(to > from, 'missing ' + end + ' after ' + start);
  return html.slice(from, to);
}

test('learning event hooks are wired into the main user actions', () => {
  const openReader = sliceBetween('async function openReader(options){',
    '\nfunction closeReader(){');
  assert.match(openReader, /learningTrack\('open_material'/);
  assert.match(openReader, /learningLocalDate\(new Date\(\)\)/);

  const saveDoc = sliceBetween('async function saveKnowledgeDoc(){',
    '\nfunction openKnowledgeNewDialog(');
  assert.match(saveDoc, /learningTrack\('note_saved'/);
  assert.match(saveDoc, /savedVersion/);

  const batch = sliceBetween('async function commitCandidateBatch(batch){',
    '\nfunction openCandidateConfirmPreview(');
  assert.match(batch, /learningTrack\(isConfirm \? 'candidate_confirmed' : 'candidate_rejected'/);
  assert.match(batch, /batch\.action/);

  const rejectDialog = sliceBetween('function openCandidateRejectDialog(course, ids){',
    '\nfunction bindCandidateConsole(');
  assert.match(rejectDialog, /learningTrack\('candidate_rejected'/);

  const confirmHandler = sliceBetween(
    'function bindCandidateConsole(list, course, pendingCandidates){', '\nfunction ');
  assert.match(confirmHandler, /learningTrack\('candidate_confirmed'/);

  const graphDetail = sliceBetween('function setGraphDetail(id){', '\nfunction ');
  assert.match(graphDetail, /learningTrack\('open_kp'/);
  assert.match(graphDetail, /learningTrackMinuteKey\(\)/);
});
