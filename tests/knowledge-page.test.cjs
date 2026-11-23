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
const KNOWLEDGE_SLICE = html.slice(
  html.indexOf('const KNOWLEDGE_PAGE_SIZE = 200;'),
  html.indexOf('\nfunction render(id){')
);
assert.ok(API_SLICE.includes('async function apiPost'), 'api helpers found');
assert.ok(KNOWLEDGE_SLICE.includes('function renderKnowledgePage'), 'knowledge module found');

function escapeHtml(value){
  return String(value == null ? '' : value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
}

function fakeElement(id){
  return {id, hidden:false, innerHTML:'', textContent:'', value:'', checked:false, disabled:false,
    dataset:{}, onclick:null, classList:{add(){}, remove(){}, toggle(){}},
    addEventListener(){}, querySelectorAll(){return [];}, querySelector(){return null;}, closest(){return null;},
    appendChild(){}, removeChild(){}, focus(){}};
}

function knowledgeRuntime(fetchImpl = () => ({status:200, json:{}})){
  const calls = [];
  const timers = [];
  const toasts = [];
  const errors = [];
  const dialogs = [];
  const elements = new Map();
  const element = id => {
    if(!elements.has(id)) elements.set(id, fakeElement(id));
    return elements.get(id);
  };
  const S = {workspace:'studypal', page:'knowledge',
    courses:[{id:3, name:'会计学', kp_count:2}],
    kp:[{id:9, name:'审计证据', course_name:'会计学'}]};
  const context = vm.createContext({
    S, API:'', URL:URL, location:{href:'http://127.0.0.1/'},
    esc:escapeHtml,
    window:{confirm:() => true},
    document:{getElementById:id => element(id)},
    el:id => element(id),
    fetch:async(url, options) => {
      const entry = {url:String(url), method:(options && options.method) || 'GET', body:options && options.body ? JSON.parse(options.body) : null};
      calls.push(entry);
      const spec = fetchImpl(entry) || {};
      const status = spec.status || 200;
      return {ok:status >= 200 && status < 300, status:status, json:async() => spec.json === undefined ? {} : spec.json};
    },
    setTimeout:(fn, ms) => {timers.push({fn, ms}); return timers.length;},
    clearTimeout:() => {},
    toast:text => toasts.push(String(text)),
    notifyError:(kind, message) => errors.push({kind, message:String(message)}),
    openAppDialog:options => dialogs.push(options),
    appDialogEls:() => ({body:{querySelector:() => null}}),
    mdToHtml:md => '<p>' + escapeHtml(String(md)) + '</p>',
    renderKnowledgeContextSidebar:() => {},
    renderSourceStatus:async() => {}
  });
  vm.runInContext(API_SLICE + '\n' + KNOWLEDGE_SLICE, context);
  const state = () => ({titleIndex:vm.runInContext('knowledgeState.titleIndex', context),
    lookupCache:vm.runInContext('knowledgeState.lookupCache', context),
    docs:vm.runInContext('knowledgeState.docs', context),
    dirty:vm.runInContext('knowledgeState.dirty', context),
    listError:vm.runInContext('knowledgeState.listError', context),
    current:vm.runInContext('knowledgeState.current', context)});
  return {context, S, calls, timers, toasts, errors, dialogs, element, state};
}

test('navigation, page shell and render dispatch include the knowledge page', () => {
  assert.match(html, /\{id:'knowledge', ic:'📝', name:'知识', badge:null\}/);
  assert.match(html, /<section class="page" id="p-knowledge">/);
  assert.match(html, /else if\(id==='knowledge'\) renderKnowledgePage\(\);/);
  assert.match(html, /knowledge: \{kicker:'知识目录', title:'知识'/);
  assert.match(html, /if\(S\.page === 'knowledge'\)\{ renderKnowledgeContextSidebar\(\); return; \}/);
  assert.match(html, /if\(S\.page === 'knowledge' && id !== 'knowledge'\)/);
  assert.match(html, /patchKnowledgeContextSidebar/);
  assert.match(html, /id="knowledgeList"/);
  assert.match(html, /id="knowledgeBody"/);
  assert.match(html, /id="knowledgePreview"/);
  assert.match(html, /id="knowledgeSaveBtn"/);
  assert.match(html, /id="knowledgeBacklinks"/);
  assert.match(html, /id="knowledgeBadLinks"/);
});

test('wikilink parsing and rendering resolve documents and flag bad links', () => {
  const rt = knowledgeRuntime();
  const index = new Map([['会计学', {id:5, title:'会计学'}]]);
  const links = rt.context.knowledgeParseWikiLinks('先看 [[会计学]]，再看 [[审计|审计学]]。');
  assert.equal(links.length, 2);
  assert.equal(links[1].target, '审计');
  assert.equal(links[1].label, '审计学');
  const good = rt.context.knowledgeWikiAnchorHtml(links[0], index);
  assert.match(good, /class="wikilink"/);
  assert.match(good, /data-doc-id="5"/);
  assert.match(good, /href="#"/);
  assert.ok(!/javascript:/i.test(good));
  const bad = rt.context.knowledgeWikiAnchorHtml(links[1], index);
  assert.match(bad, /bad-link/);
  assert.match(bad, /data-bad-link="审计"/);
  const codeLinks = rt.context.knowledgeParseWikiLinks('行内 `[[不算]]` 和\n```\n[[也不算]]\n```\n以及 [[算]]');
  assert.equal(codeLinks.length, 1);
  assert.equal(codeLinks[0].target, '算');
  const rendered = rt.context.knowledgeRenderMarkdown('链接 [[会计学]] 与 [[审计|审计学]]', index, md => '<p>' + md + '</p>');
  assert.match(rendered, /data-doc-id="5"/);
  assert.match(rendered, /审计学/);
  assert.match(rendered, /bad-link/);
});

test('wikilink rendering escapes labels and targets before injecting anchors', () => {
  const rt = knowledgeRuntime();
  const rendered = rt.context.knowledgeRenderMarkdown('[[<script>alert(1)</script>|"><img onerror=alert(1)>]]', new Map(), md => '<p>' + md + '</p>');
  assert.ok(!/<script/i.test(rendered));
  assert.ok(!/<img/i.test(rendered));
  assert.match(rendered, /&lt;script&gt;/);
  assert.match(rendered, /bad-link/);
});

test('tag parsing trims, dedupes and joins tags for the editor', () => {
  const rt = knowledgeRuntime();
  assert.equal(rt.context.knowledgeParseTags(' 审计, 重点，审计; #证据 ').join('|'), '审计|重点|证据');
  assert.equal(rt.context.knowledgeTagsText(['审计', '重点']), '审计, 重点');
  assert.equal(rt.context.knowledgeParseTags('a'.repeat(80)).pop().length, 64);
});

test('list rendering includes title, kind, course, tags and empty guidance', () => {
  const rt = knowledgeRuntime();
  const rows = [
    {id:5, title:'审计证据', kind:'note', course_id:3, updated_at:'2026-10-17T10:00:00', tags:['审计'], status:'active'},
    {id:6, title:'审计风险', kind:'concept', course_id:null, updated_at:'2026-10-17T09:00:00', tags:[], status:'deleted'}
  ];
  rt.context.knowledgeRebuildTitleIndex(rows);
  const listHtml = rt.context.knowledgeListHtml(rows);
  assert.match(listHtml, /审计证据/);
  assert.match(listHtml, /会计学/);
  assert.match(listHtml, /#审计/);
  assert.match(listHtml, /笔记/);
  assert.match(listHtml, /已删除 · 点击恢复/);
  assert.match(rt.context.knowledgeListHtml([]), /还没有知识页/);
  assert.equal(rt.context.knowledgeDocCourseId(rows[0]), 3);
  assert.equal(rt.context.knowledgeDocCourseId(rows[1]), null);
});

test('list fetch uses the documents contract and renders into the page', async()=>{
  const rt = knowledgeRuntime(entry => {
    if(entry.url.includes('/api/documents')) return {status:200, json:[{id:5, title:'审计证据', kind:'note', tags:['审计']}]};
    return {status:200, json:[]};
  });
  const rows = await rt.context.knowledgeFetchList();
  assert.equal(rows.length, 1);
  const call = rt.calls.find(item => item.url.includes('/api/documents'));
  assert.ok(call.url.includes('workspace=studypal'));
  assert.ok(call.url.includes('status=active'));
  assert.ok(call.url.includes('limit=200'));
  rt.context.knowledgeRebuildTitleIndex(rows);
  rt.context.renderKnowledgeList(rows);
  assert.match(rt.element('knowledgeList').innerHTML, /审计证据/);
  assert.match(rt.element('knowledgeListStatus').textContent, /共 1 篇知识/);
});

test('create and update document calls match the API payload contract', async()=>{
  const rt = knowledgeRuntime(entry => {
    if(entry.method === 'POST') return {status:200, json:{id:7, title:entry.body.title, version:1, tags:entry.body.tags}};
    return {status:200, json:{id:7, title:entry.body.title, version:2, tags:entry.body.tags}};
  });
  const payload = rt.context.knowledgeNewDocPayload('  审计证据  ', '正文 [[会计学]]', 'note', '3', '', '审计, 重点');
  assert.equal(payload.title, '审计证据');
  assert.equal(payload.course_id, 3);
  assert.equal(payload.kp_id, null);
  assert.equal(payload.tags.join('|'), '审计|重点');
  const created = await rt.context.knowledgeCreateDoc(payload);
  assert.equal(created.id, 7);
  const post = rt.calls.find(item => item.method === 'POST');
  assert.equal(post.url, '/api/documents');
  assert.equal(post.body.workspace_key, 'studypal');
  assert.equal(post.body.title, '审计证据');
  const updated = await rt.context.knowledgeUpdateDoc(7, payload, 1);
  assert.equal(updated.version, 2);
  const patch = rt.calls.find(item => item.method === 'PATCH');
  assert.ok(patch.url.endsWith('/api/documents/7'));
  assert.equal(patch.body.expected_version, 1);
});

test('version conflict surfaces as 409 and is classified for the editor', async()=>{
  const rt = knowledgeRuntime(entry => entry.method === 'PATCH'
    ? {status:409, json:{detail:'文档版本冲突，请刷新后重试'}}
    : {status:200, json:{}});
  const payload = rt.context.knowledgeNewDocPayload('标题', '正文', 'note', '', '', []);
  let error = null;
  try{ await rt.context.knowledgeUpdateDoc(5, payload, 2); }
  catch(caught){ error = caught; }
  assert.ok(error, '409 rejects');
  assert.match(error.message, /版本冲突/);
  assert.equal(rt.context.knowledgeIsConflict(error), true);
});

test('document detail fetches tags, backlinks and bad links for the panels', async()=>{
  const rt = knowledgeRuntime(entry => entry.url.includes('/api/documents/5')
    ? {status:200, json:{document:{id:5, title:'审计证据', kind:'note', body_md:'见 [[关联页]]', version:3},
        tags:['审计'], backlinks:[{document_id:9, title:'关联页', link_text:'审计', context:'…审计证据…'}],
        bad_links:[{target:'不存在页', reason:'missing'}], revisions_count:2}}
    : {status:200, json:[]});
  const detail = await rt.context.knowledgeFetchDoc(5);
  assert.equal(detail.tags.join('|'), '审计');
  assert.equal(detail.backlinks[0].document_id, 9);
  assert.equal(detail.revisionsCount, 2);
  assert.match(rt.context.knowledgeBacklinksHtml(detail.backlinks), /关联页/);
  const badHtml = rt.context.knowledgeBadLinksHtml(detail.badLinks);
  assert.match(badHtml, /不存在页/);
  assert.match(badHtml, /新建同名知识/);
  assert.match(badHtml, /尚未创建同名知识页/);
});

test('lookup calls T2.21 endpoint and caches document and kp-only resolutions', async()=>{
  const rt = knowledgeRuntime(entry => {
    if(entry.url.includes('/api/documents/lookup')) return {status:200, json:{ids:{'会计学':5}, kp_ids:{'审计证据':9}, missing:[]}};
    return {status:200, json:[]};
  });
  const result = await rt.context.knowledgeLookupTitles(['会计学', '审计证据', '不存在']);
  assert.equal(result.resolved.length, 1);
  assert.equal(result.kpOnly, 1);
  const call = rt.calls.find(item => item.url.includes('/api/documents/lookup'));
  assert.ok(call.url.includes('workspace_key=studypal'));
  assert.ok(call.url.includes('titles=' + encodeURIComponent('会计学,审计证据,不存在')));
  const state = rt.state();
  assert.equal(state.lookupCache.get('会计学').id, 5);
  assert.equal(state.lookupCache.get('审计证据').kpId, 9);
  const good = rt.context.knowledgeWikiAnchorHtml({target:'会计学', label:'会计学'});
  assert.match(good, /data-doc-id="5"/);
  const kpOnly = rt.context.knowledgeWikiAnchorHtml({target:'审计证据', label:'审计证据'});
  assert.match(kpOnly, /bad-link/);
  assert.match(kpOnly, /知识点已存在但还没有知识页/);
});

test('revision list renders versions with view and rollback actions', () => {
  const rt = knowledgeRuntime();
  const rows = [
    {version:2, title:'审计证据', origin:'api', created_at:'2026-10-17T10:00:00'},
    {version:1, title:'审计证据', origin:'api', created_at:'2026-10-17T09:00:00'}
  ];
  const markup = rt.context.knowledgeRevisionsHtml(rows);
  assert.match(markup, /v2/);
  assert.match(markup, /data-knowledge-revision-view="2"/);
  assert.match(markup, /data-knowledge-revision-restore="1"/);
  assert.match(rt.context.knowledgeRevisionsHtml([]), /还没有修订记录/);
});

test('unsaved changes mark the dirty badge and are guarded before leaving', () => {
  const rt = knowledgeRuntime();
  rt.context.markKnowledgeDirty(true);
  assert.equal(rt.element('knowledgeDirty').hidden, false);
  assert.equal(rt.context.knowledgeCanLeave(), true);
  assert.equal(rt.state().dirty, false);
  rt.context.markKnowledgeDirty(true);
  rt.context.window.confirm = () => false;
  assert.equal(rt.context.knowledgeCanLeave(), false);
  assert.equal(rt.state().dirty, true);
  rt.context.markKnowledgeDirty(false);
  assert.equal(rt.element('knowledgeDirty').hidden, true);
});

test('list load failure shows the recoverable backend-not-ready state', async()=>{
  const rt = knowledgeRuntime(() => ({status:404, json:{detail:'Not Found'}}));
  await rt.context.loadKnowledgeList();
  assert.match(rt.element('knowledgeList').innerHTML, /知识接口尚未就绪/);
  assert.match(rt.element('knowledgeList').innerHTML, /data-knowledge-retry="list"/);
  assert.match(rt.state().listError, /知识接口尚未就绪/);
});

test('opening a document loads editor fields from the detail endpoint', async()=>{
  const rt = knowledgeRuntime(entry => entry.url.includes('/api/documents/5')
    ? {status:200, json:{document:{id:5, title:'审计证据', kind:'note', body_md:'正文 [[关联页]]', version:3,
        course_id:3, kp_id:null, updated_at:'2026-10-17T10:00:00'}, tags:['审计'], backlinks:[], bad_links:[], revisions_count:0}}
    : {status:200, json:[]});
  await rt.context.openKnowledgeDoc(5, {force:true});
  assert.equal(rt.element('knowledgeTitle').value, '审计证据');
  assert.equal(rt.element('knowledgeBody').value, '正文 [[关联页]]');
  assert.equal(rt.element('knowledgeTags').value, '审计');
  assert.equal(rt.element('knowledgeEditor').hidden, false);
  assert.equal(rt.element('knowledgeEditorEmpty').hidden, true);
  assert.equal(rt.state().current.id, 5);
  assert.equal(rt.state().dirty, false);
});
