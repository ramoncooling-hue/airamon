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
assert.ok(KNOWLEDGE_SLICE.includes('function knowledgeExportMarkdown'), 'export module found');
assert.ok(KNOWLEDGE_SLICE.includes('function knowledgeToggleTag'), 'tag module found');

const urlOf = entry => new URL(entry.url, 'http://127.0.0.1/');

function escapeHtml(value){
  return String(value == null ? '' : value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
}

function fakeElement(id){
  const listeners = new Map();
  const classes = new Set();
  return {
    id, hidden:false, innerHTML:'', textContent:'', value:'', checked:false, disabled:false,
    dataset:{}, onclick:null,
    classList:{
      add(name){ classes.add(name); },
      remove(name){ classes.delete(name); },
      contains(name){ return classes.has(name); },
      toggle(name){ if(classes.has(name)) classes.delete(name); else classes.add(name); }
    },
    addEventListener(type, handler){ if(!listeners.has(type)) listeners.set(type, []); listeners.get(type).push(handler); },
    removeEventListener(){},
    dispatch(type, event){ (listeners.get(type) || []).forEach(handler => handler(event)); },
    querySelectorAll(){ return []; },
    querySelector(){ return null; },
    closest(){ return null; },
    appendChild(){}, removeChild(){}, focus(){}
  };
}

function createRuntime(fetchImpl = () => ({status:200, json:{}})){
  const calls = [];
  const toasts = [];
  const elements = new Map();
  const timers = new Map();
  let timerSeq = 0;
  const element = id => {
    if(!elements.has(id)) elements.set(id, fakeElement(id));
    return elements.get(id);
  };
  const context = vm.createContext({
    S:{workspace:'studypal', workspaceEpoch:0, page:'knowledge', courses:[], kp:[]},
    API:'', URL, location:{href:'http://127.0.0.1/'},
    esc:escapeHtml,
    window:{confirm:() => true, addEventListener(){}},
    document:{getElementById:element, addEventListener(){}},
    el:element,
    fetch:async (url, options) => {
      const entry = {url:String(url), method:(options && options.method) || 'GET',
        body:options && options.body ? JSON.parse(options.body) : null};
      calls.push(entry);
      const spec = fetchImpl(entry) || {};
      const status = spec.status || 200;
      return {ok:status >= 200 && status < 300, status,
        json:async () => (spec.json === undefined ? {} : spec.json)};
    },
    setTimeout:fn => { timerSeq += 1; timers.set(timerSeq, fn); return timerSeq; },
    clearTimeout:id => { timers.delete(id); },
    toast:text => toasts.push(String(text)),
    notifyError(){},
    openAppDialog(){},
    appDialogEls:() => ({body:{querySelector:() => null}}),
    mdToHtml:md => '<p>' + escapeHtml(String(md)) + '</p>',
    renderKnowledgeContextSidebar(){}
  });
  vm.runInContext(API_SLICE + '\n' + KNOWLEDGE_SLICE, context);
  const fn = name => vm.runInContext(name, context);
  const run = code => vm.runInContext(code, context);
  const state = expr => JSON.parse(vm.runInContext('JSON.stringify(' + expr + ')', context));
  const plain = value => JSON.parse(JSON.stringify(value));
  const settle = async (rounds = 6) => {
    for(let round = 0; round < rounds; round += 1){
      timers.forEach((callback, id) => { timers.delete(id); try{ callback(); }catch{} });
      await new Promise(resolve => setImmediate(resolve));
    }
  };
  return {context, calls, toasts, element, fn, run, state, plain, settle};
}

function tagsImpl(entries, extra){
  return entry => {
    const url = urlOf(entry);
    if(url.pathname === '/api/tags') return {status:200, json:entries};
    return (extra && extra(entry, url)) || {status:200, json:{}};
  };
}

test('knowledge page markup ships the tag filter bar, chips field and export entry', () => {
  assert.match(html, /class="knowledge-tag-filter" id="knowledgeTagFilter"/);
  assert.match(html, /id="knowledgeExportBtn"[^>]*>导出 Markdown</);
  assert.match(html, /本机生成，含个人数据/);
  assert.match(html, /id="knowledgeTagChips"/);
  assert.match(html, /id="knowledgeTagSuggest"[^>]*hidden/);
  assert.ok(html.indexOf('本机生成，含个人数据') < html.indexOf('id="knowledgeExportBtn"'), 'privacy note sits before the export button');
  assert.match(html, /typeof knowledgeExportMarkdown === 'function'/);
});

test('loadKnowledgeTags reads the workspace contract once and caches for 30s', async () => {
  const entries = [{id:1, name:'审计', count:3, document_count:2}, {id:2, name:'重点', count:1}];
  const rt = createRuntime(tagsImpl(entries));
  const items = await rt.fn('loadKnowledgeTags')();
  assert.equal(rt.plain(items).length, 2);
  assert.equal(rt.plain(items)[0].count, 3);
  assert.equal(rt.calls.length, 1);
  const url = urlOf(rt.calls[0]);
  assert.equal(url.pathname, '/api/tags');
  assert.equal(url.searchParams.get('workspace'), 'studypal');
  await rt.fn('loadKnowledgeTags')();
  assert.equal(rt.calls.length, 1, 'second load reuses the cache');
  await rt.fn('loadKnowledgeTags')(true);
  assert.equal(rt.calls.length, 2, 'force bypasses the cache');
});

test('tag filter renders counts, selected state and escapes tag names', async () => {
  const entries = [{id:1, name:'审计', count:3}, {id:2, name:'<img src=x>', count:0}];
  const rt = createRuntime(tagsImpl(entries));
  await rt.fn('loadKnowledgeTags')();
  rt.run("knowledgeState.selectedTags = ['审计'];");
  rt.fn('renderKnowledgeTagFilter')();
  const markup = rt.element('knowledgeTagFilter').innerHTML;
  assert.match(markup, /data-knowledge-tag="审计"/);
  assert.match(markup, /class="knowledge-tag-filter-btn on"/);
  assert.match(markup, /aria-pressed="true"/);
  assert.match(markup, /#审计<i>3<\/i>/);
  assert.match(markup, /清空标签筛选/);
  assert.match(markup, /&lt;img src=x&gt;/);
  assert.ok(!markup.includes('<img src=x>'), 'tag names are escaped before injection');
});

test('toggling a tag stores selection and reloads the document list with the tag parameter', async () => {
  const rt = createRuntime(tagsImpl([{name:'审计', count:3}], entry => {
    const url = urlOf(entry);
    if(url.pathname === '/api/documents') return {status:200, json:[{id:7, title:'审计证据'}]};
    return {status:200, json:{}};
  }));
  rt.run('knowledgeState.selectedTags = [];');
  rt.fn('knowledgeToggleTag')('审计');
  assert.deepEqual(rt.state('knowledgeState.selectedTags'), ['审计']);
  await rt.settle();
  const docCalls = rt.calls.filter(call => urlOf(call).pathname === '/api/documents');
  assert.equal(docCalls.length, 1);
  assert.equal(urlOf(docCalls[0]).searchParams.get('tag'), '审计');
  rt.fn('knowledgeToggleTag')('审计');
  assert.deepEqual(rt.state('knowledgeState.selectedTags'), []);
  await rt.settle();
  const afterCalls = rt.calls.filter(call => urlOf(call).pathname === '/api/documents');
  assert.equal(urlOf(afterCalls[afterCalls.length - 1]).searchParams.get('tag'), null);
});

test('multi tag filtering unions per-tag document requests and dedupes by id', async () => {
  const rt = createRuntime(entry => {
    const url = urlOf(entry);
    if(url.pathname !== '/api/documents') return {status:200, json:{}};
    const tag = url.searchParams.get('tag');
    const rows = tag === '审计'
      ? [{id:1, title:'A', updated_at:'2026-01-02T00:00:00'}, {id:2, title:'B', updated_at:'2026-01-01T00:00:00'}]
      : [{id:2, title:'B', updated_at:'2026-01-01T00:00:00'}, {id:3, title:'C', updated_at:'2026-01-03T00:00:00'}];
    return {status:200, json:rows};
  });
  rt.run("knowledgeState.selectedTags = ['审计', '重点'];");
  const rows = rt.plain(await rt.fn('knowledgeFetchList')());
  assert.deepEqual(rows.map(row => row.id), [3, 1, 2], 'union dedupes and sorts by updated_at desc');
  const urls = rt.calls.map(call => urlOf(call));
  assert.equal(urls.length, 2);
  assert.deepEqual(urls.map(url => url.searchParams.get('tag')), ['审计', '重点']);
  assert.ok(urls.every(url => url.searchParams.get('limit') === '200'), 'tag requests ask for a full page');
  assert.ok(urls.every(url => url.searchParams.get('status') === 'active'));
});

test('clearing tag selection falls back to a single document request', async () => {
  const rt = createRuntime();
  rt.run("knowledgeState.selectedTags = ['审计', '重点'];");
  assert.equal(rt.fn('knowledgeClearTags')(), true);
  assert.deepEqual(rt.state('knowledgeState.selectedTags'), []);
  await rt.settle();
  const docCalls = rt.calls.filter(call => urlOf(call).pathname === '/api/documents');
  assert.equal(docCalls.length, 1);
  assert.equal(urlOf(docCalls[0]).searchParams.get('tag'), null);
  assert.ok(!rt.element('knowledgeTagFilter').innerHTML.includes('清空标签筛选'));
  assert.equal(rt.fn('knowledgeClearTags')(), false, 'clearing an empty selection is a no-op');
});

test('list status and empty copy reflect the active tag filter', async () => {
  const rt = createRuntime();
  rt.run("knowledgeState.selectedTags = ['审计', '重点'];");
  assert.equal(rt.fn('knowledgeListStatusText')([]), '标签筛选 0 篇 · 2 个标签');
  assert.match(rt.fn('knowledgeListHtml')([]), /没有匹配标签的知识页/);
  rt.run('knowledgeState.selectedTags = [];');
  assert.equal(rt.fn('knowledgeListStatusText')([{}, {}, {}]), '共 3 篇知识');
});

test('editor chips render current tags and removal writes back to the input', () => {
  const rt = createRuntime();
  rt.element('knowledgeTags').value = '审计, 重点';
  assert.deepEqual(rt.plain(rt.fn('renderKnowledgeTagChips')()), ['审计', '重点']);
  const markup = rt.element('knowledgeTagChips').innerHTML;
  assert.equal((markup.match(/data-knowledge-tag-remove/g) || []).length, 2);
  assert.match(markup, /#审计/);
  rt.fn('knowledgeRemoveTag')('审计');
  assert.equal(rt.element('knowledgeTags').value, '重点');
  assert.equal(rt.state('knowledgeState.dirty'), true);
});

test('picking a suggestion appends it once and marks the editor dirty', () => {
  const rt = createRuntime();
  rt.element('knowledgeTags').value = '审计';
  rt.fn('knowledgePickTag')('重点');
  assert.equal(rt.element('knowledgeTags').value, '审计, 重点');
  rt.fn('knowledgePickTag')('重点');
  assert.equal(rt.element('knowledgeTags').value, '审计, 重点', 'duplicates are not appended');
  assert.equal(rt.state('knowledgeState.dirty'), true);
});

test('tag suggestions sort by usage count and filter by the current fragment', async () => {
  const entries = [{name:'审计', count:3}, {name:'重点', count:1}, {name:'会计', count:5}];
  const rt = createRuntime(tagsImpl(entries));
  await rt.fn('loadKnowledgeTags')();
  rt.element('knowledgeTags').value = '';
  assert.deepEqual(rt.plain(rt.fn('renderKnowledgeTagSuggest')()).map(item => item.name), ['会计', '审计', '重点']);
  const markup = rt.element('knowledgeTagSuggest').innerHTML;
  assert.ok(markup.indexOf('#会计') < markup.indexOf('#审计'), 'higher count renders first');
  rt.element('knowledgeTags').value = '审';
  assert.deepEqual(rt.plain(rt.fn('renderKnowledgeTagSuggest')()).map(item => item.name), ['审计']);
  rt.element('knowledgeTags').value = '不存在';
  assert.deepEqual(rt.plain(rt.fn('renderKnowledgeTagSuggest')()), []);
  assert.equal(rt.element('knowledgeTagSuggest').hidden, true);
});

test('export posts the workspace contract and toasts dir, file count and reveal button', async () => {
  const rt = createRuntime(entry => {
    const url = urlOf(entry);
    if(url.pathname === '/api/knowledge/export') return {status:200, json:{ok:true, dir:'/data/exports/knowledge-1', files:['a.md', 'b.md'], skipped:1}};
    return {status:200, json:{}};
  });
  const result = await rt.fn('knowledgeExportMarkdown')();
  assert.equal(rt.plain(result).dir, '/data/exports/knowledge-1');
  const call = rt.calls.find(item => urlOf(item).pathname === '/api/knowledge/export');
  assert.equal(call.method, 'POST');
  assert.deepEqual(call.body, {workspace_key:'studypal'});
  const toast = rt.toasts[rt.toasts.length - 1];
  assert.match(toast, /已导出 2 个文件/);
  assert.match(toast, /跳过 1 个/);
  assert.match(toast, /data-reveal-path="\/data\/exports\/knowledge-1"/);
  assert.match(toast, /打开导出目录/);
  assert.equal(rt.element('knowledgeExportBtn').disabled, false);
  assert.equal(rt.element('knowledgeExportBtn').textContent, '导出 Markdown');
});

test('export failure surfaces the backend detail and restores the button', async () => {
  const rt = createRuntime(entry => {
    const url = urlOf(entry);
    if(url.pathname === '/api/knowledge/export') return {status:500, json:{detail:'磁盘已满'}};
    return {status:200, json:{}};
  });
  const result = await rt.fn('knowledgeExportMarkdown')();
  assert.equal(result, null);
  assert.match(rt.toasts[rt.toasts.length - 1], /导出失败：磁盘已满/);
  assert.equal(rt.element('knowledgeExportBtn').disabled, false);
});

test('reveal posts the exported dir as path and reports failures', async () => {
  const rt = createRuntime(() => ({status:200, json:{ok:true}}));
  assert.equal(await rt.fn('knowledgeRevealExport')('/data/exports/knowledge-1'), true);
  const call = rt.calls.find(item => urlOf(item).pathname === '/api/runtime/reveal');
  assert.equal(call.method, 'POST');
  assert.deepEqual(call.body, {path:'/data/exports/knowledge-1'});
  assert.equal(rt.toasts[rt.toasts.length - 1], '已打开导出目录');

  const failing = createRuntime(() => ({status:500, json:{detail:'打不开'}}));
  assert.equal(await failing.fn('knowledgeRevealExport')('/data/exports/knowledge-1'), false);
  assert.match(failing.toasts[failing.toasts.length - 1], /打开导出目录失败：打不开/);
});

test('bindKnowledgePage wires tag clicks and the toast reveal delegation', async () => {
  const rt = createRuntime(entry => {
    const url = urlOf(entry);
    if(url.pathname === '/api/runtime/reveal') return {status:200, json:{ok:true}};
    return {status:200, json:{}};
  });
  rt.fn('bindKnowledgePage')();
  const filter = rt.element('knowledgeTagFilter');
  filter.dispatch('click', {
    preventDefault(){},
    target:{closest(selector){ return selector === '[data-knowledge-tag]' ? {dataset:{knowledgeTag:'审计'}} : null; }}
  });
  assert.deepEqual(rt.state('knowledgeState.selectedTags'), ['审计']);
  await rt.settle();
  const toast = rt.element('toast');
  toast.dispatch('click', {
    preventDefault(){},
    target:{closest(selector){ return selector === '[data-reveal-path]' ? {dataset:{revealPath:'/data/exports/knowledge-1'}} : null; }}
  });
  await rt.settle();
  const reveal = rt.calls.find(item => urlOf(item).pathname === '/api/runtime/reveal');
  assert.ok(reveal, 'toast reveal button triggers the reveal endpoint');
  assert.deepEqual(reveal.body, {path:'/data/exports/knowledge-1'});
});
