/* Keep the original material nodes and their upload/review bindings intact. */
const courseDesktopOriginalRenderCourse = renderCourse;
const courseDesktopOriginalRenderSourceStatus = renderSourceStatus;
const courseDesktopState = {key:'', tab:'materials', items:[], request:0, graphRequest:0, saving:new Set(), sourceRequest:0, sourcePending:null};

function courseDesktopScope(){
  const courses = (S.courses || []).filter(course=>!course.workspace_key || course.workspace_key===S.workspace);
  const course = courses.find(item=>item.name===S.chatCourse) || courses[0];
  return {workspace:S.workspace, course:course?.name || '', key:JSON.stringify([S.workspace,course?.name || ''])};
}

function courseDesktopScopeCurrent(scope){
  return scope.workspace===S.workspace && scope.course===S.chatCourse && courseDesktopScope().key===scope.key;
}

function courseDesktopMount(){
  const page = el('p-course');
  if(!page || page.dataset.courseDesktop==='true') return page;
  page.dataset.courseDesktop='true';
  const header = document.createElement('header');
  header.className='course-desktop-header';
  header.innerHTML=`<div class="course-desktop-heading"><h1 id="courseDesktopTitle"></h1><span id="courseDesktopMeta"></span></div><button type="button" class="act" id="courseUploadToggle" aria-controls="courseUploadPanel" aria-expanded="false">＋ 上传材料</button>`;
  const tabs = document.createElement('div');
  tabs.className='course-desktop-tabs';
  tabs.setAttribute('role','tablist');
  tabs.setAttribute('aria-label','课程内容');
  tabs.innerHTML=`<button type="button" role="tab" id="courseMaterialsTab" aria-controls="courseMaterialsPanel" aria-selected="true">材料 <span id="courseMaterialCount"></span></button><button type="button" role="tab" id="courseKnowledgeTab" aria-controls="courseKnowledgePanel" aria-selected="false" tabindex="-1">知识点 <span id="courseKnowledgeCount"></span></button>`;
  const materials = document.createElement('div');
  materials.id='courseMaterialsPanel';
  materials.setAttribute('role','tabpanel');
  materials.setAttribute('aria-labelledby','courseMaterialsTab');
  page.appendChild(materials);
  const upload = document.createElement('details');
  upload.id='courseUploadPanel';
  upload.className='course-upload-panel';
  upload.innerHTML='<summary>上传材料</summary>';
  const workspace = page.querySelector('.course-workspace');
  if(workspace) upload.appendChild(workspace);
  materials.appendChild(upload);
  for(const id of ['intakeQueue','ingestJobs']){
    const node=el(id);
    if(node) materials.appendChild(node);
  }
  const memory = page.querySelector('.course-memory');
  if(memory) materials.appendChild(memory);
  const artifacts = document.createElement('details');
  artifacts.id='courseMaterialArtifacts';
  artifacts.innerHTML='<summary>材料产物与历史</summary>';
  const inventory=el('materialInventory');
  if(inventory){inventory.before(artifacts);artifacts.appendChild(inventory);}
  const knowledge = document.createElement('div');
  knowledge.id='courseKnowledgePanel';
  knowledge.hidden=true;
  knowledge.setAttribute('role','tabpanel');
  knowledge.setAttribute('aria-labelledby','courseKnowledgeTab');
  knowledge.innerHTML=`<div class="course-knowledge-tools"><input type="search" id="courseKnowledgeSearch" aria-label="搜索知识点" placeholder="搜索知识点、要点或来源"><select id="courseKnowledgeChapter" aria-label="按章节筛选"><option value="">全部章节</option></select><select id="courseKnowledgeMastery" aria-label="按掌握度筛选"><option value="">全部掌握度</option>${M_ORDER.map(level=>`<option value="${esc(level)}">${esc(level)}</option>`).join('')}</select><span id="courseKnowledgeSummary" role="status"></span><button type="button" class="act" id="courseKnowledgeRetry" title="刷新知识点" aria-label="刷新知识点">↻</button></div><div id="courseKnowledgeList" aria-live="polite"></div>`;
  page.prepend(header,tabs);
  page.append(materials,knowledge);
  const picker=page.querySelector('.course-picker');
  if(picker) picker.hidden=true;
  el('courseMaterialsTab').onclick=()=>courseDesktopSetTab('materials');
  el('courseKnowledgeTab').onclick=()=>courseDesktopSetTab('knowledge');
  tabs.addEventListener('keydown',event=>{
    if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
    event.preventDefault();
    const target=event.key==='Home'?'materials':event.key==='End'?'knowledge':courseDesktopState.tab==='materials'?'knowledge':'materials';
    courseDesktopSetTab(target);
    el(target==='materials'?'courseMaterialsTab':'courseKnowledgeTab').focus();
  });
  el('courseUploadToggle').onclick=()=>{
    courseDesktopSetTab('materials');
    upload.open=!upload.open;
    el('courseUploadToggle').setAttribute('aria-expanded',String(upload.open));
    if(upload.open) upload.querySelector('summary').focus();
  };
  upload.addEventListener('toggle',()=>el('courseUploadToggle').setAttribute('aria-expanded',String(upload.open)));
  el('courseKnowledgeSearch').oninput=courseDesktopDrawKnowledge;
  el('courseKnowledgeChapter').onchange=courseDesktopDrawKnowledge;
  el('courseKnowledgeMastery').onchange=courseDesktopDrawKnowledge;
  el('courseKnowledgeRetry').onclick=()=>courseDesktopRenderKnowledge();
  const drop=el('dropZone');
  if(drop){
    drop.tabIndex=0;
    drop.setAttribute('role','button');
    drop.setAttribute('aria-label','选择课程材料');
    drop.addEventListener('keydown',event=>{
      if(event.key==='Enter' || event.key===' '){event.preventDefault();el('courseFileInput').click();}
    });
  }
  return page;
}

