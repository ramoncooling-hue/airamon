const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');

test('desktop shell exposes a fixed primary rail, context sidebar, and scrollable main region', () => {
  assert.match(html, /class="app-shell"/);
  assert.match(html, /id="primarySidebar"/);
  assert.match(html, /id="primaryNav"/);
  assert.match(html, /id="contextSidebar"/);
  assert.match(html, /id="contextNav"/);
  assert.match(html, /id="appMain"/);
  assert.match(html, /id="pageScroll"/);
  assert.match(html, /\.app-shell\{[^}]*grid-template-columns:72px 248px minmax\(0,1fr\)/);
  assert.match(html, /\.primary-sidebar\{[^}]*position:sticky/);
  assert.match(html, /\.context-sidebar\{[^}]*position:sticky/);
  assert.match(html, /\.page-scroll\{[^}]*overflow-y:auto/);
});

test('primary navigation is rendered as icon buttons and context navigation follows the active page', () => {
  assert.match(html, /function renderPrimaryNav\(\)/);
  assert.match(html, /function renderContextSidebar\(\)/);
  assert.match(html, /data-primary-page="\$\{n\.id\}"/);
  assert.match(html, /contextNav\.innerHTML/);
  assert.match(html, /renderContextSidebar\(\);/);
  assert.match(html, /aria-label="\$\{esc\(n\.name\)\}"/);
});

test('desktop shell does not use the old top navigation as the primary route control', () => {
  assert.match(html, /\.topnav\{[^}]*display:none/);
  assert.match(html, /id="nav"[^>]*aria-hidden="true"/);
});
