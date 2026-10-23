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
    openMemory, paintMemory, openPreflight, renderDebug, chatStats,
    getLastSent: () => lastSent, setLastSent(v) { lastSent = v; },
    setState(p, h) {
      personas[p.id] = p; world = p.world;
      hist = {normal: [], crash: []}; hist[world] = h;
      loadedWorlds.clear(); loadedWorlds.add(world);
    },
    getHistory: (w = world) => hist[w], getWorld: () => world, isBusy: () => busy,
    // 字符画（CHAT-017）：导出给一致性比对用，与 textart.py 逐例对照。
    isArt, checkArt, textWidth, stripPrefix, looksLikeArtPrefix, artBlocks, renderBlocks,
    setSettings(s) { settings = {...settings, ...s}; },
    setWorlds(w) { worlds = w; },
  };
` + js.slice(cut);

// mkEl 既要能当「按选择器缓存」的固定节点，也要能当 createElement 的产物。
// 这里退掉一层：每次 createElement 都有独立的 children，
// 于是面板渲染出来的结构能被断言，而不是所有节点共享同一个数组。
function mkEl() {
  const selectors = new Map(), classes = new Set();
  return {
    textContent: '', innerHTML: '', value: '', className: '', dataset: {}, children: [],
    disabled: false, scrollHeight: 0, id: '',
    style: {setProperty(k, v) { this[k] = v; }},
    classList: {add: k => classes.add(k), remove: k => classes.delete(k),
      toggle() {}, contains: k => classes.has(k)},
    appendChild(c) { this.children.push(c); return c; }, remove() {},
    querySelector(s) { if (!selectors.has(s)) selectors.set(s, mkEl()); return selectors.get(s); },
    addEventListener() {}, focus() {}, click() { if (this.onclick) return this.onclick(); },
  };
}

// 把一棵 mkEl 树的文字拼起来。面板用 appendChild 建 DOM，
// 断言语义只能靠这个把内容读回来。
// 把一棵 mkEl 树的文字拼起来，包含 querySelector 造出来的子节点。
// mkEl 不解析 innerHTML，而 buildMsg 是先写 innerHTML 再 querySelector('.txt')
// 填内容——只看 children / textContent 会读不到正文。
function textOf(node) {
  if (!node) return '';
  const out = [];
  const seen = new Set();
  const walk = n => {
    if (!n || seen.has(n)) return;   // 判重用**对象**（同一节点被两条路径引用时只算一次），
    seen.add(n);                     // 不能按文本去重：相同文本可能是不同节点。
    out.push(n.textContent || '');
    out.push((n.innerHTML || '').replace(/<[^>]*>/g, ' '));
    (n.children || []).forEach(walk);
    // mkEl 的 querySelector 是"点播"创建：只有被代码点过才会存在。
    // harness 里没有统一的子节点索引，所以这里只走 children，
    // 需要 .txt 之类时由调用方显式取（见下 textOfDeep）。
  };
  walk(node);
  return out.join(' ').replace(/\s+/g, ' ');
}

// 需要连 querySelector 子节点一起读时用它。
function textOfDeep(node, selectors = []) {
  if (!node) return '';
  return [textOf(node), ...selectors.map(sel => textOf(node.querySelector(sel)))].join(' ');
}

// 读一个 .txt 里的正文：renderBlocks 把内容切成若干 .blk 子节点，
// 直接读父节点的 textContent 是空的。块边界还原成换行——这正是用户复制
// 出去看到的东西，也是「文字行有没有被压成一行」这类问题的观测点。
function bodyOf(node) {
  // 只认 .blk：光标（.caret）也是 appendChild 进去的，它不是正文。
  // 注意：`renderBlocks` 每次重画都会**清空再重建**，正确实现下真正的 DOM
  // 只会有最新那一批子节点。harness 的 mkEl 不具备这个语义（appendChild 只增
  // 不减），所以这里只能取最后一批——取全部会把每一次重画都拼进来。
  // harness 的这一限制本身不会掩盖问题：块结构是否对，由别的用例断言。
  const blocks = ((node && node.children) || []).filter(k => /\bblk\b/.test(k.className || ''));
  if (!blocks.length) return (node && node.textContent) || '';
  return blocks[blocks.length - 1].textContent || '';
}

// 按 class 找后代。
function findByClass(node, cls, out = []) {
  if (!node) return out;
  if ((node.className || '').split(/\s+/).includes(cls)) out.push(node);
  (node.children || []).forEach(c => findByClass(c, cls, out));
  return out;
}

async function harness({manualTimers = false, reducedMotion = true} = {}) {
  const els = new Map(), store = {}, timers = [];
  const state = {quotaFull: false, fetchChat: null, alerts: [], promptValue: undefined,
    memGets: [], memPosts: [], fetchMemory: null, previewBodies: [], fetchPreview: null};
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
    // 记忆接口也接进假 fetch：面板的读写、报错路径都要能在没有服务端时测。
    // `worlds` 走真值而不是空对象，因为面板标题要用它显示世界名。
    fetch: async (url, options) => {
      if (url === '/api/personas') return {json: async () => cards};
      if (url === '/api/worlds') return {json: async () => ({worlds, corpus: {lines: 13070, episodes: []}})};
      if (url.startsWith('/api/memory')) return state.fetchMemory(url, options);
      if (url === '/api/preview') return state.fetchPreview(url, options);
      return state.fetchChat(url, options);
    },
    addEventListener() {}, matchMedia: () => ({matches: reducedMotion}),
    alert(msg) { state.alerts.push(String(msg)); },
    prompt(msg, def) { return state.promptValue === undefined ? def : state.promptValue; },
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
  // 切分点：第一条 system 之后是 2×示例条数 条示例，再往后是真实历史。
  // 不能再用「以上是语气示例」定位——那句收尾语与截断说明都已并进第一条
  // system（见 prompt_builder.build_messages 与 test_09b / test_07）。
  // 用示例条数定位还有个好处：示例若被插进历史段，len 断言会立刻报。
  const real = msgs => msgs.slice(1 + 2 * cards[0].examples.length, -1)
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
    // 分段豁免（CHAT-017）：字符画带空行不算「分段」。
    // 这条进样本的理由是它曾经两边不一致——Python 模拟器漏了豁免，
    // 自检报「分段」而界面不报，而当时没有任何用例比对这一条。
    [p.guard, '┌──────┐\n│      │\n└──────┘\n\n雨声一直没停。', ''],
    // 对照：普通空行分段两边都仍要报，否则上面那条会被「一律豁免」蒙过去。
    [p.guard, '看书。\n\n另外，温度在下降。', ''],
  ].map(c => ({id: p.id, c})));
  const sim = spawnSync(PY, ['-c',
    'import json,sys; from scripts.check_persona import simulate_check_reply as s; print(json.dumps([s(c[1],c[2],c[0]) for c in json.load(sys.stdin)],ensure_ascii=True))'],
    {cwd: ROOT, env: PY_ENV, input: JSON.stringify(cases.map(x => x.c)), encoding: 'utf8'});
  assert.equal(sim.status, 0, sim.stderr);
  JSON.parse(sim.stdout).forEach((expected, i) => {
    T.setState(cards.find(p => p.id === cases[i].id), []);
    assert.equal(T.checkReply(cases[i].c[1]), expected);
  });
  ok(true, 'Python 自检与前端逐例一致（含超长样本、字符画的分段豁免）');

  // 字符画（CHAT-017 / DEC-013）：判据也是两处实现，必须逐例一致。
  // 宽度换算最容易分叉——Python 用 unicodedata，JS 只能靠范围表。
  // 所以比对同时覆盖「是不是画」和「对齐问题」两个返回值，不只比宽窄。
  const ART_CASES = [
    '┌──────┐\n└──────┘\n雨声一直没停。',
    '  /\\_/\\\n  ( o.o )\n  > ^ <',
    '  /\\_/\\\n ( o.o )\n   > ^ <',
    '  ⣠⣾⣿\n  ⠿⠿⠿',
    '  ⣿猫\n  ⣿⣿',
    '┌────┐\n│ ok │\n└────┘',
    '第一句，\n第二句。',
    '看书。\n\n另外，温度在下降。',
    '床前明月光\n疑是地上霜\n举头望明月\n低头思故乡',
    '  aa \n  bb',
    '  ' + String.fromCodePoint(0x1F319) + "\n" + '  ' + String.fromCodePoint(0x1F319),
    '  e\u0301e\u0301\n  e\u0301e\u0301',
    '  单行',
    '',
    '猫ab',
    // 文字 + 图案 + 文字：分块的主要现场（CHAT-017 阶段 A 的回归）。
    '雨声一直没停。\n┌──────┐\n│      │\n└──────┘\n窗外没开灯。',
    // 单行看起来像图案、但没有第二行陪着：不当画（免得一整行 ==== 被压成一行）。
    '上式成立\n=====\n下面继续',
    // 两张图中间隔一个空行：断成两块，各自保空白。
    '┌────┐\n│    │\n└────┘\n\n┌────┐\n│    │\n└────┘',
    // 缩进两行的普通文字**不是**画（复审查出来的误判：空格曾算进「符号」）。
    '    你好呀\n    今天很好',
    '    床前明月光\n    疑是地上霜\n    举头望明月\n    低头思故乡',
    // 右边距不齐要报（左边距齐、但一条长一条短）。
    '  ────\n      ────',
    '  ┌──┐\n  │  │\n  └─┘',
    // 组合字符：**前端内部两处判据曾经不一致**（独立复核发现）——
    // 排版时 `\\u05b0` 按 0 格算，`checkArt` 却只查 `0300-036f`，查不出它。
    // 现在两处共用一个 COMBINING 常量。这两例保证它不会又分开：
    // 希伯来元音点在范围外曾是漏点，20d0（组合用符号）是原先就覆盖的。
    'a\u05b0\n---',
    'a\u0301\n---',
    'a\u20d0\n---',
  ];
  const art = spawnSync(PY, ['-c',
    `import json,sys; import textart as t
