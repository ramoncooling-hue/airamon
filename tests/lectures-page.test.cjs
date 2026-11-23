const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const PALETTE_ANCHOR = 'B3 T3.12/T3.13 命令面板 + 键盘与批量操作';
const paletteComment = html.indexOf(PALETTE_ANCHOR);
assert.ok(paletteComment > 0, 'command palette module found after lectures module');
const LECTURES_SLICE = html.slice(
  html.indexOf('const LECTURES_CHUNK_MS = 2000;'),
  html.lastIndexOf('\n/* ', paletteComment)
);
const ESC_SLICE = html.slice(html.indexOf('function esc(s){'), html.indexOf('\nfunction toast('));
assert.ok(LECTURES_SLICE.includes('function lectureStatusHtml'), 'status rendering found');
assert.ok(LECTURES_SLICE.includes('function lecturePushChunk'), 'chunk upload found');
assert.ok(LECTURES_SLICE.includes('function renderLecturesPage'), 'page renderer found');
assert.ok(ESC_SLICE.includes('String(s'), 'production esc found');

function fakeElement(id){
  const listeners = new Map();
  const classes = new Set();
  return {
    id, hidden:false, innerHTML:'', textContent:'', value:'', disabled:false, className:'',
    scrollTop:0, scrollHeight:120, dataset:{}, onclick:null,
    classList:{
      add(name){ classes.add(name); },
      remove(name){ classes.delete(name); },
      contains(name){ return classes.has(name); },
      toggle(name, on){
        if(on === undefined){ if(classes.has(name)) classes.delete(name); else classes.add(name); }
        else if(on) classes.add(name);
        else classes.delete(name);
      }
    },
    addEventListener(type, handler){
      if(!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(handler);
    },
    removeEventListener(){},
    dispatch(type, event){ (listeners.get(type) || []).forEach(handler => handler(event)); },
    scrollIntoView(options){ this.scrolled = options; },
    focus(){ this.focused = true; },
    querySelectorAll(){ return []; },
    querySelector(){ return null; },
    closest(){ return null; },
    setAttribute(name, value){ this[name] = String(value); },
    getAttribute(name){ return this[name] == null ? null : this[name]; }
  };
}

class FakeAudioContext {
  constructor(options){
    this.state = 'running';
    this.sampleRate = (options && options.sampleRate) || 16000;
    this.destination = {};
    this.closed = false;
  }
  createMediaStreamSource(){ return {connect(){}, disconnect(){}}; }
  createScriptProcessor(){ return {connect(){}, disconnect(){}, onaudioprocess:null}; }
  close(){ this.closed = true; }
  resume(){ return Promise.resolve(); }
}
class FakeMediaStream {
  constructor(tracks){ this.tracks = tracks || []; }
  getTracks(){ return this.tracks; }
  getAudioTracks(){ return this.tracks.filter(track => track.kind === 'audio'); }
  getVideoTracks(){ return this.tracks.filter(track => track.kind === 'video'); }
}
function fakeTrack(kind){
  return {kind, stopped:false, stop(){ this.stopped = true; }};
}
function fakeNavigator(){
  return {mediaDevices:{
    getUserMedia: async () => new FakeMediaStream([fakeTrack('audio')]),
    getDisplayMedia: async () => new FakeMediaStream([fakeTrack('video'), fakeTrack('audio')])
  }};
}

function createRuntime(fetchImpl, overrides = {}){
  const state = {
    calls:[], toasts:[], apiCalls:[], apiPosts:[], apiDeletes:[],
    elements:new Map(), timeouts:new Map(), intervals:new Map(), seq:0,
    docListeners:{}, windowListeners:{}
  };
  const element = id => {
    if(!state.elements.has(id)) state.elements.set(id, fakeElement(id));
    return state.elements.get(id);
  };
  const context = vm.createContext({
    S:{workspace:'studypal', workspaceEpoch:0, page:'lectures', courses:[{name:'审计学'},{name:'会计学'}], chatCourse:'审计学'},
    API:'',
    URL,
    el:element,
    toast:text => state.toasts.push(String(text)),
    fetch:async (url, init) => {
      const entry = {
        url:String(url),
        method:(init && init.method) || 'GET',
        headers:(init && init.headers) || {},
        body:init && Object.prototype.hasOwnProperty.call(init, 'body') ? init.body : undefined
      };
      state.calls.push(entry);
      const spec = fetchImpl ? await fetchImpl(entry, state) : null;
      if(!spec) throw new Error('unexpected fetch: ' + entry.url);
      const status = spec.status || 200;
      return {ok:status >= 200 && status < 300, status, headers:{get:name => {
        const key = Object.keys(spec.headers || {}).find(key => key.toLowerCase() === name.toLowerCase());
        return key ? spec.headers[key] : null;
      }}, json:async () => {
        if(spec.jsonError) throw spec.jsonError;
        return spec.json === undefined ? {} : spec.json;
      }};
    },
    api:async (requestPath, params) => {
      state.apiCalls.push({path:requestPath, params});
      const spec = overrides.api ? overrides.api(requestPath, params, state) : null;
      return spec === undefined ? {items:[]} : spec;
    },
    apiPost:async (requestPath, body) => {
      state.apiPosts.push({path:requestPath, body});
      const spec = overrides.apiPost ? overrides.apiPost(requestPath, body, state) : null;
      return spec === undefined ? {ok:true} : spec;
    },
    apiDelete:async requestPath => {
      state.apiDeletes.push({path:requestPath});
      const spec = overrides.apiDelete ? overrides.apiDelete(requestPath, state) : null;
      return spec === undefined ? {ok:true} : spec;
    },
    navigator:overrides.navigator === undefined ? null : overrides.navigator,
    MediaStream:FakeMediaStream,
    AudioContext:FakeAudioContext,
    window:{confirm:overrides.confirm || (() => true), addEventListener(type, handler){ state.windowListeners[type] = handler; }},
    document:{getElementById:element, addEventListener(type, handler){ state.docListeners[type] = handler; }},
    setTimeout:(handler) => { state.seq += 1; state.timeouts.set(state.seq, handler); return state.seq; },
    clearTimeout:id => { state.timeouts.delete(id); },
    setInterval:(handler) => { state.seq += 1; state.intervals.set(state.seq, handler); return state.seq; },
    clearInterval:id => { state.intervals.delete(id); }
  });
  vm.runInContext(ESC_SLICE, context);
  vm.runInContext(LECTURES_SLICE, context);
  context.window.AudioContext = FakeAudioContext;
  const fn = name => vm.runInContext(name, context);
  const get = expression => vm.runInContext(expression, context);
  return {state, element, fn, get, context};
}

function jsonResponse(payload, status = 200){ return {status, json:payload}; }

function recordingRuntime(fetchImpl, seconds = 25, rate = 16000){
  const rt = createRuntime(fetchImpl);
  const st = rt.get('lectureState');
  st.recording = true;
  st.session = {id:9, status:'live', course:'审计学'};
  st.audio.sampleRate = rate;
  st.audio.buffers = [new Float32Array(rate * seconds)];
  return rt;
}

function finishResponse(){
  return jsonResponse({ok:true, session:{id:9, status:'ended'}, document:{id:42}, segment_count:0});
}

function deferred(){
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return {promise, resolve};
}

function installWorkspaceGuard(rt){
  const code = html.slice(html.indexOf('async function switchWorkspace(key){'));
  vm.runInContext(code.slice(0, code.indexOf("el('workspaceMenu')")) + '\n}', rt.context);
  rt.context.S.workspaces = [{key:'studypal'},{key:'workpal'}];
  rt.context.chatSending = false;
  rt.context.flushCandidateBatch = () => {};
  rt.context.renderWorkspacePicker = () => {};
}

test('响应丢失后重试保持分片ID和PCM字节，新音频使用新ID', async () => {
  let uploads = 0;
  const rt = recordingRuntime(entry => {
    if(!entry.url.endsWith('/chunks')) return finishResponse();
    uploads += 1;
    if(uploads === 1) throw new TypeError('response lost after acceptance');
    return jsonResponse({ok:true, segments:[]});
  }, 1);
  const st = rt.get('lectureState');
  const originalSamples = st.audio.buffers[0];
  originalSamples.fill(0.2);
  await assert.rejects(rt.fn('lectureStopRecording')(), /response lost/);
  const first = rt.state.calls[0];
  const bytes = Array.from(new Uint8Array(first.body));
  originalSamples.fill(0.8);
  st.audio.buffers.push(new Float32Array(3200).fill(0.3));
  await rt.fn('lectureStopRecording')();
  const chunks = rt.state.calls.filter(call => call.url.endsWith('/chunks'));
  assert.equal(chunks.length, 3);
  assert.ok(first.headers['X-Lecture-Chunk-Id']);
  assert.equal(chunks[1].headers['X-Lecture-Chunk-Id'], first.headers['X-Lecture-Chunk-Id']);
  assert.deepEqual(Array.from(new Uint8Array(chunks[1].body)), bytes);
  assert.notEqual(chunks[2].headers['X-Lecture-Chunk-Id'], first.headers['X-Lecture-Chunk-Id']);
  assert.equal(chunks[2].body.byteLength, 6400);
});

test('业务失败和无效JSON不确认分片，重试沿用ID和字节', async () => {
  const failures = [
    jsonResponse({ok:false, error:'business rejected'}),
    {status:200, jsonError:new SyntaxError('invalid JSON')},
    jsonResponse(null),
    jsonResponse({}),
    jsonResponse({ok:true}),
    jsonResponse({ok:true, segments:{}}),
  ];
  for(const failure of failures){
    let failed = false;
    const rt = recordingRuntime(entry => {
      if(!entry.url.endsWith('/chunks')) return finishResponse();
      if(!failed){ failed = true; return failure; }
      return jsonResponse({ok:true, segments:[]});
    }, 1);
    await assert.rejects(rt.fn('lectureStopRecording')());
    assert.equal(rt.state.calls.length, 1, 'failed upload cannot finalize');
    assert.equal(rt.get('lectureState').stopPending, true);
    assert.equal(rt.get('lectureState').audio.bytes, 0);
    assert.match(rt.element('lecturesResult').innerHTML, /待传音频.*本页内存/);
    await rt.fn('lectureStopRecording')();
    const chunks = rt.state.calls.filter(call => call.url.endsWith('/chunks'));
    assert.ok(chunks[0].headers['X-Lecture-Chunk-Id']);
    assert.equal(chunks[0].headers['X-Lecture-Chunk-Id'], chunks[1].headers['X-Lecture-Chunk-Id']);
    assert.deepEqual(Array.from(new Uint8Array(chunks[0].body)), Array.from(new Uint8Array(chunks[1].body)));
  }
});

test('已接收PCM的ASR失败不再重传，错误可见且停止重试只调用finalize', async () => {
  for(const receipt of [
    jsonResponse({detail:'ASR failed after acceptance', accepted:true}, 503),
    {status:503, headers:{'X-Lecture-Chunk-Accepted':'1'}, jsonError:new SyntaxError('bad error JSON')},
    jsonResponse({ok:false, accepted:true, error:'ASR business failure'}),
  ]){
    let finalizes = 0;
    const rt = recordingRuntime(entry => {
      if(entry.url.endsWith('/chunks')) return receipt;
      finalizes += 1;
      return finalizes === 1 ? jsonResponse({detail:'finalize retry needed'}, 503) : finishResponse();
    }, 1);
    const error = await rt.fn('lectureFlushChunk')().then(() => null, error => error);
    assert.ok(error);
    assert.equal(error.lectureAccepted, true);
    rt.fn('lectureHandleChunkError')(error);
    assert.match(rt.element('lecturesLiveSub').textContent, /已接收|转写/);
    assert.ok(rt.state.toasts.some(text => /已接收/.test(text)));
    await assert.rejects(rt.fn('lectureStopRecording')(), /finalize retry needed/);
    assert.match(rt.element('lecturesResult').innerHTML, /音频已接收/);
    assert.doesNotMatch(rt.element('lecturesResult').innerHTML, /音频仅保留在本页内存/);
    await rt.fn('lectureStopRecording')();
    assert.equal(rt.state.calls.filter(call => call.url.endsWith('/chunks')).length, 1);
    assert.equal(rt.get('lectureState').finalizeResult.document.id, 42);
  }
});

test('停止等待在途上传收到accepted错误时保留显式重试，重试不重传PCM', async () => {
  const response = deferred();
  const rt = recordingRuntime(entry => entry.url.endsWith('/chunks') ? response.promise : finishResponse(), 1);
  const upload = rt.fn('lectureFlushChunk')();
  const rejectedUpload = assert.rejects(upload, /ASR delayed failure/);
  const stopping = rt.fn('lectureStopRecording')();
  const rejectedStop = assert.rejects(stopping, /ASR delayed failure/);
  response.resolve(jsonResponse({detail:'ASR delayed failure', accepted:true}, 503));
  await Promise.all([rejectedUpload, rejectedStop]);
  const st = rt.get('lectureState');
  assert.equal(st.audio.pendingChunk, null);
  assert.equal(st.audio.bytes, 32000);
  assert.equal(st.stopPending, true);
  assert.match(rt.element('lecturesResult').innerHTML, /ASR delayed failure/);
  await rt.fn('lectureStopRecording')();
  assert.deepEqual(rt.state.calls.map(call => call.url.split('/').pop()), ['chunks','finalize']);
});

test('一个慢上传的多个定时器等待者只能累计一次失败', async () => {
  const gate = deferred();
  const rt = recordingRuntime(async () => {
    await gate.promise;
    throw new TypeError('single failed request');
  }, 1);
  rt.fn('lectureStartTicker')();
  const tick = [...rt.state.intervals.values()][0];
  tick();
  const upload = rt.get('lectureState').audio.uploadPromise;
  tick();
  tick();
  const rejected = assert.rejects(upload, /single failed request/);
  gate.resolve();
  await rejected;
  const st = rt.get('lectureState');
  assert.equal(st.audio.errors, 1);
  assert.equal(st.recording, true);
  assert.equal(rt.state.calls.length, 1);
});

test('finalize响应丢失后的幂等回执恢复已有文档入口', async () => {
  let calls = 0;
  const rt = recordingRuntime(entry => {
    if(entry.url.endsWith('/chunks')) return jsonResponse({ok:true, segments:[]});
    calls += 1;
    if(calls === 1) throw new TypeError('finalize receipt lost');
    return jsonResponse({ok:true, session:{id:9, status:'ended', document_id:42, title:'课堂记录'}, document:null, segment_count:1});
  }, 1);
  await assert.rejects(rt.fn('lectureStopRecording')(), /receipt lost/);
  await rt.fn('lectureStopRecording')();
  assert.equal(rt.get('lectureState').finalizeResult.document.id, 42);
  assert.match(rt.element('lecturesResult').innerHTML, /data-lectures-open-doc="42"/);
  assert.doesNotMatch(rt.element('lecturesResult').innerHTML, /未生成笔记/);
  assert.equal(rt.state.calls.filter(call => call.url.endsWith('/chunks')).length, 1);
});

test('创建会话请求期间阻止导航、工作区切换和卸载', async () => {
  const created = deferred();
  const rt = createRuntime(() => created.promise, {navigator:fakeNavigator()});
  installWorkspaceGuard(rt);
  const writes = [];
  rt.context.apiPut = async (...args) => { writes.push(args); };
  const starting = rt.fn('lecturesStart')('mic');
  assert.equal(rt.element('lecturesStopBtn').hidden, false);
  assert.match(rt.element('lecturesStopBtn').textContent, /取消/);
  const allowed = rt.fn('lectureCanLeave')('chat');
  const event = {preventDefault(){ this.prevented = true; }};
  rt.fn('lectureBeforeUnload')(event);
  await rt.fn('switchWorkspace')('workpal');
  created.resolve(jsonResponse({session:{id:9, status:'live'}}));
  await starting;
  assert.equal(allowed, false);
  assert.equal(event.prevented, true);
  assert.equal(writes.length, 0);
  assert.equal(rt.get('S.workspace'), 'studypal');
});

test('工作区切换已在途时不能开始旁听', async () => {
  const rt = createRuntime(() => jsonResponse({session:{id:9}}), {navigator:fakeNavigator()});
  rt.context.S.workspaceSwitching = true;
  await rt.fn('lecturesStart')('mic');
  assert.equal(rt.state.calls.length, 0);
  assert.equal(rt.get('lectureState').recording, false);
});

test('创建会话未返回时取消，迟到会话被结束且不申请麦克风', async () => {
  const created = deferred();
  let permissions = 0;
  const rt = createRuntime(entry => entry.url.endsWith('/finalize') ? finishResponse() : created.promise,
    {navigator:{mediaDevices:{getUserMedia:async () => { permissions += 1; return new FakeMediaStream([fakeTrack('audio')]); }}}});
  const starting = rt.fn('lecturesStart')('mic');
  const stopping = rt.fn('lectureStopRecording')();
  created.resolve(jsonResponse({session:{id:9, status:'live'}}));
  await Promise.all([starting, stopping]);
  assert.equal(permissions, 0);
  assert.equal(rt.state.calls.filter(call => call.url.endsWith('/finalize')).length, 1);
  assert.equal(rt.get('lectureState').recording, false);
  assert.equal(rt.state.intervals.size, 0);
});

test('取消启动后创建请求失败，释放离开和重启守卫', async () => {
  const created = deferred();
  const rt = createRuntime(() => created.promise, {navigator:fakeNavigator()});
  const starting = rt.fn('lecturesStart')('mic');
  const stopping = assert.rejects(rt.fn('lectureStopRecording')(), /engine unavailable/);
  created.resolve(jsonResponse({detail:'engine unavailable'}, 503));
  await Promise.all([starting, stopping]);
  assert.equal(rt.get('lectureState').starting, false);
  assert.equal(rt.get('lectureState').stopping, false);
  assert.equal(rt.get('lectureState').stopPending, false);
  assert.equal(rt.element('lecturesMicBtn').disabled, false);
  assert.equal(rt.fn('lectureCanLeave')('chat'), true);
  assert.equal(rt.state.calls.filter(call => call.url.endsWith('/finalize')).length, 0);
});

test('停止使迟到授权失效并关闭轨道，旧启动不能覆盖新会话', async () => {
  for(const source of ['mic', 'system']){
    const permission = deferred();
    const requested = deferred();
    const lateTracks = [fakeTrack('audio')];
    if(source === 'system') lateTracks.push(fakeTrack('video'));
    let sessions = 8;
    let permissions = 0;
    const navigator = {mediaDevices:{}};
    const acquire = async () => {
      permissions += 1;
      if(permissions === 1){ requested.resolve(); return permission.promise; }
      return new FakeMediaStream([fakeTrack('audio')]);
    };
    navigator.mediaDevices.getUserMedia = acquire;
    navigator.mediaDevices.getDisplayMedia = acquire;
    const rt = createRuntime(entry => entry.url.endsWith('/finalize') ? finishResponse() : jsonResponse({session:{id:++sessions, status:'live'}}), {navigator});
    const first = rt.fn('lecturesStart')(source);
    await requested.promise;
    await rt.fn('lectureStopRecording')();
    await rt.fn('lecturesStart')('mic');
    const activeAudio = rt.get('lectureState').audio;
    permission.resolve(new FakeMediaStream(lateTracks));
    await first;
    assert.ok(lateTracks.every(track => track.stopped));
    assert.equal(rt.get('lectureState').session.id, 10);
    assert.equal(rt.get('lectureState').audio, activeAudio);
    assert.equal(rt.get('lectureState').recording, true);
    assert.equal(rt.state.intervals.size, 2);
    assert.equal(rt.state.toasts.filter(text => text.includes('已开始旁听')).length, 1);
  }
});

test('停止使等待AudioContext恢复的启动失效并关闭资源', async () => {
  const resumed = deferred();
  const waiting = deferred();
  const track = fakeTrack('audio');
  const contexts = [];
  const rt = createRuntime(entry => entry.url.endsWith('/finalize') ? finishResponse() : jsonResponse({session:{id:9, status:'live'}}),
    {navigator:{mediaDevices:{getUserMedia:async () => new FakeMediaStream([track])}}});
  rt.context.window.AudioContext = class extends FakeAudioContext {
    constructor(options){ super(options); this.state = 'suspended'; contexts.push(this); }
    resume(){ waiting.resolve(); return resumed.promise; }
  };
  const starting = rt.fn('lecturesStart')('mic');
  await waiting.promise;
  await rt.fn('lectureStopRecording')();
  resumed.resolve();
  await starting;
  assert.equal(track.stopped, true);
  assert.equal(contexts[0].closed, true);
  assert.equal(rt.get('lectureState').audio.stream, null);
  assert.equal(rt.state.intervals.size, 0);
});

test('旧会话丢弃响应不得清空新会话正在收尾的音频', async () => {
  const deleted = deferred();
  const uploading = deferred();
  const uploaded = deferred();
  const rt = createRuntime(async entry => {
    if(entry.url.endsWith('/chunks')){
      if(!rt.state.calls.some(call => call.url.endsWith('/finalize')) && rt.state.calls.filter(call => call.url.endsWith('/chunks')).length === 1){
        uploading.resolve(); await uploaded.promise;
      }
      return jsonResponse({ok:true, segments:[], duration_ms:20000});
    }
    if(entry.url.endsWith('/finalize')) return jsonResponse({ok:true, session:{id:10, status:'ended'}, document:{id:43}});
    return jsonResponse({session:{id:10, status:'live'}});
  }, {navigator:fakeNavigator(), apiDelete:() => deleted.promise});
  const st = rt.get('lectureState');
  st.session = {id:9, status:'ended'};
  const discarding = rt.fn('lectureDiscard')(9);
  await rt.fn('lecturesStart')('mic');
  st.audio.buffers = [new Float32Array(25 * 16000)];
  const stopping = rt.fn('lectureStopRecording')();
  const outcome = stopping.then(() => null, error => error);
  await uploading.promise;
  deleted.resolve({ok:true});
  await discarding;
  const retainedSession = st.session;
  const retainedFrames = st.audio.buffers.reduce((n, buffer) => n + buffer.length, 0);
  uploaded.resolve();
  const error = await outcome;
  assert.equal(retainedSession && retainedSession.id, 10);
  assert.equal(retainedFrames, 80000);
  assert.equal(error, null);
  assert.equal(st.finalizeResult.document.id, 43);
});

test('丢弃待重试会话期间不能重试或重复删除，失败后恢复重试', async () => {
  const deleted = deferred();
  const rt = recordingRuntime(() => jsonResponse({detail:'upload failed'}, 503), 1);
  await assert.rejects(rt.fn('lectureStopRecording')());
  rt.context.apiDelete = async requestPath => {
    rt.state.apiDeletes.push({path:requestPath});
    await deleted.promise;
    throw new Error('delete failed');
  };
  const discarding = rt.fn('lectureDiscard')(9);
  await rt.fn('lectureStopRecording')();
  await rt.fn('lectureDiscard')(9);
  const calls = rt.state.calls.length;
  const disabled = rt.element('lecturesStopBtn').disabled;
  deleted.resolve();
  await discarding;
  assert.equal(calls, 1);
  assert.equal(disabled, true);
  assert.equal(rt.state.apiDeletes.length, 1);
  assert.equal(rt.get('lectureState').stopPending, true);
  assert.equal(rt.element('lecturesStopBtn').disabled, false);
});

test('成功丢弃待重试会话同时释放稳定分片和未编码音频', async () => {
  const rt = recordingRuntime(() => jsonResponse({detail:'upload failed'}, 503), 25);
  await assert.rejects(rt.fn('lectureStopRecording')());
  const st = rt.get('lectureState');
  assert.ok(st.audio.pendingChunk);
  assert.equal(st.audio.buffers[0].length, 80000);
  rt.context.apiDelete = async () => ({ok:true});
  await rt.fn('lectureDiscard')(9);
  assert.equal(st.audio.pendingChunk, null);
  assert.equal(st.audio.buffers.length, 0);
  assert.equal(st.stopPending, false);
  assert.equal(st.session, null);
  assert.equal(rt.fn('lectureCanLeave')('chat'), true);
  assert.equal(rt.element('lecturesMicBtn').disabled, false);
});

test('停止排空全部25秒且48k采集仍按20秒分片', async () => {
  for(const rate of [16000, 48000]){
    const rt = recordingRuntime(entry => entry.url.endsWith('/chunks') ? jsonResponse({ok:true, segments:[]}) : finishResponse(), 25, rate);
    await rt.fn('lectureStopRecording')({finalize:true});
    assert.deepEqual(rt.state.calls.map(call => call.url.split('/').pop()), ['chunks','chunks','finalize']);
    assert.deepEqual(rt.state.calls.filter(call => call.url.endsWith('/chunks')).map(call => call.body.byteLength), [640000,160000]);
  }
});

test('停止等待在途上传，冻结采集，重复点击只finalize一次', async () => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const rt = recordingRuntime(async entry => {
    if(entry.url.endsWith('/chunks')){
      if(rt.state.calls.length === 1) await pending;
      return jsonResponse({ok:true, segments:[]});
    }
    return finishResponse();
  });
  const upload = rt.fn('lectureFlushChunk')();
  const stopping = rt.fn('lectureStopRecording')({finalize:true});
  const again = rt.fn('lectureStopRecording')({finalize:true});
  assert.strictEqual(stopping, again);
  const st = rt.get('lectureState');
  assert.equal(st.recording, false);
  rt.fn('lectureCollectSamples')({inputBuffer:{getChannelData:() => new Float32Array(16000)}});
  assert.equal(st.audio.buffers.reduce((n,b) => n+b.length,0), 80000);
  assert.equal(rt.state.calls.length, 1, 'cannot finalize before chunk response');
  release();
  await Promise.all([upload, stopping, again]);
  assert.deepEqual(rt.state.calls.map(c => c.url.split('/').pop()), ['chunks','chunks','finalize']);
});

