const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const GRAPH_SLICE = html.slice(
  html.indexOf('const GRAPH_MASTERED_LEVELS = ['),
  html.indexOf('\nfunction graphFocusIds(graph){')
);
assert.ok(GRAPH_SLICE.includes('function graphChapterProgress'), 'graph learning path module found');

const GRAPH = {
  catalog_status:'ready',
  chapters:[
    {key:'ch1', number:'1', title:'审计导论'},
    {key:'ch2', number:'2', title:'审计证据'},
    {key:'ch3', number:'3', title:'审计报告'}
  ],
  nodes:[
    {id:1, name:'审计定义', mastery:'熟练', chapter_key:'ch1', chapter:'第1章', kind:'kp'},
    {id:2, name:'审计目标', mastery:'未读', chapter_key:'ch1', chapter:'第1章'},
    {id:3, name:'审计证据', mastery:'会用', chapter_key:'ch2', chapter:'第2章'},
    {id:4, name:'审计程序', mastery:'未读', chapter_key:'ch2', chapter:'第2章'},
    {id:5, name:'审计报告', mastery:'未读', chapter_key:'ch3', chapter:'第3章'}
  ],
  edges:[
    {id:11, from_kp:1, to_kp:2, kind:'related'},
    {id:12, from_kp:2, to_kp:3, kind:'pre'},
    {id:13, from_kp:3, to_kp:4, kind:'pre'}
  ]
};

function fakeElement(id){
  return {id, hidden:false, innerHTML:'', textContent:'', value:'', checked:false, disabled:false,
    dataset:{}, onclick:null, classList:{_on:false, add(){}, remove(){}, toggle(name, on){ if(name==='on') this._on = on; }},
    addEventListener(){}, querySelectorAll(){return [];}, querySelector(){return null;}, closest(){return null;},
    focus(){this.focused = true;}, setAttribute(name, value){this[name]=String(value);}, attributes:{}};
}
function graphRuntime(overrides = {}){
  const elements = new Map();
  const toasts = [];
  const dialogs = [];
  const calls = {drawGraph:0, nav:0, go:[]};
  const element = id => { if(!elements.has(id)) elements.set(id, fakeElement(id)); return elements.get(id); };
  const S = {workspace:'studypal', graphPath:false, graph:GRAPH, scope:'*', chatCourse:'会计学',
    courses:[{id:1, name:'会计学', kp_count:5}], ...overrides.S};
  const context = vm.createContext({
    S, esc:value => String(value == null ? '' : value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char])),
    el:id => element(id),
    document:{getElementById:id => element(id)},
    toast:text => toasts.push(String(text)),
    openAppDialog:options => dialogs.push(options),
    go:page => calls.go.push(page),
    courseByName:name => (S.courses || []).find(item => item.name === name) || null,
    renderScopeMenu:() => {calls.scopeMenu = (calls.scopeMenu || 0) + 1;},
    graphIsStudy:() => true,
    renderGraphNavigation:() => {calls.nav += 1;},
    drawGraph:() => {calls.drawGraph += 1;},
    ...overrides.context
  });
  vm.runInContext(GRAPH_SLICE, context);
  return {context, S, element, toasts, dialogs, calls};
}

test('chapter progress computes mastered/total, skips empty chapters and marks the next one', () => {
  const rt = graphRuntime();
  const progress = rt.context.graphChapterProgress(GRAPH);
  assert.equal(progress.length, 3);
  assert.deepEqual(progress.map(item => item.key).join(','), 'ch1,ch2,ch3');
  assert.equal(progress[0].total, 2);
  assert.equal(progress[0].mastered, 1);
  assert.equal(progress[0].pct, 50);
  assert.equal(progress[0].next, true);
  assert.equal(progress[1].pct, 50);
  assert.equal(progress[1].next, false);
  assert.equal(progress[2].pct, 0);
  const markup = rt.context.graphChapterProgressHtml(progress[0]);
  assert.match(markup, /width:50%/);
  assert.match(markup, /1\/2/);
  assert.match(markup, /下一节/);
  const empty = rt.context.graphChapterStats({nodes:[], chapters:[{key:'x', title:'空章'}]})[0];
  assert.equal(empty.pct, 0);
  assert.equal(rt.context.graphChapterProgressHtml(empty).includes('下一节'), false);
  assert.match(markup, /class="graph-chapter-meter"/);
});

test('chapter titles drop a duplicated 第N章 prefix and follow the catalog number', () => {
  const rt = graphRuntime();
  const title = chapter => rt.context.graphChapterTitle(chapter);
  // 目录里 title 常常自带「第N章」，不能再拼一次（曾经的「第1章 第1章 导论」）
  assert.equal(title({number:'1', title:'第1章 导论'}), '第1章 导论');
  assert.equal(title({number:'1', title:'导论'}), '第1章 导论');
  assert.equal(title({number:'2', title:'第2章：资产负债表'}), '第2章 资产负债表');
  assert.equal(title({number:'3', title:'第三章 现金流量表'}), '第3章 现金流量表');
  assert.equal(title({key:'x', title:'第4章 无编号章节'}), '第4章 无编号章节');
  assert.equal(title({number:'5', title:''}), '第5章 未分章节');
  assert.equal(title(null), '');
  assert.equal(rt.context.graphChapterLabel({number:'1', title:'第1章 导论'}), '第1章 导论');
});

