/* Rule approval and explicit extraction preview. Publication remains a separate operation. */
(function (root, factory) {
  "use strict";
  const assistant = factory();
  if (typeof module === "object" && module.exports) module.exports = assistant;
  else {
    root.MinDockMappingAssistant = assistant;
    assistant.mount({
      document: root.document,
      getContext: () => ({ workspace: S.workspace, epoch: S.workspaceEpoch || 0, switching: !!S.workspaceSwitching }),
      request: assistant.createRequest({ base: typeof API === "string" ? API : "", fetch: root.fetch.bind(root) }),
      onSettings: () => { if (typeof go === "function") go("settings"); },
    });
  }
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";
  const BASE = "/api/knowledge/ontology/mappings";
  const EXTRACTIONS = "/api/knowledge/ontology/extractions";
  const RUN_STATUS = new Map(Object.entries({ ready: "待开始", running: "抽取中", partial: "部分完成", completed: "已完成", cancelled: "已取消", conflict: "存在冲突" }));
  const SEGMENT_STATUS = new Map(Object.entries({ pending: "待处理", running: "处理中", succeeded: "成功", failed: "失败", cancelled: "已取消" }));
  const canStep = run => !!run && ["ready", "running", "partial"].includes(run.status) && run.coverage.pending > 0 && run.coverage.running === 0;
  const canRetry = run => !!run && !["completed", "conflict"].includes(run.status) && (run.coverage.failed + run.coverage.running + run.coverage.cancelled > 0);
  const canCancel = run => !!run && ["ready", "running", "partial"].includes(run.status);
  const STATUS = new Map([
    ["draft", "草稿"], ["pending_review", "待人工审核"], ["approved", "已确认整理规则"],
    ["rejected", "已拒绝"], ["superseded", "已被新版本替代"],
  ]);
  const TYPES = new Map(Object.entries({
    concept: "概念", model: "模型", method: "方法", case: "案例", exam_point: "考点", material: "资料",
    entity: "实体", process: "流程", metric: "指标", event: "事件", decision: "决策", rule: "规则",
  }));
  const RELATIONS = new Map(Object.entries({
    pre: "前置依赖", related: "相关", part_of: "属于", supports: "支持", example_of: "例证",
    causes: "因果", contrasts: "对比", bridge: "跨主题连接",
  }));
  const FIELDS = new Map(Object.entries({
    name: "名称", kp_type: "知识类型", chapter: "章节 / 分类", gist: "摘要", detail: "详细内容",
    trap: "易错点 / 注意事项", source_refs: "来源引用",
  }));
  const text = value => value == null ? "" : String(value);
  const label = (labels, key) => labels.get(key) || `未登记（${text(key)}）`;
  const statusLabel = status => STATUS.get(status) || "未知状态（只读）";

  function createRequest({ fetch, base = "" }) {
    const apiRoot = base.replace(/\/+$/u, "");
    return async function request(path, { query, method = "GET", body, signal } = {}) {
      const search = new URLSearchParams(query || {}).toString();
      const options = { method, credentials: "same-origin", signal, headers: { Accept: "application/json" } };
      if (body !== undefined) {
        options.headers["Content-Type"] = "application/json";
        options.body = JSON.stringify(body);
      }
      const route = apiRoot.endsWith("/api") && path.startsWith("/api/") ? path.slice(4) : path;
      const response = await fetch(apiRoot + route + (search ? `?${search}` : ""), options);
      let payload;
      try { payload = await response.json(); } catch { /* A proxy may return an HTML error page. */ }
      if (!response.ok) {
        const error = new Error(typeof payload?.detail === "string" ? payload.detail : `HTTP ${response.status}`);
        error.status = response.status;
        throw error;
      }
      if (payload === undefined) throw new Error("整理规则接口未返回有效数据，请刷新后重试");
      return payload;
    };
  }

  function errorMessage(error) {
    const message = text(error?.message);
    if (/no.*model|model.*not.*config|model.*missing|未配置.*模型|模型.*未配置|没有.*模型/i.test(message))
      return "尚未配置可用模型，请到设置 → 模型与接口配置后重试。";
    if (error?.status === 409) return "资料、领域或规则版本已变化，请刷新资料与历史后重新核对。";
    if (error?.status === 401 || error?.status === 403) return "当前会话无法访问整理规则，请重新打开 MinDock 后重试。";
    if (error?.status === 404) return "该资料、规则或工作区已不可用，请刷新资料与历史。";
    if (/sample|evidence|样本|证据/i.test(message)) return "资料没有可用样本，或版本证据已失效，请刷新并重新生成建议。";
    if (error?.status === 422) return "请求或模型建议不符合当前整理规则，请核对资料、领域与用途后重试。";
    if (error?.status >= 500) return "模型或整理规则服务暂时不可用，请稍后重试；未确认的新建议不会进入图谱。";
    if (/fetch|network|timeout|abort/i.test(message)) return "请求未完成，请检查连接后重试。";
    return /[\u3400-\u9fff]/u.test(message) ? message.slice(0, 400) : "整理规则读取或操作失败，请刷新后重试。";
  }

  function createController({ request, getContext, onChange }) {
    const listeners = new Set(onChange ? [onChange] : []);
    let serial = 0, aborter, extractionSerial = 0, extractionAborter;
    const blankExtraction = () => ({ history: [], run: null, busy: "", running: false, blocked: false, error: "", message: "" });
    const blank = () => ({
      open: false, context: null, inputs: null, history: [], historyError: "", sourceId: 0, profileId: "",
      purpose: "", purposeDirty: false, version: null, samples: null, evidenceReady: false,
      confirmed: false, note: "", busy: "", error: "", message: "", extraction: blankExtraction(),
    });
    let state = blank();
    const emit = () => listeners.forEach(listener => listener(state));
    function invalidate() {
      serial++;
      aborter?.abort();
      aborter = new AbortController();
      invalidateExtraction();
      return serial;
    }
    function invalidateExtraction() {
      extractionSerial++;
      extractionAborter?.abort();
      extractionAborter = new AbortController();
      state.extraction.busy = "";
      state.extraction.running = false;
      return extractionSerial;
    }
    function close() {
      invalidate();
      state = blank();
      emit();
    }
    function syncContext() {
      if (!state.open) return false;
      const current = getContext();
      if (current.switching || current.workspace !== state.context.workspace || (current.epoch || 0) !== state.context.epoch) {
        close();
        return false;
      }
      return true;
    }
    const current = token => syncContext() && token === serial;
    const fail = message => { state.error = message; emit(); return false; };
    const scoped = () => ({ workspace_key: state.context.workspace });
    const read = (path, token, query = {}) => request(path, { query: { ...scoped(), ...query }, signal: aborter.signal });
    function resetSelection() {
      state.extraction = blankExtraction();
      state.version = null;
      state.samples = null;
      state.evidenceReady = false;
      state.confirmed = false;
      state.purposeDirty = false;
      state.note = "";
      state.error = "";
      state.message = "";
    }
    function versionInScope(row) {
      return !!row && row.workspace_key === state.context?.workspace &&
        (row.profile_id || row.mapping?.profile_id) === state.profileId &&
        row.mapping?.profile_id === state.profileId && row.bindings?.length > 0 &&
        row.bindings.every(binding => Number(binding.source_id) === state.sourceId);
    }
    function validVersion(row) {
      return versionInScope(row) && Number.isSafeInteger(row.id) && row.id > 0 &&
        /^[0-9a-f]{64}$/.test(row.mapping_hash) && Array.isArray(row.mapping.branch_keys) &&
        Array.isArray(row.mapping.node_types) && Array.isArray(row.mapping.relation_kinds) &&
        row.mapping.field_mappings && typeof row.mapping.field_mappings === "object";
    }
    function historyForSelection() {
      return state.history.filter(versionInScope);
    }
    function hasSamples() {
      return Array.isArray(state.samples?.samples) && state.samples.samples.length > 0;
    }
    function checkSamples(payload, version) {
      if (!Array.isArray(payload?.samples)) throw new Error("样本接口返回无效数据");
      if (payload.samples.some(sample => Number(sample.source_id) !== state.sourceId ||
        typeof sample.ref !== "string" || typeof sample.excerpt !== "string" || !sample.excerpt.trim()))
        throw new Error("样本与所选资料不一致，请刷新");
      if (version) {
        const refs = version.mapping.sample_refs || version.bindings.map(binding => `evidence:${binding.evidence_id}`);
        const bound = new Set(refs);
        const received = new Set(payload.samples.map(sample => sample.ref));
        // Older versions may bind more than the service's eight-sample preview limit.
        // The endpoint verifies all bindings; every displayed sample must belong to them.
        if (!refs.length || !received.size || received.size !== payload.samples.length ||
          payload.samples.some(sample => !bound.has(sample.ref)))
          throw new Error("该版本绑定的证据与返回样本不一致，请刷新");
      }
      return payload;
    }
    async function loadSamples(token) {
      if (!state.sourceId) return;
      state.busy = "samples";
      emit();
      try {
        const payload = await read(`${BASE}/samples`, token, { source_id: state.sourceId });
        if (!current(token)) return;
        state.samples = checkSamples(payload);
        if (!hasSamples()) state.error = "所选资料没有可用样本，请先完成资料索引，或选择其他资料。";
      } catch (error) {
        if (current(token)) state.error = errorMessage(error);
      } finally {
        if (current(token)) { state.busy = ""; emit(); }
      }
    }
    async function open(keep) {
      const context = getContext();
      if (context.switching || !context.workspace) return false;
      const previous = keep && state.open ? { sourceId: state.sourceId, profileId: state.profileId, purpose: state.purpose } : {};
      const token = invalidate();
      state = { ...blank(), ...previous, open: true, context: { workspace: context.workspace, epoch: context.epoch || 0 }, busy: "inputs" };
      emit();
      const [inputResult, historyResult] = await Promise.allSettled([read(`${BASE}/inputs`, token), read(BASE, token)]);
      if (!current(token)) return;
      if (historyResult.status === "fulfilled" && Array.isArray(historyResult.value)) {
        state.history = historyResult.value.filter(row => row.workspace_key === context.workspace);
      } else state.historyError = "历史版本读取失败，请刷新后重试。";
      if (inputResult.status === "rejected") {
        state.busy = "";
        return fail(errorMessage(inputResult.reason));
      }
      const data = inputResult.value;
      if (!Array.isArray(data?.sources) || !Array.isArray(data?.profiles) || !Array.isArray(data?.branches)) {
        state.busy = "";
        return fail("资料与领域列表无效，请刷新后重试。");
      }
      state.inputs = data;
      if (!data.sources.some(source => Number(source.id) === state.sourceId)) state.sourceId = Number(data.sources[0]?.id) || 0;
      if (!data.profiles.some(profile => profile.id === state.profileId)) state.profileId = data.profiles[0]?.id || "";
      state.busy = "";
      if (!state.sourceId) state.error = "当前工作区暂无可用资料，请先添加资料并完成索引。";
      else if (!state.profileId) state.error = "当前工作区没有可用领域配置，请检查领域设置。";
      emit();
      await loadSamples(token);
    }
    async function setSelection(key, value) {
      if (!syncContext() || !state.inputs) return;
      const valid = key === "sourceId" ? state.inputs.sources.some(row => Number(row.id) === value) : state.inputs.profiles.some(row => row.id === value);
      if (!valid || state[key] === value) return;
      const token = invalidate();
      state[key] = value;
      resetSelection();
      await loadSamples(token);
    }
    async function loadEvidence(token) {
      const version = state.version;
      state.samples = null;
      state.evidenceReady = false;
      state.busy = "evidence";
      emit();
      try {
        const payload = await read(`${BASE}/${version.id}/evidence`, token);
        if (!current(token)) return;
        state.samples = checkSamples(payload, version);
        state.evidenceReady = hasSamples();
      } catch (error) {
        if (current(token)) { state.error = errorMessage(error); state.confirmed = false; }
      } finally {
        if (current(token)) { state.busy = ""; emit(); }
      }
    }
    async function selectVersion(id) {
      if (!syncContext()) return;
      const row = historyForSelection().find(item => item.id === Number(id));
      if (!validVersion(row)) return fail("该版本不属于所选资料与领域，请刷新历史版本。");
      const token = invalidate();
      resetSelection();
      state.version = row;
      state.message = row.status === "approved" ? "已确认整理规则，未写入图谱。" : "请核对该版本绑定的样本证据。";
      await loadEvidence(token);
      if (current(token) && row.status === "approved") await refreshExtractions();
    }
    async function generate() {
      if (!syncContext() || state.busy) return false;
      if (!state.sourceId || !state.profileId) return fail("请先选择资料与领域。");
      if (!hasSamples()) return fail("所选资料没有可用样本，请先完成索引或刷新资料。");
      if (!state.purpose.trim()) return fail("请填写整理用途，再生成建议。");
      const token = invalidate();
      const body = { ...scoped(), source_id: state.sourceId, profile_id: state.profileId, purpose: state.purpose.trim() };
      state.version = null;
      state.extraction = blankExtraction();
      state.evidenceReady = false;
      state.confirmed = false;
      state.note = "";
      state.error = "";
      state.message = "";
      state.busy = "suggest";
      emit();
      try {
        const row = await request(`${BASE}/suggest`, { method: "POST", body, signal: aborter.signal });
        if (!current(token)) return;
        if (!validVersion(row) || row.status !== "draft") throw new Error("建议未返回有效草稿，请刷新历史后核对。");
        state.version = row;
        state.purposeDirty = false;
        state.history = [row, ...state.history.filter(item => item.id !== row.id)];
        state.message = "建议已保存为草稿，请核对后提交审核。";
        await loadEvidence(token);
      } catch (error) {
        if (current(token)) state.error = errorMessage(error);
      } finally {
        if (current(token)) { state.busy = ""; emit(); }
      }
    }
    async function review(action) {
      if (!syncContext() || state.busy) return false;
      const row = state.version;
      if (!validVersion(row)) return fail("请先选择当前工作区的有效规则版本。");
      const expected = { submit: "draft", approve: "pending_review", reject: "pending_review" };
      if (!Object.hasOwn(expected, action) || row.status !== expected[action]) return fail("当前版本状态不支持此操作。");
      if (action !== "reject" && (state.purposeDirty || !state.evidenceReady))
        return fail(state.purposeDirty ? "用途已调整，请重新生成建议后再审核。" : "样本证据尚未核对或已失效，请刷新后重新核对。");
      if (action === "approve" && state.confirmed !== true) return fail("请先勾选人工确认，核对整理规则与样本证据。");
      const token = invalidate();
      const body = { ...scoped(), expected_hash: row.mapping_hash };
      if (action === "approve") body.confirmed = true;
      if (action !== "submit" && state.note.trim()) body.note = state.note.trim();
      state.busy = action;
      state.error = "";
      state.confirmed = false;
      emit();
      try {
        const updated = await request(`${BASE}/${row.id}/${action}`, { method: "POST", body, signal: aborter.signal });
        if (!current(token)) return;
        const nextStatus = { submit: "pending_review", approve: "approved", reject: "rejected" }[action];
        if (!validVersion(updated) || updated.id !== row.id || updated.mapping_hash !== row.mapping_hash || updated.status !== nextStatus)
          throw new Error("规则状态返回不一致，请刷新历史后重新核对。");
        state.version = updated;
        state.history = [updated, ...state.history.filter(item => item.id !== updated.id)];
        state.note = "";
        state.message = { submit: "已提交审核，请人工核对并勾选确认。", approve: "已确认整理规则，未写入图谱。", reject: "已拒绝该版本，可调整用途后重新生成建议。" }[action];
      } catch (error) {
        if (current(token)) {
          state.error = errorMessage(error);
          // A failed/uncertain write is never retried with an old approval basis.
          if (action !== "reject") state.evidenceReady = false;
        }
      } finally {
        if (current(token)) { state.busy = ""; emit(); }
      }
      if (current(token) && state.version?.status === "approved") await refreshExtractions();
    }

    // Every operation captures an immutable scope; a later selection can never supply
    // the workspace or rule for an old response (or for the next model step).
    function extractionScope() {
      const version = state.version;
      if (!validVersion(version) || version.status !== "approved" || state.purposeDirty) return null;
      return { workspace_key: state.context.workspace, mapping_version_id: version.id, mapping_hash: version.mapping_hash,
        source_ids: [...new Set(version.bindings.map(binding => Number(binding.source_id)))].sort((a, b) => a - b) };
    }
    function sameSources(received, expected) {
      return Array.isArray(received) && received.length === expected.length &&
        received.every(id => Number.isSafeInteger(id) && id > 0) &&
        [...received].sort((a, b) => a - b).every((id, index) => id === expected[index]);
    }
    function currentExtraction(token, scope) {
      if (!syncContext() || token !== extractionSerial) return false;
      const selected = extractionScope();
      if (selected && selected.workspace_key === scope.workspace_key && selected.mapping_version_id === scope.mapping_version_id &&
        selected.mapping_hash === scope.mapping_hash && sameSources(selected.source_ids, scope.source_ids)) return true;
      invalidateExtraction();
      state.extraction = blankExtraction();
      emit();
      return false;
    }
    function checkRun(run, scope, id) {
      if (!run || !Number.isSafeInteger(run.id) || run.id <= 0 || (id !== undefined && run.id !== id) ||
        run.workspace_key !== scope.workspace_key || run.mapping_version_id !== scope.mapping_version_id ||
        run.mapping_hash !== scope.mapping_hash || !sameSources(run.source_ids, scope.source_ids))
        throw new Error("抽取任务与当前工作区、规则版本或资料范围不一致，请刷新进度。");
      const counts = ["pending", "running", "succeeded", "failed", "cancelled"];
      if (!RUN_STATUS.has(run.status) || !run.coverage ||
        !["total", ...counts].every(key => Number.isSafeInteger(run.coverage[key]) && run.coverage[key] >= 0) ||
        counts.reduce((sum, key) => sum + run.coverage[key], 0) !== run.coverage.total ||
        !["segments", "candidates", "relations", "conflicts"].every(key => Array.isArray(run[key])) ||
        run.candidates.some(candidate => !candidate || typeof candidate !== "object" || Array.isArray(candidate)) ||
        run.segments.length !== run.coverage.total || run.segments.some(segment =>
          !segment || !scope.source_ids.includes(segment.source_id) || !SEGMENT_STATUS.has(segment.status)))
        throw new Error("抽取进度数据无效，请刷新进度后重新核对。");
      return run;
    }
    function saveRun(run) {
      state.extraction.run = run;
      state.extraction.history = [run, ...state.extraction.history.filter(row => row.id !== run.id)];
    }
    function extractionFailure(error) {
      state.extraction.error = errorMessage(error);
      // An uncertain write or mismatched scope must be read again before a model call.
      state.extraction.blocked = true;
    }
    function beginExtraction(busy) {
      const token = invalidateExtraction();
      state.extraction.busy = busy;
      state.extraction.error = "";
      state.extraction.message = "";
      return token;
    }
    function extractionRead(path, scope, signal, extra = {}) {
      return request(path, { query: { workspace_key: scope.workspace_key, ...extra }, signal });
    }
    async function refreshExtractions() {
      if (!syncContext() || state.busy || state.extraction.busy) return;
      const scope = extractionScope();
      if (!scope) return;
      const id = state.extraction.run?.id, token = beginExtraction("refresh"), signal = extractionAborter.signal;
      emit();
      try {
        const [history, selected] = await Promise.all([
          extractionRead(EXTRACTIONS, scope, signal, { mapping_version_id: scope.mapping_version_id }),
          id === undefined ? null : extractionRead(`${EXTRACTIONS}/${id}`, scope, signal),
        ]);
        if (!currentExtraction(token, scope)) return;
        if (!Array.isArray(history)) throw new Error("抽取任务历史无效，请刷新进度。");
        history.forEach(row => checkRun(row, scope));
        const run = id === undefined ? history[0] || null : checkRun(selected, scope, id);
        state.extraction.history = history;
        state.extraction.run = null;
        if (run) saveRun(run);
        state.extraction.blocked = false;
      } catch (error) {
        if (currentExtraction(token, scope)) extractionFailure(error);
      } finally {
        if (currentExtraction(token, scope)) { state.extraction.busy = ""; emit(); }
      }
    }
    async function selectExtraction(id) {
      if (!syncContext() || state.busy) return;
      const scope = extractionScope(), runId = Number(id);
      if (!scope || !state.extraction.history.some(run => run.id === runId)) return;
      const token = beginExtraction("read"), signal = extractionAborter.signal;
      state.extraction.run = null;
      emit();
      try {
        const run = await extractionRead(`${EXTRACTIONS}/${runId}`, scope, signal);
        if (!currentExtraction(token, scope)) return;
        saveRun(checkRun(run, scope, runId));
        state.extraction.blocked = false;
      } catch (error) {
        if (currentExtraction(token, scope)) extractionFailure(error);
      } finally {
        if (currentExtraction(token, scope)) { state.extraction.busy = ""; emit(); }
      }
    }
    async function extractionWrite(action) {
      if (!syncContext() || state.busy || (state.extraction.busy && action !== "cancel") || state.extraction.busy === "cancel") return;
      const scope = extractionScope(), previous = state.extraction.run;
      if (!scope) return;
      if (action === "retry" && (!canRetry(previous) || state.extraction.blocked)) return;
      if (action === "cancel" && !canCancel(previous)) return;
      const token = beginExtraction(action);
      // Cancel gets a fresh signal, independent of the in-flight step it invalidates.
      const signal = extractionAborter.signal;
      emit();
      try {
        if (action !== "create") checkRun(previous, scope);
        const run = await request(action === "create" ? EXTRACTIONS : `${EXTRACTIONS}/${previous.id}/${action}`, {
          method: "POST", body: action === "create" ? scope : { workspace_key: scope.workspace_key }, signal,
        });
        if (!currentExtraction(token, scope)) return;
        saveRun(checkRun(run, scope, action === "create" ? undefined : previous.id));
        state.extraction.blocked = false;
        state.extraction.message = { create: "任务已创建；点击开始抽取才会发送资料内容。", retry: "失败或中断段已准备重试；请点击开始抽取。", cancel: "已取消后续抽取，可刷新查看持久化进度。" }[action];
      } catch (error) {
        if (currentExtraction(token, scope)) extractionFailure(error);
      } finally {
        if (currentExtraction(token, scope)) { state.extraction.busy = ""; emit(); }
      }
    }
    async function startExtraction() {
      if (!syncContext() || state.busy || state.extraction.busy || state.extraction.blocked) return;
      const scope = extractionScope(), previous = state.extraction.run;
      if (!scope || !canStep(previous)) return;
      const token = beginExtraction("step"), signal = extractionAborter.signal, id = previous.id;
      state.extraction.running = true;
      emit();
      try {
        checkRun(previous, scope);
        while (currentExtraction(token, scope) && canStep(state.extraction.run)) {
          const before = state.extraction.run.coverage;
          const response = await request(`${EXTRACTIONS}/${id}/step`, { method: "POST", body: { workspace_key: scope.workspace_key }, signal });
          if (!currentExtraction(token, scope)) return;
          const run = checkRun(response, scope, id);
          saveRun(run);
          emit();
          if (run.coverage.succeeded + run.coverage.failed <= before.succeeded + before.failed) {
            state.extraction.message = "本次未新增完成段，请刷新进度；中断段需显式重试。";
            break;
          }
        }
      } catch (error) {
        if (currentExtraction(token, scope)) extractionFailure(error);
      } finally {
        if (currentExtraction(token, scope)) {
          state.extraction.busy = "";
          state.extraction.running = false;
          emit();
        }
      }
    }
    return {
      getState: () => state,
      subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); },
      historyForSelection, open, close, syncContext, generate, selectVersion, review,
      refreshExtractions, selectExtraction, startExtraction,
      createExtraction: () => extractionWrite("create"),
      retryExtraction: () => extractionWrite("retry"),
      cancelExtraction: () => extractionWrite("cancel"),
      refresh: () => open(true),
      setSource: id => setSelection("sourceId", Number(id)),
      setProfile: id => setSelection("profileId", id),
      setPurpose(value) {
        if (!syncContext() || state.purpose === text(value)) return;
        if (state.busy) return;
        state.purpose = text(value).slice(0, 2000);
        state.confirmed = false;
        if (state.version) {
          state.purposeDirty = true;
          invalidateExtraction();
          state.extraction = blankExtraction();
        }
        emit();
      },
      setConfirmed(value) {
        if (!syncContext()) return;
        state.confirmed = value === true && !state.busy && state.version?.status === "pending_review" && state.evidenceReady && !state.purposeDirty;
        emit();
      },
      setNote(value) { if (syncContext() && !state.busy) { state.note = text(value).slice(0, 2000); emit(); } },
    };
  }

  function createView({ document, controller, onSettings = () => {}, onClosed = () => {} }) {
    function node(tag, className, value) {
      const element = document.createElement(tag);
      if (className) element.className = className;
      if (value !== undefined) element.textContent = text(value);
      return element;
    }
    function button(value, action, className = "ma-button") {
      const element = node("button", className, value);
      element.type = "button";
      element.addEventListener("click", action);
      return element;
    }
    function field(value, control, id) {
      const wrap = node("div", "ma-field");
      const title = node("label", "", value);
      control.id = id;
      title.setAttribute("for", id);
      wrap.append(title, control);
      return wrap;
    }
    const dialog = node("dialog", "mapping-assistant");
    dialog.id = "mappingAssistant";
    dialog.setAttribute("aria-labelledby", "mappingAssistantTitle");
    const header = node("header", "ma-header");
    const title = node("h2", "", "整理规则");
    title.id = "mappingAssistantTitle";
    const workspace = node("span", "ma-workspace");
    const close = button("关闭", () => controller.close(), "ma-button ma-close");
    close.setAttribute("aria-label", "关闭整理规则");
    header.append(title, workspace, close);
    const layout = node("div", "ma-layout");
    const aside = node("aside", "ma-setup");
    aside.setAttribute("aria-label", "选择资料与整理用途");
    const source = node("select"), profile = node("select"), purpose = node("textarea");
    source.addEventListener("change", () => controller.setSource(source.value));
    profile.addEventListener("change", () => controller.setProfile(profile.value));
    purpose.maxLength = 2000;
    purpose.rows = 5;
    purpose.placeholder = "例如：按主题整理概念、方法和它们的依据";
    purpose.addEventListener("input", () => controller.setPurpose(purpose.value));
    const scopeNote = node("p", "ma-hint");
    const generate = button("AI 生成建议", () => controller.generate(), "ma-button ma-primary");
    const disclosure = node("p", "ma-hint", "所选资料的抽样片段将发送到已配置模型；样本不代表全文覆盖。");
    disclosure.id = "mappingSampleDisclosure";
    generate.setAttribute("aria-describedby", disclosure.id);
    const settings = button("去配置模型", () => { controller.close(); onSettings(); });
    aside.append(field("资料", source, "mappingSource"), field("领域", profile, "mappingProfile"),
      scopeNote, field("整理用途", purpose, "mappingPurpose"), generate, disclosure, settings);
    const review = node("section", "ma-review");
    review.setAttribute("aria-label", "规则预览与样本证据");
    const historyRow = node("div", "ma-history-row");
    const history = node("select");
    history.addEventListener("change", () => { if (history.value) controller.selectVersion(history.value); });
    const refresh = button("刷新", () => controller.refresh());
    historyRow.append(field("此资料与领域的历史版本", history, "mappingHistory"), refresh);
    const historyError = node("p", "ma-error");
    const notice = node("p", "ma-error");
    notice.setAttribute("role", "alert");
    const content = node("div", "ma-rule-content");
    const evidence = node("details", "ma-evidence");
    const evidenceSummary = node("summary"), evidenceBody = node("div", "ma-evidence-body");
    evidence.append(evidenceSummary, evidenceBody);
    const note = node("textarea");
    note.rows = 2;
    note.maxLength = 2000;
    note.placeholder = "可记录批准或拒绝原因";
    note.addEventListener("input", () => controller.setNote(note.value));
    const noteField = field("审核备注（可选）", note, "mappingReviewNote");
    const extractionPanel = node("section", "ma-extraction");
    extractionPanel.setAttribute("aria-label", "批准规则的抽取预览");
    const extractionHistory = node("select");
    extractionHistory.addEventListener("change", () => { if (extractionHistory.value) controller.selectExtraction(extractionHistory.value); });
    const extractionDisclosure = node("p", "ma-hint", "开始抽取将逐段把来源内容发送到已配置的 fast 路由。结果仅供预览，未写入图谱；覆盖仅指已索引片段，不代表原文件全文完整或知识正确。创建、刷新与重试不调用模型。");
    extractionDisclosure.id = "mappingExtractionDisclosure";
    const extractionCounts = node("p", "ma-extraction-counts");
    extractionCounts.setAttribute("role", "status");
    extractionCounts.setAttribute("aria-live", "polite");
    const extractionError = node("p", "ma-error");
    extractionError.setAttribute("role", "alert");
    const extractionMessage = node("p", "ma-hint");
    const extractionPreview = node("div", "ma-extraction-preview");
    extractionPanel.append(node("h3", "", "抽取任务与候选预览"), extractionDisclosure,
      field("此规则版本的抽取历史（选择后可手动续跑）", extractionHistory, "mappingExtractionHistory"),
      extractionCounts, extractionError, extractionMessage, extractionPreview);
    review.append(historyRow, historyError, notice, content, evidence, noteField, extractionPanel);
    layout.append(aside, review);
    const footer = node("footer", "ma-footer");
    const confirmation = node("label", "ma-confirmation");
    const checkbox = node("input");
    checkbox.type = "checkbox";
    checkbox.id = "mappingConfirmed";
    checkbox.addEventListener("change", () => controller.setConfirmed(checkbox.checked));
    confirmation.append(checkbox, node("span", "", "我已人工核对所选版本的整理规则与样本证据"));
    const actions = node("div", "ma-actions");
    const message = node("p", "ma-message");
    message.setAttribute("role", "status");
    message.setAttribute("aria-live", "polite");
    const reject = button("拒绝", () => controller.review("reject"));
    const submit = button("提交审核", () => controller.review("submit"), "ma-button ma-primary");
    const approve = button("批准整理规则", () => controller.review("approve"), "ma-button ma-primary");
    actions.append(message, reject, submit, approve);
    const extractionActions = node("div", "ma-extraction-actions");
    const createExtraction = button("创建抽取任务", () => controller.createExtraction());
    const startExtraction = button("开始抽取", () => controller.startExtraction(), "ma-button ma-primary");
    startExtraction.setAttribute("aria-describedby", extractionDisclosure.id);
    const cancelExtraction = button("取消", () => controller.cancelExtraction());
    const retryExtraction = button("重试失败段", () => controller.retryExtraction());
    const refreshExtraction = button("刷新进度", () => controller.refreshExtractions());
    extractionActions.append(createExtraction, startExtraction, cancelExtraction, retryExtraction, refreshExtraction);
    footer.append(confirmation, actions, extractionActions);
    dialog.append(header, layout, footer);
    document.body.append(dialog);
    dialog.addEventListener("cancel", event => { event.preventDefault(); controller.close(); });
    dialog.addEventListener("close", () => { if (!dialog.open) controller.close(); });

    function options(select, values, chosen, empty) {
      const items = [node("option", "", empty)];
      items[0].value = "";
      values.forEach(([value, title]) => {
        const item = node("option", "", title);
        item.value = text(value);
        items.push(item);
      });
      select.replaceChildren(...items);
      select.value = text(chosen);
    }
    function ruleSection(title, values) {
      const section = node("section", "ma-rule-section"), heading = node("h3", "", title);
      const list = node("ul", "ma-chips");
      (values.length ? values : ["未指定"]).forEach(value => list.append(node("li", "", value)));
      section.append(heading, list);
      return section;
    }
    function renderExtraction(state) {
      const approved = state.version?.status === "approved";
      extractionPanel.hidden = extractionActions.hidden = !approved;
      if (!approved) return;
      const extraction = state.extraction, run = extraction.run;
      const disabled = !!state.busy || !!extraction.busy || state.purposeDirty;
      options(extractionHistory, extraction.history.map((row, index) => [row.id, `任务 ${index + 1} · ${RUN_STATUS.get(row.status)}${row.created_at ? ` · ${row.created_at}` : ""}`]), run?.id || "", "选择抽取任务");
      extractionHistory.disabled = !!state.busy || state.purposeDirty;
      createExtraction.disabled = startExtraction.disabled = retryExtraction.disabled = refreshExtraction.disabled = disabled;
      startExtraction.disabled ||= extraction.blocked || !canStep(run);
      retryExtraction.disabled ||= extraction.blocked || !canRetry(run);
      // A pending model request must never disable cancellation.
      cancelExtraction.disabled = !!state.busy || state.purposeDirty || extraction.busy === "cancel" || !canCancel(run);
      extractionError.textContent = extraction.error;
      extractionError.hidden = !extraction.error;
      const progressMessages = { create: "正在创建任务…", refresh: "正在刷新进度…", read: "正在读取任务…", step: "正在逐段抽取，可随时取消。", retry: "正在准备重试…", cancel: "正在取消，已停止后续调度…" };
      extractionMessage.textContent = state.purposeDirty ? "用途已调整，请选择有效批准版本后再抽取。" : progressMessages[extraction.busy] || extraction.message;
      extractionMessage.hidden = !extractionMessage.textContent;
      extractionPreview.replaceChildren();
      if (!run) {
        extractionCounts.textContent = "尚未选择抽取任务。创建任务不会调用模型。";
        return;
      }
      const c = run.coverage;
      extractionCounts.textContent = `${RUN_STATUS.get(run.status)} · 已索引片段：总段数 ${c.total} · 成功 ${c.succeeded} · 失败 ${c.failed} · 待处理 ${c.pending} · 处理中 ${c.running} · 已取消 ${c.cancelled}`;
      const displayValue = value => Array.isArray(value) ? value.map(displayValue).filter(Boolean).join("、") : typeof value === "string" || typeof value === "number" ? String(value) : "";
      const sources = new Map((state.inputs?.sources || []).map(source => [String(source.id), source.name]));
      const candidateNames = new Map(run.candidates.map(candidate => [candidate.id, displayValue(candidate.name)]));
      const evidenceNumbers = new Map();
      function evidenceLabel(ref) {
        if (ref.evidence_id == null) return "证据编号未提供";
        const key = JSON.stringify([ref.source_id, ref.evidence_id]);
        if (!evidenceNumbers.has(key)) evidenceNumbers.set(key, evidenceNumbers.size + 1);
        return `证据 ${evidenceNumbers.get(key)}`;
      }
      run.segments.forEach(evidenceLabel);
      function renderReferences(parent, refs) {
        if (!Array.isArray(refs)) return;
        refs.forEach(ref => {
          if (!ref || typeof ref !== "object") return;
          const item = node("div", "ma-extraction-reference");
          const offset = value => Number.isInteger(value) && value >= 0 ? value : "—";
          item.append(node("p", "ma-hint", `来源：${sources.get(String(ref.source_id)) || "未提供来源名称"} · ${evidenceLabel(ref)} · 字符位置 ${offset(ref.start)}–${offset(ref.end)}`));
          if (displayValue(ref.quote)) item.append(node("blockquote", "ma-excerpt", displayValue(ref.quote)));
          parent.append(item);
        });
      }
      function conflictMessage(conflict) {
        const name = displayValue(conflict?.name) || "未命名知识";
        switch (conflict?.kind) {
          case "same_name_variants": return `同名知识有不同表述：${name}（${Array.isArray(conflict.candidate_ids) ? `${new Set(conflict.candidate_ids).size} 个版本` : "版本数量未提供"}），请核对来源与条件后处理`;
          case "existing_kp": return `与图谱中已有知识同名：${name}，请核对内容、来源与条件后处理`;
          case "existing_candidate": return `与已有候选知识同名：${name}，请核对内容、来源与条件后处理`;
          case "collapsed_relation": return "关系两端归并为同一知识，请核对原文后处理";
          case "stale_contract": return "资料、领域配置或整理规则已变化，请重新创建任务并核对后再抽取";
          default: return "发现未识别的抽取冲突，请核对来源与条件后处理";
        }
      }
      const conflicts = node("section", "ma-extraction-conflicts");
      conflicts.append(node("h3", "", `冲突列表 · ${run.conflicts.length}`));
      if (!run.conflicts.length) conflicts.append(node("p", "ma-hint", "当前无已报告冲突。"));
      else {
        const list = node("ul");
        run.conflicts.forEach(conflict => {
          const item = node("li", "ma-excerpt", conflictMessage(conflict));
          renderReferences(item, conflict?.evidence_refs);
          list.append(item);
        });
        conflicts.append(list);
      }
      extractionPreview.append(conflicts, node("h3", "", `候选预览 · ${run.candidates.length}`));
      if (!run.candidates.length) extractionPreview.append(node("p", "ma-hint", run.status === "completed" ? "已完成，未发现候选（0 条）；这是有效抽取结果。" : "当前候选为 0 条，请结合任务进度查看。"));
      const candidateFields = new Map(Object.entries({ name: "名称", kp_type: "知识类型", branch_keys: "归入分支", gist: "摘要", detail: "详细内容", chapter: "章节", quote: "原文引用", conditions: "条件", valid_from: "生效日期", valid_to: "失效日期" }));
      const branches = new Map((state.inputs?.branches || []).map(branch => [branch.key, branch.display_name]));
      run.candidates.forEach(candidate => {
        const card = node("details", "ma-extraction-candidate");
        card.append(node("summary", "", candidate.name));
        const fields = node("dl");
        candidateFields.forEach((title, key) => {
          let value = candidate[key];
          if (key === "kp_type") value = TYPES.get(value) || "未指定类型";
          if (key === "branch_keys" && Array.isArray(value)) value = value.map(key => branches.get(key) || "未登记分支");
          if (key === "chapter" && candidate.chapters?.length) value = candidate.chapters;
          fields.append(node("dt", "", title), node("dd", "ma-excerpt", displayValue(value) || "未提供"));
        });
        card.append(fields);
        renderReferences(card, candidate.evidence_refs);
        extractionPreview.append(card);
      });
      const segmentDetails = node("details", "ma-extraction-segments");
      segmentDetails.append(node("summary", "", `已索引片段与证据定位 · ${run.segments.length}`));
      run.segments.forEach((segment, index) => {
        const item = node("article", "ma-sample");
        item.append(node("h4", "", `片段 ${index + 1} · ${SEGMENT_STATUS.get(segment.status)} · 尝试 ${segment.attempts ?? 0} 次`));
        renderReferences(item, [segment]);
        if (segment.error) item.append(node("p", "ma-error", "此片段抽取未成功，请核对来源后重试；详细原因可在技术信息中查看。"));
        segmentDetails.append(item);
      });
      extractionPreview.append(segmentDetails);
      const relations = node("details", "ma-extraction-relations");
      relations.append(node("summary", "", `关系预览 · ${run.relations.length}`));
      if (!run.relations.length) relations.append(node("p", "ma-hint", "当前未发现关系。"));
      run.relations.forEach(relation => {
        const item = node("article", "ma-sample");
        const refs = Array.isArray(relation.evidence_refs) ? relation.evidence_refs : [];
        const from = candidateNames.get(relation.from_id) || "未找到对应候选";
        const to = candidateNames.get(relation.to_id) || "未找到对应候选";
        item.append(node("h4", "", `${from} → ${RELATIONS.get(relation.kind) || "未登记关系"} → ${to} · ${refs.length} 条证据`));
        if (displayValue(relation.quote)) item.append(node("blockquote", "ma-excerpt", displayValue(relation.quote)));
        renderReferences(item, refs);
        relations.append(item);
      });
      // Retain the complete host response, including future provenance fields,
      // without exposing machine identifiers or raw metadata in ordinary content.
      const technical = node("details", "ma-technical");
      technical.append(node("summary", "", "技术信息"), node("pre", "ma-provenance", JSON.stringify(run, null, 2)));
      extractionPreview.append(relations, technical);
    }
    let wasOpen = false;
    function render(state) {
      if (!state.open) {
        if (dialog.open) dialog.close();
        if (wasOpen) onClosed();
        wasOpen = false;
        return;
      }
      if (!dialog.open) dialog.showModal();
      wasOpen = true;
      dialog.setAttribute("aria-busy", String(!!state.busy || !!state.extraction.busy));
      const workspaceNames = new Map([["workpal", "工作"], ["studypal", "学习"], ["lifepal", "生活"]]);
      workspace.textContent = workspaceNames.get(state.context.workspace) || state.context.workspace;
      const data = state.inputs || { sources: [], profiles: [], branches: [] };
      options(source, data.sources.map(row => [row.id, `${row.name} · ${row.evidence_count} 条证据`]), state.sourceId || "", "选择资料");
      options(profile, data.profiles.map(row => [row.id, `${row.display_name} · ${row.version}`]), state.profileId, "选择领域");
      source.disabled = profile.disabled = !state.inputs || state.busy === "inputs";
      if (purpose.value !== state.purpose) purpose.value = state.purpose;
      purpose.disabled = !!state.busy;
      scopeNote.textContent = data.truncated ? "资料列表已截断，仅显示返回的部分资料。" : "一次只整理所选的一份资料。";
      generate.textContent = state.busy === "suggest" ? "正在生成…" : state.version ? "重新生成建议" : "AI 生成建议";
      generate.disabled = !!state.busy || !!state.extraction.busy || !state.sourceId || !state.profileId || !state.purpose.trim() || !state.samples?.samples.length;
      settings.hidden = !/模型/.test(state.error);
      options(history, controller.historyForSelection().map(row => [row.id, `版本 #${row.id} · ${statusLabel(row.status)}${row.created_at ? ` · ${row.created_at}` : ""}`]), state.version?.id || "", "选择历史版本");
      history.disabled = !state.inputs || state.busy === "inputs";
      refresh.disabled = !!state.busy;
      historyError.textContent = state.historyError;
      historyError.hidden = !state.historyError;
      notice.textContent = state.error;
      notice.hidden = !state.error;
      content.replaceChildren();
      const version = state.version;
      if (!version) {
        content.append(node("p", "ma-empty", state.busy === "suggest" ? "正在根据抽样资料生成草稿…" : "填写用途并生成建议，或选择历史版本查看整理规则。"));
      } else {
        const versionHead = node("div", "ma-version-head");
        versionHead.append(node("h3", "", `版本 #${version.id}`), node("span", "ma-status", statusLabel(version.status)));
        content.append(versionHead);
        if (version.base_version_id) content.append(node("p", "ma-hint", `基于已确认版本 #${version.base_version_id}，批准时仍需核对当前版本。`));
        const branches = new Map(data.branches.map(branch => [branch.key, branch.display_name]));
        content.append(ruleSection("归入分支", version.mapping.branch_keys.map(key => label(branches, key))),
          ruleSection("知识类型", version.mapping.node_types.map(key => label(TYPES, key))),
          ruleSection("关系", version.mapping.relation_kinds.map(key => label(RELATIONS, key))));
        const table = node("table", "ma-fields");
        table.append(node("caption", "", "字段映射"));
        const thead = node("thead"), headingRow = node("tr");
        ["资料字段", "对应知识字段"].forEach(value => { const cell = node("th", "", value); cell.setAttribute("scope", "col"); headingRow.append(cell); });
        thead.append(headingRow);
        const tbody = node("tbody");
        Object.entries(version.mapping.field_mappings).forEach(([from, to]) => {
          const row = node("tr");
          row.append(node("td", "", from), node("td", "", label(FIELDS, to)));
          tbody.append(row);
        });
        table.append(thead, tbody);
        content.append(table);
        if (version.note) content.append(node("p", "ma-hint", `版本备注：${version.note}`));
      }
      const samples = state.samples;
      const count = samples?.samples.length || 0;
      evidenceSummary.textContent = `${version ? "此版本绑定的" : "所选资料的"}样本证据 · ${count} / ${samples?.total ?? "—"} 条${samples?.truncated ? " · 部分抽样" : ""}`;
      evidenceBody.replaceChildren();
      evidenceBody.append(node("p", "ma-hint", version ? "仅展示该版本绑定且仍有效的样本，不以新样本替换旧审核依据。" : "仅为资料抽样，不能据此判断全文覆盖。"));
      if (!count) evidenceBody.append(node("p", "ma-hint", state.busy ? "正在读取样本…" : "暂无可核对的有效样本。"));
      const locators = new Map([["page", "页码"], ["line", "行号"], ["markdown_line", "行号"], ["file", "文件"], ["time", "时间"], ["timestamp", "时间"]]);
      (samples?.samples || []).forEach(sample => {
        const item = node("article", "ma-sample");
        const location = [sample.locator_start, sample.locator_end].filter(value => value != null && value !== "").map(text).join("–");
        item.append(node("h4", "", sample.source_name || "所选资料"),
          node("p", "ma-hint", `${sample.ref} · ${locators.get(sample.locator_type) || text(sample.locator_type) || "定位"}${location ? `：${location}` : ""}`),
          node("p", "ma-excerpt", sample.excerpt));
        evidenceBody.append(item);
      });
      noteField.hidden = version?.status !== "pending_review";
      if (note.value !== state.note) note.value = state.note;
      note.disabled = !!state.busy;
      const pending = version?.status === "pending_review";
      confirmation.hidden = !pending;
      checkbox.checked = state.confirmed;
      checkbox.disabled = !!state.busy || !state.evidenceReady || state.purposeDirty;
      submit.hidden = version?.status !== "draft";
      approve.hidden = reject.hidden = !pending;
      submit.disabled = !!state.busy || !state.evidenceReady || state.purposeDirty;
      approve.disabled = submit.disabled || !state.confirmed;
      reject.disabled = !!state.busy;
      submit.textContent = state.busy === "submit" ? "提交中…" : "提交审核";
      approve.textContent = state.busy === "approve" ? "批准中…" : "批准整理规则";
      reject.textContent = state.busy === "reject" ? "拒绝中…" : "拒绝";
      const busyMessages = { inputs: "正在读取资料与历史…", samples: "正在读取抽样证据…", evidence: "正在核对版本证据…", suggest: "正在生成草稿…", submit: "正在提交审核…", approve: "正在确认规则…", reject: "正在拒绝版本…" };
      message.textContent = busyMessages[state.busy] || (state.purposeDirty ? "用途已调整，请重新生成建议。" : state.message) || "规则确认后仍需另行抽取与发布。";
      renderExtraction(state);
    }
    controller.subscribe(render);
    return { dialog, render };
  }

  function mount({ document, getContext, request, onSettings }) {
    const trigger = document?.getElementById("graphMappingBtn");
    if (!trigger || trigger.mappingAssistant) return trigger?.mappingAssistant;
    const controller = createController({ request, getContext });
    const view = createView({ document, controller, onSettings, onClosed: () => trigger.focus() });
    let watch;
    controller.subscribe(state => {
      if (state.open && !watch) watch = setInterval(() => controller.syncContext(), 150);
      if (!state.open && watch) { clearInterval(watch); watch = null; }
    });
    trigger.addEventListener("click", () => controller.open());
    trigger.mappingAssistant = { controller, view };
    return trigger.mappingAssistant;
  }
  return { createRequest, createController, createView, mount };
});