test('普通上传保留100ms短缓冲，停止时上传尾音', async () => {
  const rt = recordingRuntime(entry => entry.url.endsWith('/chunks') ? jsonResponse({ok:true, segments:[]}) : finishResponse(), 0.1);
  await rt.fn('lectureFlushChunk')();
  assert.equal(rt.state.calls.length, 0);
  assert.equal(rt.get('lectureState').audio.buffers[0].length, 1600);
  await rt.fn('lectureStopRecording')({finalize:true});
  assert.equal(rt.state.calls[0].body.byteLength, 3200);
});

test('停止上传失败不finalize，保留队列、阻止新录音并可显式重试', async () => {
  let fail = true;
  const rt = recordingRuntime(entry => {
    if(entry.url.endsWith('/chunks')) return fail ? jsonResponse({detail:'暂时失败'},503) : jsonResponse({ok:true, segments:[]});
    return finishResponse();
  }, 1);
  await assert.rejects(rt.fn('lectureStopRecording')({finalize:true}), /暂时失败/);
  const st = rt.get('lectureState');
  assert.equal(rt.state.calls.length, 1);
  assert.equal(st.audio.pendingChunk.buffer.byteLength, 32000);
  assert.equal(st.audio.buffers.length, 0);
  assert.equal(st.recording, false);
  assert.equal(rt.element('lecturesStopBtn').hidden, false);
  assert.match(rt.element('lecturesStopBtn').textContent, /重试/);
  assert.equal(rt.element('lecturesMicBtn').disabled, true);
  assert.match(st.finalizeResult.error, /暂时失败/);
  fail = false;
  await rt.fn('lectureStopRecording')({finalize:true});
  assert.equal(st.finalizeResult.document.id, 42);
  assert.equal(rt.state.calls.filter(c => c.url.endsWith('/finalize')).length, 1);
});

