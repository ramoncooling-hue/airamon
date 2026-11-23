/* Desktop routes reuse the existing editors and persistence APIs. */
const originalRenderSettings = renderSettings;
const originalRenderIfacePanel = renderIfacePanel;
const originalGo = go;
const originalRenderScopeMenu = renderScopeMenu;
const originalRenderChat = renderChat;

function isStudyWorkspace(){
  return activeWorkspace().kind === 'study' || S.workspace === 'studypal';
}
for(const [id,name,ic] of [['agent','Agent','♙'],['skill','Skill','✧'],['mcp','MCP','⌘']]){
  if(!NAV.some(n=>n.id===id)) NAV.splice(NAV.length-1,0,{id,name,ic,badge:null});
}
visibleNav = function(){return NAV.filter(n=>isStudyWorkspace() || !['course','mastery'].includes(n.id));};
const desktopIcons={workspace:'panels-top-left',chat:'messages-square',course:'folder-open',graph:'network',mastery:'chart-no-axes-column-increasing',calendar:'calendar-days',orchestra:'workflow',agent:'bot',skill:'blocks',mcp:'plug',settings:'settings'};
const originalRenderPrimaryNav=renderPrimaryNav;
renderPrimaryNav=function(){const result=originalRenderPrimaryNav();el('primaryNav').querySelectorAll('[data-page]').forEach(button=>{button.querySelector('.ic').innerHTML=`<i data-lucide="${desktopIcons[button.dataset.page]||'circle'}"></i>`;});window.lucide?.createIcons();return result;};
const workflowDirectory=el('workflowList');
const workflowDirectoryHome=workflowDirectory?.parentElement;

function mountGraphDirectory(){
  const tree=el('graphNav');
  if(!tree)return;
  let home=el('graphNavHome');
  if(!home){home=document.createElement('div');home.id='graphNavHome';tree.before(home);}
  if(S.page==='graph') el('contextNav').appendChild(tree);
  else home.appendChild(tree);
}

