const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const PALETTE_ANCHOR = 'B3 T3.12/T3.13 命令面板 + 键盘与批量操作（D-09 / D-12）';
const paletteComment = html.indexOf(PALETTE_ANCHOR);
assert.ok(paletteComment > 0, 'command palette anchor found');

const NAV_START = html.indexOf('const NAV = [');
const NAV_END = html.indexOf('];', NAV_START) + 2;
const NAV_SLICE = html.slice(NAV_START, NAV_END);
const VISIBLE_START = html.indexOf('function visibleNav(){');
const VISIBLE_END = html.indexOf('\n}', VISIBLE_START) + 2;
const VISIBLE_SLICE = html.slice(VISIBLE_START, VISIBLE_END);
const ROUTE_START = html.indexOf('function routeAllowed(id){');
const ROUTE_END = html.indexOf('\n}', ROUTE_START) + 2;
const ROUTE_SLICE = html.slice(ROUTE_START, ROUTE_END);
const ROUTE_ONLY_SRC = html.match(/const ROUTE_ONLY = \['lectures'\];/)[0];
const FUNCS_START = html.indexOf('function captureFlashIntake(');
const SIDEBAR_START = html.indexOf('function renderCaptureContextSidebar(');
const FUNCS_SLICE = html.slice(FUNCS_START, SIDEBAR_START);
const SIDEBAR_SLICE = html.slice(SIDEBAR_START, html.lastIndexOf('\n/* ', paletteComment));

assert.ok(NAV_START > 0 && VISIBLE_START > 0 && ROUTE_START > 0, 'routing slices exist');
assert.ok(FUNCS_START > 0 && SIDEBAR_START > FUNCS_START, 'capture module slices exist');

const P_CAPTURE_START = html.indexOf('<section class="page" id="p-capture">');
const P_CAPTURE_END = html.indexOf('二、课程', P_CAPTURE_START);
const CAPTURE_MARKUP = html.slice(P_CAPTURE_START, P_CAPTURE_END);

function routeRuntime(){
  const context = vm.createContext({
    S:{workspace:'studypal'},
    el(id){ return id === 'p-graph' || id === 'p-archive' ? {id} : null; }
  });
  vm.runInContext([NAV_SLICE, ROUTE_ONLY_SRC, VISIBLE_SLICE, ROUTE_SLICE].join('\n'), context);
  return {
    context,
    allowed(id){ return vm.runInContext('routeAllowed(' + JSON.stringify(id) + ')', context); },
    navIds(){ return Array.from(vm.runInContext('NAV.map(n => n.id)', context)); },
    visibleIds(workspace){
      vm.runInContext('S.workspace = ' + JSON.stringify(workspace), context);
      return Array.from(vm.runInContext('visibleNav().map(n => n.id)', context));
    }
  };
}

function captureRuntime(options){
  const opts = options || {};
  const state = {go:[], toasts:[], courseDialogs:0, knowledgeDialogs:[], wikiImports:0, scrolls:[], flash:[], sidebarButtons:[], navHtml:''};
  const elements = new Map();
  function add(id, node){ elements.set(id, node); return node; }
  ['captureLecturesBtn','captureMaterialsBtn','captureKnowledgeBtn','captureWikiBtn'].forEach(function(id){ add(id, {id, onclick:null}); });
  ['contextKicker','contextTitle','contextDesc'].forEach(function(id){ add(id, {id, textContent:''}); });
  const classes = new Set();
  const intakeCard = {
    scrollIntoView(args){ state.scrolls.push(args || null); },
    classList:{
      add(name){ classes.add(name); state.flash.push('+' + name); },
      remove(name){ classes.delete(name); state.flash.push('-' + name); },
      contains(name){ return classes.has(name); }
    }
  };
  const nav = {querySelectorAll(){ return state.sidebarButtons; }};
  Object.defineProperty(nav, 'innerHTML', {
    get(){ return state.navHtml; },
    set(value){
      state.navHtml = String(value);
      state.sidebarButtons = [];
      const pattern = /data-capture-mode="([a-z]+)"/g;
      let match = pattern.exec(state.navHtml);
      while(match){
        const mode = match[1];
        state.sidebarButtons.push({
          mode,
          getAttribute(name){ return name === 'data-capture-mode' ? mode : null; },
          onclick:null
        });
        match = pattern.exec(state.navHtml);
      }
    }
  });
  add('contextNav', nav);
  add('intakeTitle', {id:'intakeTitle', closest(selector){ return selector === '.card' ? intakeCard : null; }});
  const context = vm.createContext({
    S: opts.S || {workspace:'studypal', courses:[]},
    go(id){ state.go.push(id); },
    toast(message){ state.toasts.push(String(message)); },
    openCourseDialog(){ state.courseDialogs += 1; },
    openKnowledgeNewDialog(prefill){ state.knowledgeDialogs.push(prefill); },
    wikiImportOpen(){ state.wikiImports += 1; },
    setTimeout(fn){ fn(); return 0; },
    el(id){ return elements.get(id) || null; }
  });
  vm.runInContext(FUNCS_SLICE + '\n' + SIDEBAR_SLICE, context);
  return {
    context,
    state,
    elements,
    classes,
    run(name){ return vm.runInContext(name, context); },
    click(id){
      const node = elements.get(id);
      assert.equal(typeof node.onclick, 'function', id + ' 已绑定点击处理');
      node.onclick();
    }
  };
}