test('finalize失败可重试且不重发已成功音频，保存期间阻止刷新', async () => {
  let fail = true;
  const rt = recordingRuntime(entry => {
    if(entry.url.endsWith('/chunks')) return jsonResponse({ok:true, segments:[]});
    return fail ? jsonResponse({detail:'收尾转写失败'},500) : finishResponse();
  }, 1);
  await assert.rejects(rt.fn('lectureStopRecording')(), /收尾转写失败/);
  const event = {preventDefault(){ this.prevented = true; }};
  rt.fn('lectureBeforeUnload')(event);
  assert.equal(event.prevented, true);
  fail = false;
  await rt.fn('lectureStopRecording')();
  assert.equal(rt.state.calls.filter(c => c.url.endsWith('/chunks')).length,1);
  assert.equal(rt.get('lectureState').finalizeResult.document.id,42);
});

test('工作区切换在旁听收尾之前不能修改范围，禁止丢弃正在收尾的会话', async () => {
  const rt = recordingRuntime(() => finishResponse(), 0);
  const switchCode = html.slice(html.indexOf('async function switchWorkspace(key){'), html.indexOf('\nasync function renderWorkspace('));
  const body = switchCode.slice(0, switchCode.indexOf("el('workspaceMenu')"));
  vm.runInContext(body + '\n}', rt.context);
  rt.context.S.workspaces = [{key:'studypal'},{key:'workpal'}];
  rt.context.chatSending = false;
  rt.context.renderWorkspacePicker = () => {};
  rt.context.apiPut = async () => { throw new Error('must not write profile'); };
  await rt.fn('switchWorkspace')('workpal');
  assert.equal(rt.get('S.workspaceSwitching'), undefined);
  const st = rt.get('lectureState');
  st.stopPending = true;
  st.stopping = true;
  await rt.fn('lectureDiscard')(9);
  assert.equal(rt.state.apiDeletes.length, 0);
});

