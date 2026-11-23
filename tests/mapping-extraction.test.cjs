const test = require("node:test");
const assert = require("node:assert/strict");
const assistant = require("../web/assets/mapping-assistant.js");

const mappings = "/api/knowledge/ontology/mappings";
const base = "/api/knowledge/ontology/extractions";
const hash = "a".repeat(64);
const clone = value => JSON.parse(JSON.stringify(value));
const tick = () => new Promise(resolve => setImmediate(resolve));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function rule(status = "approved", id = 31) {
  return { id, status, workspace_key: "workpal", mapping_hash: hash, profile_id: "work",
    mapping: { profile_id: "work", branch_keys: ["work.industry_knowledge"], node_types: ["concept"], relation_kinds: ["supports"], field_mappings: { 标题: "name" }, sample_refs: ["evidence:11"] },
    bindings: [{ source_id: 11, evidence_id: 11 }, { source_id: 11, evidence_id: 12 }] };
}
function run(status = "ready", statuses = ["pending", "pending"], extra = {}) {
  const segments = statuses.map((status, index) => ({ id: index + 101, evidence_id: index + 11,
    source_id: 11, start: index * 4000, end: index * 4000 + 100, status, attempts: status === "pending" ? 0 : 1,
    error: status === "failed" ? "模型暂不可用" : "", candidates: [], relations: [] }));
  const coverage = { total: segments.length, pending: 0, running: 0, succeeded: 0, failed: 0, cancelled: 0 };
  statuses.forEach(status => coverage[status]++);
  return { id: 61, workspace_key: "workpal", mapping_version_id: 31, mapping_hash: hash, source_ids: [11], status,
    coverage, segments, candidates: [], relations: [], conflicts: [], ...extra };
}
function runtime(handler = () => undefined, versions = [rule(), rule("approved", 32)]) {
  const context = { workspace: "workpal", epoch: 4, switching: false }, calls = [];
  const request = assistant.createRequest({ fetch: async (url, options) => {
    const parsed = new URL(url, "http://localhost");
    const call = { path: parsed.pathname, query: Object.fromEntries(parsed.searchParams), method: options.method,
      body: options.body ? JSON.parse(options.body) : undefined, signal: options.signal };
    calls.push(call);
    let payload = handler(call);
    if (payload === undefined) {
      if (call.path === `${mappings}/inputs`) payload = {
        sources: [{ id: 11, name: "合成材料", evidence_count: 2 }, { id: 12, name: "另一材料", evidence_count: 1 }],
        profiles: [{ id: "work", display_name: "工作", version: "1" }, { id: "general", display_name: "通用", version: "1" }],
        branches: [{ key: "work.industry_knowledge", display_name: "行业与专业知识" }] };
      else if (call.path === mappings) payload = versions;
      else if (call.path.endsWith("/samples") || call.path.endsWith("/evidence")) payload = {
        samples: [{ ref: "evidence:11", source_id: Number(call.query.source_id || 11), excerpt: "合成证据" }], total: 2 };
      else if (call.path === base) payload = call.method === "GET" ? [] : run();
      else if (call.path === `${base}/61`) payload = run();
      else throw new Error(`Unexpected request: ${call.path}`);
    }
    payload = await payload;
    if (payload?.httpError) return { ok: false, status: payload.httpError.status, json: async () => ({ detail: payload.httpError.detail }) };
    return { ok: true, json: async () => clone(payload) };
  } });
  const controller = assistant.createController({ request, getContext: () => context });
  return { controller, context, calls, state: () => controller.getState(),
    extraction: () => controller.getState().extraction,
    steps: () => calls.filter(call => call.path.endsWith("/step")),
    async open() { await controller.open(); await controller.selectVersion(31); } };
}

