const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const htmlPath = path.join(__dirname, '..', 'web', 'index.html');
const html = fs.readFileSync(htmlPath, 'utf8');

function test(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (error) {
    console.error(`not ok - ${name}`);
    throw error;
  }
}

test('graph exposes a readable, clickable relation path rail', () => {
  assert.match(html, /data-path-node/);
  assert.match(html, /串联路径/);
  assert.match(html, /traversal-path/);
});

test('focused graph uses a radial exploration layout around the selected node', () => {
  assert.match(html, /selectedId/);
  assert.match(html, /depth === 1/);
  assert.match(html, /depth === 2/);
});

test('chapter focus uses a bounded detail layout instead of the overview layout', () => {
  assert.match(html, /S\.graphFocus\?\.kind === 'chapter'/);
  assert.match(html, /graphChapterDetailLayout/);
  assert.match(html, /chapterDetailMinRadius/);
});

test('overview label density is not promoted to all labels by automatic zoom', () => {
  assert.match(html, /const level=overview \? \(activeCy\.zoom\(\)>=\.42\?'key':'cluster'\)/);
  assert.doesNotMatch(html, /overview \? \'all\'/);
});

test('direction and relation type are visible on graph edges', () => {
  assert.match(html, /kindLabel/);
  assert.match(html, /target-arrow-shape/);
});

test('ordinary page scrolling does not zoom the graph', () => {
  assert.match(html, /event\.ctrlKey \|\| event\.metaKey/);
  assert.doesNotMatch(html, /userZoomingEnabled:true/);
});

test('WorkPal keeps the graph route and uses Wiki topic language', () => {
  assert.match(html, /\['workspace','chat','knowledge','learning','graph','calendar','settings'\]/);
  assert.match(html, /Wiki 主题导航/);
  assert.match(html, /主题簇/);
  assert.match(html, /data-graph-context/);
  assert.match(html, /id="graphPathBtn"/);
  assert.match(html, /data-graph-next-chapter/);
});

test('WorkPal 下知识入口可见，学习专属入口仍然隐藏', () => {
  const navStart = html.indexOf('const NAV = [');
  const navEnd = html.indexOf('];', navStart) + 2;
  const visibleStart = html.indexOf('function visibleNav(){');
  const visibleEnd = html.indexOf('\n}', visibleStart) + 2;
  assert.ok(navStart > 0 && visibleStart > 0, 'NAV/visibleNav slices exist');
  const context = vm.createContext({S:{workspace:'workpal'}});
  vm.runInContext(html.slice(navStart, navEnd) + '\n' + html.slice(visibleStart, visibleEnd)
    + '\nthis.__ids = visibleNav().map(n => n.id);', context);
  assert.ok(context.__ids.includes('knowledge'), 'WorkPal 能看到知识入口');
  assert.ok(context.__ids.includes('graph'), 'WorkPal 保留图谱入口');
  assert.ok(!context.__ids.includes('course'), '课程入口仍是学习工作区专属');
  assert.ok(!context.__ids.includes('mastery'), '掌握度仍是学习工作区专属');
});