renderContextSidebar = function(){
  if(workflowDirectory && workflowDirectory.parentElement!==workflowDirectoryHome)workflowDirectoryHome.appendChild(workflowDirectory);
  mountGraphDirectory();
  const tree=el('graphNav');
  if(tree?.parentElement===el('contextNav')) el('graphNavHome').appendChild(tree);
  el('contextKicker').hidden=true;el('contextTitle').hidden=true;el('contextDesc').hidden=true;
  let controls=el('desktopScope');
  if(!controls){controls=document.createElement('div');controls.id='desktopScope';el('contextTitle').parentElement.appendChild(controls);}
  const options=S.workspaces.map(w=>`<option value="${esc(w.key)}" ${w.key===S.workspace?'selected':''}>${esc(w.name)}</option>`).join('');
  const collection=S.page==='chat'?S.scope:S.page==='graph'?S.graphCourse:S.chatCourse;
  const range=['chat','graph'].includes(S.page);
  controls.innerHTML=`<label>所属工作区<select id="desktopWorkspace" aria-label="所属工作区">${options}</select></label>${range?`<label>${isStudyWorkspace()?'所属课程':'知识主题'}<select id="desktopCollection" aria-label="${isStudyWorkspace()?'所属课程':'知识主题'}">${S.page==='chat'?'<option value="*">'+(isStudyWorkspace()?'全部课程':'当前工作区全部内容')+'</option>':''}${S.courses.map(c=>`<option value="${esc(c.name)}" ${collection===c.name?'selected':''}>${esc(c.name)}</option>`).join('')}${!S.courses.length&&S.page==='graph'?'<option value="">暂无知识主题</option>':''}</select></label>`:''}`;
  if(S.page==='chat'){
    const prompts=isStudyWorkspace()?['这门课有哪些待复习知识点？','把这个问题和已有知识联系起来']:['当前工作区有哪些相关记录？','帮我整理这次讨论的要点'];
    controls.insertAdjacentHTML('beforeend',`<details class="desktop-prompts"><summary>快捷问题</summary>${prompts.map(p=>`<button type="button" data-prompt="${esc(p)}">${esc(p)}</button>`).join('')}</details>`);
    controls.querySelectorAll('[data-prompt]').forEach(b=>b.onclick=()=>{el('chatIn').value=b.dataset.prompt;el('chatIn').focus();});
  }
  el('desktopWorkspace').onchange=e=>switchWorkspace(e.target.value);
  el('desktopWorkspace').disabled=!!S.workspaceSwitching;
  if(el('desktopCollection')){el('desktopCollection').value=collection;el('desktopCollection').onchange=e=>{if(S.page==='chat')setScope(e.target.value);else{S.graphCourse=e.target.value;S.selectedKp=null;S.graphViewport=null;S.graphFocus={kind:'all',chapter:'',nodeId:null,depth:2};renderGraph();}};}
  let rows='';
  const item=(name,attrs,active=false)=>`<button class="context-action ${active?'on':''}" ${attrs}>${name}</button>`;
  if(S.page==='workspace') rows=`<div class="context-section-label">工作区</div>`+S.workspaces.map(w=>item(esc(w.name),`data-ws="${esc(w.key)}"`,w.key===S.workspace)).join('')+item('＋ 新增工作区','data-new-workspace');
  else if(S.page==='chat')rows='<div class="context-section-label">会话 <button class="act" data-new-chat>＋</button></div>'+S.convs.map(c=>item(esc(c.title||'新对话'),`data-conv="${c.id}"`,c.id===S.convId)).join('');
  else if(S.page==='course'||S.page==='mastery')rows='<div class="context-section-label">课程'+(S.page==='course'?' <button class="act" data-new-course>＋</button>':'')+'</div>'+S.courses.map(c=>item(esc(c.name),`data-course="${esc(c.name)}"`,c.name===(S.page==='course'?S.chatCourse:S.masteryCourse))).join('');
  else if(S.page==='calendar')rows='<div class="context-section-label">事项分类</div>'+[['all','全部事项'],['class','课程'],['hw','作业'],['exam','考试'],['personal','个人']].filter(([k])=>isStudyWorkspace()||['all','personal'].includes(k)).map(([k,n])=>item(n,`data-calendar-filter="${k}"`,S.evFilter===k)).join('');
  else if(S.page==='orchestra'){const list=el('workflowList');if(list){rows='<div id="desktopWorkflowList"></div>';}}
  else if(['agent','skill','mcp','settings'].includes(S.page)){
    if(S.page==='settings'){
      const section=S._settingsSection || (S._settingsSection=localStorage.getItem('mindock-settings-section')||'models');
      rows='<div class="context-section-label">本机配置</div>'+[
        ['models','模型库','已接入模型与新增管理'],
        ['routing','任务分工','快速回答与深度分析'],
        ['search','联网搜索','搜索方式与引用规则'],
        ['extensions','本机扩展','数据库、向量索引与通道']
      ].map(([key,label,desc])=>item(`${label}<small class="context-item-note">${desc}</small>`,`data-settings-section="${key}"`,section===key)).join('');
    }else{
      rows='<div class="context-section-label">'+({agent:'智能体',skill:'技能',mcp:'外部服务'}[S.page])+'</div>'+(['agent','skill'].includes(S.page)?(S._hub?.[S.page==='agent'?'agents':'skills']||[]).map(x=>item(esc(x.name),`data-hub-key="${esc(x.key)}"`)).join(''):'');
    }
  }
  el('contextNav').innerHTML=rows;
  if(S.page==='workspace'){
    el('contextNav').insertAdjacentHTML('beforeend','<div class="context-section-label">记录</div>'+[['all','全部记录'],['note','知识记录'],['task','待办任务']].map(([k,n])=>item(n,`data-record-filter="${k}"`,(S.desktopRecordFilter||'all')===k)).join(''));
    el('contextNav').querySelectorAll('[data-record-filter]').forEach(b=>b.onclick=()=>{S.desktopRecordFilter=b.dataset.recordFilter;filterDesktopRecords();renderContextSidebar();});
  }
  if(S.page==='orchestra' && workflowDirectory)el('desktopWorkflowList')?.appendChild(workflowDirectory);
  el('contextNav').querySelectorAll('[data-ws]').forEach(b=>b.onclick=()=>switchWorkspace(b.dataset.ws));
  el('contextNav').querySelector('[data-new-workspace]')?.addEventListener('click',createDesktopWorkspace);
  el('contextNav').querySelector('[data-new-chat]')?.addEventListener('click',newConv);
  el('contextNav').querySelector('[data-new-course]')?.addEventListener('click',openCourseDialog);
  el('contextNav').querySelectorAll('[data-conv]').forEach(b=>b.onclick=()=>openConv(Number(b.dataset.conv)));
  el('contextNav').querySelectorAll('[data-course]').forEach(b=>b.onclick=()=>{if(S.page==='course'){selectCourse(b.dataset.course);renderContextSidebar();}else{S.masteryCourse=b.dataset.course;renderMastery();renderContextSidebar();}});
  el('contextNav').querySelectorAll('[data-calendar-filter]').forEach(b=>b.onclick=()=>{S.evFilter=b.dataset.calendarFilter;renderCalendar();renderContextSidebar();});
  el('contextNav').querySelectorAll('[data-hub-key]').forEach(b=>b.onclick=()=>S.page==='agent'?openAgentDialog(b.dataset.hubKey):openSkillDialog(b.dataset.hubKey));
  el('contextNav').querySelectorAll('[data-settings-section]').forEach(b=>b.onclick=()=>{
    S._settingsSection=b.dataset.settingsSection;
    localStorage.setItem('mindock-settings-section',S._settingsSection);
    applySettingsSection();
    renderContextSidebar();
  });
  mountGraphDirectory();
};