test("only approved detail lists durable history; opening and creation never step", async () => {
  const r = runtime();
  await r.open();
  assert.deepEqual(r.calls.find(call => call.path === base).query, { workspace_key: "workpal", mapping_version_id: "31" });
  await r.controller.createExtraction();
  assert.deepEqual(r.calls.find(call => call.path === base && call.method === "POST").body,
    { workspace_key: "workpal", mapping_version_id: 31, mapping_hash: hash, source_ids: [11] });
  assert.equal(r.extraction().run.status, "ready");
  assert.equal(r.steps().length, 0);
  const draft = runtime(() => undefined, [rule("draft")]);
  await draft.open();
  await draft.controller.createExtraction();
  assert.equal(draft.calls.filter(call => call.path.startsWith(base)).length, 0);
});

test("explicit start serializes steps, prevents double scheduling and accepts zero candidates", async () => {
  const pending = deferred();
  let active = 0, maxActive = 0, step = 0;
  const r = runtime(call => {
    if (!call.path.endsWith("/step")) return;
    return (async () => {
      maxActive = Math.max(maxActive, ++active);
      if (++step === 1) await pending.promise;
      active--;
      return step === 1 ? run("running", ["succeeded", "pending"]) : run("completed", ["succeeded", "succeeded"]);
    })();
  });
  await r.open();
  await r.controller.createExtraction();
  const starting = r.controller.startExtraction();
  await r.controller.startExtraction();
  assert.equal(r.steps().length, 1);
  pending.resolve();
  await starting;
  assert.equal(r.steps().length, 2);
  assert.equal(maxActive, 1);
  assert.equal(r.extraction().run.status, "completed");
  assert.deepEqual(r.extraction().run.candidates, []);
  await r.controller.startExtraction();
  assert.equal(r.steps().length, 2);
  assert.ok(r.steps().every(call => JSON.stringify(call.body) === '{"workspace_key":"workpal"}'));
});

test("completed durable history resumes by GET and never restarts the model", async () => {
  const completed = run("completed", ["succeeded", "succeeded"]);
  const r = runtime(call => call.path === base && call.method === "GET" ? [completed] : call.path === `${base}/61` ? completed : undefined);
  await r.open();
  assert.equal(r.extraction().history.length, 1);
  await r.controller.selectExtraction(61);
  await r.controller.startExtraction();
  await r.controller.refreshExtractions();
  assert.equal(r.extraction().run.status, "completed");
  assert.equal(r.steps().length, 0);
  assert.ok(r.calls.some(call => call.path === `${base}/61` && call.query.workspace_key === "workpal"));
});

test("resume partial history skips successful segments; retry only prepares failed segments", async () => {
  let detail = run("partial", ["succeeded", "failed"]);
  const r = runtime(call => {
    if (call.path === base && call.method === "GET") return [detail];
    if (call.path === `${base}/61`) return detail;
    if (call.path.endsWith("/retry")) return detail = run("ready", ["succeeded", "pending"]);
    if (call.path.endsWith("/step")) return detail = run("completed", ["succeeded", "succeeded"]);
  });
  await r.open();
  await r.controller.selectExtraction(61);
  await r.controller.startExtraction();
  assert.equal(r.steps().length, 0);
  await r.controller.retryExtraction();
  assert.equal(r.steps().length, 0, "retry must not call the model");
  assert.equal(r.extraction().run.coverage.succeeded, 1);
  assert.deepEqual(r.calls.find(call => call.path.endsWith("/retry")).body, { workspace_key: "workpal" });
  await r.controller.startExtraction();
  assert.equal(r.steps().length, 1);
});