test('导航注册「录入」并移除旁听项', () => {
  assert.ok(html.includes("{id:'capture', ic:'＋', name:'录入', badge:null}"), 'capture 导航项存在');
  const rt = routeRuntime();
  assert.deepEqual(rt.navIds(), ['workspace','chat','capture','knowledge','course','learning','graph','mastery','calendar','orchestra','settings']);
  assert.ok(!rt.navIds().includes('lectures'), '左侧导航不再单列旁听');
  assert.ok(rt.navIds().indexOf('capture') < rt.navIds().indexOf('knowledge'), '录入紧跟在对话之后');
});

test('两个工作区都保留录入入口，学习专属入口规则不变', () => {
  const rt = routeRuntime();
  const studyIds = rt.visibleIds('studypal');
  assert.ok(studyIds.includes('capture'));
  assert.ok(studyIds.includes('course'), 'StudyPal 保留课程入口');
  const workIds = rt.visibleIds('workpal');
  assert.ok(workIds.includes('capture'), 'WorkPal 也能看到录入入口');
  assert.ok(workIds.includes('knowledge') && workIds.includes('graph'));
  assert.ok(!workIds.includes('course') && !workIds.includes('mastery') && !workIds.includes('lectures'));
});

test('routeAllowed 允许旁听路由直达，未知 id 仍回落', () => {
  const rt = routeRuntime();
  assert.equal(rt.allowed('lectures'), true, '#lectures 保留可达');
  assert.equal(rt.allowed('capture'), true);
  assert.equal(rt.allowed('graph'), true);
  assert.equal(rt.allowed('unknown-route'), false);
  rt.visibleIds('workpal');
  assert.equal(rt.allowed('capture'), true, 'WorkPal 同样可达录入页');
});

test('真实存在的页面容器也能被路由命中', () => {
  const rt = routeRuntime();
  assert.equal(rt.allowed('archive'), true, '页面容器存在时允许进入');
  assert.equal(rt.allowed(''), false);
});

test('go / hashchange / boot 使用 routeAllowed 与 ROUTE_ONLY', () => {
  assert.ok(html.includes("if(!routeAllowed(id)) id = S.workspace === 'studypal' ? 'chat' : 'workspace';"), 'go 回落使用 routeAllowed');
  assert.ok(html.includes("if((NAV.some(n=>n.id===id) || ROUTE_ONLY.indexOf(id) >= 0) && id !== S.page) go(id);"), 'hashchange 允许 ROUTE_ONLY');
  assert.ok(html.includes("go(routeAllowed(initialPage) ? initialPage : (S.workspace==='studypal'?'chat':'workspace'));"), 'boot 初始页使用 routeAllowed');
  assert.ok(html.includes('if(typeof lectureCanLeave === ' + "'function'" + ' && !lectureCanLeave(id)) return;'), '旁听离开守卫保留');
  assert.ok(html.includes("else if(id==='capture') renderCapture();"), 'render 分发注册录入页');
  assert.ok(html.includes("if(S.page === 'capture'){ renderCaptureContextSidebar(); return; }"), '录入页侧栏分支存在');
  assert.ok(html.includes("capture: {kicker:'录入中心', title:'录入', desc:'选择一种记录方式，后续拆解与分析自动匹配。'}"), 'CONTEXT_META 注册录入页');
});

