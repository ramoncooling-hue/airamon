const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const {performance} = require('node:perf_hooks');
const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const graphCode = script.slice(script.indexOf('async function renderGraph(){'), script.indexOf('async function renderMastery(){'));

function element() {
  const children = new Map();
  return {innerHTML:'', textContent:'', hidden:false, open:false, dataset:{}, classList:{toggle() {},add() {},remove() {}},
    querySelector(selector) {if(!children.has(selector)) children.set(selector, element()); return children.get(selector);},
    querySelectorAll() {return [];}, addEventListener() {}, setAttribute() {}};
}
function runtime(extra={}) {
  const elements = new Map();
  const state = {page:'graph', workspace:'studypal', graphCourse:'real-course', courses:[{name:'real-course'}],
    graphFocus:{kind:'all',chapter:'',nodeId:null,depth:2}, graphNavQuery:'',graphNavMastery:'all',graphNavSource:'all',graphNavTag:'all',graphFilter:'all',
    graph:{nodes:[],edges:[],chapters:[]}};
  const context=vm.createContext({S:state, el:id=>{if(!elements.has(id)) elements.set(id,element()); return elements.get(id);},
    document:{querySelectorAll:()=>[],activeElement:null}, window:{addEventListener() {}},
    M_ORDER:['熟练','会用','读过','未读'], M_FILL_BG:{}, esc:value=>String(value??''),mCls:()=>'',
    toast() {}, setTimeout,clearTimeout, ...extra});
  vm.runInContext(graphCode,context);
  return {context,state,elements};
}
function sample() {
  return {nodes:[
    {id:1,name:'alpha',chapter_key:'c1',section_key:'s1',mastery:'未读',freq:'高频'},
    {id:2,name:'beta',chapter_key:'c1',section_key:'obsolete',mastery:'熟练',freq:'高频'},
    {id:3,name:'gamma',chapter_key:'c2',mastery:'未读',freq:'低频'},
    {id:4,name:'delta',chapter_key:'c3',mastery:'读过'},
    {id:5,name:'orphan',chapter_key:null,mastery:'未读'}],
    edges:[{id:1,from_kp:1,to_kp:2},{id:2,from_kp:2,to_kp:3},{id:3,from_kp:3,to_kp:4}],
    chapters:[{key:'c1',number:'1',title:'Canonical title',sections:[{key:'s1',number:'1.1',title:'Section title'}]},
      {key:'c2',number:'2',title:'Other',sections:[]},{key:'c3',number:'3',title:'Last',sections:[]}]};
}
test('all inline application JavaScript parses',()=>new vm.Script(script));
test('directory retains a stable movable home and CSS is graph scoped',()=>{
  assert.match(html, /id="graphNavHome"[^>]*><div[^>]*id="graphNav"/);
  assert.equal((html.match(/id="graphNav"/g)||[]).length,1);
  assert.match(html, /assets\/graph-desktop\.css/);
  assert.match(html, /id="graphDetailPanel"[^>]*hidden/);
});
test('chapter focus includes real cross-chapter edges, node focus uses one or two hops',()=>{
  const {context:c,state:s}=runtime();s.graph=sample();
  s.graphFocus={kind:'chapter',chapter:'c1'};
  assert.deepEqual([...c.graphFocusIds(s.graph)],[1,2,3]);
  s.graphFocus={kind:'node',nodeId:1,depth:1};
  assert.deepEqual([...c.graphFocusIds(s.graph)],[1,2]);
  s.graphFocus.depth=2;
  assert.deepEqual([...c.graphFocusIds(s.graph)],[1,2,3]);
  const layout=c.graphCanvasLayout(s.graph,s.graph.nodes.slice(0,3),false,760);
  assert.equal(layout.positions.n1.x,380);
  assert.ok(Math.hypot(layout.positions.n3.x-380,layout.positions.n3.y-250)>Math.hypot(layout.positions.n2.x-380,layout.positions.n2.y-250));
  s.graphFocus={kind:'chapter',chapter:'c1'};
  const chapterLayout=c.graphCanvasLayout(s.graph,s.graph.nodes.slice(0,3),false,560);
  assert.equal(chapterLayout.positions['chapter:c1'].x,chapterLayout.width/2);
  assert.equal(chapterLayout.positions['chapter:c1'].y,chapterLayout.height/2-300-40);
  assert.ok(chapterLayout.positions['chapter:c2'].x>0);
  assert.ok(chapterLayout.positions['chapter:c2'].y>0);
});
test('canonical chapter and section names are searchable and all filters affect the same IDs',()=>{
  const {context:c,state:s}=runtime();s.graph=sample();
  s.graphNavQuery='Canonical title';assert.deepEqual([...c.graphFocusIds(s.graph)],[1,2]);
  s.graphNavQuery='Section title';assert.deepEqual([...c.graphFocusIds(s.graph)],[1]);
  s.graphNavQuery='';s.graphFilter='risk';assert.deepEqual([...c.graphFocusIds(s.graph)],[1]);
  s.graphFilter='all';s.graphNavMastery='未读';assert.deepEqual([...c.graphFocusIds(s.graph)],[1,3,5]);
  s.graphNavQuery='missing';assert.equal(c.graphFocusIds(s.graph).size,0);
});
test('directory contains every visible node, including nodes without current section metadata',()=>{
  const {context:c,state:s,elements}=runtime();s.graph=sample();c.renderGraphNavigation();
  const ids=[...elements.get('graphNav').innerHTML.matchAll(/data-graph-tree-id="(\d+)"/g)].map(match=>+match[1]).sort();
  assert.deepEqual(ids,[...c.graphFocusIds(s.graph)].sort());
  s.graphFocus={kind:'chapter',chapter:'c1'};c.renderGraphNavigation();
  assert.deepEqual([...elements.get('graphNav').innerHTML.matchAll(/data-graph-tree-id="(\d+)"/g)].map(match=>+match[1]).sort(),[1,2,3]);
});
test('back restores selection, filters and viewport',()=>{
  const {context:c,state:s}=runtime();s.graph=sample();
  c.renderGraphNavigation=()=>{};c.updateGraphChrome=()=>{};c.drawGraph=()=>{};c.setGraphDetail=()=>{};
  s.graphViewport={zoom:.7,pan:{x:20,y:40}};
  c.navigateGraph({kind:'chapter',chapter:'c1',depth:1,nodeId:null});
  c.graphSelectNode(1);assert.equal(s.selectedKp,1);
  c.graphGoBack();assert.equal(s.graphFocus.kind,'chapter');assert.equal(s.selectedKp,null);
  c.graphGoBack();assert.equal(s.graphFocus.kind,'all');assert.equal(s.graphViewport.zoom,.7);assert.equal(s.graphViewport.pan.y,40);
});
test('custom study workspaces use the learning directory',()=>{
  const {context:c,state:s,elements}=runtime();s.workspace='custom-study';s.workspaces=[{key:s.workspace,kind:'study'}];
  assert.equal(c.graphIsStudy(),true);c.renderGraphNavigation();assert.match(elements.get('graphNav').innerHTML,/教材知识导航/);
});
test('chapter rows show one canonical title and give the progress meter its own line',()=>{
  const {context:c,state:s,elements}=runtime();
  s.graph={nodes:[{id:1,name:'alpha',chapter_key:'c1',mastery:'未读'},
    {id:2,name:'beta',chapter_key:'c2',mastery:'未读'}],edges:[],
    chapters:[{key:'c1',number:'1',title:'第1章 导论',sections:[]},
      {key:'c2',number:'2',title:'资产负债表',sections:[]}]};
  s.graphFocus={kind:'all',chapter:'',nodeId:null,depth:2};
  c.renderGraphNavigation();
  const markup=elements.get('graphNav').innerHTML;
  assert.equal(markup.includes('第1章 第1章'),false);
  assert.match(markup, /<span>第1章 导论<\/span>/);
  assert.match(markup, /<span>第2章 资产负债表<\/span>/);
  assert.match(markup, /class="graph-chapter-meter"/);
  assert.match(markup, /data-graph-next-chapter="c1"[^>]*>下一节：第1章 导论</);
});
test('600 nodes and real cross-group edges receive finite stable positions without mutation',()=>{
  const {context:c}=runtime();
  const graph={nodes:Array.from({length:600},(_,i)=>({id:i+1,name:`node ${i}`,chapter_key:`c${i%18}`})),
    edges:Array.from({length:1800},(_,i)=>({id:i+1,from_kp:i%600+1,to_kp:(i*7+19)%600+1}))};
  const before=JSON.stringify(graph),start=performance.now();
  const layout=c.computeGraphOverviewLayout(graph),duration=performance.now()-start;
  assert.equal(Object.keys(layout.positions).length,600);assert.equal(layout.clusters.length,18);
  assert.ok(Object.values(layout.positions).every(p=>Number.isFinite(p.x)&&Number.isFinite(p.y)));
  assert.equal(JSON.stringify(graph),before);assert.deepEqual(layout,c.computeGraphOverviewLayout(graph));
  assert.ok(duration<1000,`layout took ${duration}ms`);
});
test('small overview graphs keep a stable minimum spread instead of auto-enlarging labels',()=>{
  const {context:c}=runtime();
  const layout=c.computeGraphOverviewLayout({nodes:[
    {id:1,name:'a',chapter_key:'c1',chapter:'A'},
    {id:2,name:'b',chapter_key:'c2',chapter:'B'},
    {id:3,name:'c',chapter_key:'c3',chapter:'C'}
  ],edges:[]});
  assert.ok(layout.width>=900);
  assert.ok(layout.height>=900);
});
test('chapter fit caps sparse detail graphs without changing overview fit',()=>{
  const {context:c,state:s}=runtime();
  s.graphFocus={kind:'chapter',chapter:'c1',nodeId:null,depth:1};
  let zoom=1.2,fitCalls=0,centerCalls=0;
  const cy={
    destroyed:()=>false,
    container:()=>({clientWidth:900,clientHeight:600}),
    resize(){},
    fit(){fitCalls++;zoom=1.2;},
    zoom(value){if(value) zoom=value.level;return zoom;},
    center(){centerCalls++;},
    elements:()=>({}),
    getElementById:()=>({length:0})
  };
  const box={_syncGraphViewport(){}};
  assert.equal(c.fitGraphViewport(cy,box),true);
  assert.equal(fitCalls,1);
  assert.equal(zoom,.82);
  assert.equal(centerCalls,1);
  s.graphFocus={kind:'all',chapter:'',nodeId:null,depth:2};
  zoom=.7; c.fitGraphViewport(cy,box);
  assert.equal(zoom,1.2);
});
test('empty graph destroys the old canvas and clears its active reference',()=>{
  const {context:c,state:s,elements}=runtime();let destroyed=false;
  s.graphCy={destroy(){destroyed=true;}};c.drawGraph();
  assert.equal(destroyed,true);assert.equal(s.graphCy,null);assert.equal(elements.get('graphBox')._activeCy,null);
  assert.match(elements.get('graphBox').innerHTML,/当前范围没有知识点/);
});
test('older graph requests cannot replace a newer course graph',async()=>{
  const pending=[];
  const {context:c,state:s}=runtime({api:(url,params)=>url==='/api/graph'?new Promise(resolve=>pending.push({resolve,params})):Promise.resolve({node_types:[],relation_kinds:[],quality_rules:[]})});
  c.drawGraph=()=>{};c.renderGraphNavigation=()=>{};c.renderBridges=()=>{};
  s.courses.push({name:'next-course'});
  const first=c.renderGraph();s.graphCourse='next-course';const second=c.renderGraph();
  pending[1].resolve({nodes:[{id:77}],edges:[],chapters:[]});await second;
  pending[0].resolve({nodes:[{id:11}],edges:[],chapters:[]});await first;
  assert.equal(s.graph.nodes[0].id,77);assert.equal(pending[1].params.course,'next-course');assert.equal(pending[1].params.workspace,'studypal');
});
test('scope guards reject a response after leaving and re-entering the same workspace',()=>{
  const {context:c,state:s}=runtime();
  const scope=c.graphScopeSnapshot();
  assert.equal(c.graphScopeCurrent(scope),true);
  s.workspaceEpoch=2;
  assert.equal(c.graphScopeCurrent(scope),false);
});
test('slower traversal of the previous selection cannot overwrite the latest detail',async()=>{
  const pending=[];
  const {context:c,state:s,elements}=runtime({api:()=>new Promise(resolve=>pending.push(resolve))});
  s.selectedKp=1;const old=c.renderGraphTraversal(1);
  s.selectedKp=2;const latest=c.renderGraphTraversal(2);
  pending[2]({nodes:[],paths:[]});pending[3]({name:'latest',revisions:[],evidence:[{id:222}]});await latest;
  const text=elements.get('graphTraversalBox').innerHTML;
  pending[0]({nodes:[],paths:[]});pending[1]({name:'old',revisions:[],evidence:[{id:111}]});await old;
  assert.match(text,/222/);assert.equal(elements.get('graphTraversalBox').innerHTML,text);
});
test('ordinary wheel pans and modified wheel zooms; editable fields retain space input',()=>{
  const listeners={},windowListeners={};let zoom=1,pan={x:0,y:0};
  const cy={on(){},zoom(value){if(value) zoom=value.level;return zoom;},pan(value){if(value)pan=value;return pan;},panBy(value){pan={x:pan.x+value.x,y:pan.y+value.y};},destroyed:()=>false,batch:fn=>fn(),nodes:()=>({toggleClass(){},style(){}})};
  const {context:c}=runtime({window:{addEventListener:(name,fn)=>windowListeners[name]=fn}});
  const box={...element(),addEventListener:(name,fn)=>listeners[name]=fn,getBoundingClientRect:()=>({left:0,top:0})};
  c.installGraphCyInteractions(cy,box);
  listeners.wheel({deltaX:8,deltaY:30,ctrlKey:false,metaKey:false,preventDefault(){},stopImmediatePropagation(){}});
  assert.equal(zoom,1);assert.deepEqual(pan,{x:-8,y:-30});
  listeners.wheel({deltaY:-30,ctrlKey:true,metaKey:false,clientX:100,clientY:100,preventDefault(){},stopImmediatePropagation(){}});
  assert.ok(zoom>1);
  windowListeners.keydown({code:'Space',target:{closest:()=>true},preventDefault(){assert.fail('space in input was intercepted');}});
});
