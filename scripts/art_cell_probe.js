#!/usr/bin/env node
/**
 * art_cell_probe.js — 字符画**局部独立排版**方案的可行性量测（PEND-010）。
 *
 * ## 为什么不是「再找一个字体」
 *
 * 2026-10-26 第二轮已经查实：本机 205 个已装字体里，**没有一个是**
 * 「ASCII/框线/方块/盲文全部 1 格」且「汉字正好 2 格」。所以「找万能字体」
 * 这条路是断的。用户这一轮的要求因此是：**字体先保持不变**，先研究
 * 字符画**局部的**独立排版方案（统一字符单元宽度、中英文占格规则），
 * 再决定值不值得动渲染层。
 *
 * ## 量什么
 *
 * 每个方案都用真实浏览器渲染同样四类图案，逐字符取 `getBoundingClientRect()`，
 * 报三个数：
 *
 *   cell_width_px   一个 ASCII 格的宽度（用 20 个 `M` 的总宽 / 20 量，
 *                   **不用某个字符的宽度**——那个字符可能来自回退字体）
 *   gap_spread_px   相邻两个「1 格字符」之间距离的最大差值。0 = 每格严格等宽
 *   max_drift_px    行内任一字符偏离「整数格」的最大值。这是真正决定
 *                   「图案会不会歪」的量：误差是累加的
 *
 * ## 四个方案（都只作用于图案块，不动正文）
 *
 *   current      现状：整行一个 span，与 `web/index.html` 的 `.blk.art` 同参数
 *   per-char-ch  每个字符一个 `inline-block`，1 格字符 `width:1ch`、汉字 `width:2ch`
 *                ——**用 DOM 强制统一字符单元**
 *   per-char-px  同上，但用像素（字号的一半 = 半角基准），绕开 `ch` 单位
 *                （`ch` 取的是「0」的宽度，字体没有 `0` 时不可靠）
 *   fw-cjk       只给汉字强制 `width:2ch`，其余不管——改动最小的一种
 *
 * ## 它不做什么
 *
 *   - 不改 `web/index.html`、不装字体、不引 web font；
 *   - 不判「哪个方案更好」。代价（DOM 膨胀、选中复制、性能）要人定。
 *
 * 用法：
 *   node scripts/art_cell_probe.js --out out/art-cell-<时间戳>
 */
'use strict';
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const EDGE_CANDIDATES = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
];

function arg(name, fallback) {
  const i = process.argv.indexOf('--' + name);
  return i >= 0 ? process.argv[i + 1] : fallback;
}
function findBrowser() {
  for (const p of EDGE_CANDIDATES) if (fs.existsSync(p)) return p;
  throw new Error('找不到 Edge/Chrome 可执行文件');
}

let nextId = 1;
class CDP {
  constructor(ws) {
    this.ws = ws;
    this.pending = new Map();
    ws.addEventListener('message', ev => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.error) reject(new Error(JSON.stringify(msg.error)));
        else resolve(msg.result);
      }
    });
  }
  send(method, params) {
    const id = nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      // 传参前先自检：CDP 对 expression 的类型要求是严格 string，
      // 传进去一个 undefined / 非字符串只会在浏览器那头报
      // "string value expected at position N"，看不出是哪个调用。
      if (params && 'expression' in params && typeof params.expression !== 'string') {
        reject(new Error(method + ': expression 类型是 ' + typeof params.expression));
        return;
      }
      this.ws.send(JSON.stringify({ id, method, params: params || {} }));
      setTimeout(() => {
        if (this.pending.has(id)) { this.pending.delete(id); reject(new Error('CDP timeout: ' + method)); }
      }, 60000);
    });
  }
  async eval(expression) {
    if (typeof expression !== 'string') {
      throw new Error('eval 收到非字符串表达式：' + typeof expression);
    }
    const r = await this.send('Runtime.evaluate', {
      expression, returnByValue: true, awaitPromise: true, userGesture: true,
    });
    if (r.exceptionDetails) throw new Error('eval failed: ' + JSON.stringify(r.exceptionDetails));
    return r.result.value;
  }
  close() { try { this.ws.close(); } catch (e) {} }
}

function launch() {
  const bin = findBrowser();
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'cv-cell-'));
  const child = spawn(bin, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-extensions', '--disable-background-networking',
    '--remote-debugging-port=0', '--user-data-dir=' + profile,
    '--window-size=1440,1000', 'about:blank',
  ], { stdio: ['ignore', 'pipe', 'pipe'] });
  return new Promise((resolve, reject) => {
    let buf = '';
    const timer = setTimeout(() => reject(new Error('浏览器启动超时: ' + buf.slice(0, 300))), 20000);
    child.stderr.on('data', d => {
      buf += d.toString();
      const m = buf.match(/DevTools listening on (ws:\/\/\S+)/);
      if (m) { clearTimeout(timer); resolve({ child, profile, wsUrl: m[1] }); }
    });
  });
}

