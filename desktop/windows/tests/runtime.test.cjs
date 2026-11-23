'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const os=require('node:os');
const path=require('node:path');
const {Runtime,healthy}=require('../runtime.cjs');

test('desktop owns backend startup, private health session and graceful stop',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'mindock-shell-test-'));
  const script=path.join(root,'backend.cjs');
  await fs.writeFile(script,`
    const http=require('node:http'),fs=require('node:fs');
    const server=http.createServer((req,res)=>{
      const authorized=req.headers['x-mindock-session']===process.env.MINDOCK_SESSION_TOKEN;
      res.statusCode=authorized?200:403;
      res.end(authorized?JSON.stringify({nonce:process.env.MINDOCK_READY_NONCE}):'{}');
    });
    server.listen(0,'127.0.0.1',()=>fs.writeFileSync(process.env.MINDOCK_SESSION_FILE,JSON.stringify({port:server.address().port})));
    const timer=setInterval(()=>{if(fs.existsSync(process.env.MINDOCK_SHUTDOWN_FILE)){clearInterval(timer);server.close(()=>process.exit(0));}},20);
  `);
  const runtime=new Runtime({root,resources:root,python:process.execPath,script,ocr:script,platform:process.platform});
  try{
    const base=await runtime.start();
    assert.match(base,/^http:\/\/127\.0\.0\.1:\d+$/);
    const child=runtime.child;
    await runtime.stop();
    assert.equal(child.exitCode,0);
    assert.equal(runtime.child,null);
    await runtime.start();
    await runtime.dispose();
    await assert.rejects(fs.stat(runtime.runDir),{code:'ENOENT'});
  }finally{
    await runtime.dispose();
    await fs.rm(root,{recursive:true,force:true});
  }
});

test('a foreign local service on the recovered port is rejected by the ready nonce',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'mindock-shell-nonce-'));
  const script=path.join(root,'impostor.cjs');
  await fs.writeFile(script,`
    const http=require('node:http'),fs=require('node:fs');
    const server=http.createServer((req,res)=>{
      res.statusCode=200;
      res.end(JSON.stringify({nonce:'not-the-launch-nonce'}));
    });
    server.listen(0,'127.0.0.1',()=>fs.writeFileSync(process.env.MINDOCK_SESSION_FILE,JSON.stringify({port:server.address().port})));
    const timer=setInterval(()=>{if(fs.existsSync(process.env.MINDOCK_SHUTDOWN_FILE)){clearInterval(timer);server.close(()=>process.exit(0));}},20);
  `);
  const runtime=new Runtime({root,resources:root,python:process.execPath,script,ocr:script,platform:process.platform});
  try{
    await assert.rejects(runtime.start(),/ready nonce mismatch/);
    assert.equal(runtime.base,'');
  }finally{
    await runtime.dispose();
    await fs.rm(root,{recursive:true,force:true});
  }
});

test('healthy() compares the ready nonce but keeps the status-only contract',async()=>{
  const http=require('node:http');
  const server=http.createServer((req,res)=>{
    res.statusCode=req.headers['x-mindock-session']==='session-token'?200:403;
    res.end(JSON.stringify({nonce:'launch-nonce'}));
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const base='http://127.0.0.1:'+server.address().port;
  try{
    assert.equal(await healthy(base,'session-token','launch-nonce'),true);
    assert.equal(await healthy(base,'session-token','other-nonce'),false);
    assert.equal(await healthy(base,'session-token'),true);
    assert.equal(await healthy(base,'wrong-token','launch-nonce'),false);
  }finally{await new Promise(resolve=>server.close(resolve));}
});

test('maintenance cannot write while backend is running and startup error is surfaced',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'mindock-shell-failure-'));
  const runtime=new Runtime({root,resources:root,python:path.join(root,'missing.exe'),script:'missing',ocr:'missing'});
  try{
    runtime.child={};
    await assert.rejects(runtime.maintenance('restore','backup.zip'),/Stop backend/);
    runtime.child=null;
    await assert.rejects(runtime.start(),/ENOENT/);
  }finally{await runtime.dispose();await fs.rm(root,{recursive:true,force:true});}
});
