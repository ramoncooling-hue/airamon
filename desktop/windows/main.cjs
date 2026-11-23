'use strict';
const {app, BrowserWindow, Menu, dialog, shell, session} = require('electron');
const path = require('node:path');
const fs = require('node:fs/promises');
const {Runtime} = require('./runtime.cjs');
const {dataRoot, sameApp, externalUrl, installMediaPermissions} = require('./policy.cjs');

app.setName('MinDock');
require('node:fs').mkdirSync(path.join(dataRoot(), 'app', 'desktop'), {recursive:true});
app.setPath('userData', path.join(dataRoot(), 'app', 'desktop'));
let window, runtime, busy=false, quitting=false, quitComplete=false, statusUrl='';
const single=app.requestSingleInstanceLock();
if (!single) app.quit();
app.on('second-instance',()=>{if(window){if(window.isMinimized())window.restore();window.focus();}});

function status(message) {
  statusUrl='data:text/html;charset=utf-8,'+encodeURIComponent(`<!doctype html><meta charset="utf-8"><title>MinDock</title><style>body{margin:0;padding:64px;background:#f6f5f8;color:#292232;font:18px system-ui}h1{font-size:28px;color:#662898}</style><h1>MinDock 思泊</h1><p>${message}</p>`);
  return window.loadURL(statusUrl);
}

function protect(contents) {
  contents.on('will-navigate',(event,url)=>{
    if (url === statusUrl || sameApp(url,runtime.base)) return;
    event.preventDefault();
    if(externalUrl(url)) shell.openExternal(url);
  });
  contents.setWindowOpenHandler(({url})=>{
    if(sameApp(url,runtime.base)) return {action:'allow',overrideBrowserWindowOptions:{
      width:1100,height:800,webPreferences:{session:contents.session,nodeIntegration:false,contextIsolation:true,sandbox:true}}};
    if(externalUrl(url)) shell.openExternal(url);
    return {action:'deny'};
  });
  contents.on('did-create-window',child=>protect(child.webContents));
}

async function loadApp() {
  const base=await runtime.start();
  await window.webContents.session.cookies.set({url:base,name:'mindock_session',value:runtime.token,
    httpOnly:true,sameSite:'strict',secure:false,path:'/'});
  await window.loadURL(base+'/#chat');
}

async function maintenance(action) {
  if(busy || quitting) return;
  busy=true;
  let archive, digest;
  try {
    if(action === 'backup') {
      const saved=await dialog.showSaveDialog(window,{title:'备份个人资料',
        defaultPath:path.join(app.getPath('documents'),'MinDock-backup-'+new Date().toISOString().slice(0,10)+'.zip'),
        filters:[{name:'MinDock 备份',extensions:['zip']}]});
      if(saved.canceled) return;
      archive=saved.filePath;
      const answer=await dialog.showMessageBox(window,{type:'question',title:'备份个人资料',
        message:'将暂时停止对话和识别，完成后重新打开。',
        detail:'备份包含私人材料、图谱、日历和对话，不含模型密钥。请勿作为安装包分享。',
        buttons:['开始备份','取消'],cancelId:1,defaultId:1});
      if(answer.response !== 0) return;
    } else {
      const selected=await dialog.showOpenDialog(window,{title:'从备份恢复',properties:['openFile'],
        filters:[{name:'MinDock 备份',extensions:['zip']}]});
      if(selected.canceled) return;
      archive=selected.filePaths[0];
      const summary=await runtime.maintenance('inspect',archive);
      digest=summary.sha256;
      const answer=await dialog.showMessageBox(window,{type:'warning',title:'恢复个人资料',
        message:'确认用此备份恢复？',
        detail:`日期：${summary.created_at}\n版本：${summary.app_version}\n文件：${summary.file_count} 个\n大小：${Math.round(summary.total_bytes/1024/1024)} MB\n\n现有资料会保留副本。备份不含模型密钥，恢复后需重新配置。`,
        buttons:['确认恢复','取消'],cancelId:1,defaultId:1});
      if(answer.response !== 0) return;
    }
    await status(action === 'backup'?'正在备份个人资料，请保持应用打开…':'正在恢复个人资料，请保持应用打开…');
    await runtime.stop();
    const result=await runtime.maintenance(action,archive,digest);
    await loadApp();
    await dialog.showMessageBox(window,{type:'info',message:action === 'backup'?'备份完成':'恢复完成',
      detail:action === 'backup'?archive:`原资料副本：${result.recovery_dir || path.join(runtime.root,'app','recovery')}\n请在设置中检查模型配置。`});
  } catch(err) {
    dialog.showErrorBox('操作未完成',String(err.message));
    if(!runtime.child && !quitting) {
      try{await loadApp();}catch(error){dialog.showErrorBox('无法重新打开',error.message);}
    }
  } finally {busy=false;}
}