for (const change of ["close", "workspace", "epoch", "source", "profile", "version", "hash"]) {
  test(`late step cannot update or schedule after ${change} changes`, async () => {
    const pending = deferred();
    const r = runtime(call => call.path.endsWith("/step") ? pending.promise : undefined);
    await r.open();
    await r.controller.createExtraction();
    const starting = r.controller.startExtraction();
    if (change === "close") { r.controller.close(); await r.controller.open(); }
    if (change === "workspace") r.context.workspace = "lifepal";
    if (change === "epoch") r.context.epoch++;
    if (change === "source") await r.controller.setSource(12);
    if (change === "profile") await r.controller.setProfile("general");
    if (change === "version") await r.controller.selectVersion(32);
    if (change === "hash") r.state().version.mapping_hash = "b".repeat(64);
    pending.resolve(run("running", ["succeeded", "pending"]));
    await starting;
    assert.equal(r.steps().length, 1);
    assert.notEqual(r.extraction().run?.coverage.succeeded, 1);
    assert.equal(r.extraction().busy, "");
    assert.equal(r.extraction().running, false);
  });
}

test("changed rule hash cannot send an existing run to step, retry or cancel", async () => {
  for (const action of ["startExtraction", "retryExtraction", "cancelExtraction"]) {
    const r = runtime(call => call.path === base && call.method === "GET" ? [run("partial", ["pending", "failed"])] : undefined);
    await r.open();
    r.state().version.mapping_hash = "b".repeat(64);
    await r.controller[action]();
    assert.equal(r.calls.filter(call => /\/(step|retry|cancel)$/.test(call.path)).length, 0, action);
    assert.ok(r.extraction().blocked, action);
    assert.ok(r.extraction().error, action);
    assert.equal(r.extraction().busy, "");
  }
});

test("HTTP conflict stops stepping until an explicit refresh and does not auto retry", async () => {
  const r = runtime(call => call.path.endsWith("/step") ? { httpError: { status: 409, detail: "rule changed" } } : undefined);
  await r.open(); await r.controller.createExtraction();
  await r.controller.startExtraction();
  await r.controller.startExtraction();
  await r.controller.retryExtraction();
  assert.equal(r.steps().length, 1);
  assert.ok(r.extraction().blocked);
  assert.match(r.extraction().error, /规则版本已变化/);
  await r.controller.refreshExtractions();
  assert.equal(r.extraction().blocked, false);
  assert.equal(r.steps().length, 1);
});

test("invalid candidate or segment objects are rejected before entering the view", async () => {
  for (const extra of [{ candidates: [null] }, { candidates: ["invalid"] }, { segments: [null, null] }]) {
    const r = runtime(call => call.path === base && call.method === "GET" ? [run("ready", undefined, extra)] : undefined);
    await r.open();
    assert.equal(r.extraction().run, null);
    assert.ok(r.extraction().error);
    assert.equal(r.extraction().busy, "");
  }
});

test("refresh rejects an empty selected-detail response even when history is valid", async () => {
  const r = runtime(call => call.path === base && call.method === "GET" ? [run()] : call.path === `${base}/61` ? null : undefined);
  await r.open();
  await r.controller.refreshExtractions();
  assert.ok(r.extraction().blocked);
  assert.match(r.extraction().error, /范围不一致/);
  assert.equal(r.extraction().busy, "");
});

test("cancel stays usable during step and wins either response order with its own request", async () => {
  for (const stepFirst of [true, false]) {
    const step = deferred(), cancel = deferred();
    const r = runtime(call => call.path.endsWith("/step") ? step.promise : call.path.endsWith("/cancel") ? cancel.promise : undefined);
    await r.open();
    await r.controller.createExtraction();
    const starting = r.controller.startExtraction();
    const cancelling = r.controller.cancelExtraction();
    const call = r.calls.find(call => call.path.endsWith("/cancel"));
    assert.ok(call, "cancel must be sent while step is pending");
    assert.deepEqual(call.body, { workspace_key: "workpal" });
    assert.notEqual(call.signal, r.steps()[0].signal);
    assert.ok(!call.signal?.aborted);
    if (stepFirst) { step.resolve(run("running", ["succeeded", "pending"])); await starting; }
    cancel.resolve(run("cancelled", ["cancelled", "cancelled"]));
    await cancelling;
    if (!stepFirst) { step.resolve(run("running", ["succeeded", "pending"])); await starting; }
    assert.equal(r.extraction().run.status, "cancelled");
    assert.equal(r.steps().length, 1);
    assert.equal(r.extraction().busy, "");
    assert.equal(r.extraction().running, false);
  }
});

