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
const CITE_SLICE = html.slice(
  html.indexOf('const CITE_SNIPPET_LIMIT = 160;'),
  html.indexOf('\nfunction renderMessages(){')
);
assert.ok(API_SLICE.includes('async function apiPost'), 'api helpers found');
assert.ok(CITE_SLICE.includes('function citationDrawerHtml'), 'citation module found');

const localRef = {label:'记忆1', kind:'kp', id:7, name:'审计证据', course_name:'会计学', source:'审计学教材.pdf', chapter:'第3章', gist:'审计证据的充分性要求证据数量足以支持结论。'};
const webRef = {title:'Astra 审计观察', url:'https://example.com/audit', snippet:'独立审计有助于降低信息风险。'};
const manualRef = {citation:'手册2', name:'引用与依据', source:'平台使用手册', chapter:'第4章', gist:'回答中的引用编号来自本轮检索结果。'};

function escapeHtml(value){
  return String(value == null ? '' : value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
}
function fakeElement(id){
  return {id, hidden:false, innerHTML:'', textContent:'', value:'', checked:false, disabled:false,
    dataset:{}, onclick:null, classList:{add(){}, remove(){}, toggle(){}},
    addEventListener(){}, querySelectorAll(){return [];}, querySelector(){return null;}, closest(){return null;},
    appendChild(){}, removeChild(){}, focus(){this.focused = true;}, select(){}, setAttribute(){},
    scrollIntoView(){this.scrolled = true;}, style:{}};
}
function citeRuntime(fetchImpl = () => ({status:200, json:{ok:true, id:9}})){
  const calls = [];
  const toasts = [];
  const dialogs = [];
  const elements = new Map();
  const element = id => { if(!elements.has(id)) elements.set(id, fakeElement(id)); return elements.get(id); };
  const S = {workspace:'studypal', msgs:[], courses:[{id:3, name:'会计学', kp_count:2}], graphCourse:'', selectedKp:null};
  const context = vm.createContext({
    S, API:'', location:{href:'http://127.0.0.1/'},
    esc:escapeHtml,
    document:{getElementById:id => element(id)},
    el:id => element(id),
    toast:text => toasts.push(String(text)),
    stripHtml:text => String(text || '').replace(/<[^>]+>/g, ''),
    openAppDialog:options => dialogs.push(options),
    go:page => {context.__goCalls.push(page);},
    setGraphDetail:id => {context.__detailCalls.push(id);},
    courseByName:name => (S.courses || []).find(item => String(item.name) === String(name)) || null,
    navigator:{clipboard:{writeText:async text => {context.__clipboard.push(String(text));}}},
    window:{open:url => {context.__opened.push(String(url));}},
    fetch:async(url, options) => {
      const entry = {url:String(url), method:(options && options.method) || 'GET', body:options && options.body ? JSON.parse(options.body) : null};
      calls.push(entry);
      const spec = fetchImpl(entry) || {};
      const status = spec.status || 200;
      return {ok:status >= 200 && status < 300, status, json:async() => spec.json === undefined ? {} : spec.json};
    },
    __goCalls:[],
    __detailCalls:[],
    __clipboard:[],
    __opened:[]
  });
  vm.runInContext(API_SLICE + '\n' + CITE_SLICE, context);
  return {context, S, calls, toasts, dialogs, element};
}
function sampleMessage(){
  return {who:'ai', rawText:'审计证据需要充分且适当。', cites:[localRef], webResults:[webRef], manualResults:[manualRef]};
}

test('chat markup exposes the citation drawer, answer action bar and followup hint', () => {
  assert.match(html, /id="citeDrawer"/);
  assert.match(html, /id="citeDrawerBody"/);
  assert.match(html, /id="citeDrawerCount"/);
  assert.match(html, /id="citeDrawerBtn"/);
  assert.match(html, /id="citeFollowupHint"/);
  assert.match(html, /data-answer-copy=/);
  assert.match(html, /data-answer-note=/);
  assert.match(html, /data-answer-followup=/);
  assert.match(html, /apiPost\('\/api\/documents'/);
  assert.match(html, /本轮为通用回答，未引用本地资料/);
  assert.match(html, /材料阅读器原文定位（B3\/T3\.5）尚未接入/);
  assert.match(html, /生成知识候选」与材料阅读器原文定位推迟/);
});

test('refs are grouped into local, web and manual with snippets and action buttons', () => {
  const rt = citeRuntime();
  const groups = rt.context.citeRefsForMessage(sampleMessage());
  assert.equal(groups.local.length, 1);
  assert.equal(groups.web.length, 1);
  assert.equal(groups.manual.length, 1);
  assert.equal(groups.count, 3);
  const markup = rt.context.citationDrawerHtml(sampleMessage());
  assert.match(markup, /本地资料/);
  assert.match(markup, /网页来源/);
  assert.match(markup, /平台手册/);
  assert.match(markup, /审计证据/);
  assert.match(markup, /第3章/);
  assert.match(markup, /审计证据的充分性/);
  assert.match(markup, /example\.com/);
  assert.match(markup, /data-cite-locate="local:0"/);
  assert.match(markup, /data-cite-open="web:0"/);
  assert.match(markup, /data-cite-note="manual:0"/);
  assert.match(markup, /本轮共 3 条依据/);
});

test('empty turn and legacy citations degrade without crashing', () => {
  const rt = citeRuntime();
  assert.match(rt.context.citationDrawerHtml({who:'ai', text:'普通回答'}), /本轮为通用回答，未引用本地资料/);
  const legacy = {who:'ai', cites:[{label:'记忆1'}], text:'旧会话回答'};
  const groups = rt.context.citeRefsForMessage(legacy);
  assert.equal(groups.count, 1);
  const markup = rt.context.citationDrawerHtml(legacy);
  assert.match(markup, /记忆1/);
  assert.match(markup, /data-cite-locate="local:0" disabled/);
  assert.match(markup, /data-cite-open="local:0" disabled/);
  assert.match(markup, /没有可展示的片段/);
});

test('long snippets are truncated for the drawer', () => {
  const rt = citeRuntime();
  const longRef = {group:'local', key:'local:0', name:'长片段', snippet:'证据'.repeat(200)};
  const snippet = rt.context.citeRefSnippetText(longRef);
  assert.ok(snippet.length <= 161);
  assert.ok(snippet.endsWith('…'));
});

test('locate action opens the graph and selects the kp; missing id stays disabled', () => {
  const rt = citeRuntime();
  const groups = rt.context.citeRefsForMessage(sampleMessage());
  assert.equal(rt.context.locateCitationInGraph(groups.local[0]), true);
  assert.equal(rt.S.selectedKp, 7);
  assert.equal(rt.S.graphCourse, '会计学');
  assert.deepEqual(rt.context.__goCalls.slice(0, 1), ['graph']);
  assert.ok(rt.toasts.some(text => text.includes('已在知识图谱中定位')));
  assert.equal(rt.context.locateCitationInGraph(groups.web[0]), false);
  assert.ok(rt.toasts.some(text => text.includes('无法定位图谱')));
});

test('snippet dialog is read-only, escaped and explicitly defers the material reader', () => {
  const rt = citeRuntime();
  const groups = rt.context.citeRefsForMessage(sampleMessage());
  rt.context.openCitationSnippet(groups.local[0]);
  assert.equal(rt.dialogs.length, 1);
  assert.match(rt.dialogs[0].title, /引用片段/);
  assert.match(rt.dialogs[0].bodyHtml, /审计证据的充分性/);
  assert.match(rt.dialogs[0].bodyHtml, /B3\/T3\.5/);
  const unsafe = {group:'local', key:'local:9', name:'x', snippet:'<script>alert(1)</script>', source:'s'};
  rt.context.openCitationSnippet(unsafe);
  assert.ok(!/<script/i.test(rt.dialogs[1].bodyHtml));
  assert.match(rt.dialogs[1].bodyHtml, /&lt;script&gt;/);
  rt.context.openCitationSnippet({group:'local', key:'local:2', name:'空片段'});
  assert.ok(rt.toasts.some(text => text.includes('材料阅读器原文定位将在 B3 T3.5 提供')));
});

test('save as note posts a kind=note document with snippet, source and course', async()=>{
  const rt = citeRuntime();
  const groups = rt.context.citeRefsForMessage(sampleMessage());
  const saved = await rt.context.saveCitationAsNote(groups.local[0]);
  assert.ok(saved);
  const post = rt.calls.find(entry => entry.method === 'POST');
  assert.equal(post.url, '/api/documents');
  assert.equal(post.body.workspace_key, 'studypal');
  assert.equal(post.body.kind, 'note');
  assert.equal(post.body.course_id, 3);
  assert.deepEqual(post.body.tags, ['对话引用']);
  assert.match(post.body.title, /审计证据/);
  assert.match(post.body.body_md, /> 审计证据的充分性/);
  assert.match(post.body.body_md, /来源：审计学教材\.pdf/);
  assert.ok(rt.toasts.some(text => text.includes('已存为知识笔记')));
});

test('save the whole answer as a note includes the answer and reference list', async()=>{
  const rt = citeRuntime();
  const saved = await rt.context.saveAnswerAsNote(sampleMessage());
  assert.ok(saved);
  const post = rt.calls.find(entry => entry.method === 'POST');
  assert.equal(post.url, '/api/documents');
  assert.match(post.body.title, /对话回答/);
  assert.match(post.body.body_md, /审计证据需要充分且适当/);
  assert.match(post.body.body_md, /## 本轮依据/);
  assert.match(post.body.body_md, /\[记忆1\]/);
  assert.equal(post.body.kind, 'note');
});

test('note failures surface the reason in toast and drawer status', async()=>{
  const rt = citeRuntime(() => ({status:500, json:{detail:'数据库只读'}}));
  const groups = rt.context.citeRefsForMessage(sampleMessage());
  const saved = await rt.context.saveCitationAsNote(groups.local[0]);
  assert.equal(saved, null);
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(rt.toasts.some(text => text.includes('存为笔记失败')));
  assert.ok(rt.toasts.some(text => text.includes('数据库只读')));
  assert.match(rt.element('citeDrawerStatus').textContent, /存为笔记失败/);
  assert.match(rt.element('citeDrawerStatus').className, /is-error/);
});

test('copy answer uses raw text and clipboard', async()=>{
  const rt = citeRuntime();
  assert.equal(rt.context.copyMessageAnswerText(sampleMessage()), true);
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(rt.context.__clipboard.slice(0, 1), ['审计证据需要充分且适当。']);
  assert.ok(rt.toasts.some(text => text.includes('已复制回答')));
});

test('follow-up focuses the composer with the citation context hint', () => {
  const rt = citeRuntime();
  const count = rt.context.focusCitationFollowup(sampleMessage());
  assert.equal(count, 3);
  assert.equal(rt.element('chatIn').focused, true);
  assert.match(rt.element('chatIn').placeholder, /本轮 3 条引用/);
  assert.equal(rt.element('citeFollowupHint').hidden, false);
  assert.match(rt.element('citeFollowupText').textContent, /3 条引用/);
  rt.context.clearCitationFollowup();
  assert.equal(rt.element('citeFollowupHint').hidden, true);
  assert.match(rt.element('chatIn').placeholder, /接着问下去/);
});

test('drawer count tracks the latest answer references', () => {
  const rt = citeRuntime();
  rt.S.msgs = [{who:'me', text:'问题'}, sampleMessage()];
  assert.equal(rt.context.updateCitationDrawerCount(), 3);
  assert.equal(rt.element('citeDrawerCount').textContent, '3');
  rt.S.msgs = [{who:'me', text:'没有回答的问题'}];
  assert.equal(rt.context.updateCitationDrawerCount(), 0);
});
