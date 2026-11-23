'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
const {installMediaPermissions}=require('../policy.cjs');

function fixture() {
  const base='http://127.0.0.1:51234';
  const contents={getURL:()=>base+'/#lectures',isDestroyed:()=>false};
  const handlers={};
  const session={setPermissionRequestHandler:fn=>{handlers.request=fn;},
    setPermissionCheckHandler:fn=>{handlers.check=fn;}};
  installMediaPermissions(session,()=>({contents,base}));
  const details={isMainFrame:true,requestingUrl:base+'/',securityOrigin:base,mediaTypes:['audio']};
  return {base,contents,handlers,details};
}

test('only the running app main frame receives audio capture permission',()=>{
  const {base,contents,handlers,details}=fixture();
  let result;
  handlers.request(contents,'media',value=>{result=value;},details);
  assert.equal(result,true);
  assert.equal(handlers.check(contents,'media',base,{...details,mediaType:'audio'}),true);
});

test('video, mixed capture, display capture and unspecified media fail closed',()=>{
  const {base,contents,handlers,details}=fixture();
  for(const mediaTypes of [['video'],['audio','video'],[],undefined]) {
    let result;
    handlers.request(contents,'media',value=>{result=value;},{...details,mediaTypes});
    assert.equal(result,false);
  }
  for(const permission of ['display-capture','notifications','geolocation','clipboard-read']) {
    handlers.request(contents,permission,value=>assert.equal(value,false),details);
    assert.equal(handlers.check(contents,permission,base,{...details,mediaType:'audio'}),false);
  }
  for(const mediaType of ['video','unknown',undefined])
    assert.equal(handlers.check(contents,'media',base,{...details,mediaType}),false);
});

test('foreign pages, iframe, other contents, stale port and missing request URL are refused',()=>{
  const {base,contents,handlers,details}=fixture();
  for(const change of [
    {requestingUrl:'https://example.com/'},{requestingUrl:'http://127.0.0.1:51235/'},
    {requestingUrl:'http://user@127.0.0.1:51234/'},{isMainFrame:false},
    {requestingUrl:undefined},{securityOrigin:'https://example.com'},
  ]) {
    handlers.request(contents,'media',value=>assert.equal(value,false),{...details,...change});
    assert.equal(handlers.check(contents,'media',base,{...details,mediaType:'audio',...change}),false);
  }
  for(const other of [null,{getURL:()=>base+'/'}]) {
    handlers.request(other,'media',value=>assert.equal(value,false),details);
    assert.equal(handlers.check(other,'media',base,{...details,mediaType:'audio'}),false);
  }
  contents.getURL=()=> 'https://example.com';
  handlers.request(contents,'media',value=>assert.equal(value,false),details);
});

test('backend environment exposes the bundled ffmpeg without retaining host PATH casing duplicates',()=>{
  const {backendEnvironment}=require('../policy.cjs');
  const env=backendEnvironment({root:'test',sessionFile:'s',shutdownFile:'s',token:'t',parentPid:1,
    python:'C:\\MinDock\\resources\\backend\\python\\python.exe',ocr:'ocr.py'}, {Path:'C:\\Windows',PATH:'host'});
  assert.equal(env.Path,undefined);
  assert.ok(env.PATH.startsWith('C:\\MinDock\\resources\\backend\\bin;'));
});

test('Electron main installs both permission handlers from the tested policy',async()=>{
  const original=require('../policy.cjs');
  let installed=0;
  const app={setName(){},setPath(){},requestSingleInstanceLock:()=>true,on(){},
    whenReady:()=>({then:fn=>fn()}),getPath:()=>'.',quit(){}};
  const contents={on(){},setWindowOpenHandler(){}};
  class Window { constructor(){this.webContents=contents;} on(){} loadURL(){} }
  const session={on(){}};
  const stub={app,BrowserWindow:Window,Menu:{setApplicationMenu(){},buildFromTemplate(){}},
    dialog:{showErrorBox(){}},shell:{},session:{fromPartition:()=>session}};
  const source=fs.readFileSync(path.join(__dirname,'../main.cjs'),'utf8');
  vm.runInNewContext(source,{process:{env:{},resourcesPath:'.'},require(name){
    if(name==='electron')return stub;
    if(name==='./policy.cjs')return {...original,dataRoot:()=>'.',installMediaPermissions:()=>{installed++;}};
    if(name==='./runtime.cjs')return {Runtime:class {
      constructor(options){Object.assign(this,options);}
      async start(){throw new Error('test stop');}
    }};
    if(name==='node:fs')return {mkdirSync(){}};
    return require(name);
  }});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(installed,1);
});