async function newTab(browserWs) {
  const host = 'http://' + new URL(browserWs).host;
  const r = await fetch(host + '/json/new?about:blank', { method: 'PUT' });
  return (await r.json()).webSocketDebuggerUrl;
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

// ---------------------------------------------------------------
// 在页面里跑的量测函数（String.raw：不解析反斜杠，图案里的 `\` 原样保留）。
// ---------------------------------------------------------------
const MEASURE_FN = String.raw`
(() => {
  const MONO = getComputedStyle(document.documentElement).getPropertyValue('--mono').trim()
    || 'ui-monospace,SFMono-Regular,Menlo,Consolas,"Cascadia Mono",monospace';
  const SIZE = 14.5;
  const baseCss = 'font-family:' + MONO + ';font-size:' + SIZE + 'px;'
    + 'line-height:1.35;letter-spacing:0;white-space:pre;';

  const SAMPLES = {
    cat:     ['  /\\_/\\', ' ( o.o )', '  > ^ <'],
    box:     ['┌──────┐', '│      │', '└──────┘'],
    braille: ['  ⣠⣾⣿⣦⡀  ', '  ⠙⠿⠿⠿⠋  '],
    block:   ['█▀▀▀▀▀█', '█     █', '█▄▄▄▄▄█'],
    mixed:   ['┌─雨─┐', '│雪  │', '└────┘']
  };

  const isWide = ch => {
    const o = ch.codePointAt(0);
    if (o >= 0x1100 && o <= 0x115F) return true;
    if (o >= 0x2E80 && o <= 0xA4CF) return true;
    if (o >= 0xAC00 && o <= 0xD7A3) return true;
    if (o >= 0xF900 && o <= 0xFAFF) return true;
    if (o >= 0xFE30 && o <= 0xFE6F) return true;
    if (o >= 0xFF00 && o <= 0xFF60) return true;
    if (o >= 0xFFE0 && o <= 0xFFE6) return true;
    return false;
  };

  // 格宽：20 个 M 的总宽 / 20。不用某个字符的宽度当基准——
  // 那个字符可能来自回退字体（Consolas 没有盲文，实测会逐字回退）。
  const measureCell = () => {
    const d = document.createElement('div');
    d.style.cssText = baseCss + 'position:absolute;visibility:hidden;';
    d.textContent = 'M'.repeat(20);
    document.body.appendChild(d);
    const w = d.getBoundingClientRect().width / 20;
    d.remove();
    return w;
  };

  const renderLine = (host, line, mode) => {
    host.textContent = '';
    const rect = host.getBoundingClientRect();
    const chars = [...line];
    const boxes = [];
    if (mode === 'current') {
      const span = document.createElement('span');
      span.textContent = line;
      span.style.cssText = baseCss + 'display:inline-block;';
      host.appendChild(span);
      const tn = span.firstChild;
      const range = document.createRange();
      for (let i = 0; i < tn.nodeValue.length; i++) {
        range.setStart(tn, i); range.setEnd(tn, i + 1);
        const r = range.getBoundingClientRect();
        boxes.push({ ch: tn.nodeValue[i], x: r.left - rect.left, w: r.width });
      }
      return { boxes, width: span.getBoundingClientRect().width };
    }
    for (const ch of chars) {
      const s = document.createElement('span');
      s.textContent = ch;
      let w = '';
      if (mode === 'per-char-ch') w = (isWide(ch) ? 2 : 1) + 'ch';
      else if (mode === 'per-char-px') w = ((isWide(ch) ? 2 : 1) * SIZE / 2) + 'px';
      else if (mode === 'fw-cjk') w = isWide(ch) ? '2ch' : '';
      s.style.cssText = baseCss + 'display:inline-block;' + (w ? 'width:' + w + ';' : '');
      host.appendChild(s);
    }
    let x = 0;
    for (const s of host.children) {
      const r = s.getBoundingClientRect();
      boxes.push({ ch: s.textContent, x: r.left - rect.left, w: r.width });
    }
    return { boxes, width: host.getBoundingClientRect().width };
  };

  /**
   * 累计漂移：把每个字符的实际 x 与「前面所有字符应有的格数之和」比。
   *
   * 第一版这里写错过：用 solid.indexOf(c) 取序号，而同一行里有两个斜杠，
   * indexOf 永远返回第一个，于是第二个斜杠被算成偏移 24px，
   * 四个方案全报 47.8px。**那是量测的 bug，不是排版的 bug。**
   * 现在按下标累加，宽字符按 2 格计。
   *
   * （这段注释本身也踩过一次坑：它原先用了反引号举例，
   * 而整段函数是 String.raw 模板字符串——一个反引号就把模板提前结束了，
   * MEASURE_FN 变成数字，CDP 报 "string value expected"。
   * 模板里不能出现反引号，这条写在这里免得下次再犯。）
   */
  const driftOf = (line, cellW) => {
    if (!line.boxes.length || !cellW) return 0;
    let expected = 0, worst = 0;
    for (const b of line.boxes) {
      worst = Math.max(worst, Math.abs(b.x - expected));
      expected += (isWide(b.ch) ? 2 : 1) * cellW;
    }
    return worst;
  };

  const out = { size_px: SIZE, mono: MONO, modes: {}, generated_at: new Date().toISOString() };
  const MODES = ['current', 'per-char-ch', 'per-char-px', 'fw-cjk'];

  for (const mode of MODES) {
    const cellW = measureCell();
    const res = { cell_width_px: +cellW.toFixed(3), samples: {}, summary: {} };
    for (const name of Object.keys(SAMPLES)) {
      const host = document.createElement('div');
      host.style.cssText = baseCss + 'position:absolute;left:0;top:0;visibility:hidden;';
      document.body.appendChild(host);
      const lines = [];
      for (const line of SAMPLES[name]) {
        const { boxes, width } = renderLine(host, line, mode);
        // 相邻间距只统计「两个都是 1 格字符」的对：汉字与 ASCII 相邻
        // 本来就该变宽，算进去会把正常行为当成漂移。
        const gaps = [];
        for (let i = 1; i < boxes.length; i++) {
          const p = boxes[i - 1], c = boxes[i];
          if (isWide(p.ch) || isWide(c.ch)) continue;
          gaps.push(+(c.x - p.x).toFixed(3));
        }
        lines.push({
          text: line,
          boxes: boxes.map(b => ({ ch: b.ch, x: +b.x.toFixed(3), w: +b.w.toFixed(3) })),
          gaps,
          width: +width.toFixed(3),
          drift: +driftOf({ boxes }, cellW).toFixed(3)
        });
      }
      host.remove();
      res.samples[name] = lines;
    }
    const allGaps = Object.values(res.samples).flat().flatMap(l => l.gaps);
    const mixed = res.samples.mixed[0].boxes.find(b => b.ch === '雨');
    const brailleBox = res.samples.braille[0].boxes
      .filter(b => b.ch.codePointAt(0) >= 0x2800 && b.ch.codePointAt(0) <= 0x28FF)
      .map(b => b.w);
    res.summary = {
      // 格宽放在 summary 里也存一份：控制台与 cells.json 都从这里读，
      // 免得两处各取一个字段、打印出来是 undefined（第一版就是这样）。
      cell_width_px: +cellW.toFixed(3),
      cjk_width_px: mixed ? +mixed.w.toFixed(3) : null,
      cjk_cells: mixed ? +(mixed.w / cellW).toFixed(3) : null,
      braille_width_px: brailleBox.length
        ? +(brailleBox.reduce((a, b) => a + b, 0) / brailleBox.length).toFixed(3) : null,
      braille_cells: brailleBox.length
        ? +(brailleBox.reduce((a, b) => a + b, 0) / brailleBox.length / cellW).toFixed(3) : null,
      gap_spread_px: allGaps.length
        ? +(Math.max(...allGaps) - Math.min(...allGaps)).toFixed(3) : null,
      max_drift_px: +Math.max(...Object.values(res.samples).flat()
        .map(l => l.drift)).toFixed(3),
    };
    out.modes[mode] = res;
  }
  return out;
})()
`;

// 给截图用：把四种方案并排画出来（同一个图案），留一张能直接看的图。
const SHOT_FN = String.raw`
(() => {
  const MONO = getComputedStyle(document.documentElement).getPropertyValue('--mono').trim();
  const SIZE = 14.5;
  const isWide = ch => { const o = ch.codePointAt(0);
    return (o >= 0x2E80 && o <= 0xA4CF) || (o >= 0xFF00 && o <= 0xFF60); };
  // 样例行宽必须一致：第一版给的是 8 格的顶边配一条含两个汉字的行（10 格），
  // 方框上下对不上，那张截图看起来像「四个方案都拼不起来」——那是样例
  // 本身的问题，不是方案的问题。现在每行内宽都是 6 格（汉字按 2 格计）：
  //   顶边  1 + 6 + 1 = 8
  //   汉字行 1 + 2+1+1+2 + 1 = 8
  //   盲文行 1 + 6 + 1 = 8   ← 看盲文漂不漂
  //   底边  1 + 6 + 1 = 8
  const LINES = ['┌──────┐', '│雨  雪│', '│⣿⣿⣿⣿⣿⣿│', '└──────┘'];
  document.body.innerHTML = '';
  const wrap = document.createElement('div');
  wrap.style.cssText = 'padding:22px;background:#0d1015;color:#c6d0dc;font-family:' + MONO
    + ';font-size:' + SIZE + 'px;line-height:1.5;';
  document.body.appendChild(wrap);
  const title = document.createElement('div');
  title.textContent = '同一段图案 · 四种局部排版方案（PEND-010 取证）';
  title.style.cssText = 'font-size:11px;letter-spacing:.12em;color:#7fb2c4;margin-bottom:16px';
  wrap.appendChild(title);
  const row = document.createElement('div');
  row.style.cssText = 'display:flex;gap:34px;align-items:flex-start';
  wrap.appendChild(row);
  for (const mode of ['current', 'per-char-ch', 'per-char-px', 'fw-cjk']) {
    const col = document.createElement('div');
    const cap = document.createElement('div');
    cap.textContent = mode;
    cap.style.cssText = 'font-size:9.5px;letter-spacing:.1em;color:#6b7889;margin-bottom:8px';
    col.appendChild(cap);
    const art = document.createElement('div');
    art.style.cssText = 'font-family:' + MONO + ';font-size:' + SIZE + 'px;line-height:1.35;'
      + 'letter-spacing:0;white-space:pre;';
    for (const line of LINES) {
      const ln = document.createElement('div');
      ln.style.cssText = 'white-space:pre;';
      for (const ch of [...line]) {
        const s = document.createElement('span');
        s.textContent = ch;
        let w = '';
        if (mode === 'per-char-ch') w = (isWide(ch) ? 2 : 1) + 'ch';
        else if (mode === 'per-char-px') w = ((isWide(ch) ? 2 : 1) * SIZE / 2) + 'px';
        else if (mode === 'fw-cjk') w = isWide(ch) ? '2ch' : '';
        if (w) s.style.cssText = 'display:inline-block;width:' + w;
        ln.appendChild(s);
      }
      art.appendChild(ln);
    }
    col.appendChild(art);
    row.appendChild(col);
  }
  return true;
})()
`;

async function main() {
  const outDir = arg('out', path.join('out', 'art-cell-' + Date.now()));
  fs.mkdirSync(outDir, { recursive: true });

  const { child, wsUrl } = await launch();
  const tabWs = await newTab(wsUrl);
  const ws = new WebSocket(tabWs);
  await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); });
  const cdp = new CDP(ws);
  await cdp.send('Page.enable');
  await cdp.send('Runtime.enable');
  await cdp.send('Page.navigate', { url: 'about:blank' });
  await sleep(400);
  await cdp.eval(`(() => {
    document.documentElement.style.setProperty('--mono',
      'ui-monospace,SFMono-Regular,"JetBrains Mono",Menlo,Consolas,"Cascadia Mono",monospace');
    document.body.style.margin = '0';
    return true;
  })()`);

  const data = await cdp.eval(MEASURE_FN);

  await cdp.eval(SHOT_FN);
  await sleep(300);
  const shot = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
  fs.writeFileSync(path.join(outDir, 'modes.png'), Buffer.from(shot.data, 'base64'));

  fs.writeFileSync(path.join(outDir, 'cells.json'), JSON.stringify(data, null, 2), 'utf8');

  console.log('字号 ' + data.size_px + 'px');
  console.log('\n方案\t\t格宽\t汉字/格\t盲文/格\t间距差\t最大漂移');
  for (const [mode, r] of Object.entries(data.modes)) {
    const s = r.summary;
    console.log(mode.padEnd(14) + s.cell_width_px + '\t'
      + (s.cjk_cells === null ? '-' : s.cjk_cells) + '\t'
      + (s.braille_cells === null ? '-' : s.braille_cells) + '\t'
      + (s.gap_spread_px === null ? '-' : s.gap_spread_px) + '\t'
      + s.max_drift_px);
  }
  console.log('\n逐样本漂移（px）');
  for (const [mode, r] of Object.entries(data.modes)) {
    const per = Object.entries(r.samples)
      .map(([k, v]) => k + '=' + v.map(l => l.drift).join('/')).join('  ');
    console.log('  ' + mode.padEnd(14) + per);
  }
  console.log('\n数据 ' + path.join(outDir, 'cells.json'));
  console.log('截图 ' + path.join(outDir, 'modes.png'));

  cdp.close();
  child.kill();
}

main().catch(e => { console.error(e && e.stack || e); process.exit(1); });