test('旁听页注册：导航项、页面容器、渲染分发、离开守卫与模块锚点', () => {
  assert.match(html, /<section class="page" id="p-lectures">/);
  // 0.6.3 起旁听不再单列在左侧导航，只保留 #lectures 路由（入口在「录入」页）。
  assert.match(html, /const ROUTE_ONLY = \['lectures'\];/);
  assert.doesNotMatch(html, /\{id:'lectures', ic:'🎙'/);
  assert.match(html, /else if\(id==='lectures'\) renderLecturesPage\(\);/);
  assert.match(html, /if\(typeof lectureCanLeave === 'function' && !lectureCanLeave\(id\)\) return;/);
  assert.match(html, /if\(S\.page === 'lectures'\)\{ renderLecturesContextSidebar\(\); return; \}/);
  assert.match(html, /lectures: \{kicker:'旁听记录'/);
  assert.match(html, /id="lecturesMicBtn"/);
  assert.match(html, /id="lecturesSystemBtn"/);
  assert.match(html, /id="lecturesStopBtn"/);
  assert.match(html, /id="lecturesFeed"[^>]*aria-live="polite"/);
  assert.match(html, /id="lecturesHistory"/);
  assert.ok(html.includes('.lectures-layout{display:grid'), 'lectures css block present');
  assert.ok(LECTURES_SLICE.includes('const LECTURES_SAMPLE_RATE = 16000;'), '16k sample rate pinned');
  assert.ok(LECTURES_SLICE.includes("'/api/asr/status'"), 'status api used');
});

test('时长与大小格式化', () => {
  const rt = createRuntime();
  assert.equal(rt.fn('lectureClockText')(0), '00:00');
  assert.equal(rt.fn('lectureClockText')(65000), '01:05');
  assert.equal(rt.fn('lectureClockText')(3661000), '1:01:01');
  assert.equal(rt.fn('lectureDurationText')(125), '02:05');
  assert.equal(rt.fn('lectureBytesText')(1048576), '1.0 MB');
  assert.equal(rt.fn('lectureBytesText')(0), '');
});

test('状态卡：引擎缺失显示 install_hint，去重模型卡片与下载按钮', () => {
  const rt = createRuntime();
  const st = rt.get('lectureState');
  st.statusLoading = false;
  st.status = {
    engine:{available:false, install_hint:'pip install funasr'},
    models:{
      turbo:{name:'turbo', label:'高精度', present:false, size_bytes:1048576},
      small:{name:'small', label:'快速', present:true, size_bytes:524288}
    },
    download:null
  };
  st.model = 'turbo';
  const markup = rt.fn('lectureStatusHtml')();
  assert.ok(markup.includes('本地转写引擎未安装'));
  assert.ok(markup.includes('pip install funasr'));
  assert.ok(markup.includes('data-lectures-download="turbo"'));
  assert.ok(markup.includes('已下载，可以使用'));
  assert.ok(!markup.includes('data-lectures-download="small"'));
});

test('状态卡：下载中显示百分比进度，错误态可重试', () => {
  const rt = createRuntime();
  const st = rt.get('lectureState');
  st.statusLoading = false;
  st.status = {
    engine:{available:true},
    models:{turbo:{name:'turbo', label:'高精度', present:false, size_bytes:2097152}},
    download:{model:'turbo', state:'downloading', percent:42.5, downloaded:1048576, total:2097152, error:null}
  };
  st.model = 'turbo';
  let markup = rt.fn('lectureStatusHtml')();
  assert.ok(markup.includes('width:42.5%'));
  assert.ok(markup.includes('下载中 42.5%'));

  st.status.download = {model:'turbo', state:'error', percent:0, error:'网络<断开>'};
  markup = rt.fn('lectureStatusHtml')();
  assert.ok(markup.includes('下载失败'));
  assert.ok(markup.includes('网络&lt;断开&gt;'));
  assert.ok(markup.includes('data-lectures-download="turbo"'));
});

test('状态卡：状态读取失败展示重试入口', () => {
  const rt = createRuntime();
  const st = rt.get('lectureState');
  st.statusLoading = false;
  st.status = null;
  st.statusError = 'asr status → 500';
  const markup = rt.fn('lectureStatusHtml')();
  assert.ok(markup.includes('asr status → 500'));
  assert.ok(markup.includes('data-lectures-status-retry="1"'));
});

test('字幕段渲染：mm:ss 时间戳且文本被转义', () => {
  const rt = createRuntime();
  const markup = rt.fn('lectureSegmentHtml')({start_ms:65000, end_ms:68000, text:'<img src=x onerror="alert(1)">审计'});
  assert.ok(markup.includes('<time>01:05</time>'));
  assert.ok(markup.includes('&lt;img src=x onerror=&quot;alert(1)&quot;&gt;审计'));
  assert.ok(!markup.includes('<img'));
});

test('历史会话卡片：元信息、丢弃按钮与转义', () => {
  const rt = createRuntime();
  const item = {
    id:11, title:'审计 <b>第一课</b>', course:'审计学', source:'system', status:'ended',
    started_at:'2026-10-22 10:00', duration_seconds:125, segment_count:12,
    preview:'<script>alert(1)</script>'
  };
  let markup = rt.fn('lectureHistoryItemHtml')(item);
  assert.ok(markup.includes('data-lectures-history="11"'));
  assert.ok(markup.includes('&lt;b&gt;第一课&lt;/b&gt;'));
  assert.ok(markup.includes('系统声音'));
  assert.ok(markup.includes('02:05'));
  assert.ok(markup.includes('12 段'));
  assert.ok(markup.includes('已结束'));
  assert.ok(markup.includes('data-lectures-discard="11"'));
  assert.ok(!markup.includes('<script>'));

  markup = rt.fn('lectureHistoryItemHtml')({id:12, title:'已丢', status:'discarded'});
  assert.ok(markup.includes('已丢弃'));
  assert.ok(!markup.includes('data-lectures-discard="12"'));
});

test('PCM16 编码：16k 单声道小端', () => {
  const rt = createRuntime();
  const buffer = rt.fn('lectureEncodePcm16')(new Float32Array([0, 0.5, -0.5, 1, -1]), 16000);
  assert.equal(buffer.byteLength, 10);
  const view = new DataView(buffer);
  assert.equal(view.getInt16(0, true), 0);
  assert.equal(view.getInt16(2, true), 16384);
  assert.equal(view.getInt16(4, true), -16384);
  assert.equal(view.getInt16(6, true), 32767);
  assert.equal(view.getInt16(8, true), -32768);

  const resampled = rt.fn('lectureResample')(new Float32Array([0, 0.25, 0.5, 0.75]), 32000, 16000);
  assert.equal(resampled.length, 2);
  assert.ok(Math.abs(resampled[1] - 0.5) < 1e-6);
});

test('采集缓冲合并、空文本过滤与实时字幕渲染', () => {
  const rt = createRuntime();
  const st = rt.get('lectureState');
  st.audio.buffers = [new Float32Array([0.1, 0.2]), new Float32Array([0.3])];
  const merged = rt.fn('lectureTakeBuffers')();
  assert.equal(merged.length, 3);
  assert.equal(st.audio.buffers.length, 0);

  rt.fn('lectureAppendSegment')({seq:1, start_ms:1000, end_ms:2000, text:'  审计证据  ', confidence:0.9});
  rt.fn('lectureAppendSegment')({seq:2, start_ms:2000, text:'   '});
  assert.equal(st.segments.length, 1);
  assert.equal(st.segments[0].text, '审计证据');
  assert.ok(rt.element('lecturesFeed').innerHTML.includes('00:01'));
  assert.ok(rt.element('lecturesFeed').innerHTML.includes('审计证据'));
});

test('分片上传：octet-stream、ArrayBuffer body、错误携带状态码', async () => {
  const rt = createRuntime(entry => {
    if(entry.url.includes('/chunks')) return jsonResponse({ok:true, segments:[{seq:1, start_ms:0, text:'第一段'}], duration_ms:2000});
    return null;
  });
  const st = rt.get('lectureState');
  st.session = {id:7, course:'审计学'};
  const data = await rt.fn('lecturePushChunk')(new ArrayBuffer(8));
  assert.equal(data.segments[0].text, '第一段');
  const call = rt.state.calls[0];
  assert.ok(call.url.endsWith('/api/lectures/7/chunks'));
  assert.equal(call.method, 'POST');
  assert.equal(call.headers['Content-Type'], 'application/octet-stream');
  assert.equal(call.body.byteLength, 8);

  const failing = createRuntime(() => jsonResponse({detail:'转写引擎未安装'}, 503));
  failing.get('lectureState').session = {id:8};
  let caught = null;
  try{ await failing.fn('lecturePushChunk')(new ArrayBuffer(4)); }catch(error){ caught = error; }
  assert.ok(caught);
  assert.equal(caught.lectureStatus, 503);
  assert.match(caught.message, /转写引擎未安装/);
});

test('开始旁听：创建会话字段与录音启动', async () => {
  const rt = createRuntime(entry => {
    if(entry.url === '/api/lectures' && entry.method === 'POST'){
      return jsonResponse({ok:true, session:{id:21, course:'审计学', title:'审计学 · 旁听', source:'mic', status:'live', model:'turbo'}});
    }
    return null;
  }, {navigator:fakeNavigator()});
  await rt.fn('lecturesStart')('mic');
  const st = rt.get('lectureState');
  assert.equal(rt.state.calls.length, 1);
  const call = rt.state.calls[0];
  assert.equal(call.url, '/api/lectures');
  assert.equal(call.method, 'POST');
  const body = JSON.parse(call.body);
  assert.deepEqual(body, {workspace_key:'studypal', course:'审计学', title:'', source:'mic', model:'small', idempotency_key:''});
  assert.equal(st.recording, true);
  assert.equal(st.session.id, 21);
  assert.equal(st.audio.sampleRate, 16000);
  assert.equal(rt.state.intervals.size, 2, 'chunk timer and ui timer started');
  assert.ok(rt.state.toasts.some(text => text.includes('已开始旁听')));
});

test('开始旁听失败：503 引导安装、409 引导下载模型', async () => {
  const engineMissing = createRuntime(() => jsonResponse({detail:'本地转写引擎未安装'}, 503), {navigator:fakeNavigator()});
  await engineMissing.fn('lecturesStart')('mic');
  assert.equal(engineMissing.get('lectureState').recording, false);
  assert.ok(engineMissing.state.toasts.some(text => text.includes('引擎') && text.includes('安装')));
  assert.ok(engineMissing.element('lecturesStatus').scrolled, '状态卡被滚动聚焦');

  const modelMissing = createRuntime(() => jsonResponse({detail:'转写模型未下载'}, 409), {navigator:fakeNavigator()});
  await modelMissing.fn('lecturesStart')('system');
  assert.equal(modelMissing.get('lectureState').recording, false);
  assert.ok(modelMissing.state.toasts.some(text => text.includes('下载模型')));
});

test('停止旁听：先推最后分片，再 finalize 并渲染打开笔记', async () => {
  const rt = createRuntime(entry => {
    if(entry.url.includes('/chunks')) return jsonResponse({ok:true, segments:[{seq:1, start_ms:0, end_ms:2000, text:'最后一段'}], duration_ms:2000});
    if(entry.url.includes('/finalize')) return jsonResponse({ok:true, session:{id:9, status:'ended', course:'审计学', title:'审计学 · 旁听'}, document:{id:42, title:'审计学 课堂记录'}, segment_count:1});
    return null;
  });
  const st = rt.get('lectureState');
  st.recording = true;
  st.session = {id:9, course:'审计学'};
  st.segments = [{seq:0, start_ms:0, text:'旧字幕'}];
  st.audio = {ctx:null, source:null, processor:null, stream:null, buffers:[new Float32Array(3200).fill(0.2)], sampleRate:16000, busy:false, errors:0, bytes:0, chunkTimer:null, uiTimer:null, startedAt:Date.now()};
  await rt.fn('lectureStopRecording')({finalize:true});

  const chunkCall = rt.state.calls.find(call => call.url.includes('/chunks'));
  assert.ok(chunkCall, 'last chunk flushed before finalize');
  assert.equal(chunkCall.headers['Content-Type'], 'application/octet-stream');
  const finalizeCall = rt.state.calls.find(call => call.url.includes('/finalize'));
  assert.ok(finalizeCall.url.endsWith('/api/lectures/9/finalize'));
  assert.deepEqual(JSON.parse(finalizeCall.body), {title:'', make_document:true});
  assert.equal(st.recording, false);
  assert.equal(st.finalizeResult.document.id, 42);
  assert.ok(rt.element('lecturesResult').innerHTML.includes('已生成课堂记录'));
  assert.ok(rt.element('lecturesResult').innerHTML.includes('data-lectures-open-doc="42"'));
  assert.ok(rt.state.toasts.some(text => text.includes('已生成课堂记录')));
});

test('下载模型：调用契约路径并在完成后轮询', async () => {
  const rt = createRuntime(
    () => jsonResponse({engine:{available:true}, models:{turbo:{present:false}}, download:{model:'turbo', state:'downloading', percent:1}}),
    {apiPost:() => ({ok:true, started:true, download:{model:'turbo', state:'downloading', percent:1}})}
  );
  await rt.fn('lectureDownloadModel')('turbo');
  assert.equal(rt.state.apiPosts.length, 1);
  assert.equal(rt.state.apiPosts[0].path, '/api/asr/models/turbo/download');
  assert.equal(JSON.stringify(rt.state.apiPosts[0].body), '{}');
  assert.equal(rt.state.timeouts.size, 1, 'status poll scheduled');
  assert.ok(rt.state.toasts.some(text => text.includes('下载模型')));
});

test('历史列表：加载、渲染与失败重试文案', async () => {
  const rt = createRuntime(null, {
    api:() => ({items:[{id:5, title:'审计证据课', course:'审计学', source:'mic', status:'ended', duration_seconds:65, segment_count:3, preview:'证据链'}, {id:6, title:'会计课', status:'live'}]})
  });
  await rt.fn('lectureLoadHistory')();
  assert.equal(rt.state.apiCalls[0].path, '/api/lectures');
  assert.equal(rt.state.apiCalls[0].params.workspace_key, 'studypal');
  assert.equal(rt.state.apiCalls[0].params.limit, 20);
  assert.equal(rt.get('lectureState').history.items.length, 2);
  assert.ok(rt.element('lecturesHistorySub').textContent.includes('最近 2 次'));
  assert.ok(rt.element('lecturesHistory').innerHTML.includes('data-lectures-history="5"'));
  assert.ok(rt.element('lecturesHistory').innerHTML.includes('证据链'));

  const failing = createRuntime(null, {api:() => { throw new Error('数据库不可用'); }});
  await failing.fn('lectureLoadHistory')();
  assert.ok(failing.element('lecturesHistory').innerHTML.includes('读取失败'));
  assert.ok(failing.element('lecturesHistory').innerHTML.includes('data-lectures-history-retry="1"'));
});

test('会话详情：打开渲染全文与打开笔记，Esc 关闭', async () => {
  const rt = createRuntime(entry => {
    if(entry.url === '/api/lectures/5') return jsonResponse({session:{id:5, title:'审计学 · 旁听', course:'审计学', duration_seconds:125, document_id:42}, segments:[{seq:1, start_ms:0, end_ms:2000, text:'你好'}]});
    return null;
  });
  await rt.fn('lectureOpenDetail')(5);
  assert.equal(rt.element('lecturesDetailCard').hidden, false);
  assert.ok(rt.element('lecturesDetailBody').innerHTML.includes('你好'));
  assert.ok(rt.element('lecturesDetailBody').innerHTML.includes('data-lectures-open-doc="42"'));
  const st = rt.get('lectureState');
  assert.equal(st.detail.open, true);

  const event = {key:'Escape', defaultPrevented:false, preventDefault(){ this.defaultPrevented = true; }};
  rt.fn('lectureKeydown')(event);
  assert.equal(event.defaultPrevented, true);
  assert.equal(st.detail.open, false);
  assert.equal(rt.element('lecturesDetailCard').hidden, true);
});

test('录音中刷新拦截与切页确认，保存完成后才允许离开', async () => {
  const rt = createRuntime(() => finishResponse());
  const st = rt.get('lectureState');
  st.recording = false;
  assert.equal(rt.fn('lectureBeforeUnload')({preventDefault(){ this.called = true; }}), undefined);

  st.recording = true;
  st.session = {id:9, status:'live'};
  const event = {preventDefault(){ this.called = true; }, returnValue:null};
  const message = rt.fn('lectureBeforeUnload')(event);
  assert.equal(event.called, true);
  assert.match(String(message), /正在旁听/);
  assert.equal(rt.fn('lectureCanLeave')('lectures'), true);
  assert.equal(rt.fn('lectureCanLeave')('chat'), false);
  assert.equal(st.recording, false, 'capture stops before navigation can proceed');
  assert.equal(rt.fn('lectureCanLeave')('chat'), false);
  await st.stopPromise;
  assert.equal(rt.fn('lectureCanLeave')('chat'), true);

  const refusing = createRuntime(null, {confirm:() => false});
  refusing.get('lectureState').recording = true;
  assert.equal(refusing.fn('lectureCanLeave')('chat'), false);
  assert.equal(refusing.get('lectureState').recording, true);
});

test('默认模型：实时旁听优先 small，small 未下载才退到 turbo', () => {
  const rt = createRuntime();
  const st = rt.get('lectureState');
  st.status = {engine:{available:true}, models:{
    turbo:{name:'turbo', label:'高精度', present:true},
    small:{name:'small', label:'快速', present:false}
  }};
  st.model = 'small';
  rt.fn('lecturePickDefaultModel')();
  assert.equal(st.model, 'turbo');

  st.status.models.small.present = true;
  st.model = 'nope';
  rt.fn('lecturePickDefaultModel')();
  assert.equal(st.model, 'small');
  assert.equal(rt.get('LECTURES_LIVE_MODEL'), 'small');
});

test('状态卡：模型卡展示说明与「实时推荐」标记', () => {
  const rt = createRuntime();
  const st = rt.get('lectureState');
  st.statusLoading = false;
  st.status = {
    engine:{available:true},
    models:{
      turbo:{name:'turbo', label:'高精度', present:true, note:'较慢，适合精校'},
      small:{name:'small', label:'快速', present:true, note:'实时旁听默认模型'}
    },
    download:null
  };
  st.model = 'small';
  const markup = rt.fn('lectureStatusHtml')();
  assert.ok(markup.includes('实时推荐'));
  assert.ok(markup.includes('实时旁听默认模型'));
  assert.ok(markup.includes('较慢，适合精校'));
  assert.ok(markup.includes('实时旁听建议用这个'));
});

test('分片上传：单次最多 20 秒，余量留在缓冲区', async () => {
  const rt = createRuntime(entry => {
    if(entry.url.includes('/chunks')) return jsonResponse({ok:true, segments:[], duration_ms:20000});
    return null;
  });
  const st = rt.get('lectureState');
  st.recording = true;
  st.session = {id:9};
  st.audio.sampleRate = 16000;
  st.audio.busy = false;
  st.audio.buffers = [new Float32Array(16000 * 25)];
  await rt.fn('lectureFlushChunk')();
  const call = rt.state.calls[0];
  assert.equal(call.body.byteLength, 16000 * 20 * 2);
  const rest = st.audio.buffers[0];
  assert.equal(rest.length, 16000 * 5);
  assert.equal(st.audio.busy, false);
  assert.equal(st.audio.bytes, 16000 * 20 * 2);
});

test('状态刷新会同步刷新左侧旁听侧栏', async () => {
  const rt = createRuntime(entry => {
    if(entry.url === '/api/asr/status') return jsonResponse({engine:{available:true}, models:{small:{name:'small', present:true}}, download:null});
    return null;
  });
  const nav = rt.element('contextNav');
  await rt.fn('lectureFetchAsrStatus')();
  assert.ok(nav.innerHTML.includes('引擎已就绪'));
  assert.ok(nav.innerHTML.includes('模型 快速（small） 已下载'));
});