test('#p-capture 位于工作区页之后、课程页之前', () => {
  const workspaceStart = html.indexOf('id="p-workspace"');
  const courseStart = html.indexOf('<section class="page" id="p-course">');
  assert.ok(P_CAPTURE_START > 0 && workspaceStart > 0 && courseStart > 0, '页面锚点齐全');
  assert.ok(workspaceStart < P_CAPTURE_START, '录入页在工作区页之后');
  assert.ok(P_CAPTURE_START < courseStart, '录入页在课程页之前');
});

test('录入页头部与四张卡片按旁听→材料→知识→Wiki 排列', () => {
  assert.ok(CAPTURE_MARKUP.includes('录入中心'));
  assert.ok(CAPTURE_MARKUP.includes('今天要记点什么？'));
  assert.ok(CAPTURE_MARKUP.includes('选一种记录方式，系统自动匹配后续的抽取、拆解与确认流程；所有内容都进同一个知识库。'));
  assert.equal(CAPTURE_MARKUP.split('选中后发生什么').length - 1, 4, '每张卡片都说明「选中后发生什么」');
  const modes = ['lectures','materials','knowledge','wiki'];
  const positions = modes.map(function(mode){ return CAPTURE_MARKUP.indexOf('data-capture-card="' + mode + '"'); });
  positions.forEach(function(position, index){ assert.ok(position > 0, modes[index] + ' 卡片存在'); });
  for(let i = 1; i < positions.length; i += 1) assert.ok(positions[i-1] < positions[i], '卡片顺序为旁听→材料→知识→Wiki');
  const buttons = ['captureLecturesBtn','captureMaterialsBtn','captureKnowledgeBtn','captureWikiBtn'];
  const buttonPositions = buttons.map(function(id){ return CAPTURE_MARKUP.indexOf('id="' + id + '"'); });
  buttonPositions.forEach(function(position, index){ assert.ok(position > 0, buttons[index] + ' 按钮存在'); });
  for(let i = 1; i < buttonPositions.length; i += 1) assert.ok(buttonPositions[i-1] < buttonPositions[i]);
});

test('四张卡片文案与步骤 chips 完整', () => {
  [['🎙 上课旁听','边听边转文字','录音','实时字幕','课堂记录','开始旁听'],
   ['📄 导入材料','自动抽取与切片','上传','抽取','待确认候选','选择课程并导入'],
   ['✍️ 写一条知识','正文 · 标签 · 双链','正文','标签 / 双链','知识库','新建知识'],
   ['🗂 接入 Obsidian Wiki','已有库一键接入','扫描目录','生成节点','关系图','导入 Wiki']
  ].forEach(function(row){
    row.forEach(function(text){ assert.ok(CAPTURE_MARKUP.includes(text), '缺少文案：' + text); });
  });
  assert.ok(CAPTURE_MARKUP.includes('PDF / Word / PPT / 图片 / 音频 / Markdown'));
  assert.ok(CAPTURE_MARKUP.includes('[[双链]]'));
});

test('录入页使用静态标记，无内联事件与动态 HTML 注入点', () => {
  assert.ok(!CAPTURE_MARKUP.includes('onclick='), '不引入内联事件属性');
  assert.ok(!CAPTURE_MARKUP.includes('innerHTML'), '静态页面不写 innerHTML');
  assert.ok(!CAPTURE_MARKUP.includes('<img'), '不内嵌远程图片');
  assert.ok(CAPTURE_MARKUP.includes('type="button"'));
});

test('renderCapture 绑定四个入口并按工作区转发', () => {
  const rt = captureRuntime();
  rt.run('renderCapture')();
  ['captureLecturesBtn','captureMaterialsBtn','captureKnowledgeBtn','captureWikiBtn'].forEach(function(id){
    assert.equal(typeof rt.elements.get(id).onclick, 'function', id + ' 已绑定');
  });
  rt.run('renderCapture')();
  assert.equal(typeof rt.elements.get('captureLecturesBtn').onclick, 'function', '重复渲染仍绑定');
  rt.click('captureLecturesBtn');
  assert.deepEqual(rt.state.go, ['lectures'], '开始旁听跳到旁听页');
  rt.click('captureKnowledgeBtn');
  assert.ok(rt.state.go.includes('knowledge'), '新建知识跳到知识页');
  assert.deepEqual(rt.state.knowledgeDialogs, [''], '打开新建知识对话框');
  rt.click('captureWikiBtn');
  assert.equal(rt.state.wikiImports, 0, '非 WorkPal 不打开 Wiki 导入');
  assert.ok(rt.state.toasts.some(function(message){ return message.includes('切换到 WorkPal'); }), '提示切换到 WorkPal');
});

