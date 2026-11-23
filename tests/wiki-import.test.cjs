const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const PALETTE_ANCHOR = 'B3 T3.12/T3.13 命令面板 + 键盘与批量操作（D-09 / D-12）';
const paletteComment = html.indexOf(PALETTE_ANCHOR);
assert.ok(paletteComment > 0, 'command palette anchor found');
const WIKI_SLICE = html.slice(html.indexOf('const WIKI_IMPORT_POLL_MS = 900;'), html.lastIndexOf('\n/* ', paletteComment));
const ESC_SLICE = html.slice(html.indexOf('function esc(s){'), html.indexOf('\nfunction toast('));
const STORAGE_KEY = 'mindock.wikiVault';

assert.ok(WIKI_SLICE.includes('function wikiImportMount'), 'mount function found in slice');
assert.ok(WIKI_SLICE.includes('function wikiImportRun'), 'run function found in slice');
assert.ok(WIKI_SLICE.includes('function wikiImportOpen'), 'open function found in slice');
assert.ok(ESC_SLICE.includes('String(s'), 'production esc found');

function fakeElement(id){
  const classes = new Set();
  const node = {
    id, hidden:false, textContent:'', value:'', focused:false, onclick:null,
    classList:{
      toggle(name, on){ if(on) classes.add(name); else classes.delete(name); },
      contains(name){ return classes.has(name); }
    },
    focus(){ node.focused = true; },
    getAttribute(name){ return node[name] == null ? null : String(node[name]); },
    setAttribute(name, value){ node[name] = String(value); }
  };
  return node;
}

function createRuntime(options){
  const opts = options || {};
  const state = {toasts:[], apiCalls:[], apiPosts:[], dialogs:[], navigations:[], renders:0, storage:new Map(), elements:new Map()};
  if(opts.saved) state.storage.set(STORAGE_KEY, opts.saved);
  const context = {
    S:{workspace:opts.workspace || 'workpal', workspaceEpoch:0, page:opts.page || 'graph', courses:[]},
    localStorage:{
      getItem(key){ return state.storage.has(key) ? state.storage.get(key) : null; },
      setItem(key, value){ state.storage.set(key, String(value)); }
    },
    setTimeout:opts.setTimeout || function(fn){ return setTimeout(fn, 0); },
    toast(message){ state.toasts.push(String(message)); },
    el(id){
      if(!state.elements.has(id)) state.elements.set(id, fakeElement(id));
      return state.elements.get(id);
    },
    async api(apiPath, params){
      state.apiCalls.push({path:apiPath, params});
      if(typeof opts.api === 'function') return opts.api(apiPath, params, state);
      return {state:'idle', candidates:[]};
    },
    async apiPost(apiPath, body){
      state.apiPosts.push({path:apiPath, body});
      if(typeof opts.apiPost === 'function') return opts.apiPost(apiPath, body, state);
      return {ok:true, state:'running'};
    },
    openAppDialog(dialogOptions){
      state.dialogs.push(dialogOptions);
      context.el('appDialogConfirm').textContent = dialogOptions.confirmLabel;
      return opts.backdrop === undefined ? null : opts.backdrop;
    },
    go(page){ state.navigations.push(page); context.S.page = page; },
    async renderGraph(){ state.renders += 1; }
  };
  vm.createContext(context);
  vm.runInContext(ESC_SLICE, context, {filename:'esc-inline.js'});
  vm.runInContext(WIKI_SLICE, context, {filename:'wiki-import-inline.js'});
  return {
    context, state,
    element(id){ return context.el(id); },
    fn(name){ return vm.runInContext(name, context); }
  };
}

function deferred(){
  let resolve, reject;
  const promise = new Promise((yes, no)=>{ resolve = yes; reject = no; });
  return {promise, resolve, reject};
}

function switchToStudyPal(rt){
  rt.context.S.workspace = 'studypal';
  rt.context.S.workspaceEpoch += 1;
  rt.context.S.page = 'chat';
  rt.context.S.graphCourse = '组织行为学';
  rt.context.S.courses = [{name:'组织行为学'}];
  rt.element('wikiImportStatus').textContent = '新工作区状态';
}

