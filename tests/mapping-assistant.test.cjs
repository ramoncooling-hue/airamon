const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const asset = path.join(__dirname, "../web/assets/mapping-assistant.js");
test("mapping assistant exposes the real controller and DOM view", () => {
  assert.ok(fs.existsSync(asset), "the toolbar assistant implementation must exist");
});
const assistant = fs.existsSync(asset) ? require(asset) : {};
const base = "/api/knowledge/ontology/mappings";
const inputs = {
  sources: [{ id: 11, name: "合成资料甲", evidence_count: 2 }, { id: 12, name: "合成资料乙", evidence_count: 1 }],
  profiles: [
    { id: "work", display_name: "工作领域", version: "1.0.0", node_types: ["concept"], relation_kinds: ["supports"] },
    { id: "general", display_name: "通用领域", version: "1.0.0", node_types: ["concept"], relation_kinds: [] },
  ],
  branches: [{ key: "work.industry_knowledge", display_name: "行业与专业知识", parent_key: "work" }],
  truncated: false,
};
function samples(sourceId = 11) {
  return { samples: [{ ref: `evidence:${sourceId}`, source_id: sourceId, source_name: "合成资料", excerpt: "仅用于测试的样本", locator_type: "page", locator_start: "2", locator_end: "3" }], total: 6, selected: 1, truncated: true };
}
function version(status = "draft", id = 31, workspace = "workpal") {
  return {
    id, workspace_key: workspace, mapping_hash: "a".repeat(64), status, profile_id: "work", base_version_id: null,
    mapping: { profile_id: "work", branch_keys: ["work.industry_knowledge"], node_types: ["concept"], relation_kinds: ["supports"], field_mappings: { 标题: "name", 摘要: "gist" }, sample_refs: ["evidence:11"] },
    bindings: [{ source_id: 11, evidence_id: 11 }],
  };
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function runtime(handler = () => undefined) {
  const context = { workspace: "workpal", epoch: 4, switching: false };
  const calls = [];
  const controller = assistant.createController({
    getContext: () => context,
    request: async (url, options = {}) => {
      calls.push({ url, ...options });
      const override = handler(url, options);
      if (override !== undefined) return override;
      if (url === `${base}/inputs`) return JSON.parse(JSON.stringify(inputs));
      if (url === base) return [version("pending_review")];
      if (url === `${base}/samples`) return samples(Number(options.query.source_id));
      if (url.endsWith("/evidence")) return samples();
      if (url.endsWith("/suggest")) return version();
      if (url.endsWith("/submit")) return version("pending_review");
      if (url.endsWith("/approve")) return version("approved");
      if (url.endsWith("/reject")) return version("rejected");
      throw new Error(`Unexpected request: ${url}`);
    },
  });
  return { controller, context, calls, state: () => controller.getState(), posts: () => calls.filter(call => call.method === "POST") };
}

test("generation saves one draft, with a numeric source and no automatic submission", async () => {
  const r = runtime();
  await r.controller.open();
  r.controller.setPurpose("  整理行业概念  ");
  await r.controller.generate();
  assert.equal(r.state().version.status, "draft");
  assert.equal(r.state().evidenceReady, true);
  assert.equal(r.posts().length, 1);
  assert.deepEqual(r.posts()[0].body, { workspace_key: "workpal", source_id: 11, profile_id: "work", purpose: "整理行业概念" });
  assert.ok(r.calls.some(call => call.url === `${base}/31/evidence`), "review uses bound version evidence");
});

test("submission and approval are separate requests and explicit boolean confirmation is mandatory", async () => {
  const r = runtime();
  await r.controller.open();
  r.controller.setPurpose("整理");
  await r.controller.generate();
  await r.controller.review("approve");
  assert.equal(r.posts().length, 1, "cannot approve a draft");
  await r.controller.review("submit");
  assert.equal(r.state().version.status, "pending_review");
  await r.controller.review("approve");
  assert.equal(r.posts().length, 2, "unchecked approval cannot send");
  r.controller.setConfirmed("true");
  await r.controller.review("approve");
  assert.equal(r.posts().length, 2, "truthy strings are not human confirmation");
  r.controller.setConfirmed(true);
  r.controller.setNote("核对过样本");
  await r.controller.review("approve");
  assert.deepEqual(r.posts()[2].body, { workspace_key: "workpal", expected_hash: "a".repeat(64), confirmed: true, note: "核对过样本" });
  assert.equal(r.state().version.status, "approved");
  assert.match(r.state().message, /已确认整理规则.*未写入图谱/);
  assert.equal(r.state().confirmed, false);
});

test("pending history can be rejected with an optional note even if evidence is stale", async () => {
  const r = runtime(url => url.endsWith("/evidence") ? Promise.reject(Object.assign(new Error("stale"), { status: 409 })) : undefined);
  await r.controller.open();
  await r.controller.selectVersion(31);
  assert.equal(r.state().evidenceReady, false);
  await r.controller.review("approve");
  assert.equal(r.posts().length, 0);
  r.controller.setNote("  来源已更新  ");
  await r.controller.review("reject");
  assert.deepEqual(r.posts()[0].body, { workspace_key: "workpal", expected_hash: "a".repeat(64), note: "来源已更新" });
  assert.equal(r.state().version.status, "rejected");
});

test("double clicks only send one generation or review while busy", async () => {
  const suggestion = deferred(), approval = deferred();
  const r = runtime(url => url.endsWith("/suggest") ? suggestion.promise : url.endsWith("/approve") ? approval.promise : undefined);
  await r.controller.open();
  r.controller.setPurpose("整理");
  const first = r.controller.generate();
  await r.controller.generate();
  assert.equal(r.posts().length, 1);
  assert.equal(r.state().busy, "suggest");
  suggestion.resolve(version());
  await first;
  await r.controller.review("submit");
  r.controller.setConfirmed(true);
  const approve = r.controller.review("approve");
  await r.controller.review("approve");
  assert.equal(r.posts().filter(call => call.url.endsWith("/approve")).length, 1);
  approval.resolve(version("approved"));
  await approve;
});

for (const change of ["source", "profile", "close", "workspace", "epoch"]) {
  test(`late generation is discarded after ${change} changes`, async () => {
    const pending = deferred();
    const r = runtime(url => url.endsWith("/suggest") ? pending.promise : undefined);
    await r.controller.open();
    r.controller.setPurpose("原用途");
    const work = r.controller.generate();
    if (change === "source") await r.controller.setSource(12);
    if (change === "profile") await r.controller.setProfile("general");
    if (change === "close") { r.controller.close(); await r.controller.open(); }
    if (change === "workspace") r.context.workspace = "lifepal";
    if (change === "epoch") r.context.epoch++;
    pending.resolve(version());
    await work;
    assert.equal(r.state().version, null);
    assert.equal(r.state().confirmed, false);
    if (["workspace", "epoch"].includes(change)) assert.equal(r.state().open, false);
  });
}

test("out-of-order samples cannot overwrite a newer source or its busy state", async () => {
  const pending = deferred();
  const r = runtime((url, options) => url.endsWith("/samples") && options.query.source_id === 11 ? pending.promise : undefined);
  const opening = r.controller.open();
  await new Promise(resolve => setImmediate(resolve));
  await r.controller.setSource(12);
  pending.resolve(samples(11));
  await opening;
  assert.equal(r.state().sourceId, 12);
  assert.equal(r.state().samples.samples[0].source_id, 12);
  assert.equal(r.state().busy, "");
});

test("late input and history responses cannot repopulate a closed/reopened dialog", async () => {
  const oldInputs = deferred(), oldHistory = deferred();
  let count = 0;
  const r = runtime(url => {
    if (url === `${base}/inputs` && count++ === 0) return oldInputs.promise;
    if (url === base && count === 1) return oldHistory.promise;
  });
  const opening = r.controller.open();
  r.controller.close();
  await r.controller.open();
  oldInputs.resolve({ ...inputs, sources: [{ id: 999, name: "旧工作区", evidence_count: 1 }] });
  oldHistory.resolve([version("approved", 999)]);
  await opening;
  assert.equal(r.state().sourceId, 11);
  assert.ok(!r.state().history.some(row => row.id === 999));
});

test("old evidence cannot approve a different history version; changing versions resets confirmation", async () => {
  const pending = deferred();
  const r = runtime(url => url === base ? [version("pending_review"), version("pending_review", 32)] : url === `${base}/31/evidence` ? pending.promise : undefined);
  await r.controller.open();
  const old = r.controller.selectVersion(31);
  await r.controller.selectVersion(32);
  r.controller.setConfirmed(true);
  pending.resolve(samples());
  await old;
  assert.equal(r.state().version.id, 32);
  await r.controller.review("approve");
  assert.equal(r.posts()[0].url, `${base}/32/approve`);
});

test("historical versions with more than eight bindings accept a verified evidence subset", async () => {
  const row = version("pending_review");
  row.mapping.sample_refs = Array.from({ length: 12 }, (_, index) => `evidence:${index + 101}`);
  row.bindings = row.mapping.sample_refs.map(ref => ({ source_id: 11, evidence_id: Number(ref.split(":")[1]) }));
  const preview = {
    samples: row.mapping.sample_refs.slice(0, 8).map(ref => ({ ...samples().samples[0], ref })),
    total: 12, selected: 8, truncated: true,
  };
  const r = runtime(url => url === base ? [row] : url.endsWith("/evidence") ? preview : undefined);
  await r.controller.open();
  await r.controller.selectVersion(31);
  assert.equal(r.state().evidenceReady, true, "an eight-sample preview is not a full-binding equality check");
  assert.equal(r.state().samples.selected, 8);
  assert.equal(r.state().samples.total, 12);
  assert.equal(r.state().error, "");
});

test("partial history evidence still rejects an unbound or duplicate reference", async () => {
  for (const refs of [["evidence:999"], ["evidence:11", "evidence:11"]]) {
    const r = runtime(url => url.endsWith("/evidence") ? {
      samples: refs.map(ref => ({ ...samples().samples[0], ref })), total: 20, selected: refs.length, truncated: true,
    } : undefined);
    await r.controller.open();
    await r.controller.selectVersion(31);
    r.controller.setConfirmed(true);
    await r.controller.review("approve");
    assert.equal(r.state().evidenceReady, false);
    assert.equal(r.posts().length, 0);
  }
});

test("late rejected source samples cannot clear or overwrite the next source preview", async () => {
  const old = deferred();
  const r = runtime((url, options) => url.endsWith("/samples") && options.query.source_id === 11 ? old.promise : undefined);
  const opening = r.controller.open();
  await new Promise(resolve => setImmediate(resolve));
  await r.controller.setSource(12);
  old.reject(new Error("旧资料读取失败"));
  await opening;
  assert.equal(r.state().samples.samples[0].source_id, 12);
  assert.equal(r.state().error, "");
  assert.equal(r.state().busy, "");
});

test("workspace changes block approval synchronously, even before the UI observer runs", async () => {
  const r = runtime();
  await r.controller.open();
  await r.controller.selectVersion(31);
  r.controller.setConfirmed(true);
  r.context.workspace = "lifepal";
  await r.controller.review("approve");
  assert.equal(r.posts().length, 0);
  assert.equal(r.state().open, false);
});

test("foreign-workspace history rows are never eligible for review", async () => {
  const r = runtime(url => url === base ? [version("pending_review", 31, "lifepal")] : undefined);
  await r.controller.open();
  await r.controller.selectVersion(31);
  r.controller.setConfirmed(true);
  await r.controller.review("approve");
  assert.equal(r.posts().length, 0);
  assert.equal(r.state().version, null);
});

test("a late review response cannot replace the next source's state", async () => {
  const pending = deferred();
  const r = runtime(url => url.endsWith("/approve") ? pending.promise : undefined);
  await r.controller.open();
  await r.controller.selectVersion(31);
  r.controller.setConfirmed(true);
  const work = r.controller.review("approve");
  await r.controller.setSource(12);
  pending.resolve(version("approved"));
  await work;
  assert.equal(r.state().version, null);
  assert.equal(r.state().confirmed, false);
  assert.equal(r.state().busy, "");
});

test("missing samples block generation and model errors do not fabricate drafts", async () => {
  const empty = runtime(url => url.endsWith("/samples") ? { samples: [], total: 0, selected: 0, truncated: false } : undefined);
  await empty.controller.open();
  empty.controller.setPurpose("整理");
  await empty.controller.generate();
  assert.equal(empty.posts().length, 0);
  assert.match(empty.state().error, /样本/);
  const r = runtime(url => url.endsWith("/suggest") ? Promise.reject(Object.assign(new Error("No model configured"), { status: 503 })) : undefined);
  await r.controller.open();
  r.controller.setPurpose("整理");
  await r.controller.generate();
  assert.equal(r.state().version, null);
  assert.equal(r.state().busy, "");
  assert.match(r.state().error, /模型.*设置|设置.*模型/);
});

test("conflicts invalidate confirmation and block retries until evidence/history are refreshed", async () => {
  const r = runtime(url => url.endsWith("/approve") ? Promise.reject(Object.assign(new Error("changed"), { status: 409 })) : undefined);
  await r.controller.open();
  await r.controller.selectVersion(31);
  r.controller.setConfirmed(true);
  await r.controller.review("approve");
  assert.equal(r.state().confirmed, false);
  assert.equal(r.state().evidenceReady, false);
  assert.match(r.state().error, /刷新/);
  r.controller.setConfirmed(true);
  await r.controller.review("approve");
  assert.equal(r.posts().length, 1);
});

test("editing purpose requires a new draft and never rewrites the historical version", async () => {
  const r = runtime();
  await r.controller.open();
  await r.controller.selectVersion(31);
  r.controller.setConfirmed(true);
  r.controller.setPurpose("换一种整理用途");
  await r.controller.review("approve");
  assert.equal(r.posts().length, 0);
  assert.equal(r.state().confirmed, false);
  assert.equal(r.state().version.status, "pending_review");
});

test("fetch adapter sends same-origin session credentials, query scope and structured errors", async () => {
  const calls = [];
  const request = assistant.createRequest({ base: "", fetch: async (url, options) => {
    calls.push({ url, options });
    return { ok: calls.length === 1, status: 409, json: async () => calls.length === 1 ? inputs : { detail: "changed" } };
  } });
  await request(`${base}/inputs`, { query: { workspace_key: "work space" } });
  assert.equal(calls[0].url, `${base}/inputs?workspace_key=work+space`);
  assert.equal(calls[0].options.credentials, "same-origin");
  await assert.rejects(request(`${base}/31/approve`, { method: "POST", body: { confirmed: true } }), error => error.status === 409);
  assert.equal(calls[1].options.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(calls[1].options.body), { confirmed: true });
});

test("API roots and API-prefixed bases compose with /api routes exactly once", async () => {
  for (const apiBase of ["", "/api", "http://127.0.0.1:8899", "http://127.0.0.1:8899/", "http://127.0.0.1:8899/api/"]) {
    let received;
    const request = assistant.createRequest({ base: apiBase, fetch: async url => {
      received = url;
      return { ok: true, json: async () => ({}) };
    } });
    await request(`${base}/inputs`, { query: { workspace_key: "workpal" } });
    const origin = apiBase.startsWith("http") ? "http://127.0.0.1:8899" : "";
    assert.equal(received, `${origin}${base}/inputs?workspace_key=workpal`);
  }
});

// Deliberately rejects HTML APIs: the production renderer must create text nodes.
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.attrs = {}; this.listeners = {}; this.value = ""; this._text = ""; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(""); }
  set innerHTML(_value) { throw new Error("Untrusted content must never use innerHTML"); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; this._text = ""; }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  focus() {}
  showModal() { this.open = true; }
  close() { this.open = false; this.listeners.close?.(); }
}
test("real DOM renderer treats model, sources, evidence, notes and errors as plain text", async () => {
  const attack = '<img src=x onerror="globalThis.pwned=1"><script>pwned=2</script>';
  const r = runtime(url => {
    if (url === `${base}/inputs`) return { ...inputs, sources: [{ id: 11, name: attack, evidence_count: 1 }], profiles: [{ ...inputs.profiles[0], display_name: attack }] };
    if (url.endsWith("/evidence")) return { ...samples(), samples: [{ ...samples().samples[0], excerpt: attack, source_name: attack, locator_start: attack }] };
    if (url === base) return [{ ...version("pending_review"), note: attack, mapping: { ...version().mapping, field_mappings: { [attack]: "name" } } }];
  });
  const document = { createElement: tag => new Element(tag), body: new Element("body") };
  const view = assistant.createView({ document, controller: r.controller });
  await r.controller.open();
  await r.controller.selectVersion(31);
  view.render(r.state());
  assert.ok(view.dialog.textContent.includes(attack));
  assert.equal(globalThis.pwned, undefined);
  assert.ok(!JSON.stringify(document.body.children).includes('"tagName":"img"'));
  assert.ok(view.dialog.textContent.includes("行业与专业知识"));
  assert.ok(view.dialog.textContent.includes("概念"));
  assert.ok(view.dialog.textContent.includes("支持"));
});
