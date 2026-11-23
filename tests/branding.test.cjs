const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');
const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'agents', 'laoba', 'manifest.json'), 'utf8'));
const prompt = fs.readFileSync(path.join(__dirname, '..', 'agents', 'laoba', 'system.md'), 'utf8');

test('the default assistant uses the generic MinDock display name', () => {
  assert.match(html, /小思 Sense/);
  assert.doesNotMatch(html, /老巴助教|思泊助教/);
  assert.equal(manifest.name, '小思 Sense');
  assert.match(prompt, /小思 Sense/);
  assert.doesNotMatch(prompt, /老巴|老八/);
  assert.match(html, /const currentRole = ROLES\.find\(role => role\.key === \(S\.role && S\.role\.key\)\)/);
  assert.match(html, /S\.role = \{\.\.\.S\.role, \.\.\.currentRole\}/);
});
