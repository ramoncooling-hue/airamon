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
const READER_SLICE = html.slice(
  html.indexOf('const READER_BLOCK_LIMIT = 50;'),
  html.indexOf('\nfunction openCitationSnippet(ref){')
);
assert.ok(API_SLICE.includes('async function apiPost'), 'api helpers found');
assert.ok(READER_SLICE.includes('async function openReader'), 'reader module found');

const PAGE1 = {blocks:[
  {evidence_id:101, locator_type:'page', locator_start:10, locator_end:10, text:'第一段证据 <script>alert(1)<\/script>'},
  {evidence_id:102, locator_type:'page', locator_start:20, locator_end:20, text:'第二段证据'}
], has_more:true};
const PAGE2 = {blocks:[
  {evidence_id:102, locator_type:'page', locator_start:20, locator_end:20, text:'第二段证据'},
  {evidence_id:103, locator_type:'page', locator_start:30, locator_end:30, text:'第三段证据'}
], has_more:false};

function escapeHtml(value){
  return String(value == null ? '' : value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
}
function fakeElement(id){
  return {id, hidden:false, innerHTML:'', textContent:'', value:'', checked:false, disabled:false, src:'',
    dataset:{}, onclick:null, classList:{add(){}, remove(){}, toggle(){}},
    addEventListener(){}, querySelectorAll(){return [];}, querySelector(){return null;}, closest(){return null;},
    appendChild(){}, removeChild(){}, focus(){this.focused = true;}, setAttribute(){}, removeAttribute(){}, style:{}};
}
function readerRuntime(overrides = {}){
  const elements = new Map();
  const calls = [];
  const toasts = [];
  const dialogs = [];
  const goCalls = [];
  const clipboard = [];
  const element = id => { if(!elements.has(id)) elements.set(id, fakeElement(id)); return elements.get(id); };
  const S = {workspace:'studypal', courses:[], page:'course'};
  const state = {failSource: overrides.failSource || 0, failSave: !!overrides.failSave, pageMode: overrides.pageMode || 'png'};
  const context = vm.createContext({
    S, API:'', URL:URL, location:{href:'http://127.0.0.1/'},
    esc:escapeHtml,
    document:{getElementById:id => element(id), createElement:id => fakeElement(id), body:{appendChild(){}}},
    el:id => element(id),
    toast:text => toasts.push(String(text)),
    openAppDialog:options => dialogs.push(options),
    appDialogEls:() => ({body:{querySelectorAll(){return [];}, querySelector(){return null;}}}),
    go:page => goCalls.push(page),
    navigator:{clipboard:{writeText:async text => {clipboard.push(String(text));}}},
    window:{getSelection:() => ({toString:() => '', anchorNode:null})},
    fetch:async(url, options) => {
      const textUrl = String(url);
      calls.push({url:textUrl, method:(options && options.method) || 'GET', body:options && options.body ? JSON.parse(options.body) : null});
      const ok = (json, status) => ({ok:(status || 200) >= 200 && (status || 200) < 300, status:status || 200, headers:{get:() => 'application/json'}, json:async() => json});
      if(textUrl.includes('/page.png')){
        const status = state.pageMode === 'fallback' ? 415 : 200;
        return {ok:status === 200, status, headers:{get:() => status === 200 ? 'image/png' : 'application/json'}, json:async() => ({fallback:'text'})};
      }
      if(options && options.method === 'POST' && textUrl === '/api/documents'){
        return state.failSave ? ok({detail:'数据库只读'}, 500) : ok({ok:true, id:900});
      }
      let match = textUrl.match(/\/api\/materials\/(\d+)$/);
      if(match){
        const id = Number(match[1]);
        if(id === state.failSource) return ok({detail:'boom'}, 500);
        return ok({source:{id, path:'/data/教材.pdf', name:'教材.pdf', source_kind:'textbook', locator_types:['page'], chunk_count:3, evidence_count:2}});
      }
      match = textUrl.match(/\/api\/materials\/(\d+)\/blocks/);
      if(match){
        const id = Number(match[1]);
        if(id === state.failSource) return ok({detail:'boom'}, 500);
        if(id === 13) return ok({blocks:[], has_more:false});
        const from = Number(new URL(textUrl, 'http://127.0.0.1/').searchParams.get('from_locator') || 0);
        return ok(from >= 21 ? PAGE2 : PAGE1);
      }
      if(textUrl.includes('/api/evidence/101/context')){
        return ok({evidence:{evidence_id:101, locator_type:'page', locator_start:10, locator_end:10, text:'证据原文片段'},
          before:[{evidence_id:100, locator_type:'page', locator_start:5, locator_end:5, text:'前文片段'}],
          after:[{evidence_id:102, locator_type:'page', locator_start:20, locator_end:20, text:'后文片段'}],
          source:{id:12, path:'/data/教材.pdf', name:'教材.pdf'}});
      }
      return ok({});
    },
    ...(overrides.context || {})
  });
  vm.runInContext(API_SLICE + '\n' + READER_SLICE, context);
  const stateRef = vm.runInContext('readerState', context);
  return {context, S, calls, toasts, dialogs, goCalls, clipboard, element, state:() => stateRef};
}
async function settle(){
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
}

test('reader markup and integration hooks exist', () => {
  assert.match(html, /id="readerDrawer"/);
  assert.match(html, /id="readerBlocks"/);
  assert.match(html, /id="readerPagePane"/);
  assert.match(html, /data-reader-source/);
  assert.match(html, /data-reader-evidence/);
  assert.match(html, /reader-hit/);
  assert.match(html, /openReader\(\{evidenceId:Number\(ref\.id\)\}\)/);
  assert.match(html, /\/api\/materials\/' \+ sourceId \+ '\/blocks/);
  assert.match(html, /\/api\/evidence\/' \+ target \+ '\/context/);
});

test('page url and block helpers are deterministic', () => {
  const rt = readerRuntime();
  assert.equal(rt.context.readerPageUrl(12, 34), '/api/materials/12/page.png?locator=34&dpi=144');
  assert.equal(rt.context.readerPageUrl(12, null, 96), '/api/materials/12/page.png?dpi=96');
  const merged = rt.context.readerMergeBlocks(PAGE1.blocks, PAGE2.blocks);
  assert.equal(merged.length, 3);
  assert.equal(rt.context.readerHitBlockIndex(merged, 102), 1);
  assert.equal(rt.context.readerHitBlockIndex(merged, 999), -1);
  assert.equal(rt.context.readerLayoutState(false).textFull, true);
  assert.equal(rt.context.readerPageFallbackText(true, 'x'), '');
});

test('opening a material loads source, blocks and the page image', async()=>{
  const rt = readerRuntime();
  await rt.context.openReader({sourceId:12});
  await settle();
  assert.equal(rt.element('readerDrawer').hidden, false);
  assert.equal(rt.element('readerTitle').textContent, '教材.pdf');
  const listCalls = rt.calls.filter(item => item.url.includes('/blocks'));
  assert.ok(listCalls.length >= 1);
  assert.ok(listCalls[0].url.includes('/api/materials/12/blocks'));
  assert.ok(listCalls[0].url.includes('from_locator=0'));
  assert.ok(listCalls[0].url.includes('limit=50'));
  assert.match(rt.element('readerBlocks').innerHTML, /第一段证据/);
  assert.ok(!/<script/i.test(rt.element('readerBlocks').innerHTML), 'block text is escaped');
  assert.match(rt.element('readerBlocks').innerHTML, /&lt;script&gt;/);
  assert.equal(rt.element('readerMoreBtn').hidden, false);
  assert.equal(rt.element('readerPageImg').hidden, false);
  assert.equal(rt.element('readerPageImg').src, '/api/materials/12/page.png?dpi=144');
});

test('blocks pagination merges pages, dedupes and stops when has_more is false', async()=>{
  const rt = readerRuntime();
  await rt.context.openReader({sourceId:12});
  await settle();
  assert.equal(rt.state().blocks.length, 2);
  await rt.context.loadMoreReaderBlocks();
  await settle();
  assert.equal(rt.state().blocks.length, 3);
  assert.equal(rt.state().hasMore, false);
  assert.equal(rt.element('readerMoreBtn').hidden, true);
  const calls = rt.calls.filter(item => item.url.includes('/blocks'));
  assert.ok(calls.some(item => item.url.includes('from_locator=21')));
  assert.equal(rt.state().blocks.filter(block => block.evidence_id === 102).length, 1);
});

test('415 page response degrades to full-width text mode', async()=>{
  const rt = readerRuntime({pageMode:'fallback'});
  await rt.context.openReader({sourceId:13});
  await settle();
  assert.equal(rt.state().pageOk, false);
  assert.equal(rt.element('readerPagePane').hidden, true);
  assert.match(rt.element('readerPageNote').textContent, /415/);
  assert.match(rt.element('readerPageNote').textContent, /文本模式/);
  assert.equal(rt.element('readerTextPane').classList.full, undefined);
});

test('evidence context opens the reader at the exact block', async()=>{
  const rt = readerRuntime();
  await rt.context.openReader({evidenceId:101});
  await settle();
  assert.equal(rt.state().sourceId, 12);
  assert.equal(rt.state().hitEvidenceId, 101);
  const htmlBlocks = rt.element('readerBlocks').innerHTML;
  assert.match(htmlBlocks, /证据原文片段/);
  assert.match(htmlBlocks, /reader-hit/);
  assert.ok(rt.calls.some(item => item.url.includes('/api/evidence/101/context')));
  assert.ok(rt.calls.some(item => item.url.includes('/page.png') && item.url.includes('locator=10')));
  const ordered = rt.state().blocks.map(block => block.evidence_id).join(',');
  assert.equal(ordered, '100,101,102');
});

test('excerpt payload shape targets a kind=note document with 摘录 tag', () => {
  const rt = readerRuntime();
  const payload = rt.context.readerExcerptPayload({path:'/data/教材.pdf'}, '  原文摘录\n第二行  ', PAGE1.blocks[0]);
  assert.equal(payload.workspace_key, 'studypal');
  assert.equal(payload.kind, 'note');
  assert.equal(payload.tags.join('|'), '摘录');
  assert.match(payload.title, /^摘录 · 教材\.pdf/);
  assert.match(payload.body_md, /> 原文摘录/);
  assert.match(payload.body_md, /来源：\/data\/教材\.pdf/);
  assert.match(payload.body_md, /证据：#101/);
  assert.match(payload.body_md, /定位：page 10/);
});

test('saving an excerpt posts to /api/documents and offers the knowledge page', async()=>{
  const rt = readerRuntime();
  await rt.context.openReader({sourceId:12});
  await settle();
  const saved = await rt.context.readerSaveExcerpt('摘录内容', PAGE1.blocks[0]);
  await settle();
  assert.ok(saved && saved.ok);
  const post = rt.calls.find(item => item.method === 'POST');
  assert.equal(post.url, '/api/documents');
  assert.equal(post.body.kind, 'note');
  assert.equal(post.body.tags.join('|'), '摘录');
  assert.match(rt.element('readerStatus').innerHTML, /去知识页/);
  assert.ok(rt.toasts.some(text => text.includes('已摘录成笔记')));
});

test('excerpt save failures surface an inline error without throwing', async()=>{
  const rt = readerRuntime({failSave:true});
  await rt.context.openReader({sourceId:12});
  await settle();
  const saved = await rt.context.readerSaveExcerpt('摘录内容', PAGE1.blocks[0]);
  await settle();
  assert.equal(saved, null);
  assert.match(rt.element('readerStatus').innerHTML, /摘录保存失败/);
  assert.ok(rt.toasts.some(text => text.includes('数据库只读')));
});

test('load failures keep a retry affordance and 404 is explained as T3.5 not ready', async()=>{
  const rt = readerRuntime({failSource:14});
  await rt.context.openReader({sourceId:14});
  await settle();
  assert.match(rt.state().error, /\/api\/materials\/14 → 500/);
  assert.match(rt.element('readerBlocks').innerHTML, /data-reader-retry="1"/);
  assert.match(rt.element('readerBlocks').innerHTML, /重试/);
  assert.match(rt.context.readerErrorMessage(new Error('/api/materials/1 → 404')), /原文接口尚未就绪/);
});

test('copy and invalid-open paths degrade gracefully', async()=>{
  const rt = readerRuntime();
  assert.equal(rt.context.readerCopyText('  复制内容  '), true);
  await settle();
  assert.deepEqual(rt.clipboard.slice(0, 1), ['复制内容']);
  assert.ok(rt.toasts.some(text => text.includes('已复制摘录')));
  await rt.context.openReader({});
  await settle();
  assert.match(rt.state().error, /缺少材料或证据标识/);
  assert.equal(rt.context.closeReader(), true);
  assert.equal(rt.element('readerDrawer').hidden, true);
});