test("failed cancel still stops scheduling and cannot restart without a refresh", async () => {
  const pending = deferred();
  const r = runtime(call => call.path.endsWith("/step") ? pending.promise : call.path.endsWith("/cancel") ? Promise.reject(new Error("network")) : undefined);
  await r.open(); await r.controller.createExtraction();
  const starting = r.controller.startExtraction();
  await r.controller.cancelExtraction();
  pending.resolve(run("running", ["succeeded", "pending"])); await starting;
  await r.controller.startExtraction();
  assert.equal(r.steps().length, 1);
  assert.ok(r.extraction().error);
});

for (const patch of [{ workspace_key: "lifepal" }, { mapping_version_id: 32 }, { mapping_hash: "b".repeat(64) }, { source_ids: [11, 12] }, { source_ids: [] }, { source_ids: [11, 11] }]) {
  test(`cross-scope response rejected on every extraction endpoint: ${JSON.stringify(patch)}`, async () => {
    for (const endpoint of ["list", "create", "read", "step", "retry", "cancel"]) {
      let active = false;
      const good = run("partial", ["pending", "failed"]);
      const r = runtime(call => {
        const endpointPath = { list: base, create: base, read: `${base}/61`, step: `${base}/61/step`, retry: `${base}/61/retry`, cancel: `${base}/61/cancel` }[endpoint];
        if (active && call.path === endpointPath && (endpoint !== "list" || call.method === "GET")) return endpoint === "list" ? [run("ready", undefined, patch)] : run("ready", undefined, patch);
        if (call.path === base) return call.method === "GET" ? [good] : good;
        if (call.path === `${base}/61`) return good;
      });
      await r.open();
      active = true;
      await ({ list: () => r.controller.refreshExtractions(), create: () => r.controller.createExtraction(), read: () => r.controller.selectExtraction(61), step: () => r.controller.startExtraction(), retry: () => r.controller.retryExtraction(), cancel: () => r.controller.cancelExtraction() }[endpoint])();
      assert.ok(r.extraction().error, endpoint);
      assert.notDeepEqual(r.extraction().run, run("ready", undefined, patch), endpoint);
      assert.ok(r.steps().length <= 1);
    }
  });
}

test("stale history and creates cannot repopulate another approved rule", async () => {
  for (const endpoint of ["list", "create"]) {
    const pending = deferred();
    let hold = endpoint === "list";
    const r = runtime(call => call.path === base && hold && (endpoint === "list" ? call.method === "GET" && call.query.mapping_version_id === "31" : call.method === "POST") ? pending.promise : undefined);
    const opening = r.open();
    if (endpoint === "list") await tick(); else await opening;
    hold = true;
    const creating = endpoint === "create" ? r.controller.createExtraction() : opening;
    await r.controller.selectVersion(32);
    pending.resolve(endpoint === "list" ? [run()] : run());
    await creating;
    assert.equal(r.state().version.id, 32);
    assert.deepEqual(r.extraction().history, []);
    assert.equal(r.extraction().run, null);
  }
});

test("running leases, conflicts, and unchanged step progress never spin the loop", async () => {
  for (const detail of [run("running", ["running", "pending"]), run("conflict"), run()]) {
    const r = runtime(call => call.path === base && call.method === "GET" ? [detail] : call.path.endsWith("/step") ? detail : undefined);
    await r.open();
    await r.controller.startExtraction();
    assert.equal(r.steps().length, detail.status === "ready" ? 1 : 0);
    assert.equal(r.extraction().running, false);
  }
});