function menus() {
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    {label:'MinDock',submenu:[{label:'关于 MinDock',click:()=>dialog.showMessageBox(window,{
      message:'MinDock 思泊',detail:`版本 ${app.getVersion()}\n让思考有所停泊，让成长自有航向`})},
      {type:'separator'},{label:'退出',accelerator:'Alt+F4',click:()=>app.quit()}]},
    {label:'编辑',submenu:[{role:'undo',label:'撤销'},{role:'redo',label:'重做'},{type:'separator'},
      {role:'cut',label:'剪切'},{role:'copy',label:'复制'},{role:'paste',label:'粘贴'},{role:'selectAll',label:'全选'}]},
    {label:'显示',submenu:[{label:'刷新页面',accelerator:'Ctrl+R',click:()=>{if(!busy)window.reload();}},
      {role:'resetZoom',label:'实际大小'},{role:'zoomIn',label:'放大'},{role:'zoomOut',label:'缩小'},
      {role:'togglefullscreen',label:'全屏'}]},
    {label:'资料',submenu:[{label:'打开数据文件夹',click:()=>shell.openPath(runtime.root)},
      {label:'打开日志文件夹',click:()=>shell.openPath(path.join(runtime.root,'app','logs'))},
      {type:'separator'},{label:'备份个人资料…',click:()=>maintenance('backup')},
      {label:'从备份恢复…',click:()=>maintenance('restore')}]}
  ]));
}

if(single) app.whenReady().then(async()=>{
  const resources=process.env.MINDOCK_BACKEND_DIR || path.join(process.resourcesPath,'backend');
  runtime=new Runtime({root:dataRoot(),resources,
    python:process.env.MINDOCK_PYTHON || path.join(resources,'python','python.exe'),
    script:path.join(resources,'desktop','backend.py'),
    ocr:path.join(resources,'desktop','windows','ocr.py')});
  const privateSession=session.fromPartition('persist:mindock');
  installMediaPermissions(privateSession,()=>({contents:window?.webContents,base:runtime.base}));
  privateSession.on('will-download',async(_event,item)=>{
    item.pause();
    try{
      const selected=await dialog.showSaveDialog(window,{title:'保存文件',
        defaultPath:path.join(app.getPath('downloads'),path.basename(item.getFilename()))});
      if(selected.canceled)item.cancel();else{item.setSavePath(selected.filePath);item.resume();}
    }catch{item.cancel();}
  });
  window=new BrowserWindow({width:1440,height:960,minWidth:1100,minHeight:720,title:'MinDock 思泊',
    backgroundColor:'#f6f5f8',webPreferences:{session:privateSession,nodeIntegration:false,contextIsolation:true,sandbox:true}});
  protect(window.webContents);
  window.on('close',event=>{
    if(busy){event.preventDefault();dialog.showMessageBox(window,{message:'资料操作进行中，请稍后再退出。'});return;}
    if(!quitComplete){event.preventDefault();app.quit();}
  });
  menus();
  await status('正在打开你的个人知识空间…');
  try{await loadApp();}catch(err){
    dialog.showErrorBox('MinDock 无法启动',`${err.message}\n\n日志目录：${path.join(runtime.root,'app','logs')}`);
    app.quit();
  }
});

app.on('before-quit',event=>{
  if(quitComplete)return;
  event.preventDefault();
  if(busy || quitting)return;
  quitting=true;
  Promise.resolve(runtime?.dispose()).catch(err=>dialog.showErrorBox('退出未完成',err.message)).finally(()=>{
    quitComplete=true;app.quit();
  });
});
app.on('window-all-closed',()=>app.quit());