function assertStudyPalUnchanged(rt){
  assert.equal(rt.context.S.page, 'chat');
  assert.equal(rt.context.S.graphCourse, '组织行为学');
  assert.deepEqual(rt.context.S.courses, [{name:'组织行为学'}]);
  assert.equal(rt.element('wikiImportStatus').textContent, '新工作区状态');
  assert.equal(rt.state.renders, 0);
  assert.deepEqual(rt.state.toasts, []);
  assert.deepEqual(rt.state.navigations, []);
}

test('图谱治理区渲染「导入 Wiki」按钮且默认隐藏', ()=>{
  const health = html.indexOf('id="graphHealthBtn"');
  assert.ok(health > 0, 'graph health button found');
  const row = html.slice(health, html.indexOf('</div>', health));
  const importPos = row.indexOf('id="graphImportBtn"');
  const wikiPos = row.indexOf('id="wikiImportBtn"');
  const filePos = row.indexOf('id="graphImportFile"');
  assert.ok(importPos > 0 && wikiPos > importPos && filePos > wikiPos, '按钮在导入 JSON 之后、文件输入之前');
  assert.match(row, /<button class="act" id="wikiImportBtn" type="button" hidden>导入 Wiki<\/button>/);
  assert.ok(!/<button[^>]*id="wikiImportBtn"[^>]*onclick=/.test(row), '无内联 onclick');
  const governance = html.indexOf('id="graphGovernance"');
  assert.ok(governance > 0 && governance < health, '位于图谱治理折叠区');
});

test('renderGraph 渲染后重挂 Wiki 导入入口', ()=>{
  assert.match(html, /el\('graphImportBtn'\)\.onclick = \(\)=>el\('graphImportFile'\)\.click\(\);\n  if\(typeof wikiImportMount === 'function'\) wikiImportMount\(\);/);
});

test('材料类型标签映射新增 wiki_page 且既有标签保持不变', ()=>{
  const match = html.match(/const kindLabel = (\{[^}]*\})\[f\.source_kind\]/);
  assert.ok(match, '找到 kindLabel 映射');
  const expected = {textbook:'教材', class_note:'课堂记录', supplement:'补充资料', course_catalog:'课程目录', event_capture:'事件材料', development_doc:'开发文档', wiki_page:'Wiki 页面', unclassified:'未分类'};
  for(const key of Object.keys(expected)){
    assert.ok(match[1].includes(key + ":'" + expected[key] + "'"), key + ' 标签存在');
  }
});

test('Wiki 导入模块包含后端契约路径、轮询参数与转义', ()=>{
  const fragments = [
    "apiPost('/api/wiki/import'",
    "api('/api/wiki/import/status'",
    'const WIKI_IMPORT_POLL_MS = 900;',
    'const WIKI_IMPORT_TIMEOUT_MS = 10 * 60 * 1000;',
    "const WIKI_IMPORT_STORAGE_KEY = 'mindock.wikiVault';",
    'function wikiImportMount',
    'function wikiImportDoneToast',
    'esc(candidate.path)',
    'esc(defaultPath)'
  ];
  for(const fragment of fragments) assert.ok(WIKI_SLICE.includes(fragment), fragment);
  assert.ok(html.includes('role="status" aria-live="polite"'), '状态区 aria-live');
  assert.ok(html.includes('.wiki-import-status{'), '状态区样式');
});

test('wikiImportMount 仅 WorkPal 显示入口，StudyPal 隐藏', ()=>{
  const workpal = createRuntime({workspace:'workpal'});
  const button = workpal.fn('wikiImportMount')();
  assert.equal(button.hidden, false);
  assert.equal(typeof button.onclick, 'function');
  const studypal = createRuntime({workspace:'studypal'});
  const hiddenButton = studypal.fn('wikiImportMount')();
  assert.equal(hiddenButton.hidden, true);
  assert.equal(hiddenButton.onclick, null);
  studypal.context.S.workspace = 'workpal';
  assert.equal(studypal.fn('wikiImportMount')().hidden, false);
});