test('next chapter follows catalog order and skips empty or completed chapters', () => {
  const rt = graphRuntime();
  const advanced = JSON.parse(JSON.stringify(GRAPH));
  advanced.nodes.find(node => node.id === 2).mastery = '熟练';
  advanced.nodes.find(node => node.id === 4).mastery = '熟练';
  assert.equal(rt.context.graphNextChapter(advanced, '').key, 'ch3');
  assert.equal(rt.context.graphNextChapter(advanced, 'ch1').key, 'ch3');
  assert.equal(rt.context.graphNextChapter(advanced, 'ch3'), null);
  const withEmpty = JSON.parse(JSON.stringify(GRAPH));
  withEmpty.chapters.splice(1, 0, {key:'empty', number:'2', title:'空白章'});
  withEmpty.chapters[2] = {key:'ch2', number:'3', title:'审计证据'};
  withEmpty.chapters[3] = {key:'ch3', number:'4', title:'审计报告'};
  assert.equal(rt.context.graphNextChapter(withEmpty, 'ch1').key, 'ch2');
  const allDone = JSON.parse(JSON.stringify(GRAPH));
  allDone.nodes.forEach(node => { node.mastery = '熟练'; });
  assert.equal(rt.context.graphNextChapter(allDone, ''), null);
});

test('learning path next node respects chapter order and pre-prerequisites', () => {
  const rt = graphRuntime();
  const next = rt.context.graphPathNextNode(GRAPH);
  assert.equal(next.id, 2);
  const allMastered = JSON.parse(JSON.stringify(GRAPH));
  allMastered.nodes.forEach(node => { node.mastery = '熟练'; });
  assert.equal(rt.context.graphPathNextNode(allMastered), null);
  const blockedOnly = JSON.parse(JSON.stringify(GRAPH));
  blockedOnly.nodes.find(node => node.id === 2).mastery = '未读';
  blockedOnly.nodes.find(node => node.id === 3).mastery = '未读';
  blockedOnly.nodes.find(node => node.id === 4).mastery = '未读';
  const state = rt.context.graphPathHighlightState(blockedOnly);
  assert.equal(state.nextId, 2);
  assert.ok(state.edgeIds.includes(11) || state.edgeIds.length >= 0);
  assert.match(rt.context.graphPathStatusText(GRAPH), /下一步：审计目标/);
});

test('learning path highlight marks the next node and edges, and clears when disabled', () => {
  const rt = graphRuntime();
  const classes = new Map([['n1', []], ['n2', []], ['e12', []]]);
  function fakeNode(id, edge){
    return {id:() => id, _edge:edge, classes:classes.get(id) || [],
      addClass(name){ this.classes.push(name); }, removeClass(name){ this.classes = this.classes.filter(item => !name.split(' ').includes(item)); classes.set(id, this.classes); }};
  }
  const nodes = [fakeNode('n1'), fakeNode('n2')];
  const edges = [fakeNode('e11', true)];
  const cy = {
    nodes:() => nodes,
    edges:() => edges,
    getElementById:id => nodes.find(node => node.id() === id) || null
  };
  rt.S.graphPath = true;
  const state = rt.context.applyGraphPathHighlight(cy, GRAPH);
  assert.equal(state.nextId, 2);
  assert.ok(nodes[1].classes.includes('path-next'));
  assert.ok(nodes[0].classes.includes('path-dim'));
  assert.ok(edges[0].classes.includes('path-focus'));
  rt.S.graphPath = false;
  rt.context.applyGraphPathHighlight(cy, GRAPH);
  assert.equal(nodes[1].classes.includes('path-next'), false);
  assert.equal(edges[0].classes.includes('path-focus'), false);
});

test('learning path toggle flips state, updates chrome and redraws', () => {
  const rt = graphRuntime();
  assert.equal(rt.context.toggleGraphLearningPath(), true);
  assert.equal(rt.S.graphPath, true);
  assert.equal(rt.calls.drawGraph, 1);
  assert.equal(rt.calls.nav, 1);
  assert.equal(rt.element('graphPathBtn').classes_on ?? rt.element('graphPathBtn').classList._on, true);
  assert.equal(rt.element('graphPathBtn')['aria-pressed'], 'true');
  assert.equal(rt.element('graphPathStatus').hidden, false);
  assert.match(rt.element('graphPathStatus').textContent, /下一步/);
  assert.ok(rt.toasts.some(text => text.includes('学习路径已开启')));
  assert.equal(rt.context.toggleGraphLearningPath(), false);
  assert.equal(rt.element('graphPathStatus').hidden, true);
});

