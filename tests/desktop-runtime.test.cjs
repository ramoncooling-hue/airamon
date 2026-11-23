'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const {Runtime} = require('../desktop/windows/runtime.cjs');

test('real Runtime stops backend and OCR gracefully, inspects online and restores offline', {timeout:45000}, async () => {
  const project = path.resolve(__dirname, '..');
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'mindock-runtime-test-'));
  const root = path.join(temporary, 'data');
  const python = process.env.MINDOCK_TEST_PYTHON || (process.platform === 'win32'
    ? path.join(project, '.venv', 'Scripts', 'python.exe') : path.join(project, '.venv', 'bin', 'python'));
  const ocr = path.join(temporary, 'ocr.py');
  await fs.writeFile(ocr, 'import sys,time\nprint(\'{"event":"progress","page":1,"total":4}\',file=sys.stderr,flush=True)\ntime.sleep(60)\n');
  const runtime = new Runtime({root, resources:project, python,
    script:path.join(project, 'desktop', 'backend.py'), ocr});
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  const request = async (url, options={}) => {
    const result = await fetch(runtime.base+url, {...options,
      headers:{'X-MinDock-Session':runtime.token,...options.headers}});
    assert.equal(result.status, 200, await result.clone().text());
    return result.json();
  };
  const checkStopped = async child => {
    assert.equal(child.exitCode, 0);
    assert.equal(child.signalCode, null, 'file signal must exit without force killing');
    await assert.rejects(fs.stat(runtime.sessionFile), {code:'ENOENT'});
    await assert.rejects(fs.stat(runtime.shutdownFile), {code:'ENOENT'});
  };
  try {
    await runtime.start();
    const courses = await request('/api/courses');
    const course = encodeURIComponent(courses[0].name);
    const task = await request('/api/intake/upload?course='+course+'&filename=test.png',
      {method:'POST',body:'isolated test image'});
    let state;
    const deadline = Date.now()+5000;
    do {
      state = await request('/api/ingest/jobs/'+task.task_id);
      if (state.pages_done === 1) break;
      await pause(40);
    } while (Date.now()<deadline);
    assert.equal(state.pages_done, 1, JSON.stringify(state));
    const first = runtime.child;
    await runtime.stop();
    await checkStopped(first);

    const archive = path.join(temporary, 'backup.zip');
    assert.equal((await runtime.maintenance('backup', archive)).ok, true);
    await runtime.start();
    const resumed = await request('/api/ingest/jobs/'+task.task_id);
    assert.equal(resumed.status, 'interrupted');
    const summary = await runtime.maintenance('inspect', archive);
    assert.ok(summary.sha256);
    await request('/api/health');
    const second = runtime.child;
    await runtime.stop();
    await checkStopped(second);
    assert.equal((await runtime.maintenance('restore', archive, summary.sha256)).ok, true);
    await runtime.start();
    assert.equal((await request('/api/ingest/jobs/'+task.task_id)).status, 'interrupted');
    const third = runtime.child;
    await runtime.stop();
    await checkStopped(third);
  } finally {
    await runtime.dispose();
    await fs.rm(temporary, {recursive:true,force:true});
  }
});