test('打开对话框：WorkPal 主题、目录支持范围与按来源更新说明', async ()=>{
  const rt = createRuntime({api: async apiPath => {
    assert.equal(apiPath, '/api/wiki/import/status');
    return {state:'idle', candidates:[
      {path:'/Users/me/Obsidian/wiki', label:'我的 Obsidian wiki', has_wiki:true},
      {path:'/Users/me/Obsidian/other', label:'另一个 Markdown 目录', has_wiki:false}
    ]};
  }});
  await rt.fn('wikiImportOpen')();
  assert.equal(rt.state.dialogs.length, 1);
  const dialog = rt.state.dialogs[0];
  assert.equal(dialog.title, '导入 Wiki');
  assert.equal(dialog.confirmLabel, '开始导入');
  assert.ok(dialog.bodyHtml.includes('id="wikiVaultPath"'));
  assert.ok(dialog.bodyHtml.includes('value="/Users/me/Obsidian/wiki"'));
  assert.ok(dialog.bodyHtml.includes('Obsidian 库或 Markdown 目录'));
  assert.ok(dialog.bodyHtml.includes('自动优先使用 wiki 子目录'));
  assert.ok(dialog.bodyHtml.includes('源文件不会被修改'));
  assert.ok(dialog.bodyHtml.includes('导入到 WorkPal 主题'));
  assert.ok(dialog.bodyHtml.includes('按来源页面更新'));
  assert.ok(!dialog.bodyHtml.includes('导入到课程'));
  assert.ok(!dialog.bodyHtml.includes('合并同名知识点'));
  assert.ok(dialog.bodyHtml.includes('id="wikiCourseName"'));
  assert.ok(dialog.bodyHtml.includes('value="AI工作系统"'));
  assert.ok(dialog.bodyHtml.includes('role="status" aria-live="polite"'));
  assert.ok(dialog.bodyHtml.includes('data-wiki-candidate="/Users/me/Obsidian/wiki"'));
  assert.equal(typeof dialog.onConfirm, 'function');
});

test('候选路径与标签中的 HTML 被转义', async ()=>{
  const rt = createRuntime({api: async () => ({state:'idle', candidates:[
    {path:'/Users/x/<script>alert(1)</script>/wiki', label:'<img src=x onerror=1>'}
  ]})});
  await rt.fn('wikiImportOpen')();
  const body = rt.state.dialogs[0].bodyHtml;
  assert.ok(body.includes('&lt;script&gt;alert(1)&lt;/script&gt;'));
  assert.ok(!body.includes('<script>'));
  assert.ok(body.includes('&lt;img src=x onerror=1&gt;'));
  assert.ok(!body.includes('<img'));
});

test('已保存目录优先于候选默认路径', async ()=>{
  const rt = createRuntime({saved:'/saved/Obsidian/wiki', api: async () => ({state:'idle', candidates:[{path:'/candidate/wiki'}]})});
  await rt.fn('wikiImportOpen')();
  const body = rt.state.dialogs[0].bodyHtml;
  assert.ok(body.includes('value="/saved/Obsidian/wiki"'));
  assert.ok(!body.includes('value="/candidate/wiki"'));
});

for(const [name, candidates, expected] of [
  ['Documents 排在前时优先唯一 wiki', [{path:'/Users/me/Documents', has_wiki:false}, {path:'/Users/me/LLM wiki/wiki', has_wiki:true}], '/Users/me/LLM wiki/wiki'],
  ['多个无 wiki 目录不默认选择 Documents', [{path:'/Users/me/Documents', has_wiki:false}, {path:'/Users/me/Notes', has_wiki:false}], ''],
  ['多个 wiki 库留空待选', [{path:'/first/wiki', has_wiki:true}, {path:'/second/wiki', has_wiki:true}], ''],
  ['唯一 Markdown 目录可作为默认路径', [{path:'/manual/markdown', has_wiki:false}], '/manual/markdown']
]){
  test('默认路径：' + name, async ()=>{
    const rt = createRuntime({api:()=>({state:'idle', candidates})});
    await rt.fn('wikiImportOpen')();
    assert.ok(rt.state.dialogs[0].bodyHtml.includes('value="' + expected + '"'));
    assert.equal(rt.fn('wikiImportDefaultPath')(candidates), expected);
    if(!expected) assert.ok(rt.state.dialogs[0].bodyHtml.includes('data-wiki-candidate="' + candidates[0].path + '"'));
  });
}

