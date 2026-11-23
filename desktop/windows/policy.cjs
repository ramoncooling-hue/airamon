'use strict';
const path = require('node:path');

function dataRoot(env = process.env, platform = process.platform, home = require('node:os').homedir()) {
  if (env.CYBERPAL_DATA_DIR) return path.resolve(env.CYBERPAL_DATA_DIR);
  if (platform === 'win32') {
    return path.win32.join(env.LOCALAPPDATA || path.win32.join(home, 'AppData', 'Local'), 'MinDock');
  }
  return path.join(home, 'Library', 'Application Support', 'MinDock-Windows-Preview');
}

function sameApp(url, base) {
  try {
    const target = new URL(url), origin = new URL(base);
    return target.origin === origin.origin && target.username === '' && target.password === '';
  } catch { return false; }
}

function externalUrl(url) {
  try {
    const u = new URL(url);
    return ['http:', 'https:'].includes(u.protocol) && !u.username && !u.password
      && !['localhost', '127.0.0.1', '[::1]'].includes(u.hostname);
  } catch { return false; }
}

function installMediaPermissions(session, current) {
  function allowed(contents, permission, details, origin) {
    const state=current();
    if (!state || !contents || contents !== state.contents || contents.isDestroyed()
        || permission !== 'media' || details?.isMainFrame !== true) return false;
    try {
      const base=new URL(state.base);
      if (base.protocol !== 'http:' || base.hostname !== '127.0.0.1' || !base.port
          || !sameApp(contents.getURL(),state.base)
          || !sameApp(details.requestingUrl,state.base)) return false;
      for (const value of [origin,details.securityOrigin,details.embeddingOrigin]) {
        if (value !== undefined && !sameApp(value,state.base)) return false;
      }
      return true;
    } catch {return false;}
  }
  session.setPermissionRequestHandler((contents,permission,callback,details)=>{
    const audioOnly=Array.isArray(details?.mediaTypes) && details.mediaTypes.length === 1
      && details.mediaTypes[0] === 'audio';
    callback(audioOnly && allowed(contents,permission,details));
  });
  session.setPermissionCheckHandler((contents,permission,origin,details)=>
    details?.mediaType === 'audio' && allowed(contents,permission,details,origin));
}

function backendEnvironment({root, sessionFile, shutdownFile, token, parentPid, python, ocr}, inherited = process.env) {
  const env = {...inherited, CYBERPAL_DATA_DIR:root, MINDOCK_SESSION_FILE:sessionFile,
    MINDOCK_SHUTDOWN_FILE:shutdownFile, MINDOCK_SESSION_TOKEN:token,
    MINDOCK_PARENT_PID:String(parentPid), MINDOCK_BUNDLED_RUNTIME:'1',
    PYTHONUTF8:'1', PYTHONIOENCODING:'utf-8', PYTHONDONTWRITEBYTECODE:'1',
    MINDOCK_OCR:ocr, MINDOCK_OCR_PYTHON:python};
  for (const key of ['PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV', 'MINDOCK_RUNTIME_LOCKED', 'MINDOCK_MAINTENANCE_OWNER_PID']) delete env[key];
  const paths=Object.keys(env).filter(key=>key.toUpperCase()==='PATH');
  const windows=path.win32.isAbsolute(python);
  const delimiter=windows ? ';' : path.delimiter;
  const inheritedPath=paths.map(key=>env[key]).filter(Boolean).join(delimiter);
  for (const key of paths) delete env[key];
  const pathsApi=windows ? path.win32 : path;
  env.PATH=pathsApi.join(pathsApi.dirname(pathsApi.dirname(python)),'bin')+delimiter+inheritedPath;
  return env;
}

function portFromDescriptor(text) {
  const port = JSON.parse(text).port;
  if (!Number.isInteger(port) || port < 1024 || port > 65535 || port === 8899) throw new Error('Invalid desktop port');
  return port;
}

module.exports = {dataRoot, sameApp, externalUrl, backendEnvironment, portFromDescriptor, installMediaPermissions};