async function createDesktopWorkspace(){
  const name=window.prompt('新工作区名称');if(!name?.trim())return;
  const kind=window.prompt('类型：学习 / 工作 / 生活','工作');if(kind===null)return;
  const key='space-'+Date.now().toString(36);
  try{const result=await apiPost('/api/workspaces',{key,name:name.trim(),kind:({学习:'study',工作:'work',生活:'life'})[kind]||'custom'});S.workspaces=await api('/api/workspaces');await switchWorkspace(result.workspace.key);}catch(e){toast('创建失败：'+esc(e.message));}
}

switchWorkspace=async function(key){
  if(!S.workspaces.some(w=>w.key===key)||key===S.workspace)return;
  if(chatSending)return toast('当前回答完成后再切换工作区');
  if(S.workspaceSwitching)return;
  S.workspaceSwitching=true;
  const previousPage=S.page;
  try{
    await apiPut('/api/agent/profile',{active_workspace_key:key});
    S.workspace=key;S.workspaceEpoch=(S.workspaceEpoch||0)+1;localStorage.setItem('mindock-workspace',key);restoreChatMcpChoice();
    if(S._configAssistant){S._configAssistant.prompt='';S._configAssistant.preview=null;S._configAssistant.error='';}
    clearTimeout(ingestPollTimer);ingestPollGeneration++;
    S.graphCy?.destroy();S.graphCy=null;
    Object.assign(S,{scope:SCOPE_ALL,convId:null,msgs:[],convs:[],courses:[],kp:[],hits:[],graphCourse:'',masteryCourse:'',chatCourse:'',graph:{nodes:[],edges:[],chapters:[]},selectedKp:null,graphViewport:null,graphHistory:[],graphFocus:{kind:'all',chapter:'',nodeId:null,depth:2},graphNavQuery:'',graphNavSource:'all',graphNavMastery:'all',graphNavTag:'all',graphFilter:'all',calendarMonth:null,evFilter:'all',_events:[],_bridges:[],workspaceItems:[],personalAgent:{today:null,attention:null},_courseMastery:{}});
    for(const id of ['graphBox','graphDetail','graphNav','courseKnowledgeList','sourceList','materialInventory','ingestJobs','calBox','todayBox','msgs','workspaceItemList','memoryLayerList','todayPanelList','attentionPanelList','continuePanelList','matrixBox','masteryItemsBox','indexBox'])if(el(id))el(id).replaceChildren();
    el('workspaceItemForm').reset();delete el('workspaceItemForm').dataset.editId;el('workspaceItemKind').disabled=false;
    el('workspaceItemForm').querySelector('[type="submit"]').textContent='保存记录';
    S.desktopRecordFilter='all';
    el('workspaceMenu')?.classList.remove('on');
    const [courses,events,kps]=await Promise.all([api('/api/courses',{workspace:key}),api('/api/events',{workspace:key}),api('/api/kp',{workspace:key})]);
    S.courses=courses;S._events=events;S.kp=kps;
    S._bridges=isStudyWorkspace()?await apiBridges():[];
    const pick=courses.find(c=>c.kp_count>0)||courses[0];if(pick)S.graphCourse=S.masteryCourse=S.chatCourse=pick.name;
    await loadConvs();renderWorkspacePicker();renderScopeMenu();
    go(visibleNav().some(n=>n.id===previousPage)?previousPage:'workspace');
    toast('已切换到 '+esc(activeWorkspace().name));
  }catch(e){toast('切换未完成：'+esc(e.message));renderWorkspacePicker();go('workspace');}
  finally{S.workspaceSwitching=false;renderContextSidebar();}
};