test('已存路径优先于唯一 has_wiki 候选', ()=>{
  const rt = createRuntime({saved:'/saved/markdown'});
  assert.equal(rt.fn('wikiImportDefaultPath')([{path:'/Documents'}, {path:'/other/wiki', has_wiki:true}]), '/saved/markdown');
});

test('候选 chip 点击回填目录输入框', async ()=>{
  const chip = fakeElement('chip');
  chip['data-wiki-candidate'] = '/Users/me/Obsidian/other/wiki';
  const backdrop = {querySelectorAll(){ return [chip]; }};
  const rt = createRuntime({backdrop, api: async () => ({state:'idle', candidates:[{path:'/Users/me/Obsidian/wiki'}]})});
  await rt.fn('wikiImportOpen')();
  rt.element('wikiVaultPath').value = '';
  chip.onclick();
  assert.equal(rt.element('wikiVaultPath').value, '/Users/me/Obsidian/other/wiki');
});

test('进度与完成文案：running/done/error/idle', ()=>{
  const rt = createRuntime();
  assert.equal(rt.fn('wikiImportProgressText')({state:'running', done:3, total:12}), '正在导入 3/12 页…');
  assert.equal(rt.fn('wikiImportProgressText')({state:'done'}), '导入完成');
  assert.match(rt.fn('wikiImportProgressText')({state:'error', error:'坏路径'}), /^导入失败：坏路径/);
  assert.equal(rt.fn('wikiImportProgressText')(null), '正在读取导入状态…');
  rt.fn('wikiImportDoneToast')({result:{nodes_created:3, nodes_updated:2, edges_created:5, unresolved_links:4, parse_warnings:['bad.md', 'also-bad.md']}});
  rt.fn('wikiImportDoneToast')({result:{nodes_created:0, nodes_updated:3, edges_created:0, unresolved_links:0}});
  rt.fn('wikiImportDoneToast')({result:{nodes_created:0, nodes_updated:0, edges_created:0, unresolved_links:0}});
  rt.fn('wikiImportDoneToast')({result:{copied:2}});
  rt.fn('wikiImportDoneToast')({});
  assert.deepEqual(rt.state.toasts, [
    '导入完成：新增 3 个知识点 / 更新 2 个知识点 / 新增 5 条关系 / 未解析 4 个链接 / 解析告警 2 个文件',
    '导入完成：新增 0 个知识点 / 更新 3 个知识点 / 新增 0 条关系 / 未解析 0 个链接 / 解析告警 0 个文件',
    '导入完成：新增 0 个知识点 / 更新 0 个知识点 / 新增 0 条关系 / 未解析 0 个链接 / 解析告警 0 个文件',
    '导入完成：已导入 2 个 Wiki 页面', '导入完成'
  ]);
});

