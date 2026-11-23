const {test} = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'web', 'index.html'), 'utf8');
const VENDOR = path.join(ROOT, 'web', 'assets', 'vendor');

function inlineAppScript(){
  const match = html.match(/<script>([\s\S]*?)<\/script>/);
  assert.ok(match, 'inline application script exists');
  return match[1];
}

/** Slice esc()/mdToHtml() out of the app script and run them with the real
 *  markdown-it bundle plus a whitelist-enforcing DOMPurify stand-in (Node has
 *  no DOM, so the real sanitizer bundle cannot execute here). */
function mdRuntime({withPurify = true} = {}){
  const script = inlineAppScript();
  const escCode = script.slice(script.indexOf('function esc(s){'), script.indexOf('function toast(msg){'));
  const mdCode = script.slice(script.indexOf('const MD_ALLOWED_TAGS'), script.indexOf('\nfunction stripHtml('));
  assert.ok(escCode.includes('function esc'), 'esc slice found');
  assert.ok(mdCode.includes('function mdToHtml'), 'mdToHtml slice found');
  const context = vm.createContext({setTimeout, clearTimeout});
  vm.runInContext('window = globalThis; self = globalThis;', context);
  vm.runInContext(fs.readFileSync(path.join(VENDOR, 'markdown-it.umd.min.js'), 'utf8'), context, {filename:'markdown-it.umd.min.js'});
  if(withPurify){
    vm.runInContext([
      'window.__purifyCalls = [];',
      'window.DOMPurify = {sanitize(value, config){ window.__purifyCalls.push(config);',
      "  return String(value)",
      "    .replace(/<script[\\s\\S]*?<\\/script>/gi, '')",
      "    .replace(/\\son[a-z]+\\s*=\\s*(\"[^\"]*\"|'[^']*'|[^\\s>]+)/gi, '');",
      '}};'
    ].join('\n'), context);
  }
  vm.runInContext(escCode + '\n' + mdCode + '\n' +
    'window.__mdToHtml = mdToHtml; window.__safeLegacyHtml = safeLegacyHtml; window.__mdHardened = mdHardened;',
    context, {filename:'inline-app-slice.js'});
  return context;
}

test('vendor bundles are pinned, hash-locked and loaded before the app script', () => {
  const readme = fs.readFileSync(path.join(VENDOR, 'README.md'), 'utf8');
  const pins = [
    ['markdown-it.umd.min.js', '391f2aa638e75e6c7859d0a5c5650ef0956722a45998f17e05b83bc655778e1e'],
    ['purify.min.js', 'cd37898f9782adf54820c413191b5691803f0f137c799d6edcfd38523b4c2c31'],
  ];
  for(const [file, expected] of pins){
    const digest = crypto.createHash('sha256').update(fs.readFileSync(path.join(VENDOR, file))).digest('hex');
    assert.equal(digest, expected, file + ' sha256 drifted from README');
    assert.ok(readme.includes(expected), file + ' hash listed in vendor README');
  }
  assert.match(readme, /15\.0\.3/);
  assert.match(readme, /3\.4\.17/);
  assert.ok(!/\bGPL\b/.test(readme.replace(/禁止 GPL\/AGPL 依赖/g, '')), 'no GPL dependency documented');
  const appScript = html.indexOf('<script>');
  const markdownIt = html.indexOf('assets/vendor/markdown-it.umd.min.js');
  const purify = html.indexOf('assets/vendor/purify.min.js');
  assert.ok(markdownIt > 0 && markdownIt < appScript, 'markdown-it loads before inline app script');
  assert.ok(purify > 0 && purify < appScript, 'DOMPurify loads before inline app script');
});