class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.attrs = {}; this.listeners = {}; this.value = ""; this._text = ""; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(""); }
  set innerHTML(_value) { throw new Error("Only text nodes may contain API data"); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; this._text = ""; }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  showModal() { this.open = true; }
  close() { this.open = false; this.listeners.close?.(); }
}
function descendants(element) { return [element, ...element.children.flatMap(descendants)]; }
function readableText(element) {
  if (element.hidden || element.className === "ma-technical") return "";
  return element._text + element.children.map(readableText).join("");
}

test("conflicts explain backend kinds and retain full metadata only in collapsed technical information", async () => {
  const name = '<img src=x onerror="pwned=1">';
  const detail = run("conflict", ["succeeded", "succeeded"], {
    candidates: [{ id: "candidate:" + hash, name }, { id: "candidate:" + "b".repeat(64), name }],
    conflicts: [
      { kind: "same_name_variants", name, candidate_ids: ["candidate:" + hash, "candidate:" + "b".repeat(64)] },
      { kind: "existing_kp", name, existing_id: 987654, candidate_ids: ["candidate:" + hash] },
      { kind: "existing_candidate", name, existing_id: 876543, candidate_ids: ["candidate:" + hash] },
      { kind: "collapsed_relation", candidate_ids: ["candidate:" + hash] },
      { kind: "stale_contract", message: "Mapping changed /private/provider-secret" },
      { kind: "future_kind", message: "untrusted internal details", name },
    ], future_metadata: { digest: hash } });
  const r = runtime(call => call.path === base && call.method === "GET" ? [detail] : undefined);
  const document = { createElement: tag => new Element(tag), body: new Element("body") };
  const view = assistant.createView({ document, controller: r.controller });
  await r.open();
  const all = descendants(view.dialog), readable = readableText(view.dialog);
  for (const sentence of [
    `同名知识有不同表述：${name}（2 个版本），请核对来源与条件后处理`,
    `与图谱中已有知识同名：${name}，请核对内容、来源与条件后处理`,
    `与已有候选知识同名：${name}，请核对内容、来源与条件后处理`,
    "关系两端归并为同一知识，请核对原文后处理",
    "资料、领域配置或整理规则已变化，请重新创建任务并核对后再抽取",
    "发现未识别的抽取冲突，请核对来源与条件后处理",
  ]) assert.ok(readable.includes(sentence), sentence);
  for (const raw of [hash, "987654", "876543", "same_name_variants", "future_kind", "/private/provider-secret", "untrusted internal details"]) assert.ok(!readable.includes(raw), raw);
  const technical = all.filter(element => element.className === "ma-technical");
  assert.equal(technical.length, 1);
  assert.equal(technical[0].tagName, "details");
  assert.ok(!technical[0].open && !Object.hasOwn(technical[0].attrs, "open"));
  assert.equal(technical[0].children[0].textContent, "技术信息");
  assert.deepEqual(JSON.parse(technical[0].children.find(element => element.tagName === "pre").textContent), detail);
  assert.ok(!all.some(element => ["img", "script", "svg"].includes(element.tagName)));
});

