const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');

test('mastery page exposes per-knowledge-point status editing', () => {
  assert.match(html, /id="masteryItemsBox"/);
  assert.match(html, /逐项调整/);
  assert.match(html, /data-mastery-id/);
  assert.match(html, /apiPost\(`\/api\/kp\/\$\{id\}\/mastery`/);
});

test('graph detail exposes the same mastery status editor', () => {
  assert.match(html, /id="graphMasterySelect"/);
  assert.match(html, /graphMasterySelect/);
});

test('mastery page points pending source candidates back to course confirmation', () => {
  assert.match(html, /id="masteryFlowNotice"/);
  assert.match(html, /掌握度只统计已进入正式图谱的知识点/);
  assert.match(html, /api\('\/api\/sources'/);
  assert.match(html, /data-go-course/);
});