test('无课程时导入材料打开课程对话框', () => {
  const rt = captureRuntime({S:{workspace:'studypal', courses:[]}});
  rt.run('renderCapture')();
  rt.click('captureMaterialsBtn');
  assert.equal(rt.state.courseDialogs, 1, '无课程先建课程');
  assert.deepEqual(rt.state.go, [], '不跳转课程页');
  assert.deepEqual(rt.state.scrolls, [], '不滚动材料入口');
});

test('有课程时跳转课程页并高亮材料入口', () => {
  const rt = captureRuntime({S:{workspace:'studypal', courses:[{name:'审计学'}]}});
  rt.run('renderCapture')();
  rt.click('captureMaterialsBtn');
  assert.deepEqual(rt.state.go, ['course']);
  assert.equal(rt.state.scrolls.length, 1, '滚动到材料入口');
  assert.equal(rt.state.scrolls[0].block, 'center');
  assert.deepEqual(rt.state.flash, ['+capture-flash','-capture-flash'], '先高亮再在 2 秒后移除');
  assert.equal(rt.classes.has('capture-flash'), false);
  assert.equal(rt.state.courseDialogs, 0);
});

test('WorkPal 下导入 Wiki 直接打开导入对话框', () => {
  const rt = captureRuntime({S:{workspace:'workpal', courses:[]}});
  rt.run('renderCapture')();
  rt.click('captureWikiBtn');
  assert.equal(rt.state.wikiImports, 1, '调用 wikiImportOpen');
  assert.deepEqual(rt.state.toasts, []);
});

test('录入页侧栏提供四种记录方式并复用同一组入口', () => {
  const rt = captureRuntime();
  rt.run('renderCaptureContextSidebar')();
  assert.equal(rt.elements.get('contextKicker').textContent, '录入中心');
  assert.equal(rt.elements.get('contextTitle').textContent, '记录方式');
  assert.deepEqual(Array.from(rt.state.sidebarButtons, function(button){ return button.mode; }), ['lectures','materials','knowledge','wiki']);
  assert.ok(rt.state.navHtml.includes('data-capture-mode="lectures"'));
  rt.state.sidebarButtons[0].onclick();
  assert.deepEqual(rt.state.go, ['lectures'], '侧栏旁听按钮复用同一入口');
  rt.state.sidebarButtons[3].onclick();
  assert.equal(rt.state.wikiImports, 0);
  assert.ok(rt.state.toasts.some(function(message){ return message.includes('切换到 WorkPal'); }));
});

test('旁听页从导航收起但路由与渲染钩子保留', () => {
  assert.ok(html.includes("const ROUTE_ONLY = ['lectures'];"), '旁听页在路由白名单中');
  assert.ok(!html.includes("{id:'lectures', ic:'🎙'"), '导航不再单列旁听项');
  assert.ok(html.includes('<section class="page" id="p-lectures">'), '旁听页容器保留');
  assert.ok(html.includes('.lectures-layout{display:grid'), '旁听页样式保留');
  assert.ok(html.includes("else if(id==='lectures') renderLecturesPage();"), '旁听页渲染分发保留');
  assert.ok(html.includes("if(S.page === 'lectures'){ renderLecturesContextSidebar(); return; }"), '旁听页侧栏分支保留');
});

test('录入模块不拼接动态 HTML', () => {
  assert.ok(!FUNCS_SLICE.includes('innerHTML'), '入口函数不写 innerHTML');
  assert.ok(!FUNCS_SLICE.includes('insertAdjacentHTML'));
  assert.ok(SIDEBAR_SLICE.includes('data-capture-mode="'), '侧栏按钮由静态行数据生成');
  assert.ok(!SIDEBAR_SLICE.includes('S.courses'), '侧栏不拼接课程等动态文本');
});