test('手动路径导入：POST 契约不变，保留完成统计并明确查看导入主题图谱', async ()=>{
  let statusCalls = 0;
  const rt = createRuntime({
    page:'capture',
    api: async apiPath => {
      if(apiPath === '/api/wiki/import/status'){
        statusCalls += 1;
        if(statusCalls === 1) return {state:'idle', candidates:[{path:'/Users/me/Obsidian/wiki'}]};
        if(statusCalls === 2) return {state:'running', total:12, done:3};
        return {state:'done', result:{course:'AI工作系统', nodes_created:3, nodes_updated:1, edges_created:5, unresolved_links:2, parse_warnings:['Bad.md'], unresolved_details:[{page:'A.md', target:'Missing'}], copied:2}};
      }
      if(apiPath === '/api/courses') return [{name:'AI工作系统'}];
      throw new Error('unexpected api ' + apiPath);
    },
    apiPost: async () => ({ok:true, state:'running'})
  });
  await rt.fn('wikiImportOpen')();
  const dialog = rt.state.dialogs[0];
  rt.element('wikiVaultPath').value = '  /Users/me/Obsidian/wiki  ';
  rt.element('wikiCourseName').value = 'AI工作系统';
  assert.equal(await dialog.onConfirm(), false, '完成后保留结果供查看');
  assert.equal(rt.state.apiPosts.length, 1);
  assert.equal(rt.state.apiPosts[0].path, '/api/wiki/import');
  assert.equal(rt.state.apiPosts[0].body.vault_root, '/Users/me/Obsidian/wiki');
  assert.equal(rt.state.apiPosts[0].body.course, 'AI工作系统');
  assert.deepEqual(Object.keys(rt.state.apiPosts[0].body).sort(), ['course', 'vault_root']);
  assert.equal(statusCalls, 3, '打开取候选一次 + 轮询两次');
  const summary = '导入完成：新增 3 个知识点 / 更新 1 个知识点 / 新增 5 条关系 / 未解析 2 个链接 / 解析告警 1 个文件';
  assert.deepEqual(rt.state.toasts, [summary]);
  assert.deepEqual(rt.context.S.courses, [{name:'AI工作系统'}]);
  assert.equal(rt.state.renders, 0);
  assert.equal(rt.context.S.page, 'capture', '完成时不抢走当前页面');
  assert.equal(rt.state.storage.get(STORAGE_KEY), '/Users/me/Obsidian/wiki');
  assert.equal(rt.element('wikiImportStatus').textContent, summary);
  const details = rt.element('wikiImportResult');
  assert.equal(details.hidden, false);
  assert.match(details.innerHTML, /<details>/);
  assert.ok(!details.innerHTML.includes('<details open'));
  assert.ok(details.innerHTML.includes('A.md') && details.innerHTML.includes('Missing'));
  assert.equal(rt.element('appDialogConfirm').textContent, '查看图谱');
  assert.equal(await dialog.onConfirm(), true, '查看图谱后关闭对话框');
  assert.equal(rt.context.S.graphCourse, 'AI工作系统');
  assert.deepEqual(rt.state.navigations, ['graph']);
  assert.equal(rt.state.apiPosts.length, 1, '查看图谱不重复导入');
  assert.ok(rt.state.apiCalls.some(call=>call.path === '/api/courses' && call.params.workspace === 'workpal'));
});

test('空目录时保持对话框打开且不提交', async ()=>{
  const rt = createRuntime({api: async () => ({state:'idle', candidates:[]})});
  await rt.fn('wikiImportOpen')();
  const dialog = rt.state.dialogs[0];
  rt.element('wikiVaultPath').value = '   ';
  assert.equal(await dialog.onConfirm(), false);
  assert.equal(rt.state.apiPosts.length, 0);
  assert.equal(rt.element('wikiImportStatus').textContent, '请先填写 Obsidian 库或 Markdown 目录');
  assert.equal(rt.element('wikiVaultPath').focused, true);
});

test('导入状态 error 时返回 false 并在状态区显示原因', async ()=>{
  let statusCalls = 0;
  const rt = createRuntime({api: async apiPath => {
    if(apiPath === '/api/wiki/import/status'){
      statusCalls += 1;
      if(statusCalls === 1) return {state:'idle', candidates:[]};
      return {state:'error', error:'Wiki 目录不存在'};
    }
    throw new Error('unexpected api ' + apiPath);
  }});
  await rt.fn('wikiImportOpen')();
  const dialog = rt.state.dialogs[0];
  rt.element('wikiVaultPath').value = '/Users/me/Obsidian/wiki';
  assert.equal(await dialog.onConfirm(), false);
  assert.match(rt.element('wikiImportStatus').textContent, /^导入失败：Wiki 目录不存在/);
  assert.equal(rt.state.renders, 0);
});

test('已有导入任务时继续轮询，done 后使用 copied 文案', async ()=>{
  let statusCalls = 0;
  const rt = createRuntime({
    api: async apiPath => {
      if(apiPath === '/api/wiki/import/status'){
        statusCalls += 1;
        if(statusCalls === 1) return {state:'idle', candidates:[]};
        if(statusCalls === 2) return {state:'running', total:4, done:1};
        return {state:'done', course:'已有主题', result:{pages:5, copied:4}};
      }
      if(apiPath === '/api/courses') return [];
      throw new Error('unexpected api ' + apiPath);
    },
    apiPost: async () => { throw new Error('导入任务正在运行，请稍后'); }
  });
  await rt.fn('wikiImportOpen')();
  const dialog = rt.state.dialogs[0];
  rt.element('wikiVaultPath').value = '/Users/me/Obsidian/wiki';
  rt.element('wikiCourseName').value = '未提交的新主题';
  assert.equal(await dialog.onConfirm(), false);
  assert.equal(rt.state.apiPosts.length, 1);
  assert.ok(rt.state.toasts.includes('导入完成：已导入 4 个 Wiki 页面'));
  assert.equal(await dialog.onConfirm(), true);
  assert.equal(rt.context.S.graphCourse, '已有主题', '跟随实际完成任务的主题');
});

