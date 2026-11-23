'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const {dataRoot,sameApp,externalUrl,backendEnvironment,portFromDescriptor}=require('../policy.cjs');

test('Windows data stays in the user local data directory',()=>{
  assert.equal(dataRoot({LOCALAPPDATA:'C:\\Users\\Ada\\AppData\\Local'},'win32'),'C:\\Users\\Ada\\AppData\\Local\\MinDock');
});
test('navigation only shares the backend origin; external links reject privileged protocols',()=>{
  const base='http://127.0.0.1:50001';
  assert.ok(sameApp(base+'/api/manual/open',base));
  assert.ok(!sameApp('http://127.0.0.1:50002',base));
  for(const url of ['file:///C:/data','javascript:alert(1)','https://user:pass@example.com','http://localhost:8899']) assert.ok(!externalUrl(url));
  assert.ok(externalUrl('https://example.com/course'));
});
test('child environment strips host Python and runtime ownership flags',()=>{
  const env=backendEnvironment({root:'data',sessionFile:'ready',shutdownFile:'stop',token:'test-token',parentPid:10,python:'python.exe',ocr:'ocr.py'},
    {PYTHONHOME:'host',PYTHONPATH:'host',VIRTUAL_ENV:'host',MINDOCK_RUNTIME_LOCKED:'1',MINDOCK_MAINTENANCE_OWNER_PID:'wrong'});
  assert.equal(env.CYBERPAL_DATA_DIR,'data');
  assert.equal(env.MINDOCK_PARENT_PID,'10');
  assert.equal(env.MINDOCK_OCR_PYTHON,'python.exe');
  assert.equal(env.PYTHONHOME,undefined);
  assert.equal(env.MINDOCK_RUNTIME_LOCKED,undefined);
});
test('descriptor rejects invalid or development ports',()=>{
  assert.equal(portFromDescriptor('{"port":51463}'),51463);
  for(const port of [80,8899,65536,'12345']) assert.throws(()=>portFromDescriptor(JSON.stringify({port})));
});
