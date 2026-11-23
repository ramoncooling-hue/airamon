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
const PRIVACY_SLICE = html.slice(
  html.indexOf('const PRIVACY_LOG_LIMIT = 10;'),
  html.indexOf('\nfunction renderSettings(){')
);
assert.ok(API_SLICE.includes('async function apiPost'), 'api helpers found');
assert.ok(PRIVACY_SLICE.includes('async function loadPrivacyDiagnostics'), 'privacy module found');

const SAMPLE = {
  app:{version:'0.6.0', platform:'Darwin'},
  format_version:1,
  generated_at:'2026-10-17T13:00:00+00:00',
  data_root:{basename:'data', custom:false, exists:true, database_present:true, database_size_bytes:2048},
  schema:{user_version:26, migrations:26, latest:['024','025','026'], compatible:true},
  capabilities:{sqlite_version:'3.45.0', fts5:true, trigram:true, search_fts_table:true, search_fts_readable:true},
  backups:{directory_present:true, count:2, latest_at:'2026-10-17T12:00:00+00:00', items:[
    {name:'backup-20261017-1200.zip', modified_at:'2026-10-17T12:00:00+00:00', size_bytes:1048576}
  ]},
  logs:{present:true, size_bytes:4096, modified_at:'2026-10-17T12:30:00+00:00', truncated:false,
    levels:{ERROR:2, WARNING:3}, entries:[
      {time:'2026-10-17 10:00:00,000', level:'ERROR', logger:'mindock.api', message:'failed to save memory'},
      {time:'2026-10-17 10:05:00,000', level:'WARNING', logger:'mindock.llm', message:'slow upstream'}
    ]},
  workspaces:{count:2, items:[
    {workspace:'studypal', kp:10, documents:5, notes:2},
    {workspace:'workpal', kp:0, documents:1, notes:null}
  ]},
  recovery:{backup_available:true, backup_directory_present:true, browser_backup_supported:false,
    hints:['最近一次备份可在桌面壳「备份与恢复」中还原']}
};