function applyHubRoute(){
  const hub=['agent','skill','mcp','settings'].includes(S.page);
  if(!hub)return;
  const pane=S.page==='agent'?'0':S.page==='skill'?'2':'3';
  document.querySelectorAll('#p-settings .tabpane').forEach(p=>p.classList.toggle('on',p.id==='tab'+pane));
  el('p-settings').dataset.hubView=S.page;
  if(!el('desktopAbilities')){
    const details=document.createElement('details');details.id='desktopAbilities';details.className='desktop-abilities';
    details.innerHTML='<summary>工具权限矩阵</summary>';details.appendChild(el('tab1').firstElementChild);el('tab0').appendChild(details);
  }
  const mcp=el('mcpSection');
  if(mcp){mcp.open=S.page==='mcp';mcp.hidden=S.page!=='mcp';}
  if(S.page==='settings')applySettingsSection();
  renderConfigAssistant();
}

function applySettingsSection(){
  const section=S._settingsSection || (S._settingsSection=localStorage.getItem('mindock-settings-section')||'models');
  const settings=el('p-settings');
  if(!settings)return;
  settings.dataset.settingsSection=section;
  const headings={
    models:['模型库','新增、编辑和测试模型'],
    routing:['任务分工','分别设置快速回答与深度分析'],
    search:['联网搜索','设置搜索方式、引用规则和联网服务'],
    extensions:['本机扩展','查看本地数据库、向量索引与连接通道']
  };
  const heading=headings[section]||headings.models;
  if(el('settingsPanelTitle'))el('settingsPanelTitle').textContent=heading[0];
  if(el('settingsPanelSubtitle'))el('settingsPanelSubtitle').textContent=heading[1];
  const modelCard=el('settingsModelCard'), interfaceCard=el('settingsInterfaceCard');
  if(modelCard)modelCard.hidden=!['models','routing','search'].includes(section);
  if(interfaceCard)interfaceCard.hidden=section!=='extensions';
  const modelBox=el('modelBox');
  if(modelBox)modelBox.dataset.settingsView=section;
  const modelSection=el('modelSection'), legacy=el('legacyModelSection');
  if(modelSection)modelSection.open=true;
  if(legacy)legacy.open=section!=='models';
}

function configAssistantState(){
  return S._configAssistant ||= {target:'skill',prompt:'',preview:null,error:'',busy:false};
}

