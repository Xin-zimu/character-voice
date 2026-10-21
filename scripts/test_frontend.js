// Execute the actual inline script. Only DOM/storage/network boundaries are stubbed.
// HTTP tests below use the real server.py and a local fake upstream; no API key needed.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {spawn, spawnSync} = require('node:child_process');
const {once} = require('node:events');
const ROOT = path.resolve(__dirname, '..');
const PY = process.env.PYTHON || (process.platform === 'win32' ? 'py' : 'python3');
const PY_ENV = {...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8'};
const cards = ['yuki', 'yuki_school'].map(id =>
  JSON.parse(fs.readFileSync(path.join(ROOT, 'personas', id + '.json'), 'utf8')));
const worlds = {normal: {persona: 'yuki', name: '主线', en: 'NORMAL'},
  crash: {persona: 'yuki_school', name: '崩溃', en: 'CRASH'}};
const HTML = fs.readFileSync(path.join(ROOT, 'web/index.html'), 'utf8');
let js = HTML.match(/<script>([\s\S]*?)<\/script>/)[1];
const cut = js.lastIndexOf('})();');
assert(cut >= 0, 'inline script IIFE must exist');
js = js.slice(0, cut) + `
  globalThis.__T = {
    historyForApi, checkReply, saveHist, send, switchWorld, regenerate,
    createTypewriter, applyTheme, MAX_PERSIST,
    setState(p, h) {
      personas[p.id] = p; world = p.world;
      hist = {normal: [], crash: []}; hist[world] = h;
      loadedWorlds.clear(); loadedWorlds.add(world);
    },
    getHistory: (w = world) => hist[w], getWorld: () => world, isBusy: () => busy,
    setSettings(s) { settings = {...settings, ...s}; },
  };
` + js.slice(cut);

function mkEl() {
  const selectors = new Map(), classes = new Set();
  return {
    textContent: '', innerHTML: '', value: '', className: '', dataset: {}, children: [],
    disabled: false, scrollHeight: 0,
    style: {setProperty(k, v) { this[k] = v; }},
    classList: {add: k => classes.add(k), remove: k => classes.delete(k),
      toggle() {}, contains: k => classes.has(k)},
    appendChild(c) { this.children.push(c); return c; }, remove() {},
    querySelector(s) { if (!selectors.has(s)) selectors.set(s, mkEl()); return selectors.get(s); },
    addEventListener() {}, focus() {},
  };
}

async function harness({manualTimers = false, reducedMotion = true} = {}) {
  const els = new Map(), store = {}, timers = [];
  const state = {quotaFull: false, fetchChat: null};
  const document = {
    querySelector(s) { if (!els.has(s)) els.set(s, mkEl()); return els.get(s); },
    querySelectorAll: () => [], createElement: mkEl,
    documentElement: {style: {setProperty(k, v) { this[k] = v; }}},
  };
  const context = {document, TextDecoder, console, confirm: () => true,
    setTimeout: manualTimers ? (fn, delay) => { timers.push({fn, delay}); return timers.length; } : setTimeout,
    localStorage: {
      getItem: k => store[k] ?? null,
      setItem(k, v) { if (state.quotaFull) throw new Error('QuotaExceededError'); store[k] = v; },
      removeItem: k => { delete store[k]; },
    },
    fetch: async (url, options) => {
      if (url === '/api/personas') return {json: async () => cards};
      if (url === '/api/worlds') return {json: async () => ({worlds, corpus: {lines: 13070, episodes: []}})};
      return state.fetchChat(url, options);
    },
    addEventListener() {}, matchMedia: () => ({matches: reducedMotion}),
  };
  context.window = context;
  vm.runInNewContext(js, context, {filename: path.join(ROOT, 'web/index.html')});
  await new Promise(setImmediate);
  const T = context.__T;
  T.setSettings({api_key: 'local-fixture-only'});
  return {T, els, store, state, timers, document};
}

let passed = 0;
function ok(cond, description) {
  assert(cond, description);
  console.log(`  ✓ ${++passed}. ${description}`);
}
const history = Array.from({length: 30}, (_, i) => [
  {role: 'user', content: '问题' + i}, {role: 'assistant', content: '答' + i}]).flat();

async function main() {
  const h = await harness(), {T} = h;
  T.setState(cards[0], history);
  // v2 moved message composition to Python. Exercise that actual implementation
  // with history produced by the real frontend, including world/protocol layers.
  const pyCode = `import json,sys; from pathlib import Path; import prompt_builder as p
d=json.load(sys.stdin); card=json.loads(Path('personas/yuki.json').read_text(encoding='utf-8'))
print(json.dumps([p.build_messages(card,'normal',[],h,'本次提问') for h in d],ensure_ascii=True))`;
  const built = spawnSync(PY, ['-c', pyCode], {cwd: ROOT, env: PY_ENV, encoding: 'utf8',
    input: JSON.stringify([T.historyForApi(), history.slice(0, 6)])});
  assert.equal(built.status, 0, built.stderr);
  const [long, short] = JSON.parse(built.stdout);
  const real = msgs => msgs.slice(msgs.findIndex(m => m.content.startsWith('以上是语气示例')) + 1, -1)
    .filter(m => m.role !== 'system');
  const conv = real(long);
  // Preserve the original v1 suite's 14 acceptance checks.
  ok(conv.length === 24, '最近 12 轮历史进入实际 prompt');
  ok(conv[0].role === 'user', '截断后首条为 user');
  ok(conv.at(-1).content === '答29', '最新历史保留');
  ok(conv[0].content === '问题18', '从正确轮次开始');
  ok(long.some(m => m.role === 'system' && m.content.includes('已省略')), '截断带省略提示');
  ok(long[0].content.startsWith(cards[0].system_prompt.trim()), '人格 prompt 仍在最前，保留世界层');
  ok(real(short).length === 6, '短对话完整保留');
  ok(!short.some(m => m.content.includes('已省略')), '短对话无省略提示');
  h.state.quotaFull = true;
  assert.doesNotThrow(() => T.saveHist());
  ok(true, '存储配额满不外抛');
  h.state.quotaFull = false;
  T.setState(cards[0], Array.from({length: 500}, (_, i) => ({role: 'user', content: '' + i})));
  T.saveHist();
  ok(JSON.parse(h.store['yuki.hist.v2.normal']).length === T.MAX_PERSIST, '每个世界最多持久化 200 条');
  let bad = [];
  for (const p of cards) {
    T.setState(p, []);
    for (const e of p.examples) if (T.checkReply(e.assistant, e.user)) bad.push(e.user);
  }
  ok(bad.length === 0, '两张卡全部示例不触发 △');

  // 字数配额已取消（DEC-001）：长回复不得仅因长度被标记。
  T.setState(cards[0], []);
  ok(T.checkReply('字'.repeat(200), '') === '', '超长回复不因长度产生违例');
  ok(T.checkReply('这是一段比较长的回答，用来确认长度本身不再是判据。', '这段代码为什么慢') === '',
    '任务型长回答同样不因长度产生违例');
  ok(!cards[0].guard.tech_keywords && !cards[1].guard.tech_keywords,
    '人格卡不再含 tech_keywords（档位取消后无用途）');
  ok(!Object.keys(cards[0].guard).some(k => k.startsWith('max_chars')) &&
     !Object.keys(cards[1].guard).some(k => k.startsWith('max_chars')),
    '人格卡不再含 max_chars 字段（防回流）');

  T.setState(cards[0], []);
  ok(T.checkReply('看书。\n\n另外，温度在下降。').includes('分段'), '保留 v2 分段检测');
  ok(T.checkReply('作为AI，我可以回答。').includes('「作为AI」'), '助手腔仍被检测');
  T.applyTheme();
  ok(h.document.documentElement.style['--user'] === cards[0].theme.bubble_user, '用户气泡使用正确主题键');

  // Python simulator and actual JS must agree, including astral characters.
  // 长度不再是判据，所以这些超长样本必须两边都判为「无违例」。
  const cases = cards.flatMap(p => [
    ...p.examples.map(e => [p.guard, e.assistant, e.user]),
    [p.guard, '看书。\n\n另外，温度在下降。', ''],
    [p.guard, '字'.repeat(70), ''], [p.guard, '😀'.repeat(35), ''],
    [p.guard, '字'.repeat(400), ''],
    [p.guard, '作为AI，我可以回答。', ''],
  ].map(c => ({id: p.id, c})));
  const sim = spawnSync(PY, ['-c',
    'import json,sys; from scripts.check_persona import simulate_check_reply as s; print(json.dumps([s(c[1],c[2],c[0]) for c in json.load(sys.stdin)],ensure_ascii=True))'],
    {cwd: ROOT, env: PY_ENV, input: JSON.stringify(cases.map(x => x.c)), encoding: 'utf8'});
  assert.equal(sim.status, 0, sim.stderr);
  JSON.parse(sim.stdout).forEach((expected, i) => {
    T.setState(cards.find(p => p.id === cases[i].id), []);
    assert.equal(T.checkReply(cases[i].c[1]), expected);
  });
  ok(true, 'Python 自检与前端逐例一致（含超长样本）');

  // 防回流的直接验证：删掉字段后，两个检测器都不再产生长度告警。
  for (const p of cards) {
    assert(!Object.keys(p.guard).some(k => k.startsWith('max_chars')));
    assert.equal(T.checkReply('字'.repeat(500)), '');
  }
  ok(true, '字段删除后两个检测器均不再报长度');

  for (const p of cards) {
    const clock = await harness({manualTimers: true, reducedMotion: false});
    const el = mkEl(), caret = mkEl();
    const writer = clock.T.createTypewriter(el, caret, p);
    writer.update('YU');
    assert.equal(clock.timers.length, 0);
    writer.update('YUKI.N>你😀好');
    const done = writer.finish();
    for (const expected of ['你', '你😀', '你😀好']) {
      const timer = clock.timers.shift();
      assert.equal(timer.delay, p.protocol.type_delay_ms);
      timer.fn();
      assert.equal(el.textContent, expected);
    }
    await done;
    ok(true, `${p.world} 按 ${p.protocol.type_delay_ms}ms 逐字显示，前缀隐藏、emoji 完整`);
  }

  // Network read rejection is a separate path from clean EOF or SSE errors.
  T.setState(cards[0], []);
  let reads = 0;
  h.state.fetchChat = async () => ({ok: true, body: {getReader: () => ({
    async read() {
      if (reads++) throw new Error('socket interrupted');
      return {value: Buffer.from('data: {"choices":[{"delta":{"content":"已经收到。"}}]}\n\n'), done: false};
    }, async cancel() {}, releaseLock() {},
  })}});
  h.els.get('#input').value = '测试';
  await T.send();
  ok(T.getHistory().at(-1).content === '已经收到。' && T.getHistory().at(-1).warn === '流中断',
    'reader.read 抛错仍保留收到的文字');

  const fixture = spawn(PY, ['-u', path.join(__dirname, 'stream_fixture.py')],
    {cwd: ROOT, windowsHide: true, env: PY_ENV});
  let stderr = '';
  fixture.stderr.on('data', b => { stderr += b; });
  const exit = once(fixture, 'exit');
  const timeout = setTimeout(() => fixture.kill(), 30000);
  try {
    const ports = await new Promise((resolve, reject) => {
      let out = '';
      fixture.stdout.on('data', chunk => {
        out += chunk;
        if (out.includes('\n')) { try { resolve(JSON.parse(out.split('\n')[0])); } catch (e) { reject(e); } }
      });
      fixture.once('error', reject);
      fixture.once('exit', () => reject(new Error('fixture exited: ' + stderr)));
    });
    const base = `http://127.0.0.1:${ports.app}`;
    const http = await harness();
    let request;
    http.state.fetchChat = (url, opts) => { request = JSON.parse(opts.body); return fetch(base + url, opts); };
    http.T.setSettings({base_url: `http://127.0.0.1:${ports.upstream}/v1`});
    for (const world of ['normal', 'crash']) {
      for (const mode of ['ok', 'broken', 'eof', 'error-frame', 'finish-only', 'empty-eof', 'http-error']) {
        http.T.setState(cards.find(p => p.world === world), []);
        http.T.setSettings({model: mode});
        http.els.get('#input').value = '测试流';
        const sending = http.T.send();
        http.T.switchWorld(world === 'normal' ? 'crash' : 'normal');
        http.els.get('#clear').onclick();
        assert.equal(http.T.getWorld(), world);
        assert(http.T.isBusy());
        await sending;
        const answer = http.T.getHistory().at(-1);
        const partial = ['broken', 'eof', 'error-frame'].includes(mode);
        const empty = ['empty-eof', 'http-error'].includes(mode);
        assert.equal(answer.content, empty ? '……连接失败。' : '已经收到。');
        assert.equal(answer.warn === '流中断', partial);
        if (!empty && !partial) assert.equal(answer.warn, '');
        if (empty) assert(answer.warn);
        assert.equal(JSON.parse(http.store[`yuki.hist.v2.${world}`]).at(-1).content, answer.content);
        assert.equal(http.T.getHistory(world === 'normal' ? 'crash' : 'normal').length, 0);
        assert(!http.T.isBusy());
        ok(true, `HTTP 全链路 ${world}/${mode}：内容、告警、存档及世界隔离`);
      }
    }
    http.T.setState(cards[0], [{role: 'user', content: '旧问题'}, {role: 'assistant', content: '旧回答'}]);
    http.T.setSettings({model: 'inspect'});
    http.els.get('#input').value = '当前问题';
    await http.T.send();
    assert.equal(request.history.length, 2);
    const actual = JSON.parse(http.T.getHistory().at(-1).content);
    assert.deepEqual(actual.map(m => m.content), ['旧问题', '旧回答', '当前问题']);
    ok(true, '实际上游收到当前提问恰好一次，且没有空 assistant');

    http.T.setState(cards[0], []);
    http.T.setSettings({model: 'ok'});
    http.state.quotaFull = true;
    http.els.get('#input').value = '存储满';
    await http.T.send();
    const answer = http.T.getHistory().find(m => m.role === 'assistant');
    assert.equal(answer.content, '已经收到。');
    assert.equal(answer.warn, '');
    http.T.switchWorld('crash'); http.T.switchWorld('normal');
    assert.equal(http.T.getHistory().find(m => m.role === 'assistant').content, '已经收到。');
    ok(true, 'HTTP 成功后存储写满，回复和切换后的内存历史仍保留');
  } finally {
    fixture.stdin.end();
    await exit;
    clearTimeout(timeout);
  }
  console.log(`\n通过 ${passed} 项，失败 0 项`);
}

main().catch(err => { console.error(err); process.exitCode = 1; });