function courseDesktopSetTab(tab){
  courseDesktopState.tab=tab==='knowledge'?'knowledge':'materials';
  const knowledge=courseDesktopState.tab==='knowledge';
  el('courseMaterialsPanel').hidden=knowledge;
  el('courseKnowledgePanel').hidden=!knowledge;
  for(const [id,selected] of [['courseMaterialsTab',!knowledge],['courseKnowledgeTab',knowledge]]){
    el(id).setAttribute('aria-selected',String(selected));
    el(id).tabIndex=selected?0:-1;
  }
}

function courseDesktopEmpty(){
  courseDesktopState.items=[];
  courseDesktopState.request++;
  S.chatCourse='';
  el('courseDesktopTitle').textContent=isStudyWorkspace()?'暂无课程':'暂无主题';
  el('courseDesktopMeta').textContent='';
  el('courseUploadToggle').disabled=true;
  el('courseUploadPanel').open=false;
  for(const id of ['sourceList','materialInventory','ingestJobs','intakeQueue','intakeDest','courseMemoryBody']) el(id).innerHTML='';
  el('sourceList').innerHTML='<div class="empty">当前工作区暂无材料</div>';
  el('sourceSummary').textContent='';
  el('runtimeDataPath').textContent='';
  el('courseFlowNotice').hidden=true;
  el('courseKnowledgeList').innerHTML='<div class="empty">当前工作区暂无知识点</div>';
  el('courseKnowledgeList').setAttribute('aria-busy','false');
  el('courseKnowledgeSummary').textContent='0 个知识点';
  el('courseMaterialCount').textContent='0';
  el('courseKnowledgeCount').textContent='0';
  el('courseMaterialArtifacts').hidden=true;
}

renderCourse=function(){
  if(!courseDesktopMount()) return courseDesktopOriginalRenderCourse();
  const scope=courseDesktopScope();
  if(courseDesktopState.key!==scope.key){
    courseDesktopState.key=scope.key;
    courseDesktopState.items=[];
    for(const id of ['courseKnowledgeSearch','courseKnowledgeChapter','courseKnowledgeMastery']) el(id).value='';
    for(const id of ['sourceList','materialInventory','ingestJobs','intakeQueue']) el(id).innerHTML='';
    el('courseMaterialCount').textContent='';
    el('courseKnowledgeCount').textContent='';
    el('courseFlowNotice').hidden=true;
    el('courseMaterialArtifacts').open=false;
    el('courseUploadPanel').open=false;
    el('courseUploadToggle').setAttribute('aria-expanded','false');
  }
  courseDesktopSetTab(courseDesktopState.tab);
  if(!scope.course){courseDesktopEmpty();return;}
  S.chatCourse=scope.course;
  el('courseDesktopTitle').textContent=scope.course;
  el('courseDesktopMeta').textContent=isStudyWorkspace()?'课程':'知识主题';
  el('courseUploadToggle').disabled=false;
  el('courseKnowledgeMastery').hidden=!isStudyWorkspace();
  courseDesktopOriginalRenderCourse();
  return courseDesktopRenderKnowledge();
};

