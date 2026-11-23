'use strict';
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const crypto = require('node:crypto');
const {spawn} = require('node:child_process');
const {backendEnvironment, portFromDescriptor} = require('./policy.cjs');
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function untilExit(exited, ms) {
  let timer;
  try {await Promise.race([exited,new Promise(resolve=>{timer=setTimeout(resolve,ms);})]);}
  finally {clearTimeout(timer);}
}

// Reads the ready nonce echoed by /api/health so startup can prove the port
// belongs to the backend process this shell just spawned. A stale or foreign
// local service returning 200 must not be treated as ready.
function probeHealth(base, token, nonce) {
  return new Promise(resolve => {
    const req = http.get(base+'/api/health', {headers:{'X-MinDock-Session':token}, timeout:1000}, res => {
      const chunks=[];
      res.on('data', chunk => chunks.push(chunk));
      res.on('end', () => {
        let payload=null;
        try {payload=JSON.parse(Buffer.concat(chunks).toString('utf8'));} catch (_) {}
        resolve({status:res.statusCode, nonce:payload && typeof payload === 'object' ? payload.nonce : undefined});
      });
    });
    req.on('timeout', () => req.destroy());
    req.on('error', () => resolve({status:0, nonce:undefined}));
  });
}

function healthy(base, token, nonce) {
  return probeHealth(base, token, nonce).then(result => {
    if (result.status !== 200) return false;
    // Two-argument callers keep the historical status-only contract.
    if (nonce === undefined || nonce === null) return true;
    return result.nonce === nonce;
  });
}

class Runtime {
  constructor({root, resources, python, script, ocr, platform = process.platform}) {
    Object.assign(this, {root, resources, python, script, ocr, platform});
    this.child = null;
    this.maintenanceChild = null;
    this.base = '';
  }

  async initialize() {
    await fs.mkdir(path.join(this.root, 'app', 'logs'), {recursive:true, mode:0o700});
    this.runDir = await fs.mkdtemp(path.join(os.tmpdir(), 'mindock-session-'));
    this.sessionFile = path.join(this.runDir, 'session.json');
    this.shutdownFile = path.join(this.runDir, 'shutdown');
    this.token = crypto.randomBytes(32).toString('hex');
    this.readyNonce = crypto.randomBytes(16).toString('hex');
    this.env = backendEnvironment({root:this.root, sessionFile:this.sessionFile, shutdownFile:this.shutdownFile,
      token:this.token, parentPid:process.pid, python:this.python, ocr:this.ocr});
    this.env.MINDOCK_READY_NONCE = this.readyNonce;
  }

  async start() {
    if (this.child) throw new Error('Backend is already running');
    if (!this.runDir) await this.initialize();
    await fs.rm(this.sessionFile, {force:true});
    await fs.rm(this.shutdownFile, {force:true});
    this.failure=null;
    const log = await fs.open(path.join(this.root,'app','logs','windows-desktop.log'), 'a');
    const child = spawn(this.python, [this.script], {env:this.env, cwd:this.resources, windowsHide:true,
      stdio:['ignore',log.fd,log.fd]});
    this.child = child;
    const exited = new Promise(resolve => {
      child.once('error', err => {this.failure=err; resolve();});
      child.once('exit', () => resolve());
    });
    await log.close();
    const deadline = Date.now() + 90000;
    try {
      while (Date.now() < deadline) {
        if (this.failure || child.exitCode !== null || child.signalCode !== null) throw this.failure || new Error('Backend exited before startup');
        try {
          const port = portFromDescriptor(await fs.readFile(this.sessionFile,'utf8'));
          const base = 'http://127.0.0.1:'+port;
          const probe = await probeHealth(base,this.token,this.readyNonce);
          if (probe.status === 200) {
            if (probe.nonce !== this.readyNonce) throw new Error('Backend identity check failed: ready nonce mismatch');
            this.base=base; this.failure=null; return base;
          }
        } catch (err) {
          if (!(err.code === 'ENOENT' || err instanceof SyntaxError)) throw err;
        }
        await Promise.race([pause(150),exited]);
      }
      throw new Error('Backend startup timed out');
    } catch (err) {await this.stop(); throw err;}
  }

  async stop() {
    const child=this.child;
    if (!child) return;
    if (!child.pid) {this.child=null; return;}
    try {
      await fs.writeFile(this.shutdownFile,'stop', {mode:0o600});
      const exited = new Promise(resolve => child.once('exit',resolve));
      if (child.exitCode === null && child.signalCode === null) {
        await untilExit(exited,15000);
      }
      if (child.exitCode === null && child.signalCode === null) {
        if (this.platform === 'win32') {
          await new Promise(resolve => {
            const killer=spawn('taskkill.exe',['/PID',String(child.pid),'/T','/F'],{windowsHide:true});
            killer.once('error',resolve); killer.once('exit',resolve);
          });
        } else child.kill('SIGKILL');
        await untilExit(exited,5000);
      }
      if (child.exitCode === null && child.signalCode === null) throw new Error('Backend could not be stopped; maintenance cancelled');
    } finally {
      if (child.exitCode !== null || child.signalCode !== null || this.failure) this.child=null;
    }
  }

  async maintenance(action, archive, digest) {
    if (action !== 'inspect' && this.child) throw new Error('Stop backend before maintenance');
    const args=[this.script,'--maintenance',action,'--archive',archive];
    if (digest) args.push('--archive-sha256',digest);
    return new Promise((resolve,reject) => {
      const child=spawn(this.python,args,{cwd:this.resources, windowsHide:true,
        env:{...this.env,MINDOCK_MAINTENANCE_OWNER_PID:String(process.pid)},stdio:['ignore','pipe','pipe']});
      this.maintenanceChild=child;
      let stdout='',stderr='';
      child.stdout.setEncoding('utf8');
      child.stderr.setEncoding('utf8');
      child.stdout.on('data',chunk=>{stdout=(stdout+chunk).slice(-2_000_000);});
      child.stderr.on('data',chunk=>{stderr=(stderr+chunk).slice(-10000);});
      child.once('error',err=>{this.maintenanceChild=null; reject(err);});
      child.once('exit',code=>{
        this.maintenanceChild=null;
        try {
          if (code !== 0) throw new Error(stderr || 'Maintenance failed');
          const result=JSON.parse(stdout);
          if (!result.ok && action !== 'inspect') throw new Error(result.error || 'Maintenance failed');
          resolve(result);
        } catch(err) {reject(err);}
      });
    });
  }

  async dispose() {
    await this.stop();
    if (this.runDir) await fs.rm(this.runDir,{recursive:true,force:true});
  }
}

module.exports={Runtime,healthy,probeHealth};
