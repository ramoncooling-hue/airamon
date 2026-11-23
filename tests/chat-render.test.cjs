const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');

test('chat renderer turns citations, links, and image markdown into usable UI', () => {
  assert.match(html, /function mdToHtml\(md, refs/);
  assert.match(html, /inline-cite/);
  assert.ok(html.includes('https?:\\/\\/'));
  assert.match(html, /<img/);
  assert.match(html, /r\.manual_url\|\|'#'/);
  assert.match(html, /kind:'manual', source:h\.source, url:h\.manual_url/);
  assert.match(html, /class="inline-cite manual-link"/);
});

test('global title and navigation remain fixed while page content scrolls', () => {
  assert.match(html, /\.tophead\{[^}]*position:sticky/);
  assert.match(html, /\.askbar\{[^}]*top:64px/);
});