test('提交失败（非 409）时状态区显示 detail 并保持打开', async ()=>{
  const rt = createRuntime({
    api: async apiPath => {
      if(apiPath === '/api/wiki/import/status') return {state:'idle', candidates:[]};
      throw new Error('unexpected api ' + apiPath);
    },
    apiPost: async () => { throw new Error('目录不存在：/nope'); }
  });
  await rt.fn('wikiImportOpen')();
  const dialog = rt.state.dialogs[0];
  rt.element('wikiVaultPath').value = '/nope';
  assert.equal(await dialog.onConfirm(), false);
  assert.match(rt.element('wikiImportStatus').textContent, /^提交失败：目录不存在：/);
});

test('未解析明细与告警路径转义且最多展示前 200 条', async ()=>{
  const unresolved = [{page:'<img src=x onerror=1>.md', target:'<script>alert(1)</script>'}, ...Array.from({length:201}, (_, i)=>({page:'Page-' + i, target:'Target-' + i}))];
  const rt = createRuntime({api:apiPath=>apiPath === '/api/courses' ? [] : {state:'done', result:{nodes_created:0, nodes_updated:0, unresolved_links:202, parse_warnings:['<svg onload=1>.md'], unresolved_details:unresolved}}});
  await rt.fn('wikiImportOpen')();
  rt.element('wikiVaultPath').value = '/manual/markdown';
  assert.equal(await rt.state.dialogs[0].onConfirm(), false);
  const details = rt.element('wikiImportResult').innerHTML;
  assert.ok(details.includes('&lt;img src=x onerror=1&gt;.md'));
  assert.ok(details.includes('&lt;script&gt;alert(1)&lt;/script&gt;'));
  assert.ok(details.includes('&lt;svg onload=1&gt;.md'));
  assert.ok(!/<(?:img|script|svg)[ >]/.test(details));
  assert.ok(details.includes('Page-198'));
  assert.ok(!details.includes('Page-199'));
  assert.ok(details.includes('200'));
  assert.ok(rt.element('wikiImportStatus').textContent.includes('未解析 202 个链接'));
});

test('StudyPal 不能直接打开或提交 Wiki 导入', async ()=>{
  const rt = createRuntime({workspace:'studypal'});
  await rt.fn('wikiImportOpen')();
  assert.equal(await rt.fn('wikiImportRun')('/manual/markdown', '主题'), false);
  assert.equal(rt.state.dialogs.length, 0);
  assert.equal(rt.state.apiCalls.length, 0);
  assert.equal(rt.state.apiPosts.length, 0);
});

for(const fail of [false, true]){
  test('加载候选目录时切换工作区，迟到的' + (fail ? '错误' : '结果') + '不弹出导入框', async ()=>{
    const pending = deferred();
    const rt = createRuntime({api:()=>pending.promise});
    const opening = rt.fn('wikiImportOpen')();
    switchToStudyPal(rt);
    if(fail) pending.reject(new Error('工作区已切换'));
    else pending.resolve({state:'idle', candidates:[{path:'/old/wiki'}]});
    await opening;
    assert.equal(rt.state.dialogs.length, 0);
    assertStudyPalUnchanged(rt);
  });

  test('提交导入时切换工作区，迟到的' + (fail ? '错误' : '响应') + '不开始轮询', async ()=>{
    const pending = deferred();
    const rt = createRuntime({apiPost:()=>pending.promise});
    await rt.fn('wikiImportOpen')();
    rt.element('wikiVaultPath').value = '/manual/markdown';
    const running = rt.state.dialogs[0].onConfirm();
    switchToStudyPal(rt);
    if(fail) pending.reject(new Error('409 already running'));
    else pending.resolve({ok:true, state:'running'});
    assert.equal(await running, false);
    assert.equal(rt.state.apiCalls.length, 1, '切换后不再查询旧任务');
    assertStudyPalUnchanged(rt);
  });

  test('刷新主题列表时切换工作区，迟到的' + (fail ? '错误' : '结果') + '不覆盖 StudyPal', async ()=>{
    const pending = deferred(), started = deferred();
    const rt = createRuntime({api:apiPath=>{
      if(apiPath === '/api/courses'){ started.resolve(); return pending.promise; }
      return {state:'done', result:{nodes_created:3}};
    }});
    await rt.fn('wikiImportOpen')();
    rt.element('wikiVaultPath').value = '/manual/markdown';
    const running = rt.state.dialogs[0].onConfirm();
    await started.promise;
    switchToStudyPal(rt);
    if(fail) pending.reject(new Error('工作区已切换'));
    else pending.resolve([{name:'旧 WorkPal 主题'}]);
    assert.equal(await running, false);
    assertStudyPalUnchanged(rt);
  });
}

