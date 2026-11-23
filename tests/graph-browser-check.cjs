// Read-only acceptance against the running app. No API responses are mocked.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let chromium;
for (const moduleName of [process.env.PLAYWRIGHT_MODULE, '@playwright/test', 'playwright'].filter(Boolean)) {
  try { ({chromium}=require(moduleName)); break; } catch {}
}
if (!chromium) {
  console.error('缺少 Playwright：在仓库根目录执行 npm install 后重试（或设置 PLAYWRIGHT_MODULE 指向已安装模块）。');
  process.exit(2);
}

(async()=>{
  const output=path.join(__dirname,'../output/graph-desktop-20261016');
  fs.mkdirSync(output,{recursive:true});
  const browser=await chromium.launch({headless:true,channel:'chrome'});
  const page=await browser.newPage({viewport:{width:1366,height:900}});
  const errors=[],requests=[],report={url:process.env.GRAPH_CHECK_URL||'http://127.0.0.1:8899/',viewports:[]};
  page.on('pageerror',error=>errors.push(error.message));
  page.on('request',request=>{if(new URL(request.url()).pathname.startsWith('/api/')) requests.push({method:request.method(),url:request.url()});});
  await page.route('**/api/**',route=>route.request().method()==='GET'?route.continue():route.abort('blockedbyclient'));
  try {
    await page.goto(report.url);
    await page.waitForFunction(()=>S.courses.length>0);
    await page.waitForTimeout(500);
    await page.evaluate(async()=>{await switchWorkspace('studypal'); go('graph');});
    await page.waitForFunction(()=>S.workspace==='studypal' && S.courses.some(course=>course.kp_count>100));
    await page.waitForFunction(()=>S.graphCy && S.graph?.nodes?.length);
    await page.evaluate(async()=>{const course=[...S.courses].sort((a,b)=>b.kp_count-a.kp_count)[0];S.graphCourse=course.name;await renderGraph();});
    await page.waitForFunction(()=>S.graphCy && S.graph.nodes.length>100);
    report.graph=await page.evaluate(()=>({course:S.graphCourse,workspace:S.workspace,nodes:S.graph.nodes.length,edges:S.graph.edges.length,chapters:S.graph.chapters.length,renderedNodes:S.graphCy.nodes('.knowledge').length,renderedEdges:S.graphCy.edges().length,navParent:el('graphNav').parentElement.id}));
    assert.equal(report.graph.nodes,report.graph.renderedNodes);
    assert.equal(report.graph.edges,report.graph.renderedEdges);
    assert.equal(report.graph.navParent,'contextNav');
    for(const width of [1280,1366,1440]){
      await page.setViewportSize({width,height:900});
      await page.locator('#graphFit').click();
      await page.waitForTimeout(150);
      const dimensions=await page.evaluate(()=>({width:innerWidth,documentWidth:document.documentElement.scrollWidth,box:el('graphBox').getBoundingClientRect().toJSON(),detailHidden:el('graphDetailPanel').hidden,canvasCount:el('graphBox').querySelectorAll('canvas').length}));
      assert.ok(dimensions.documentWidth<=width);
      assert.ok(dimensions.box.height>600);
      assert.equal(dimensions.detailHidden,true);
      assert.ok(dimensions.canvasCount>0);
      report.viewports.push(dimensions);
      await page.screenshot({path:path.join(output,`overview-${width}.png`)});
    }
    const sameIds=()=>page.evaluate(()=>({tree:[...el('graphNav').querySelectorAll('[data-graph-tree-id]')].map(node=>+node.dataset.graphTreeId).sort((a,b)=>a-b),canvas:S.graphCy?.nodes('.knowledge').map(node=>node.data('kpId')).sort((a,b)=>a-b)||[]}));
    let ids=await sameIds();assert.deepEqual(ids.tree,ids.canvas);
    const chapter=page.locator('#graphNav [data-graph-chapter]').first();
    await chapter.click();
    await page.waitForFunction(()=>S.graphFocus.kind==='chapter');
    ids=await sameIds();assert.deepEqual(ids.tree,ids.canvas);
    report.chapter={nodes:ids.canvas.length};
    await page.screenshot({path:path.join(output,'chapter-focus.png')});
    await page.locator('#graphNav [data-graph-tree-id]:visible').first().click();
    await page.waitForFunction(()=>S.selectedKp && !el('graphDetailPanel').hidden);
    await page.waitForFunction(()=>!el('graphTraversalBox').textContent.includes('正在读取'));
    report.focus=await page.evaluate(()=>({id:S.selectedKp,depth:S.graphFocus.depth,nodes:S.graphCy.nodes('.knowledge').length,center:S.graphCy.getElementById(`n${S.selectedKp}`).renderedPosition(),canvas:{width:S.graphCy.width(),height:S.graphCy.height()},evidence:el('graphDetail').textContent.includes('溯源'),mastery:Boolean(el('graphMasterySelect'))}));
    assert.equal(report.focus.evidence,true);assert.equal(report.focus.mastery,true);
    assert.ok(Math.abs(report.focus.center.x-report.focus.canvas.width/2)<5);
    await page.screenshot({path:path.join(output,'node-focus.png')});
    await page.locator('[data-graph-depth="1"]').click();
    await page.waitForFunction(()=>S.graphFocus.depth===1);
    const direct=await page.evaluate(()=>S.graphCy.nodes('.knowledge').length);
    await page.locator('[data-graph-depth="2"]').click();
    await page.waitForFunction(()=>S.graphFocus.depth===2);
    assert.ok(await page.evaluate(()=>S.graphCy.nodes('.knowledge').length)>=direct);
    await page.locator('#graphDetailClose').click();
    assert.equal(await page.locator('#graphDetailPanel').isVisible(),false);
    await page.locator('#graphBack').click();
    await page.locator('#graphReset').click();
    await page.locator('#graphNavSearch').fill('会计');
    await page.waitForTimeout(450);
    ids=await sameIds();assert.deepEqual(ids.tree,ids.canvas);assert.ok(ids.canvas.length>0);
    report.search={matches:ids.canvas.length};
    await page.locator('#graphNavSearch').fill('graph-no-match-20261016');
    await page.waitForTimeout(400);
    assert.equal(await page.evaluate(()=>S.graphCy),null);
    assert.match(await page.locator('#graphBox').textContent(),/没有匹配/);
    await page.locator('#graphNavSearch').fill('');
    await page.waitForFunction(()=>S.graphCy && !el('graphBox')._graphFitPending && !el('graphBox')._graphFitScheduled);
    const viewport=()=>page.evaluate(()=>({zoom:S.graphCy.zoom(),pan:S.graphCy.pan()}));
    const before=await viewport(),box=await page.locator('#graphBox').boundingBox();
    await page.mouse.move(box.x+box.width/2,box.y+box.height/2);
    await page.mouse.wheel(15,80);await page.waitForTimeout(150);
    const after=await viewport();assert.equal(before.zoom,after.zoom);assert.notEqual(before.pan.y,after.pan.y);
    await page.keyboard.down('Control');await page.mouse.wheel(0,-80);await page.keyboard.up('Control');await page.waitForTimeout(150);
    assert.ok((await viewport()).zoom>after.zoom);
    const buttonBefore=(await viewport()).zoom;await page.locator('#graphZoomIn').click();assert.ok((await viewport()).zoom>buttonBefore);
    await page.locator('#graphGovernance>summary').click();
    await page.locator('#relationCandidateCard>summary').click();
    await page.waitForFunction(()=>el('relationCandidateBox').textContent.trim().length>0);
    report.candidates={text:await page.locator('#relationCandidateCount').textContent()};
    await page.locator('#graphGovernance>summary').click();
    const emptyCourse=await page.evaluate(()=>S.courses.find(course=>!course.kp_count)?.name);
    if(emptyCourse){await page.evaluate(async course=>{S.graphCourse=course;await renderGraph();},emptyCourse);assert.equal(await page.evaluate(()=>S.graphCy),null);assert.equal(await page.locator('#graphDetailPanel').isVisible(),false);report.emptyCourse=true;}
    report.errors=errors;report.readOnly=requests.every(request=>request.method==='GET');
    assert.deepEqual(errors,[]);assert.equal(report.readOnly,true);
    fs.writeFileSync(path.join(output,'browser-report.json'),JSON.stringify(report,null,2));
    console.log(JSON.stringify(report,null,2));
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