test("relations resolve candidate names and translate kinds with readable evidence and safe quotes", async () => {
  const attack = '<svg onload="pwned=1">';
  const refs = [
    { source_id: 11, evidence_id: 11, segment_id: 101, start: 0, end: 100, quote: attack, source_sha256: hash },
    { source_id: 11, evidence_id: 12, segment_id: 102, start: 4000, end: 4100, quote: "另一段原文", mapping_hash: hash },
  ];
  const detail = run("completed", ["succeeded", "succeeded"], {
    candidates: [{ id: "candidate:" + hash, name: "需求分析", evidence_refs: refs, chapters: ["第一章", "第二章"] }, { id: "candidate:" + "b".repeat(64), name: attack }],
    relations: [
      { from_id: "candidate:" + hash, to_id: "candidate:" + "b".repeat(64), kind: "supports", evidence_refs: refs },
      { from_id: "missing:" + hash, to_id: "missing:" + hash, kind: "new_relation", evidence_refs: [] },
    ],
  });
  const r = runtime(call => call.path === base && call.method === "GET" ? [detail] : undefined);
  const document = { createElement: tag => new Element(tag), body: new Element("body") };
  const view = assistant.createView({ document, controller: r.controller });
  await r.open();
  const all = descendants(view.dialog), readable = readableText(view.dialog);
  for (const value of [`需求分析 → 支持 → ${attack} · 2 条证据`, "未找到对应候选 → 未登记关系 → 未找到对应候选 · 0 条证据", "来源：合成材料", "证据 1 · 字符位置 0–100", "证据 2 · 字符位置 4000–4100", attack, "另一段原文", "第一章、第二章"]) assert.ok(readable.includes(value), value);
  for (const raw of [hash, "source_sha256", "evidence_refs", "candidate:", "missing:", "new_relation"]) assert.ok(!readable.includes(raw), raw);
  assert.ok(!all.some(element => ["img", "script", "svg", "a"].includes(element.tagName)));
  assert.equal(r.steps().length, 0);
});

test("cancel button stays enabled while the step request is busy", async () => {
  const pending = deferred();
  const r = runtime(call => call.path.endsWith("/step") ? pending.promise : call.path.endsWith("/cancel") ? run("cancelled", ["cancelled", "cancelled"]) : undefined);
  const document = { createElement: tag => new Element(tag), body: new Element("body") };
  const view = assistant.createView({ document, controller: r.controller });
  await r.open(); await r.controller.createExtraction();
  const starting = r.controller.startExtraction();
  const actions = descendants(view.dialog).find(element => element.className === "ma-extraction-actions");
  const cancel = actions.children.find(element => element.textContent === "取消");
  const start = actions.children.find(element => element.textContent === "开始抽取");
  assert.equal(cancel.disabled, false);
  assert.equal(start.disabled, true);
  await cancel.listeners.click();
  pending.resolve(run("running", ["succeeded", "pending"]));
  await starting;
  assert.equal(r.extraction().run.status, "cancelled");
  assert.equal(r.steps().length, 1);
});

test("approved panel safely previews candidate fields, provenance, conflicts and indexed coverage with fixed actions", async () => {
  const attack = '<img src=x onerror="pwned=1">';
  const detail = run("partial", ["succeeded", "failed"], {
    candidates: [{ name: attack, kp_type: "concept", branch_keys: ["work.industry_knowledge"], gist: "摘要", detail: "正文", chapter: "章节", quote: "原文", conditions: "条件", valid_from: "2026-01-01", valid_to: "2026-12-31", host_refs: [{ evidence_id: 11, segment_id: 101, start: 0, end: 100 }] }],
    conflicts: [{ name: attack, reason: "同名不同声明" }] });
  const r = runtime(call => call.path === base && call.method === "GET" ? [detail] : undefined);
  const document = { createElement: tag => new Element(tag), body: new Element("body") };
  const view = assistant.createView({ document, controller: r.controller });
  await r.open();
  const all = descendants(view.dialog), content = view.dialog.textContent;
  assert.ok(content.includes(attack));
  for (const value of ["总段数 2", "成功 1", "失败 1", "待处理 0", "已索引", "fast", "未写入图谱", "条件", "2026-12-31", "evidence", "11", "0–100", "同名不同声明"]) assert.ok(content.includes(value), value);
  const footer = all.find(element => element.tagName === "footer");
  for (const value of ["创建抽取任务", "开始抽取", "取消", "重试失败段", "刷新进度"]) assert.ok(descendants(footer).some(element => element.tagName === "button" && element.textContent === value), value);
  assert.ok(!all.some(element => element.tagName === "img"));
  assert.ok(!all.some(element => element.tagName === "button" && /发布|确认候选/.test(element.textContent)));
});