test('markdown answers cannot execute raw HTML or auto-load remote images', () => {
  const context = mdRuntime();
  const injected = context.__mdToHtml(
    '你好 <img src=x onerror="alert(1)"> <script>alert(2)</script> <a href="javascript:alert(3)">点我</a> 完');
  assert.ok(!/<script/i.test(injected), 'script tag not emitted: ' + injected);
  assert.ok(!/onerror\s*=/i.test(injected), 'event handler not emitted');
  assert.ok(!/<img/i.test(injected), 'raw img tag not emitted');
  assert.ok(!/href="javascript:/i.test(injected), 'javascript: href not emitted');
  assert.ok(injected.includes('&lt;script&gt;'), 'raw markup shown as escaped text');
});

test('remote markdown images degrade to text links; local images stay local', () => {
  const context = mdRuntime();
  const remote = context.__mdToHtml('![猫咪](https://tracker.example/pixel.png?uid=42)');
  assert.ok(!/<img/i.test(remote), 'remote image must not render as <img>');
  assert.match(remote, /class="md-image-link"/);
  assert.match(remote, /href="https:\/\/tracker\.example\/pixel\.png\?uid=42"/);
  assert.match(remote, />猫咪</);
  const local = context.__mdToHtml('![图](/assets/mindock-mark-64.png)');
  assert.match(local, /<img class="md-image" loading="lazy" alt="图" src="\/assets\/mindock-mark-64\.png">/);
  const dataUri = context.__mdToHtml('![x](data:image/png;base64,AAAA)');
  assert.ok(!/<img/i.test(dataUri), 'inline data image rejected');
});

test('links keep rel=noopener and unsafe protocols are dropped', () => {
  const context = mdRuntime();
  const link = context.__mdToHtml('[官方](https://example.com/a?b=1&c=2)');
  assert.match(link, /<a href="https:\/\/example\.com\/a\?b=1&amp;c=2" target="_blank" rel="noopener noreferrer">官方<\/a>/);
  const js = context.__mdToHtml('[点我](javascript:alert(1))');
  assert.ok(!/href="javascript:/i.test(js));
});

test('evidence citations stay interactive buttons and manual refs stay safe links', () => {
  const context = mdRuntime();
  const refs = [{label:'网页1', kind:'kp', id:7}, {citation:'手册1', kind:'manual', manual_url:'/api/manual/open?source=manual%2Fa.md'}];
  const out = context.__mdToHtml('先看 [网页1]，再看 [手册1]，最后 [网页9]。', refs, 3);
  assert.match(out, /<button type="button" class="inline-cite" data-ref="网页1" data-msg="3">网页1<\/button>/);
  assert.match(out, /<a class="inline-cite manual-link" href="\/api\/manual\/open\?source=manual%2Fa\.md" target="_blank" rel="noopener noreferrer">手册1<\/a>/);
  assert.match(out, /<span class="inline-ref">网页9<\/span>/);
  const code = context.__mdToHtml('代码里的 `[网页1]` 不该变成按钮');
  assert.ok(!code.includes('inline-cite'), 'citation inside code span stays literal');
});

test('DOMPurify is called with the explicit whitelist (tags + data refs only)', () => {
  const context = mdRuntime();
  context.__mdToHtml('一段 **回答**，带 [网页1]。', [{label:'网页1'}]);
  const calls = context.__purifyCalls;
  assert.ok(calls.length >= 1, 'sanitizer invoked');
  const config = calls[calls.length - 1];
  for(const tag of ['p', 'a', 'img', 'button', 'span', 'code', 'pre', 'ul', 'ol', 'li']) assert.ok(config.ALLOWED_TAGS.includes(tag), tag + ' allowed');
  for(const attr of ['href', 'src', 'class', 'data-ref', 'data-msg', 'target', 'rel']) assert.ok(config.ALLOWED_ATTR.includes(attr), attr + ' allowed');
  assert.equal(config.ALLOW_DATA_ATTR, false, 'arbitrary data-* attributes rejected');
  assert.ok(!config.ALLOWED_TAGS.includes('script'));
  assert.ok(!config.ALLOWED_ATTR.includes('onerror'));
});

test('fallback path sanitizes when the DOMPurify bundle is unavailable', () => {
  const context = mdRuntime({withPurify:false});
  const out = context.__mdToHtml('![x](https://evil.example/x.png) <img src=x onerror=alert(1)>');
  assert.ok(!/<script/i.test(out));
  assert.ok(!/onerror\s*=/i.test(out));
  const hardened = context.__mdHardened('<img src=x onerror="alert(1)"><script>alert(2)</script><a href="javascript:alert(3)">x</a>');
  assert.ok(!/onerror/i.test(hardened));
  assert.ok(!/<script/i.test(hardened));
  assert.ok(!/javascript:/i.test(hardened));
});

test('legacy conversations are sanitized instead of inserted raw', () => {
  const context = mdRuntime();
  const legacy = context.__safeLegacyHtml('<p>旧回答</p><img src=x onerror="alert(1)"><script>alert(2)</script>');
  assert.ok(!/<script/i.test(legacy), 'script stripped: ' + legacy);
  assert.ok(!/onerror/i.test(legacy), 'handlers stripped');
  assert.match(html, /: safeLegacyHtml\(m\.content\)/, 'openConv uses sanitizer for legacy content');
  assert.match(html, /esc\(S\.role\.av\)/);
  assert.match(html, /esc\(r\.av\)/);
});
