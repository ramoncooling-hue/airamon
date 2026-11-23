const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

test('home exposes the personal agent attention surfaces', () => {
  assert.match(html, /id=["']todayPanel["']/);
  assert.match(html, /id=["']attentionPanel["']/);
  assert.match(html, /id=["']continuePanel["']/);
});

test('home exposes stable action confirmation hooks', () => {
  assert.match(html, /data-action-state/);
  assert.match(html, /data-action-approve/);
  assert.match(html, /data-action-reject/);
  assert.match(html, /data-agent-course/);
  assert.match(html, /agentCandidatePreview/);
});

test('workspace exposes layered memory controls and graph exposes bounded traversal', () => {
  assert.match(html, /id="memoryLayerPanel"/);
  assert.match(html, /id="memoryBlockForm"/);
  assert.match(script, /api\('\/api\/memory\/blocks'/);
  assert.match(script, /data-memory-archive/);
  assert.match(script, /function renderGraphTraversal\(/);
  assert.match(script, /\/api\/kp\/\$\{id\}\/neighbors/);
  assert.match(script, /graphDepthSelect/);
});

test('home reports failed local actions and can deep-link to a waiting workflow run', () => {
  assert.match(html, /result\.status==='failed'/);
  assert.match(html, /data-agent-workflow-run/);
  assert.match(html, /openWorkflowRun\(/);
});