async function courseDesktopRenderKnowledge(){
  const scope=courseDesktopScope();
  if(!scope.course){courseDesktopEmpty();return;}
  const request=++courseDesktopState.request;
  const current=()=>courseDesktopScopeCurrent(scope) && request===courseDesktopState.request;
  const box=el('courseKnowledgeList');
  box.setAttribute('aria-busy','true');
  box.innerHTML='<div class="empty" role="status">正在读取知识点…</div>';
  el('courseKnowledgeSummary').textContent='读取中…';
  try{
    const rows=await api('/api/kp',{course:scope.course,workspace:scope.workspace});
    if(!current()) return;
    if(!Array.isArray(rows)) throw new Error('知识点返回格式不可用');
    courseDesktopState.items=rows.filter(item=>Number.isSafeInteger(Number(item.id)) && Number(item.id)>0 &&
      (!item.course_name || item.course_name===scope.course) && (!item.workspace_key || item.workspace_key===scope.workspace));
    const chapters=[...new Set(courseDesktopState.items.map(item=>item.chapter || '未归类'))].sort((a,b)=>a.localeCompare(b,'zh-CN',{numeric:true}));
    const chapter=el('courseKnowledgeChapter');
    const selected=chapter.value;
    chapter.innerHTML='<option value="">全部章节</option>'+chapters.map(name=>`<option value="${esc(name)}">${esc(name)}</option>`).join('');
    chapter.value=chapters.includes(selected)?selected:'';
    el('courseKnowledgeCount').textContent=String(courseDesktopState.items.length);
    courseDesktopDrawKnowledge();
  }catch(error){
    if(!current()) return;
    courseDesktopState.items=[];
    el('courseKnowledgeCount').textContent='';
    el('courseKnowledgeSummary').textContent='读取失败';
    box.innerHTML=`<div class="empty" role="alert">知识点读取失败：${esc(error.message)}</div>`;
  }finally{if(current()) box.setAttribute('aria-busy','false');}
}

function courseDesktopDrawKnowledge(){
  const query=el('courseKnowledgeSearch').value.trim().toLocaleLowerCase();
  const chapter=el('courseKnowledgeChapter').value;
  const mastery=isStudyWorkspace()?el('courseKnowledgeMastery').value:'';
  const items=courseDesktopState.items.filter(item=>(!chapter || (item.chapter || '未归类')===chapter) &&
    (!mastery || item.mastery===mastery) && (!query || [item.name,item.gist,item.detail,item.source].join(' ').toLocaleLowerCase().includes(query)));
  el('courseKnowledgeSummary').textContent=`${items.length} / ${courseDesktopState.items.length} 个知识点`;
  const box=el('courseKnowledgeList');
  if(!items.length){
    box.innerHTML=`<div class="empty">${courseDesktopState.items.length?'没有符合筛选条件的知识点':'暂无正式知识点，材料候选确认后会出现在这里'}</div>`;
    return;
  }
  box.innerHTML=`<ul class="course-knowledge-list">${items.map(item=>`<li class="course-knowledge-row ${isStudyWorkspace()?'':'course-knowledge-no-mastery'}"><div class="course-knowledge-main"><details><summary><span>${esc(item.name)}</span>${item.freq==='高频'?'<span class="course-frequency">高频</span>':''}</summary><div class="course-knowledge-detail">${item.gist?`<p>${esc(item.gist)}</p>`:''}${item.detail && item.detail!==item.gist?`<p>${esc(item.detail)}</p>`:''}${item.trap?`<p class="course-knowledge-trap">易错：${esc(item.trap)}</p>`:''}<p>来源：${esc(item.source || '未标注')}</p></div></details><span class="course-knowledge-source" title="${esc(item.source || '')}">${esc(item.source || '未标注来源')}</span></div><span class="course-knowledge-chapter" title="${esc(item.chapter || '未归类')}">${esc(item.chapter || '未归类')}</span>${isStudyWorkspace()?`<label class="course-knowledge-mastery"><span class="course-sr-only">${esc(item.name)}掌握度</span><select data-course-mastery="${Number(item.id)}" aria-label="${esc(item.name)}掌握度"${courseDesktopState.saving.has(JSON.stringify([S.workspace,S.chatCourse,Number(item.id)]))?' disabled':''}>${M_ORDER.map(level=>`<option value="${esc(level)}"${(item.mastery || '未读')===level?' selected':''}>${esc(level)}</option>`).join('')}</select></label>`:''}<button type="button" class="act course-graph-locate" data-course-graph="${Number(item.id)}" title="在图谱中定位" aria-label="在图谱中定位：${esc(item.name)}">↗</button></li>`).join('')}</ul>`;
  box.querySelectorAll('[data-course-mastery]').forEach(control=>control.onchange=()=>courseDesktopSaveMastery(control));
  box.querySelectorAll('[data-course-graph]').forEach(button=>button.onclick=()=>courseDesktopLocate(Number(button.dataset.courseGraph)));
}