function escapeHtml(value){
  return String(value == null ? '' : value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
}
function fakeElement(id){
  return {id, hidden:false, innerHTML:'', textContent:'', value:'', checked:false, disabled:false,
    dataset:{}, onclick:null, classList:{add(){}, remove(){}, toggle(){}},
    addEventListener(){}, querySelectorAll(){return [];}, querySelector(){return null;}, closest(){return null;},
    appendChild(){}, removeChild(){}, focus(){}};
}
function privacyRuntime(fetchImpl = () => ({status:200, json:SAMPLE})){
  const calls = [];
  const toasts = [];
  const notified = [];
  const elements = new Map();
  const element = id => { if(!elements.has(id)) elements.set(id, fakeElement(id)); return elements.get(id); };
  const context = vm.createContext({
    S:{workspace:'studypal'}, API:'', URL:URL, location:{href:'http://127.0.0.1/'},
    esc:escapeHtml,
    document:{getElementById:id => element(id)},
    el:id => element(id),
    fetch:async(url, options) => {
      const entry = {url:String(url), method:(options && options.method) || 'GET'};
      calls.push(entry);
      const spec = fetchImpl(entry) || {};
      const status = spec.status || 200;
      return {ok:status >= 200 && status < 300, status, json:async() => spec.json === undefined ? {} : spec.json};
    },
    toast:text => toasts.push(String(text)),
    notifyError:(kind, message) => notified.push({kind, message:String(message)})
  });
  vm.runInContext(API_SLICE + '\n' + PRIVACY_SLICE, context);
  return {context, calls, toasts, notified, element};
}

test('settings page exposes the data and privacy panel with export entry', () => {
  assert.match(html, /id="privacyPanel"/);
  assert.match(html, /id="privacyPanelBody"/);
  assert.match(html, /id="privacyExportBtn"/);
  assert.match(html, /id="privacyRefreshBtn"/);
  assert.match(html, /if\(typeof renderPrivacyPanel === 'function'\) renderPrivacyPanel\(\);/);
  assert.match(html, /\/api\/diagnostics/);
  assert.match(html, /\/api\/diagnostics\/export/);
  // 知识入口在 WorkPal 白名单里（task-40 第一部分）
  assert.match(html, /\['workspace','chat','knowledge','learning','graph','calendar','settings'\]/);
});

test('path, byte and time helpers never expose absolute paths', () => {
  const rt = privacyRuntime();
  assert.equal(rt.context.privacyBasename('/Users/me/Library/App/data'), 'data');
  assert.equal(rt.context.privacyBasename('C:\\Users\\me\\AppData\\data\\'), 'data');
  assert.equal(rt.context.privacyBasename('data'), 'data');
  assert.equal(rt.context.privacyBytes(2048), '2.0 KB');
  assert.equal(rt.context.privacyBytes(1048576), '1.0 MB');
  assert.equal(rt.context.privacyBytes(null), '—');
  assert.match(rt.context.privacyTime('2026-10-17T13:00:00+00:00'), /^2026-10-17 13:00:00/);
});

test('privacy summary renders schema, capabilities, backups, logs and workspace counts', () => {
  const rt = privacyRuntime();
  const markup = rt.context.privacySummaryHtml(SAMPLE, null, '');
  assert.match(markup, /数据根/);
  assert.match(markup, /<code>data<\/code>/);
  assert.match(markup, /schema 兼容/);
  assert.match(markup, /26/);
  assert.match(markup, /3\.45\.0/);
  assert.match(markup, /FTS5 可用/);
  assert.match(markup, /trigram 可用/);
  assert.match(markup, /backup-20261017-1200\.zip/);
  assert.match(markup, /1\.0 MB/);
  assert.match(markup, /ERROR 2/);
  assert.match(markup, /WARNING 3/);
  assert.match(markup, /failed to save memory/);
  assert.match(markup, /studypal/);
  assert.match(markup, /知识点 10/);
  assert.match(markup, /知识页 5/);
  assert.match(markup, /workpal/);
  assert.match(markup, /桌面壳「备份与恢复」/);
  assert.match(markup, /还没有导出诊断包/);
});

test('privacy summary escapes untrusted values and caps log entries at ten', () => {
  const rt = privacyRuntime();
  const payload = JSON.parse(JSON.stringify(SAMPLE));
  payload.data_root.basename = '/Users/secret/<img src=x onerror=alert(1)>';
  payload.backups.items[0].name = '<script>alert(9)</script>.zip';
  payload.logs.entries = Array.from({length:12}, (_, index) => ({time:'2026-10-17 10:00:00,000', level:'ERROR', logger:'x', message:'<script>alert(' + index + ')</script>'}));
  payload.workspaces.items[0].workspace = '<b>studypal</b>';
  const markup = rt.context.privacySummaryHtml(payload, {filename:'diagnostics-1.zip', size_bytes:2048, created_at:'2026-10-17T13:00:00+00:00', contents:['diagnostics.json']}, '');
  assert.ok(!/<script/i.test(markup), 'log messages are escaped');
  assert.ok(!/<img/i.test(markup), 'basename is escaped');
  assert.ok(!/<b>studypal/.test(markup), 'workspace names are escaped');
  assert.match(markup, /&lt;script&gt;/);
  assert.ok(!markup.includes('/Users/secret'), 'absolute path never rendered');
  assert.equal((markup.match(/&lt;script&gt;alert\(/g) || []).length, 10, 'only ten log entries are shown');
  assert.match(markup, /diagnostics-1\.zip/);
});

test('export result shows filename and size but never a path', () => {
  const rt = privacyRuntime();
  const markup = rt.context.privacyExportHtml({ok:true, filename:'/Users/me/private/diagnostics-20261017.zip', size_bytes:2048,
    created_at:'2026-10-17T13:00:00+00:00', contents:['diagnostics.json','logs-summary.json']});
  assert.match(markup, /diagnostics-20261017\.zip/);
  assert.match(markup, /2\.0 KB/);
  assert.match(markup, /logs-summary\.json/);
  assert.match(markup, /已脱敏/);
  assert.ok(!markup.includes('/Users/me'), 'export filename is reduced to basename');
});

test('panel loads diagnostics through the mocked endpoint and renders it', async()=>{
  const rt = privacyRuntime(() => ({status:200, json:SAMPLE}));
  rt.context.renderPrivacyPanel();
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(rt.calls.length, 1);
  assert.ok(rt.calls[0].url.includes('/api/diagnostics'));
  assert.match(rt.element('privacyPanelBody').innerHTML, /数据根/);
  assert.match(rt.element('privacyPanelBody').innerHTML, /schema 兼容/);
  assert.equal(vm.runInContext('privacyState.loaded', rt.context), true);
});

test('missing diagnostics endpoint degrades with a retry instead of crashing', async()=>{
  const rt = privacyRuntime(() => ({status:404, json:{detail:'Not Found'}}));
  rt.context.renderPrivacyPanel();
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  assert.match(rt.element('privacyPanelBody').innerHTML, /诊断接口尚未就绪/);
  assert.match(rt.element('privacyPanelBody').innerHTML, /data-privacy-retry="1"/);
});

test('export button posts to the export endpoint and renders filename and size', async()=>{
  const rt = privacyRuntime(entry => entry.method === 'POST'
    ? {status:200, json:{ok:true, filename:'diagnostics-20261017-abc12345.zip', size_bytes:2048, created_at:'2026-10-17T13:00:00+00:00', contents:['diagnostics.json','logs-summary.json']}}
    : {status:200, json:SAMPLE});
  rt.context.renderPrivacyPanel();
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  await rt.context.exportPrivacyDiagnostics();
  const post = rt.calls.find(item => item.method === 'POST');
  assert.equal(post.url, '/api/diagnostics/export');
  assert.match(rt.element('privacyPanelBody').innerHTML, /diagnostics-20261017-abc12345\.zip/);
  assert.match(rt.element('privacyPanelBody').innerHTML, /2\.0 KB/);
  assert.ok(rt.toasts.some(text => text.includes('诊断包已生成')));
});

test('export failure is shown inline and does not break the panel', async()=>{
  const rt = privacyRuntime(entry => entry.method === 'POST'
    ? {status:500, json:{detail:'诊断数据超出大小限制'}}
    : {status:200, json:SAMPLE});
  rt.context.renderPrivacyPanel();
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  await rt.context.exportPrivacyDiagnostics();
  assert.match(vm.runInContext('privacyState.exportError', rt.context), /导出失败/);
  assert.match(rt.element('privacyPanelBody').innerHTML, /privacy-error/);
  assert.equal(rt.notified.length, 1);
});
