const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

test('graph supports two-hop traversal and explicit relation opening', () => {
  const start = script.indexOf('function graphNeighbors(');
  const end = script.indexOf('function clearGraphFocus', start);
  const context = vm.createContext({S: {graph: {edges: [
    {from_kp: 1, to_kp: 2}, {from_kp: 2, to_kp: 3}, {from_kp: 3, to_kp: 4}
  ]}}});
  vm.runInContext(script.slice(start, end), context);
  assert.deepEqual([...context.graphNeighbors(1, 2)].sort((a, b) => a - b), [1, 2, 3]);
  assert.match(script, /data-edge-focus=/);
  assert.match(script, /graphFocusStatus/);
  assert.match(script, /toggleClass\('trace'/);
});

test('graph has canonical chapter focus and an overview dot layout', () => {
  assert.match(script, /graphFocus:\{kind:'all',chapter:'',nodeId:null,depth:2\}/);
  assert.match(script, /data-graph-chapter=/);
  assert.match(script, /function graphFocusIds\(/);
  assert.match(script, /graphFocusIds\(graph\)/);
  assert.match(script, /selector:'node\.knowledge'.*shape:'ellipse'/);
  assert.match(script, /item\.classList\.toggle\('on', active\)/);
  assert.match(script, /item\.setAttribute\('aria-selected', active \? 'true' : 'false'\)/);
});

test('graph uses a local Cytoscape canvas with modifier-gated zoom', () => {
  assert.match(html, /assets\/cytoscape\.min\.js/);
  assert.match(script, /window\.cytoscape\(/);
  assert.match(script, /userZoomingEnabled:false/);
  assert.match(script, /event\.ctrlKey \|\| event\.metaKey/);
  assert.match(script, /graphViewport/);
  assert.match(script, /priorViewport/);
  assert.match(script, /_activeCy\.fit\(32\)/);
});

test('graph renders unframed chapter headings and keeps chapter taps separate from knowledge taps', () => {
  assert.match(script, /function graphChapterId\(/);
  assert.doesNotMatch(script, /parent:graphChapterId\(/);
  assert.match(script, /isChapter:true/);
  assert.match(script, /chapter-cluster/);
  assert.match(script, /if\(event\.target\.data\('isChapter'\)\)/);
});

test('graph keeps knowledge labels readable while de-emphasizing only relationship lines', () => {
  assert.match(script, /selector:'node\.dim',style:\{'opacity':1/);
  assert.match(script, /selector:'node\.trace',style:\{'opacity':1/);
  assert.match(script, /selector:'edge\.dim'/);
  assert.match(script, /selector:'edge\.active'.*width:2/);
  assert.match(script, /'color':'#342638'/);
});

test('graph overview positions stable chapter clusters around a circle', () => {
  const start = script.indexOf('function computeGraphOverviewLayout(');
  assert.notEqual(start, -1, 'overview layout function exists');
  const end = script.indexOf('\nfunction ', start + 10);
  const context = vm.createContext({});
  vm.runInContext(script.slice(start, end), context);
  const graph = {
    nodes: [
      {id: 1, chapter_key: 'ch-01'}, {id: 2, chapter_key: 'ch-01'},
      {id: 3, chapter_key: 'ch-02'}, {id: 4, chapter_key: 'ch-02'},
      {id: 5, chapter_key: 'ch-03'}
    ],
    edges: [
      {from_kp: 1, to_kp: 2}, {from_kp: 1, to_kp: 3},
      {from_kp: 3, to_kp: 4}, {from_kp: 4, to_kp: 5}
    ]
  };
  const layout = context.computeGraphOverviewLayout(graph);
  assert.equal(layout.clusters.length, 3);
  assert.equal(Object.keys(layout.positions).length, graph.nodes.length);
  assert.ok(layout.size > 300);
  assert.ok(Math.abs(layout.size - layout.height) < 0.001);
  const sameChapter = Math.hypot(
    layout.positions[1].x - layout.positions[2].x,
    layout.positions[1].y - layout.positions[2].y
  );
  const otherChapter = Math.hypot(
    layout.positions[1].x - layout.positions[3].x,
    layout.positions[1].y - layout.positions[3].y
  );
  assert.ok(sameChapter < otherChapter, 'nodes from one chapter stay clustered');
  assert.deepEqual(layout, context.computeGraphOverviewLayout(graph));
});