xs = json.load(sys.stdin)
print(json.dumps([[t.is_art(x), t.check_art(x), t.text_width(x), t.art_blocks(x)] for x in xs], ensure_ascii=True))`],
    {cwd: ROOT, env: PY_ENV, input: JSON.stringify(ART_CASES), encoding: 'utf8'});
  assert.equal(art.status, 0, art.stderr);
  const pyArt = JSON.parse(art.stdout);
  ART_CASES.forEach((text, i) => {
    const [pyIs, pyProbs, pyWidth, pyBlocks] = pyArt[i];
    assert.equal(T.isArt(text), pyIs, `是不是画不一致：${JSON.stringify(text)}`);
    // 用 JSON 比对而不是 deepEqual：vm 里造出来的数组来自另一个 realm，
    // deepEqual 会连原型一起比，直接报「不一致」——那是工具的问题，不是判据的。
    assert.equal(JSON.stringify(T.checkArt(text)), JSON.stringify(pyProbs),
      `对齐判据不一致：${JSON.stringify(text)}`);
    assert.equal(T.textWidth(text), pyWidth, `宽度不一致：${JSON.stringify(text)}`);
    // 分块也要一致：两边只要有一边把文字行当图案，界面就会换一套排版。
    assert.equal(JSON.stringify(T.artBlocks(text)), JSON.stringify(pyBlocks),
      `图案分块不一致：${JSON.stringify(text)}`);
  });
  ok(true, `字符画判据两端逐例一致（${ART_CASES.length} 例：是否成画 / 对齐问题 / 每行宽度 / 图案分块）`);

  // 前缀剥离：**不许吃缩进**。原来这里 trim 过，正文看不出问题，
  // 图案的第一行会被削平——只看代码不容易发现，所以钉一条用例。
  ok(T.stripPrefix('  /\\_/\\') === '  /\\_/\\', '前缀剥离保留首行缩进（图案左边距）');
  ok(T.stripPrefix('YUKI.N>写字') === '写字', '前缀照常去掉');
  ok(T.stripPrefix('YUKI.N>\n  /\\_/\\') === '  /\\_/\\', '前缀独占一行时不影响图案');
  ok(T.stripPrefix('aa  \nbb') === 'aa  \nbb', '行尾空格保留（右对齐靠它）');

  // 括号动作（CHAT-007）：DEC-006 要求两处实现逐例一致。
  // 这里比的是 check_action_brackets 的 errors 集合，不是整条 checkReply 的输出 ——
  // 后者还混着 guard 与分段判定，混在一起会掩盖「只有括号规则漂移」的情况。
  const BRACKET_CASES = [
    '（把书合上，放在膝上）还没有。读到一半。',      // 动作 + 台词：合法
    '（把灯调暗了一点）',                            // 整条只有动作：合法
    '不夸张。你说的时候停了两次。',                    // 没有动作：合法
    '（把书合上还没有。',                            // 不配对
    '把书合上）',                                   // 孤零零的右括号
    '（（把书合上））',                              // 嵌套
    '(把书合上)还没有。',                            // 半角动作
    '从 O(n) 降到 O(1)。',                          // 半角但是代码，不能误伤
    // 复审另外测的三条：两端结论一致。收进样本，防止以后改一边改歪。
    '从(a)讲到(b)。',
    '（把书合上（很快））',
    '说(嗯)就说了。',
    '（打开终端界面）看到了。',                       // 括号内含设定术语
    '（没有在观测对象里查到）',                       // 设定术语之二
  ];
  const bs = spawnSync(PY, ['-c',
    'import json,sys; from scripts.check_persona import check_action_brackets as c; print(json.dumps([c(t)["errors"] for t in json.load(sys.stdin)],ensure_ascii=True))'],
    {cwd: ROOT, env: PY_ENV, input: JSON.stringify(BRACKET_CASES), encoding: 'utf8'});
  assert.equal(bs.status, 0, bs.stderr);
  const pyBracket = JSON.parse(bs.stdout);
  BRACKET_CASES.forEach((text, i) => {
    T.setState(cards[0], []);
    const got = T.checkReply(text);
    const want = pyBracket[i];
    for (const e of want) {
      assert(got.includes(e), `括号规则两处不一致：「${text}」Python 报「${e}」，前端只给「${got}」`);
    }
    if (want.length === 0) {
      assert(!/括号/.test(got), `前端多报了括号问题：「${text}」→「${got}」`);
    }
  });
  ok(true, `括号动作规则前后端逐例一致（${BRACKET_CASES.length} 个样本）`);

  // guard 的提示优先级：括号问题不能挤掉更基础的违例。
  T.setState(cards[0], []);
  const both = T.checkReply('（把书合上）作为AI，我可以回答。');
  ok(both.startsWith('「'), '括号问题排在 guard 违例之后，不挤掉主规则提示');

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
      assert.equal(bodyOf(el), expected);
    }
    await done;
    ok(true, `${p.world} 按 ${p.protocol.type_delay_ms}ms 逐字显示，前缀隐藏、emoji 完整`);
  }

  // 字符画的显示（CHAT-017 / V-ART-02 的显示部分）：
  // 图案不能一个字一个字地吐——中途是一堆散字符，看着像乱码。
  {
    const clock = await harness({manualTimers: true, reducedMotion: false});
    const el = mkEl(), caret = mkEl();
    const writer = clock.T.createTypewriter(el, caret, cards[1]);
    writer.update('┌──┐');
    // 第一块就已经像图案：不该排队等逐字，直接给全，而且**一次**就给全。
    assert.equal(bodyOf(el), '┌──┐');
    assert.equal(clock.timers.length, 0, '图案不该再排逐字计时器');
    writer.update('┌──┐\n└──┘');
    assert.equal(bodyOf(el), '┌──┐\n└──┘', '后续内容也要整块到达，空白原样');
    await writer.finish();
    ok(true, '字符画整块显示：不逐字、空白与换行原样保留');
  }

  // 普通回复仍然逐字——不能被「整块」那条路误伤。
  {
    const clock = await harness({manualTimers: true, reducedMotion: false});
    const el = mkEl(), caret = mkEl();
    const writer = clock.T.createTypewriter(el, caret, cards[0]);
    writer.update('还没');
    // 断言要落在「有没有排队逐字」上：`el.textContent === '还没'` 这个分支
    // 是假的——逐字模式下第一块到达时正文还是空的，那个 or 会让断言恒真。
    assert(clock.timers.length > 0, '普通回复要排队逐字，不能整块直出');
    assert.equal(bodyOf(el), '', '逐字模式下第一块到达时正文还没开始显示');
    writer.finish();
    // 手工计时器不会自己跑，这里把队列推完，否则 finish() 的 promise 永远不 resolve，
    // 整条用例会静默挂住——之前就是挂在这里，进程直接结束了，看着像「通过」。
    while (clock.timers.length) clock.timers.shift().fn();
    ok(true, '普通回复仍走逐字路径');
  }

  // 文字 + 图案同条：只有图案那几行该享受 pre 排版，文字行必须还能折行。
  // 起因是实测截图——整条按 pre 排版时，汉字与 ASCII 的宽度比只有 1.66，
  // 一行十几字就顶到边界，句号被挤到下一行单独出现，看着像一个孤零零的点。
  {
    const host = mkEl();
    T.renderBlocks(host, '雨声一直没停。\n┌──────┐\n│      │\n└──────┘\n窗外没开灯。');
    const kinds = host.children.map(c => c.className);
    assert.equal(JSON.stringify(kinds), JSON.stringify(['blk', 'blk art', 'blk']),
      `分块样式不对：${JSON.stringify(kinds)}`);
    assert.equal(host.children[1].textContent, '┌──────┐\n│      │\n└──────┘',
      '图案块里的换行不能丢');
    assert.equal(host.children[0].textContent, '雨声一直没停。', '文字块不能混进图案行');
    // 不能插 <br>：复制出去的文本会多出空行，换行信息已经由块边界表达了。
    assert(!host.children.some(c => /<br/i.test(c.innerHTML)), '块之间不插 <br>');
    ok(true, '文字 + 图案同条：图案块保空白，文字块照常折行');
  }

  // 样式层面的同一条规则：文字块必须恢复 `white-space:normal`。
  // 只测 DOM 结构测不到这一点——`.txt.multi` 要是仍保空白，结构对了、
  // 文字照样被压成一行。所以直接从样式表里读这条声明（M40 就是这条的守卫）。
  {
    const css = HTML.match(/<style>([\s\S]*?)<\/style>/)[1];
    const m = css.match(/\.line \.txt\.multi\{([^}]*)\}/);
    assert(m, '找不到 .line .txt.multi 的样式——分块排版会整体失效');
    assert(/white-space:\s*normal/.test(m[1]),
      `.txt.multi 必须恢复 white-space:normal，实际是「${m[1]}」`);
    // 图案块反过来必须保空白，否则图案塌成一团。
    const a = css.match(/\.line \.txt \.blk\.art\{([^}]*)\}/);
    assert(a && /white-space:\s*pre/.test(a[1]), '图案块必须 white-space:pre');
    ok(true, '样式：文字块恢复折行，图案块保空白');
  }

  // 打字机在图案模式下也要走分块渲染——否则流式阶段用的是旧排版，
  // 收尾那一刻整条重排一次，画面会跳。
  {
    const clock = await harness({manualTimers: true, reducedMotion: false});
    const el = mkEl(), caret = mkEl();
    const writer = clock.T.createTypewriter(el, caret, cards[1]);
    writer.update('雨声一直没停。\n┌──┐\n└──┘');
    // 只看 .blk：光标 append 在父节点上，className 是 "caret"。
    const cls = el.children.filter(c => /\bblk\b/.test(c.className || '')).map(c => c.className);
    assert.equal(cls.length, 2, `流式阶段也要分块，实际 ${JSON.stringify(cls)}`);
    assert(cls[1].includes('art'), '图案块要带 art');
    await writer.finish();
    ok(true, '图案模式的流式阶段同样按块渲染（不等到收尾才重排）');
  }

  // 图案被标 △ 的是**画**的问题，不是语气。左边距不一致要说出来。
  {
    T.setState(cards[1], []);
    const bad = '  ⣠⣾\n ⠿⠿';
    assert(T.isArt(bad), '坏样例本身要算图案，否则这条测试测的是别的');
    assert(T.checkReply(bad).includes('左边距'), '图案歪了要提示，而不是安静通过');
    T.setState(cards[0], []);
    assert.equal(T.checkReply('第一句。\n\n第二句。'), '分段',
      '同一个检查不能让普通分段漏掉');
    ok(true, '图案对齐问题走 △，普通分段仍按「分段」报');
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

  // ---------------------------------------------------------------
  // 调试面板（CHAT-006 / CHAT-006-B，V-UI-01 / V-UI-02）
  //
  // 两条规则各有一组断言：
  //   V-UI-01  主聊天区**不得**出现字数统计与逐条统计信息，这些只能在 ◇ 里。
  //   V-UI-02  「上一次实际发送」与「下一次预览」必须分开写清楚，
  //            改过输入框之后不能把预览说成刚才发的内容。
  // ---------------------------------------------------------------
  {
    const dbg = await harness();
    dbg.T.setWorlds(worlds);
    dbg.T.setState(cards[0], [
      {role: 'user', content: '上次那个事'},
      {role: 'assistant', content: '（看了你一眼）记得。', warn: '「作为AI」这类助手腔'},
      {role: 'assistant', content: '字数很多的一条回答，用来检查统计口径是否算上它。', warn: ''},
    ]);

    // pf-body 用 appendChild 建 DOM，且预览那块是异步补进去的（pre.textContent、
    // hits.innerHTML）。所以既要走 children，也要把节点的 innerHTML 读出来。
    const pfText = () => {
      const box = dbg.els.get('#pf-body');
      const grab = n => [n.textContent || '', (n.innerHTML || '').replace(/<[^>]*>/g, ' '),
        ...(n.children || []).map(grab)].join(' ');
      return textOf(box) + ' ' + grab(box);
    };

    // 1) 主聊天区：重画之后不得出现「N 字」这类统计。
    dbg.els.get('#chat').children.length = 0;
    dbg.T.switchWorld('normal', {silent: true});
    const chatText = dbg.els.get('#chat').children
      .map(c => c.className === 'msg user' ? textOf(c) : textOfDeep(c, ['.txt', '.flag']))
      .join(' ').replace(/\s+/g, ' ');
    assert(!/\d+\s*字/.test(chatText), '主聊天区仍有字数统计：' + chatText.slice(0, 200));
    assert(!dbg.els.get('#stat'), '底栏 #stat 节点应已移除（或至少不再被写入）');
    assert(chatText.includes('记得'), '重画后正文仍要在：' + chatText.slice(0, 200));
    assert(chatText.includes('助手腔'), '违例提示本身仍留在主区——它是阅读时要知道的事，不是统计');
    ok(true, '主聊天区不再显示字数统计（V-UI-01）');

    // 2) 统计口径：只数有内容的回复，标了 △ 的计数对得上。
    const st = dbg.T.chatStats();
    assert.equal(st.replies, 2, JSON.stringify(st));
    assert.equal(st.flagged, 1, JSON.stringify(st));
    assert(st.total > 0 && st.avgReply > 0 && st.avgSentence > 0, JSON.stringify(st));
    ok(true, '调试统计只算有内容的回复，△ 计数正确');

    // 3) 还没有发过请求时：只有预览，不能假装有「上一次」。
    dbg.state.fetchPreview = async (url, opts) => {
      dbg.state.previewBodies.push(JSON.parse(opts.body));
      return {ok: true, json: async () => ({system_prompt: '系统提示词内容', hits: [],
        memory_count: 0, message_count: 3, world: 'normal'})};
    };
    dbg.els.get('#input').value = '现在想说的话';
    await dbg.T.openPreflight();
    let txt = pfText();
    assert(txt.includes('还没有发出过请求'), '没有发送记录时必须明说');
    // 不能只写一句「还没有」再照常画一块 —— 必须确实没画「当时那句话」和历史。
    assert(!txt.includes('当时发出去的这句话'), '没有记录时不能画出实际发送块');
    assert(!txt.includes('随请求带上的最近历史'), '没有记录时不能画出历史');
    ok(true, '没有发送记录时不伪造「上一次实际发送」');

    // 4) 「上一次实际发送」和「下一次预览」同时存在且分开。
    dbg.T.setLastSent({
      at: '2026-10-24 10:00:00',
      payload: {world: 'normal', message: '刚才真正发出去的那句', history: [
        {role: 'user', content: '旧问题'}, {role: 'assistant', content: '旧回答'}],
        temperature: 0.85, max_tokens: 220, k: 5, model: 'deepseek-chat'},
      world: 'normal', hits: 3, status: 200,
    });
    dbg.els.get('#pf-body').children.length = 0;
    await dbg.T.openPreflight();
    await new Promise(setImmediate);
    txt = pfText();
    const iActual = txt.indexOf('上一次实际发送');
    const iPreview = txt.indexOf('下一次预览');
    assert(iActual >= 0 && iPreview > iActual, '两块必须都在，且实际发送在前：' + txt.slice(0, 300));
    assert(txt.includes('刚才真正发出去的那句'), '实际发送必须显示当时那句话');
    assert(txt.includes('旧问题') && txt.includes('旧回答'), '实际发送要带上随请求的历史');
    assert(txt.includes('命中 3 条'), '要显示服务端回报的命中条数');
    // 预览块的结果来自 /api/preview，正文里**没有**输入框的原话——
    // 原话体现在请求体里。所以断言请求体，而不是面板文字。
    assert.equal(dbg.state.previewBodies.at(-1).message, '现在想说的话',
      '预览请求必须用输入框当前内容');
    assert(txt.includes('不是上面那次实际发送的内容'), '必须写明预览不等于实际发送（V-UI-02）');
    ok(true, '「上一次实际发送」与「下一次预览」分开显示，预览明确标注不是实际（V-UI-02）');

    // 5) 预览请求必须带当前世界；改世界后预览跟着换。
    assert.equal(dbg.state.previewBodies.at(-1).world, 'normal');
    dbg.T.setState(cards[1], []);
    dbg.els.get('#input').value = '崩溃世界的话';
    dbg.els.get('#pf-body').children.length = 0;
    dbg.T.switchWorld('crash', {silent: true});
    await dbg.T.openPreflight();
    await new Promise(setImmediate);
    assert.equal(dbg.state.previewBodies.at(-1).world, 'crash',
      '预览请求必须按当前世界组装');
    txt = pfText();
    assert(txt.includes('还没有回复'), '切到空世界后统计要跟着变，不能留上一个世界的数据');
    ok(true, '调试面板随世界切换，预览请求带当前世界');

    // 6) payload 里绝不能把 api_key 带进面板。
    dbg.T.setLastSent({at: 'x', payload: {world: 'normal', message: 'm', history: [],
      temperature: 0.85, max_tokens: 220, k: 5, model: 'm'}, world: 'normal', hits: 0, status: 200});
    dbg.els.get('#pf-body').children.length = 0;
    await dbg.T.openPreflight();
    await new Promise(setImmediate);
    assert(!pfText().includes('api_key'), '调试面板不得出现 api_key');
    ok(true, '调试面板不显示 api_key');

    // 7) 场景状态（CHAT-013）：预览里要看得见她这侧「此刻」记着什么。
    //    这一块最需要能看见——状态错了（比如用户说「我摘了眼镜」却改了她的设定），
    //    聊天记录里完全看不出来，只有这里能核对。
    dbg.els.get('#chat').children.length = 0;
    dbg.T.switchWorld('normal', {silent: true});
    dbg.state.fetchPreview = async (url, opts) => {
      dbg.state.previewBodies.push(JSON.parse(opts.body));
      return {ok: true, json: async () => ({system_prompt: 's', hits: [],
        memory_count: 0, message_count: 2, world: 'normal',
        scene_state: {glasses: false, book: true, together: false}})};
    };
    dbg.els.get('#input').value = '你看完了吗';
    dbg.els.get('#pf-body').children.length = 0;
    await dbg.T.openPreflight();
    await new Promise(setImmediate);
    txt = pfText();
    assert(txt.includes('此刻记录的状态'), '预览里必须能看到场景状态：' + txt.slice(-300));
    assert(txt.includes('眼镜') && txt.includes('书'), '状态项要写出名字，不能只给数字');
    // 三项都已知，必须都列出来（3 项），不能显示成 0 项。
    assert(txt.includes('（3 项）'), '已知的三项都要列出来：' + txt.slice(-300));
    ok(true, '预览里显示此刻记录的状态');

    // 8) 没有记录时不能说成「都是否」，要明说没有记录。
    dbg.state.fetchPreview = async (url, opts) => {
      dbg.state.previewBodies.push(JSON.parse(opts.body));
      return {ok: true, json: async () => ({system_prompt: 's', hits: [],
        memory_count: 0, message_count: 2, world: 'normal', scene_state: {}})};
    };
    dbg.els.get('#pf-body').children.length = 0;
    await dbg.T.openPreflight();
    await new Promise(setImmediate);
    txt = pfText();
    assert(txt.includes('（0 项）') && txt.includes('没有任何记录'),
      '没有记录时要明说，不能编成「否」：' + txt.slice(-300));
    ok(true, '没有场景记录时明说，不编造');
  }

  // ---------------------------------------------------------------
  // 共同经历面板（CHAT-018）
  //
  // 断了服务端也要能跑：这里用状态机替换 /api/memory，只测前端自己的行为——
  // 请求带没带 world、回收站与操作记录画没画、报错是明说还是装成空。
  // 真正的隔离与原子写入在 test_backend.py 的 test_13～test_16 里测。
  // ---------------------------------------------------------------
  {
    const mem = await harness();
    mem.T.setWorlds(worlds);
    const records = {
      normal: [
        {id: 'm-1', world: 'normal', text: '你在学做饭', status: 'active', revision: 1,
         subject: 'user', created: '2026-10-20T10:00:00'},
        {id: 'm-2', world: 'normal', text: '你以前说不吃辣', status: 'retired', revision: 1,
         subject: 'user', created: '2026-10-19T09:00:00'},
        {id: 'm-3', world: 'normal', text: '你其实能吃一点辣了', status: 'active', revision: 2,
         subject: 'user', created: '2026-10-21T09:00:00', revises: 'm-2'},
      ],
      crash: [{id: 'm-9', world: 'crash', text: '天台那只猫', status: 'active', revision: 1,
               subject: 'user', created: '2026-10-22T09:00:00'}],
    };
    const events = {
      normal: [{at: '2026-10-21T09:00:00', op: 'add', world: 'normal', id: 'm-3'},
               {at: '2026-10-21T09:01:00', op: 'update', world: 'normal', id: 'm-1',
                from: '你在学做饭', to: '你在学做面'}],
      crash: [],
    };
    mem.state.fetchMemory = async (url, opts) => {
      if (!opts) {
        mem.state.memGets.push(url);
        const w = new URLSearchParams(url.split('?')[1]).get('world');
        return {ok: true, json: async () => ({world: w, records: records[w] || [], events: events[w] || [],
          warnings: w === 'crash' ? ['记忆文件损坏：测试用'] : []})};
      }
      const body = JSON.parse(opts.body);
      mem.state.memPosts.push(body);
      return {ok: true, json: async () => ({record: body, warnings: []})};
    };

    mem.T.setState(cards[0], []);
    await mem.T.openMemory();
    // 面板必须带着当前世界去读，且只显示这个世界的东西。
    assert.equal(mem.state.memGets.at(-1), '/api/memory?world=normal');
    let txt = textOf(mem.els.get('#mem-body'));
    assert(txt.includes('你在学做饭') && txt.includes('你其实能吃一点辣了'), txt);
    assert(!txt.includes('天台那只猫'), '另一世界的记忆不能出现在主线的面板里');
    assert(txt.includes('回收站'), '回收站要有一块，便于恢复');
    assert(txt.includes('纠正了旧的一条'), '被纠正的新记录要标出来');
    assert(txt.includes('「你在学做饭」→「你在学做面」'), '操作记录要显示改前改后');
    assert(txt.includes('操作记录'), '操作记录要有一块');
    assert(findByClass(mem.els.get('#mem-body'), 'mem-rec').length === 3,
      '三条记录都要画出来（含回收站里那条）');

    // 切到崩溃世界：读的是另一份，标题也跟着变。
    mem.T.switchWorld('crash', {silent: true});
    await mem.T.paintMemory();
    assert.equal(mem.state.memGets.at(-1), '/api/memory?world=crash');
    // 面板是原地重画的，但 harness 的 innerHTML 只当文本读、不会清 children。
    // 所以要看着色后的记录本身，而不是整棵树下累积的文字。
    mem.els.get('#mem-body').children.length = 0;
    await mem.T.paintMemory();
    txt = textOf(mem.els.get('#mem-body'));
    assert(txt.includes('天台那只猫') && !txt.includes('你在学做饭'), txt);
    assert(txt.includes('记忆文件损坏'), '有警告时必须显示，不能吞掉');
    assert(mem.els.get('#mem-title').textContent.includes('崩溃'), '标题要标出当前世界');
    ok(true, '共同经历面板按世界读取、回收站与操作记录可见、警告不吞');

    // 新增：请求体必须带 world 与 op，文本去掉首尾空白。
    mem.T.switchWorld('normal', {silent: true});
    mem.els.get('#mem-body').children.length = 0;
    await mem.T.paintMemory();
    const input = findByClass(mem.els.get('#mem-body'), '')
      .find(n => n.id === 'mem-input') || null;
    // 面板里那个输入框是 createElement 出来的（harness 不解析 innerHTML），
    // 按 id 找不到就按位置取第一个 input。
    const inp = input || mem.els.get('#mem-body').children.find(c => c.className === 'mem-add').children[0];
    inp.value = '  你换了个发型 ';
    const addBtn = mem.els.get('#mem-body').children.find(c => c.className === 'mem-add').children[1];
    await addBtn.onclick();
    const posted = mem.state.memPosts.at(-1);
    assert.deepEqual(posted, {world: 'normal', op: 'add', text: '你换了个发型', subject: 'user'},
      JSON.stringify(posted));
    ok(true, '面板新增记忆：带 world 与 op，文本去空白');

    // 纠正：走 add + revises，而不是 update。两者在服务端是不同的事。
    // 「最新的一条」是 m-3（2026-10-21），面板按时间倒序排，所以取第一行。
    mem.state.memPosts.length = 0;
    mem.state.promptValue = '你现在能吃辣了';
    mem.els.get('#mem-body').children.length = 0;
    await mem.T.paintMemory();
    const rows0 = findByClass(mem.els.get('#mem-body'), 'mem-rec');
    assert.equal(rows0.length, 3, '重画之后正好三条，不多不少');
    assert(rows0[0].children[0].textContent === '你其实能吃一点辣了',
      '最新的那条排在最前：' + rows0[0].children[0].textContent);
    const ops = findByClass(rows0[0], 'ops')[0];
    await ops.children[1].onclick();   // 0=改 1=换成新说法 2=删
    assert.deepEqual(mem.state.memPosts.at(-1),
      {world: 'normal', op: 'add', text: '你现在能吃辣了', subject: 'user', revises: 'm-3'},
      JSON.stringify(mem.state.memPosts.at(-1)));
    ok(true, '「换成新说法」提交 add+revises，不会覆盖掉旧记录');

    // 服务端拒绝时：弹出来，不要静默当作成功。
    mem.state.memPosts.length = 0;
    mem.state.fetchMemory = async () => ({ok: false, json: async () => ({error: '不能修改另一世界的记录'})});
    mem.state.alerts.length = 0;
    await mem.T.paintMemory().catch(() => {});
    const rows = findByClass(mem.els.get('#mem-body'), 'mem-rec');
    if (rows.length) {
      await findByClass(rows[0], 'ops')[0].children[2].onclick();   // 删
    }
    assert(mem.state.alerts.some(a => a.includes('另一世界')), JSON.stringify(mem.state.alerts));
    ok(true, '记忆接口报错时明确提示，不静默当成成功');
  }

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

// 只在**跑完全部用例**后才报「通过」。之前这里只有 catch，主流程要是中途
// 挂住（某个 await 等一个永远不会 resolve 的 promise），事件循环空了，
// node 会安静地以 0 退出，输出停在第 N 项——看起来像成功，实际少跑了一半。
// 这类假绿比红更危险：它会让人以为改过的东西都被验过了。
const DONE = Symbol('suite-finished');
main().then(
  () => { process.exitCode = 0; globalThis[DONE] = true; },
  err => {
    // 打一行 `FAIL: <用例说明>`，和 Python 那边 unittest 的格式对齐。
    // 变异脚本靠这个正则抓「是哪条用例拦住的」；没有它，报告只会写
    // 「(未捕获到用例名)」——看着像拦住了，其实没人说得清是哪条在拦。
    const msg = (err && (err.message || err.toString())) || '未知错误';
    console.error(`FAIL: ${msg.split('\n')[0]}`);
    console.error(err);
    process.exitCode = 1;
    globalThis[DONE] = true;
  },
);
process.on('exit', code => {
  if (!globalThis[DONE]) {
    console.error('\n[harness] 用例没跑完就退出了——大概率有 await 挂着。'
      + '上面最后一项之后的代码没有被验证。');
    process.exitCode = 1;
  }
});
process.on('unhandledRejection', err => {
  console.error('[harness] 未处理的 promise 拒绝：', err);
  process.exitCode = 1;
});