function renderConfigAssistant(){
  const target = S.page === 'mcp' ? 'mcp' : S.page === 'skill' ? 'skill' : '';
  let box = el('desktopConfigComposer');
  if(!target){if(box)box.remove();return;}
  const pane = el(target === 'skill' ? 'tab2' : 'tab3');
  if(!pane)return;
  if(!box){box=document.createElement('section');box.id='desktopConfigComposer';box.className='config-assistant';}
  if(box.parentElement!==pane)pane.prepend(box);
  const state=configAssistantState();
  if(state.target && state.target!==target){state.prompt='';state.preview=null;state.error='';}
  state.target=target;
  const preview=state.preview && state.preview.target===target ? state.preview : null;
  const draft=preview?.draft;
  const fields=target==='skill'
    ? [['标识',draft?.key],['名称',draft?.name],['版本',draft?.version],['说明',draft?.description]]
    : [['服务 ID',draft?.id],['名称',draft?.label],['传输',draft?.transport],['工作区',draft?.workspace],['工具',draft?Object.keys(draft.tools||{}).join('、')||'未授权':'']];
  box.innerHTML=`<div class="config-assistant-head"><div><strong>配置助手</strong><span>用一句话描述要新增的${target==='skill'?'Skill':'MCP 服务'}，先预览，再确认保存</span></div><span class="tag gy">模型只生成草稿</span></div>
    <div class="config-assistant-form"><textarea id="configAssistantPrompt" placeholder="例如：创建一个论文资料整理 Skill，先检索公开来源，再整理成带引用的初稿">${esc(state.prompt)}</textarea><button class="act on" id="configAssistantPreview" ${state.busy?'disabled':''}>${state.busy?'生成中…':'生成预览'}</button></div>
    ${state.error?`<div class="config-assistant-error" role="alert">${esc(state.error)}</div>`:''}
    ${preview?`<div class="config-assistant-preview"><div class="config-assistant-preview-head"><b>待确认草稿</b><span>${preview.requires_confirmation?'需要你确认后才会保存':''}</span></div><div class="config-assistant-fields">${fields.map(([label,value])=>`<div><small>${esc(label)}</small><strong>${esc(value||'未填写')}</strong></div>`).join('')}</div>${draft?.content?`<pre>${esc(draft.content)}</pre>`:''}${(preview.warnings||[]).map(item=>`<div class="config-assistant-warning">${esc(item)}</div>`).join('')}<div class="config-assistant-actions"><button class="act" id="configAssistantDiscard">放弃预览</button><button class="act on" id="configAssistantConfirm" ${state.busy?'disabled':''}>确认创建</button></div></div>`:''}`;
  const input=el('configAssistantPrompt');
  if(input)input.oninput=()=>{state.prompt=input.value;};
  el('configAssistantPreview').onclick=async()=>{
    state.busy=true;state.error='';renderConfigAssistant();
    try{state.preview=await apiPost('/api/config-assistant/preview',{target,prompt:state.prompt});}
    catch(e){state.error=e.message||'预览失败';}
    finally{state.busy=false;renderConfigAssistant();}
  };
  el('configAssistantDiscard')?.addEventListener('click',()=>{state.preview=null;state.error='';renderConfigAssistant();});
  el('configAssistantConfirm')?.addEventListener('click',async()=>{
    state.busy=true;state.error='';renderConfigAssistant();
    try{const result=await apiPost('/api/config-assistant/confirm',state.preview);state.preview=null;state.prompt='';await loadHub();applyHubAgents();renderSettings();toast(result.next||'配置已创建');}
    catch(e){state.error=e.message||'保存失败';}
    finally{state.busy=false;renderConfigAssistant();}
  });
}
renderSettings=function(){originalRenderSettings();applyHubRoute();};
renderIfacePanel=function(){originalRenderIfacePanel();applyHubRoute();};
go=function(id){
  const hub=['agent','skill','mcp'].includes(id);
  if(hub){S.page=id;history.replaceState(null,'','#'+id);renderNav();document.querySelectorAll('.page').forEach(p=>p.classList.remove('on'));el('p-settings').classList.add('on');renderSettings();}
  else originalGo(id);
  document.body.dataset.page=S.page;
  el('askbar').hidden=true;
  applyHubRoute();mountGraphDirectory();
  if(hub)el('pageScroll').scrollTop=0;
};
renderChat=function(){originalRenderChat();document.querySelector('.thread-title').textContent=S.convs.find(c=>c.id===S.convId)?.title||'新对话';renderContextSidebar();};
function filterDesktopRecords(){
  const filter=S.desktopRecordFilter||'all';
  el('workspaceItemList').querySelectorAll('.workspace-item').forEach((row,i)=>{row.hidden=filter!=='all' && S.workspaceItems[i]?.kind!==filter;});
}
const originalRenderWorkspace=renderWorkspace;
renderWorkspace=async function(){await originalRenderWorkspace();filterDesktopRecords();};
renderScopeMenu=function(){originalRenderScopeMenu();if(!isStudyWorkspace()){for(const id of ['askScopeLabel','chatScopeLabel'])el(id).textContent=S.scope===SCOPE_ALL?'当前工作区':S.scope;}renderContextSidebar();};
const originalRenderWorkspacePicker=renderWorkspacePicker;
renderWorkspacePicker=function(){originalRenderWorkspacePicker();el('workspaceMenu').querySelector('.workspace-option:last-child').onclick=createDesktopWorkspace;renderContextSidebar();};