async function courseDesktopSaveMastery(control){
  const scope=courseDesktopScope();
  const item=courseDesktopState.items.find(row=>Number(row.id)===Number(control.dataset.courseMastery));
  if(!isStudyWorkspace() || !item || !courseDesktopScopeCurrent(scope) || control.disabled) return;
  const savingKey=JSON.stringify([scope.workspace,scope.course,Number(item.id)]);
  if(courseDesktopState.saving.has(savingKey)) return;
  const previous=item.mastery || '未读';
  const value=control.value;
  if(!M_ORDER.includes(value)){control.value=previous;return;}
  courseDesktopState.saving.add(savingKey);
  control.disabled=true;
  try{
    const result=await apiPost(`/api/kp/${Number(item.id)}/mastery`,{mastery:value,origin:'course-page',confirmed:true,workspace:scope.workspace});
    if(!result.ok) throw new Error(result.error || '保存失败');
    if(!courseDesktopScopeCurrent(scope)) return;
    for(const row of courseDesktopState.items) if(Number(row.id)===Number(item.id)) row.mastery=value;
    for(const row of S.kp || []) if(Number(row.id)===Number(item.id)) row.mastery=value;
    if(S._graphScope===scope.key) for(const row of S.graph?.nodes || []) if(Number(row.id)===Number(item.id)) row.mastery=value;
    if(S._courseMastery) delete S._courseMastery[scope.course];
    toast(result.changed===false?'掌握度没有变化':`掌握度已更新：${esc(item.name)} → ${esc(value)}`);
  }catch(error){
    control.value=previous;
    if(courseDesktopScopeCurrent(scope)) toast('掌握度保存失败：'+esc(error.message));
  }finally{
    courseDesktopState.saving.delete(savingKey);
    control.disabled=false;
    if(courseDesktopScopeCurrent(scope)) courseDesktopDrawKnowledge();
  }
}

async function courseDesktopLocate(id){
  const scope=courseDesktopScope();
  if(!courseDesktopState.items.some(item=>Number(item.id)===id) || !courseDesktopScopeCurrent(scope)) return;
  const request=++courseDesktopState.graphRequest;
  S.graphCourse=scope.course;
  go('graph');
  // go() starts the existing async renderer; select only after its scoped graph arrives.
  for(let attempt=0;attempt<200;attempt++){
    if(request!==courseDesktopState.graphRequest || S.workspace!==scope.workspace || S.graphCourse!==scope.course || S.page!=='graph') return;
    if(S.graph?.nodes?.some(item=>Number(item.id)===id) && (!S._graphScope || S._graphScope===scope.key)){
      if(typeof graphSelectNode==='function') graphSelectNode(id);
      else{S.selectedKp=id;setGraphDetail(id);focusGraphNode(id);}
      return;
    }
    await new Promise(resolve=>setTimeout(resolve,50));
  }
  toast('图谱尚未载入该知识点，请在图谱中刷新后重试');
}

function courseDesktopCompactSources(){
  const list=el('sourceList');
  let count=0;
  list.querySelectorAll('.source-row').forEach(row=>{
    const main=row.querySelector('.source-row-main');
    const path=row.querySelector('.source-row-actions [data-copy-path]')?.dataset.copyPath;
    if(!main || !path) return;
    count++;
    if(row.querySelector('.course-source-details')) return;
    const details=document.createElement('details');
    details.className='course-source-details';
    const filename=path.split(/[\\/]/).pop() || path;
    const meta=main.textContent.replace(path,'').replace(/^[\s·]+|[\s·]+$/g,'');
    details.innerHTML=`<summary><b title="${esc(path)}">${esc(filename)}</b><span>${esc(meta)}</span></summary>`;
    main.before(details);
    details.appendChild(main);
  });
  el('courseMaterialCount').textContent=String(count);
  if(!list.innerHTML.trim()) list.innerHTML='<div class="empty">暂无材料</div>';
  el('courseMaterialArtifacts').hidden=!el('materialInventory').querySelector('.material-row, .extractor-warning');
}

renderSourceStatus=async function(course,refreshJobs=true){
  courseDesktopMount();
  const scope=courseDesktopScope();
  if(!scope.course || scope.course!==course || !courseDesktopScopeCurrent(scope)) return;
  const request=++courseDesktopState.sourceRequest;
  const previous=courseDesktopState.sourcePending || Promise.resolve();
  // The legacy renderer mutates the DOM internally; serialize it and skip superseded work.
  const pending=previous.catch(()=>{}).then(async()=>{
    if(request!==courseDesktopState.sourceRequest || !courseDesktopScopeCurrent(scope)) return;
    await courseDesktopOriginalRenderSourceStatus(course,refreshJobs);
    if(request===courseDesktopState.sourceRequest && courseDesktopScopeCurrent(scope)) courseDesktopCompactSources();
  });
  courseDesktopState.sourcePending=pending;
  try{await pending;}
  finally{if(courseDesktopState.sourcePending===pending) courseDesktopState.sourcePending=null;}
};
