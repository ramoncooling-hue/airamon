const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const CMD_SLICE = html.slice(
  html.indexOf('const CMD_CACHE_MS = 30000;'),
  html.indexOf('\nfunction render(id){')
);
assert.ok(CMD_SLICE.includes('function bindCommandPalette'), 'command palette module found');

const OVERLAY_IDS = ['cmdBackdrop', 'cmdHelp', 'appDialogBackdrop', 'brandIntroBackdrop', 'readerDrawer', 'skillRevisionBackdrop'];
const NAV_ITEMS = [
  {id:'graph', ic:'🕸', name:'图谱'},
  {id:'settings', ic:'⚗', name:'设置'},
  {id:'learning', ic:'🌱', name:'学习'}
];
const GRAPH_DEFAULT = {nodes:[
  {id:5, name:'权责发生制', chapter:'第一章', course_name:'会计学'},
  {id:6, name:'借贷记账法', chapter:'第二章', course_name:'会计学'}
]};
const DOCS_DEFAULT = [
  {id:7, title:'会计笔记', kind:'note', course_name:'会计学'},
  {id:8, title:'Audit Notes', kind:'knowledge'}
];

function escapeHtml(value){
  return String(value == null ? '' : value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
}

function baseElement(id){
  const listeners = {};
  const classes = new Set();
  const node = {
    id, hidden:OVERLAY_IDS.indexOf(id) >= 0, innerHTML:'', textContent:'', value:'', checked:false,
    disabled:false, focused:false, listeners, classes,
    addEventListener(type, handler){ (listeners[type] = listeners[type] || []).push(handler); },
    dispatch(type, event){ (listeners[type] || []).forEach(handler => handler(event)); },
    classList:{
      add(name){ classes.add(name); }, remove(name){ classes.delete(name); },
      toggle(name, force){ const on = force === undefined ? !classes.has(name) : !!force; if(on) classes.add(name); else classes.delete(name); return on; },
      contains(name){ return classes.has(name); }
    },
    querySelectorAll(){ return []; }, querySelector(){ return null; }, closest(){ return null; },
    getAttribute(){ return null; }, setAttribute(){}, removeAttribute(){},
    focus(){ this.focused = true; }, blur(){ this.blurred = true; },
    scrollIntoView(){ this.scrolled = true; },
    appendChild(){}, removeChild(){}, style:{}
  };
  return node;
}

function listRow(attrs, options = {}){
  const row = baseElement('row');
  row.getAttribute = name => (attrs && Object.prototype.hasOwnProperty.call(attrs, name) ? String(attrs[name]) : null);
  if(options.kpId != null){
    row.querySelector = selector => selector === '[data-review-kp]' ? {getAttribute: name => name === 'data-review-kp' ? String(options.kpId) : null} : null;
  }
  if(options.checkbox){
    const box = {checked:false};
    row.checkbox = box;
    row.querySelector = selector => selector === '[data-candidate-check]' ? box : null;
  }
  return row;
}

function keyEvent(key, options = {}){
  return {
    key, metaKey:!!options.metaKey, ctrlKey:!!options.ctrlKey, altKey:!!options.altKey,
    target:options.target || null, defaultPrevented:false,
    preventDefault(){ this.defaultPrevented = true; }
  };
}
function editableTarget(){
  return {closest: selector => selector.indexOf('input') >= 0 ? {tagName:'INPUT'} : null};
}

function paletteRuntime(overrides = {}){
  const elements = new Map();
  const docListeners = {};
  const goCalls = [], toasts = [], focusCalls = [], docCalls = [], newKnowledgeCalls = [];
  const feedbackCalls = [], queueCalls = [];
  let flushCalls = 0;
  const apiCalls = [];
  const candidateSelection = new Set(overrides.candidateSelection || []);
  const timers = new Map();
  let timerSeq = 0;

  const element = id => {
    if(!elements.has(id)) elements.set(id, baseElement(id));
    return elements.get(id);
  };
  const setRows = (rootId, rows) => { element(rootId).querySelectorAll = () => rows; return rows; };

  const context = vm.createContext({
    S:Object.assign({page:'chat', workspace:'studypal', graph:null, graphCourse:'会计学', chatCourse:'会计学'}, overrides.S || {}),
    API:'', URL:URL, location:{href:'http://127.0.0.1/'},
    NAV:NAV_ITEMS,
    visibleNav:() => NAV_ITEMS,
    esc:escapeHtml,
    el:id => element(id),
    toast:text => { toasts.push(String(text)); },
    go:page => { goCalls.push(page); },
    focusGraphNode:id => { focusCalls.push(id); },
    openKnowledgeDoc:id => { docCalls.push(id); },
    openKnowledgeNewDialog:title => { newKnowledgeCalls.push(title); },
    learningFeedback:(id, result) => { feedbackCalls.push({id:String(id), result:String(result)}); },
    candidateSelection,
    candidateSelectionIds:() => Array.from(candidateSelection),
    candidateToggleSelection:(id, on) => { if(on) candidateSelection.add(Number(id)); else candidateSelection.delete(Number(id)); },
    candidateSelectionCourse:'会计学',
    candidatePendingBatch:overrides.pendingBatch || null,
    candidatePendingItems:[],
    syncCandidateBatchBar:() => {},
    queueCandidateBatch:(course, ids, action) => { queueCalls.push({course, ids:ids.slice(), action}); },
    flushCandidateBatch:() => { flushCalls += 1; return true; },
    setTimeout:fn => { timerSeq += 1; timers.set(timerSeq, fn); return timerSeq; },
    clearTimeout:id => { timers.delete(id); },
    document:{
      getElementById:id => element(id),
      addEventListener:(type, handler) => { (docListeners[type] = docListeners[type] || []).push(handler); },
      body:{setAttribute(){}},
      activeElement:null
    },
    api:async (p, params) => {
      apiCalls.push({path:p, params:Object.assign({}, params)});
      if(p === '/api/graph') return JSON.parse(JSON.stringify(overrides.graph || GRAPH_DEFAULT));
      if(p === '/api/documents') return JSON.parse(JSON.stringify(overrides.docs || DOCS_DEFAULT));
      return {};
    }
  });
  if(overrides.exportStub){
    context.knowledgeExportMarkdown = () => { overrides.exportStub(); };
  }
  vm.runInContext(CMD_SLICE, context);
  vm.runInContext('bindCommandPalette()', context);
  const fn = name => vm.runInContext(name, context);

  const triggerKey = (key, options) => {
    const event = keyEvent(key, options);
    (docListeners.keydown || []).forEach(handler => handler(event));
    return event;
  };
  const settle = async () => {
    for(let round = 0; round < 4; round += 1){
      while(timers.size){
        const batch = Array.from(timers.values());
        timers.clear();
        batch.forEach(fn => fn());
      }
      await new Promise(resolve => setImmediate(resolve));
    }
  };
  return {
    context, element, setRows, triggerKey, settle, fn, apiCalls,
    goCalls, toasts, focusCalls, docCalls, newKnowledgeCalls, feedbackCalls, queueCalls,
    candidateSelection, flushCount:() => flushCalls
  };
}

async function openPalette(rt){
  rt.triggerKey('k', {metaKey:true});
  await rt.settle();
  return rt;
}

test('palette markup, highlight styles and boot hook are wired into web/index.html', () => {
  assert.match(html, /id="cmdBackdrop"/);
  assert.match(html, /id="cmdInput"/);
  assert.match(html, /id="cmdResults"/);
  assert.match(html, /id="cmdHelp"/);
  assert.match(html, /id="cmdHelpList"/);
  assert.match(html, /\.cmd-item mark\{/);
  assert.match(html, /\.kb-active\{/);
  assert.match(html, /function bindCommandPalette\(\)\{/);
  assert.match(html, /bindCommandPalette\(\);/);
  assert.match(html, /Ctrl \/ Cmd \+ K/);
});

test('Ctrl/Cmd+K opens the palette, focuses the input and Esc closes it', async () => {
  const rt = paletteRuntime();
  rt.triggerKey('k', {metaKey:true});
  assert.equal(rt.element('cmdBackdrop').hidden, false);
  assert.equal(rt.element('cmdInput').focused, true);
  await rt.settle();
  rt.triggerKey('Escape');
  assert.equal(rt.element('cmdBackdrop').hidden, true);
  rt.triggerKey('k', {ctrlKey:true});
  assert.equal(rt.element('cmdBackdrop').hidden, false);
  await rt.settle();
  rt.triggerKey('Escape');
  await rt.settle();
});

test('data sources are fetched in parallel and grouped with a five item cap', async () => {
  const graph = {nodes:Array.from({length:7}, (unused, index) => ({id:index + 1, name:'KP' + (index + 1), chapter:'第一章', course_name:'会计学'}))};
  const docs = Array.from({length:7}, (unused, index) => ({id:index + 1, title:'DOC' + (index + 1), kind:'knowledge'}));
  const rt = paletteRuntime({graph, docs});
  await openPalette(rt);
  const htmlOut = rt.element('cmdResults').innerHTML;
  ['导航', '知识点', '文档', '命令'].forEach(group => assert.ok(htmlOut.includes(group), 'group ' + group));
  assert.ok(htmlOut.includes('KP5'));
  assert.ok(!htmlOut.includes('KP6'));
  assert.ok(htmlOut.includes('DOC5'));
  assert.ok(!htmlOut.includes('DOC6'));
  const indexCount = (htmlOut.match(/data-cmd-index=/g) || []).length;
  assert.equal(indexCount, 3 + 5 + 5 + 5);
  const graphCall = rt.apiCalls.filter(call => call.path === '/api/graph')[0];
  const docCall = rt.apiCalls.filter(call => call.path === '/api/documents')[0];
  assert.equal(graphCall.params.workspace, 'studypal');
  assert.equal(graphCall.params.course, '会计学');
  assert.equal(docCall.params.workspace, 'studypal');
  assert.equal(docCall.params.limit, 200);
});

test('source results are cached for 30 seconds across palette reopenings', async () => {
  const rt = paletteRuntime();
  await openPalette(rt);
  rt.triggerKey('Escape');
  await openPalette(rt);
  assert.equal(rt.apiCalls.filter(call => call.path === '/api/graph').length, 1);
  assert.equal(rt.apiCalls.filter(call => call.path === '/api/documents').length, 1);
  assert.equal(vm.runInContext('CMD_CACHE_MS', rt.context), 30000);
});

test('fuzzy filtering is case insensitive and marks the matched span', async () => {
  const rt = paletteRuntime({docs:[{id:9, title:'Audit Notes', kind:'knowledge'}]});
  await openPalette(rt);
  rt.element('cmdInput').value = 'AUDIT';
  rt.element('cmdInput').dispatch('input', {});
  const htmlOut = rt.element('cmdResults').innerHTML;
  assert.ok(htmlOut.includes('<mark>Audit</mark>'), htmlOut);
  assert.ok(!htmlOut.includes('权责发生制'));
  assert.ok(!htmlOut.includes('导航'));
});

test('arrow keys move the active row and Enter runs the selected navigation item', async () => {
  const rt = paletteRuntime();
  await openPalette(rt);
  rt.triggerKey('ArrowDown');
  const state = vm.runInContext('cmdState', rt.context);
  assert.equal(state.active, 1);
  rt.triggerKey('ArrowUp');
  assert.equal(state.active, 0);
  rt.element('cmdInput').value = '图谱';
  rt.element('cmdInput').dispatch('input', {});
  rt.triggerKey('Enter');
  assert.equal(rt.element('cmdBackdrop').hidden, true);
  assert.equal(rt.goCalls[0], 'graph');
});

test('no result falls back to asking the chat page without sending', async () => {
  const rt = paletteRuntime();
  await openPalette(rt);
  rt.element('cmdInput').value = 'zzzz';
  rt.element('cmdInput').dispatch('input', {});
  const htmlOut = rt.element('cmdResults').innerHTML;
  assert.ok(htmlOut.includes('用对话回答'), htmlOut);
  rt.triggerKey('Enter');
  await rt.settle();
  assert.equal(rt.goCalls[0], 'chat');
  assert.equal(rt.element('chatIn').value, 'zzzz');
  assert.equal(rt.element('chatIn').focused, true);
});

test('commands run their actions: new knowledge and markdown export', async () => {
  let exported = 0;
  const rt = paletteRuntime({exportStub:() => { exported += 1; }});
  await openPalette(rt);
  rt.element('cmdInput').value = '新建知识';
  rt.element('cmdInput').dispatch('input', {});
  rt.triggerKey('Enter');
  await rt.settle();
  assert.equal(rt.goCalls[0], 'knowledge');
  assert.equal(rt.newKnowledgeCalls.length, 1);
  await openPalette(rt);
  rt.element('cmdInput').value = '导出';
  rt.element('cmdInput').dispatch('input', {});
  rt.triggerKey('Enter');
  assert.equal(exported, 1);
});

test('export command explains itself when the export task has not landed yet', async () => {
  const rt = paletteRuntime();
  await openPalette(rt);
  rt.element('cmdInput').value = '导出';
  rt.element('cmdInput').dispatch('input', {});
  rt.triggerKey('Enter');
  assert.ok(rt.toasts.some(text => text.includes('导出')));
});

test('batch hotkeys never fire while typing in an input or while the palette is open', async () => {
  const rt = paletteRuntime({S:{page:'course'}});
  const rows = [listRow({'data-cc-id':'11'}, {checkbox:true}), listRow({'data-cc-id':'12'}, {checkbox:true})];
  rt.setRows('candidateConsole', rows);
  rt.triggerKey('j', {target:editableTarget()});
  assert.ok(!rows[0].classes.has('kb-active'));
  await openPalette(rt);
  rt.triggerKey('j');
  assert.ok(!rows[0].classes.has('kb-active'));
  rt.triggerKey('Escape');
  rt.triggerKey('j');
  assert.ok(rows[0].classes.has('kb-active'));
  rt.triggerKey('j');
  assert.ok(rows[1].classes.has('kb-active'));
  assert.equal(rows[1].scrolled, true);
  rt.triggerKey('k');
  assert.ok(rows[0].classes.has('kb-active'));
});

test('candidate console hotkeys toggle, confirm and reject with the immediate flush path', async () => {
  const rt = paletteRuntime({S:{page:'course'}});
  const rows = [listRow({'data-cc-id':'11'}, {checkbox:true}), listRow({'data-cc-id':'12'}, {checkbox:true})];
  rt.setRows('candidateConsole', rows);
  rt.triggerKey('j');
  rt.triggerKey('x');
  assert.equal(rt.candidateSelection.has(11), true);
  assert.equal(rows[0].classList.contains('on'), true);
  rt.triggerKey('x');
  assert.equal(rt.candidateSelection.has(11), false);
  rt.triggerKey('y');
  assert.equal(rt.flushCount(), 1);
  assert.equal(rt.queueCalls.length, 1);
  assert.equal(rt.queueCalls[0].action, 'confirmed');
  rt.triggerKey('n');
  assert.equal(rt.queueCalls.length, 2);
  assert.equal(rt.queueCalls[1].action, 'rejected');
  const pending = paletteRuntime({S:{page:'course'}, pendingBatch:{action:'confirmed', ids:[11]}});
  const pendingRows = [listRow({'data-cc-id':'11'}, {checkbox:true})];
  pending.setRows('candidateConsole', pendingRows);
  pending.triggerKey('y');
  assert.equal(pending.flushCount(), 1);
  assert.equal(pending.queueCalls.length, 0);
  assert.equal(pendingRows.length, 1);
});

test('review queue hotkeys mark good on y and again on n', async () => {
  const rt = paletteRuntime({S:{page:'learning'}});
  const rows = [listRow({}, {kpId:5}), listRow({}, {kpId:6})];
  rt.setRows('reviewQueueBody', rows);
  rt.triggerKey('j');
  rt.triggerKey('y');
  assert.deepEqual(rt.feedbackCalls[0], {id:'5', result:'good'});
  rt.triggerKey('j');
  rt.triggerKey('n');
  assert.deepEqual(rt.feedbackCalls[1], {id:'6', result:'again'});
});

test('knowledge list hotkeys move and toggle row selection', async () => {
  const rt = paletteRuntime({S:{page:'knowledge'}});
  const rows = [listRow({'data-knowledge-doc':'7'}), listRow({'data-knowledge-doc':'8'})];
  rt.setRows('knowledgeList', rows);
  rt.triggerKey('j');
  assert.equal(rows[0].classList.contains('kb-active'), true);
  rt.triggerKey('x');
  assert.equal(rows[0].classList.contains('kb-checked'), true);
  rt.triggerKey('j');
  assert.equal(rows[1].classList.contains('kb-active'), true);
  rt.triggerKey('x');
  assert.equal(rows[1].classList.contains('kb-checked'), true);
  rt.triggerKey('Escape');
  assert.equal(rows[0].classList.contains('kb-active'), false);
  assert.equal(rows[1].classList.contains('kb-active'), false);
});

test('? toggles the shortcut help overlay and Esc closes it without touching rows', async () => {
  const rt = paletteRuntime({S:{page:'learning'}});
  const rows = [listRow({}, {kpId:5})];
  rt.setRows('reviewQueueBody', rows);
  rt.triggerKey('?');
  assert.equal(rt.element('cmdHelp').hidden, false);
  rt.triggerKey('Escape');
  assert.equal(rt.element('cmdHelp').hidden, true);
  assert.ok(!rows[0].classes.has('kb-active'));
  rt.triggerKey('?');
  assert.equal(rt.element('cmdHelp').hidden, false);
  rt.triggerKey('?');
  assert.equal(rt.element('cmdHelp').hidden, true);
  rt.triggerKey('?', {target:editableTarget()});
  assert.equal(rt.element('cmdHelp').hidden, true);
});

test('dialog overlays suppress the batch hotkeys', async () => {
  const rt = paletteRuntime({S:{page:'learning'}});
  const rows = [listRow({}, {kpId:5})];
  rt.setRows('reviewQueueBody', rows);
  rt.element('appDialogBackdrop').hidden = false;
  rt.triggerKey('j');
  assert.ok(!rows[0].classes.has('kb-active'));
  rt.element('appDialogBackdrop').hidden = true;
  rt.triggerKey('j');
  assert.ok(rows[0].classes.has('kb-active'));
});

test('palette clicks on an item execute it and backdrop clicks close without running', async () => {
  const rt = paletteRuntime();
  await openPalette(rt);
  const backdrop = rt.element('cmdBackdrop');
  backdrop.dispatch('click', {target:backdrop});
  assert.equal(backdrop.hidden, true);
  await openPalette(rt);
  const itemTarget = {closest: selector => selector === '[data-cmd-index]' ? {getAttribute:() => '0'} : null};
  rt.element('cmdBackdrop').dispatch('click', {target:itemTarget});
  assert.equal(rt.element('cmdBackdrop').hidden, true);
  assert.ok(rt.goCalls.indexOf('graph') >= 0);
});