for(const outcome of ['done', 'running', 'error', 'reject']){
  test('轮询等待期间切换工作区，忽略 ' + outcome + ' 且不刷新或提示', async ()=>{
    const pending = deferred(), started = deferred();
    let calls = 0;
    const rt = createRuntime({api:()=>{
      if(++calls === 1) return {state:'idle', candidates:[]};
      started.resolve();
      return pending.promise;
    }});
    await rt.fn('wikiImportOpen')();
    rt.element('wikiVaultPath').value = '/manual/markdown';
    const running = rt.state.dialogs[0].onConfirm();
    await started.promise;
    switchToStudyPal(rt);
    if(outcome === 'reject') pending.reject(new Error('工作区已切换'));
    else pending.resolve({state:outcome, error:'旧错误', result:{nodes_created:3}});
    assert.equal(await running, false);
    assert.equal(calls, 2);
    assertStudyPalUnchanged(rt);
  });
}

test('轮询间隔切换工作区后不再发请求', async ()=>{
  const sleeping = deferred();
  let resume, calls = 0;
  const rt = createRuntime({
    setTimeout(fn){ resume = fn; sleeping.resolve(); },
    api:()=>{ calls += 1; return {state:'running', total:3, done:1}; }
  });
  await rt.fn('wikiImportOpen')();
  rt.element('wikiVaultPath').value = '/manual/markdown';
  const running = rt.state.dialogs[0].onConfirm();
  await sleeping.promise;
  switchToStudyPal(rt);
  resume();
  assert.equal(await running, false);
  assert.equal(calls, 2, '只有打开查询与首次轮询');
  assertStudyPalUnchanged(rt);
});

test('切走再切回 WorkPal 仍忽略旧一代候选响应', async ()=>{
  const pending = deferred();
  const rt = createRuntime({api:()=>pending.promise});
  const opening = rt.fn('wikiImportOpen')();
  rt.context.S.workspaceEpoch += 2;
  pending.resolve({state:'idle', candidates:[]});
  await opening;
  assert.equal(rt.state.dialogs.length, 0);
});

test('旧对话框确认回调不能在 StudyPal 提交或覆盖路径记录', async ()=>{
  const rt = createRuntime({saved:'/saved/wiki'});
  await rt.fn('wikiImportOpen')();
  rt.element('wikiVaultPath').value = '/stale/wiki';
  switchToStudyPal(rt);
  assert.equal(await rt.state.dialogs[0].onConfirm(), false);
  assert.equal(rt.state.apiPosts.length, 0);
  assert.equal(rt.state.storage.get(STORAGE_KEY), '/saved/wiki');
  assertStudyPalUnchanged(rt);
});

test('完成后的查看图谱回调在切换工作区后失效', async ()=>{
  const rt = createRuntime({api:apiPath=>apiPath === '/api/courses' ? [{name:'主题'}] : {state:'done', result:{nodes_created:1}}});
  await rt.fn('wikiImportOpen')();
  rt.element('wikiVaultPath').value = '/manual/markdown';
  assert.equal(await rt.state.dialogs[0].onConfirm(), false);
  rt.state.toasts.length = 0;
  switchToStudyPal(rt);
  assert.equal(await rt.state.dialogs[0].onConfirm(), false);
  assertStudyPalUnchanged(rt);
});