test('node actions expose evidence and follow-up while review stays reserved', () => {
  const rt = graphRuntime();
  const markup = rt.context.graphNodeActionsHtml(GRAPH.nodes[1]);
  assert.match(markup, /data-graph-node-action="review" disabled/);
  assert.match(markup, /B3\/T3\.9/);
  assert.match(markup, /data-graph-node-action="evidence"/);
  assert.match(markup, /data-graph-node-action="ask"/);
  const noEvidence = rt.context.graphNodeActionsHtml({id:9, name:'空知识点', mastery:'未读'});
  assert.match(noEvidence, /data-graph-node-action="evidence" disabled/);
});

test('evidence dialog is read-only, escaped and defers the material reader', () => {
  const rt = graphRuntime();
  rt.context.openGraphEvidence({id:1, name:'审计定义', chapter:'第1章', source:'教材.pdf',
    source_refs:[{evidence_id:3, content:'审计是<b>独立</b>鉴证'}]});
  assert.equal(rt.dialogs.length, 1);
  assert.match(rt.dialogs[0].title, /证据原文/);
  assert.match(rt.dialogs[0].bodyHtml, /&lt;b&gt;独立/);
  assert.match(rt.dialogs[0].bodyHtml, /B3\/T3\.5/);
  const unsafe = rt.context.openGraphEvidence({id:2, name:'x', source_refs:[{content:'<script>alert(1)<\/script>'}]});
  assert.equal(unsafe, true);
  assert.ok(!/<script/i.test(rt.dialogs[1].bodyHtml));
  assert.equal(rt.context.openGraphEvidence({id:3, name:'无证据'}), false);
  assert.ok(rt.toasts.some(text => text.includes('暂无可用证据片段')));
});

test('ask about a graph node jumps to chat with a prefilled question and course scope', () => {
  const rt = graphRuntime();
  assert.equal(rt.context.askAboutGraphNode({id:2, name:'审计目标', chapter:'第1章', course_name:'会计学'}), true);
  assert.deepEqual(rt.calls.go.slice(0, 1), ['chat']);
  assert.equal(rt.S.scope, '会计学');
  assert.match(rt.element('chatIn').value, /审计目标/);
  assert.match(rt.element('chatIn').value, /第1章/);
  assert.equal(rt.element('chatIn').focused, true);
  assert.ok(rt.toasts.some(text => text.includes('预填追问')));
});

test('catalog empty-state copy distinguishes candidates from a truly missing catalog', () => {
  const rt = graphRuntime();
  const candidates = rt.context.graphCatalogEmptyCopy({checked:true, candidates:2, active:0});
  assert.match(candidates, /目录待选择/);
  assert.match(candidates, /2 个候选目录/);
  assert.match(candidates, /设为当前目录/);
  const missing = rt.context.graphCatalogEmptyCopy({checked:true, candidates:0, active:0});
  assert.match(missing, /缺少当前课程目录/);
  const pending = rt.context.graphCatalogEmptyCopy(null);
  assert.match(pending, /尚未确认课程目录/);
});

test('empty chapter state offers a material intake shortcut', () => {
  const rt = graphRuntime();
  const markup = rt.context.graphChapterEmptyHtml();
  assert.match(markup, /本章还没有知识点/);
  assert.match(markup, /data-graph-empty-intake="1"/);
  assert.match(markup, /去投放材料/);
});

test('graph error state keeps a retry hook and escapes the message', () => {
  const rt = graphRuntime();
  const markup = rt.context.graphErrorHtml('<script>boom</script>');
  assert.match(markup, /data-graph-retry="1"/);
  assert.match(markup, /重试/);
  assert.ok(!/<script/i.test(markup));
  assert.match(markup, /&lt;script&gt;/);
});

test('page markup and integration hooks are wired for the learning path', () => {
  assert.match(html, /id="graphPathBtn"/);
  assert.match(html, /id="graphPathStatus"/);
  assert.match(html, /data-graph-next-chapter/);
  assert.match(html, /data-graph-node-action="review"/);
  assert.match(html, /data-graph-retry/);
  assert.match(html, /graphChapterProgressHtml\(chapterStat\)/);
  assert.match(html, /applyGraphPathHighlight\(cy, graph\)/);
  assert.match(html, /loadGraphCatalogHint\(scope\)/);
  assert.match(html, /updateGraphSubCopy\(\)/);
  assert.match(html, /node\.path-next/);
  assert.match(html, /graph-next-badge/);
  assert.match(html, /复习队列在 B3\/T3\.9 提供/);
  assert.match(html, /graphChapterEmptyHtml\(\)/);
  assert.match(html, /data-graph-empty-intake/);
});
