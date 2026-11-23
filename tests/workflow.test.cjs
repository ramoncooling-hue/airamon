const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

test('workflow page is a real workflow center with stable page id', () => {
  assert.match(html, /id="p-orchestra"/);
  assert.match(html, /工作流中心/);
  assert.match(html, /id="workflowList"/);
  assert.match(html, /id="workflowSteps"/);
  assert.match(html, /id="workflowStepEditor"/);
  assert.match(html, /id="workflowRuns"/);
  assert.match(html, /id="workflowQuestion"/);
  assert.match(html, /高级运行输入（一般不需要修改）/);
});

test('workflow templates and live dependencies are wired to the API', () => {
  assert.match(html, /paper_research/);
  assert.match(html, /material_intake/);
  assert.match(html, /api\('\/api\/workflows'/);
  assert.match(html, /\/api\/workflows\/\$\{encodeURIComponent\(wf\.key\)\}\/steps/);
  assert.match(html, /S\._hub\?\.agents/);
  assert.match(html, /S\._hub\?\.skills/);
});

test('workflow editor supports save, reorder, copy, delete, run, approval and retry', () => {
  for (const token of [
    'workflowStepSave', 'workflowStepUp', 'workflowStepDown', 'workflowStepCopy', 'workflowStepDelete',
    'workflowRun', 'workflowTestRun', 'data-run-approve', 'data-run-reject', 'data-run-retry',
    'workflowDecision', 'workflowAction'
  ]) assert.match(html, new RegExp(token));
  assert.match(script, /function workflowMoveStep/);
  assert.match(script, /function createWorkflow/);
  assert.match(script, /agent_key:\['agent','skill'\]\.includes\(kind\)/);
  assert.match(script, /skill_key:kind==='skill'/);
});

test('workflow page constrains step overflow instead of causing body horizontal scroll', () => {
  assert.match(html, /\.workflow-stepbar\{[^}]*overflow-x:auto/);
  assert.match(html, /\.workflow-step\{[^}]*flex:0 0 auto/);
});

test('workflow JSON mapping remains available but is collapsed by default', () => {
  assert.match(html, /<details class="workflow-advanced-mapping">/);
  assert.match(html, /高级数据映射（一般不需要修改）/);
  assert.match(html, /id="workflowStepInputMap"/);
  assert.match(html, /id="workflowStepOutputSchema"/);
  assert.doesNotMatch(html, /<details[^>]*open[^>]*workflow-advanced-mapping/);
  assert.match(script, /const question=el\('workflowQuestion'\)\?\.value\.trim\(\)/);
  assert.match(script, /input_data=\{question\}/);
});
