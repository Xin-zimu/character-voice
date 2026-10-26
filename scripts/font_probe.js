#!/usr/bin/env node
/**
 * font_probe.js — 把**本机已装的字体**逐个量一遍，看谁能让字符画对齐。
 *
 * 为什么需要它（PEND-010）：之前的记录里有两条结论无法复现——
 *   1. 「逐字体测过本机全部等宽字体：只有 Noto Sans Mono CJK SC 的汉字:ASCII 是 2」
 *      —— 本机注册表里**没有**这个字体（只有 Noto Sans SC / Noto Serif SC）。
 *   2. 「盲文 8.50px vs ASCII 7.97px，所以盲文格错位」
 *      —— 8.50 那个值来自**回退字体**，不是同一个字体里的两个字符。
 *
 * 所以这里改成：按字体名逐个量，并且**同时**报「该字是否来自本字体」。
 * 判断回退的办法：同一个字在 [该字体] 与 [该字体, 兜底] 两种栈下如果宽度不同，
 * 就说明该字体没有这个字，宽度是兜底字体给的。
 *
 * 用法：
 *   node scripts/font_probe.js                    # 内置常见等宽/中文字体清单
 *   node scripts/font_probe.js --from-registry    # 从 Windows 注册表列出全部字体
 *   node scripts/font_probe.js --out out/x.json
 */
'use strict';
const { spawn, execFileSync } = require('child_process');
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
const hasFlag = name => process.argv.includes('--' + name);

// 常见等宽 + 常见中文/日文/韩文字体。名字写错只会被判「未安装」，不会报错。
const DEFAULT_FONTS = [
  'Consolas', 'Cascadia Mono', 'Cascadia Code', 'Lucida Console',
  'Lucida Sans Typewriter', 'Courier New', 'DejaVu Sans Mono', 'OCR A Extended',
  'MS Gothic', 'MS UI Gothic', 'MS PGothic', 'Yu Gothic', 'Yu Gothic UI',
  'SimSun', 'NSimSun', 'SimHei', 'FangSong', 'KaiTi', 'DengXian',
  'Microsoft YaHei', 'Microsoft JhengHei', 'Malgun Gothic',
  'Noto Sans SC', 'Noto Serif SC', 'HarmonyOS Sans SC',
  'Segoe UI', 'Segoe UI Symbol', 'Microsoft Sans Serif',
  'Leelawadee UI', 'Nirmala UI', 'Sitka Text', 'Tahoma', 'Verdana',
  'Unispace-Bold', 'Bahnschrift', 'Lucida Sans Unicode', 'Arial Unicode MS',
];

/** 从 Windows 注册表取字体名：`<名称> (TrueType) = <文件>` 里剥掉后缀与粗斜体变体。 */
function registryFamilies() {
  const out = execFileSync('powershell', ['-NoProfile', '-Command',
    '(Get-ItemProperty "HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Fonts").PSObject.Properties'
    + ' | Where-Object { $_.Name -notlike "PS*" } | ForEach-Object { $_.Name }'],
    { encoding: 'utf8' });
  const names = new Set();
  for (let line of out.split(/\r?\n/)) {
    line = line.trim();
    if (!line) continue;
    // 「Cambria & Cambria Math (TrueType)」这种一个文件多个名字，逐个留。
    const m = line.match(/^(.*?)\s*\(TrueType\)$/i);
    if (!m) continue;
    for (let n of m[1].split('&')) {
      n = n.trim()
        .replace(/\s+(Bold|Italic|Bold Italic|Oblique|Bold Oblique|Light|Semilight|Semibold|Medium|Black|Heavy|Regular)$/i, '')
        .trim();
      if (n) names.add(n);
    }
  }
  return [...names].sort();
}

function launchBrowser() {
  const bin = EDGE_CANDIDATES.find(fs.existsSync);
  if (!bin) throw new Error('找不到 Edge/Chrome 可执行文件');
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'cv-fp-'));
  const port = 9333 + Math.floor(Math.random() * 400);
  const proc = spawn(bin, ['--headless=new', '--disable-gpu', '--no-first-run',
    '--no-default-browser-check', '--disable-extensions',
    '--remote-debugging-port=' + port, '--user-data-dir=' + profile, 'about:blank'],
    { stdio: 'ignore' });
  return { proc, port };
}

async function waitPort(port) {
  for (let i = 0; i < 120; i++) {
    try {
      const r = await fetch('http://127.0.0.1:' + port + '/json/version');
      if (r.ok) return (await r.json()).webSocketDebuggerUrl;
    } catch (e) { /* 还没起来 */ }
    await new Promise(r => setTimeout(r, 150));
  }
  throw new Error('浏览器调试端口没起来：' + port);
}

(async () => {
  const fonts = hasFlag('from-registry') ? registryFamilies() : DEFAULT_FONTS;
  const out = arg('out', '');
  const { proc, port } = launchBrowser();
  const browserWs = await waitPort(port);
  const host = 'http://' + new URL(browserWs).host;
  const tab = (await (await fetch(host + '/json/new?about:blank', { method: 'PUT' })).json())
    .webSocketDebuggerUrl;
  const ws = new WebSocket(tab);
  await new Promise((r, j) => { ws.addEventListener('open', r); ws.addEventListener('error', j); });
  let id = 1;
  const pending = new Map();
  ws.addEventListener('message', ev => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result); pending.delete(m.id); }
  });
  const send = (method, params) => new Promise(r => {
    const i = id++; pending.set(i, r);
    ws.send(JSON.stringify({ id: i, method, params: params || {} }));
  });
  await send('Runtime.enable');
  const evaluate = async expr => {
    const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true });
    if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails));
    return r.result.value;
  };

  const result = await evaluate(`(() => {
    const SIZE = 14.5, N = 20;
    const cv = document.createElement('canvas').getContext('2d');
    const CH = { ascii: 'a', box: '\\u2502', dash: '\\u2500', corner: '\\u250C',
                 block: '\\u2588', shade: '\\u2591', braille: '\\u28FF',
                 cjk: '\\u96E8', cjkPunct: '\\uFF0C', fullspace: '\\u3000' };
    const fams = ${JSON.stringify(fonts)};

    const advance = (font, ch) => {
      cv.font = SIZE + 'px ' + font;
      return cv.measureText(ch.repeat(N)).width / N;
    };
    // 回退判定：该字体在前 vs 该字体在兜底字体之后。
    // 若宽度变了，说明兜底字体接管了这个字（= 该字体没有这个字）。
    const nativeOf = (name, ch) => {
      const own = advance('"' + name + '",monospace', ch);
      const withFallback = advance('"' + name + '","Cascadia Mono",monospace', ch);
      const fallbackOnly = advance('"Cascadia Mono",monospace', ch);
      if (Math.abs(own - withFallback) < 0.01) return { native: true, why: '本字体与加兜底后同宽' };
      if (Math.abs(withFallback - fallbackOnly) < 0.01)
        return { native: false, why: '加兜底后与兜底字体同宽 → 该字体无此字' };
      return { native: null, why: '宽度变化但与兜底字体也不同宽，需人工看' };
    };
    // 本机是否装了该字体。
    const installed = name => {
      const probe = 'mmmmmmmmmmlliWWWW';
      cv.font = SIZE + 'px monospace';
      const a = cv.measureText(probe).width;
      cv.font = SIZE + 'px serif';
      const b = cv.measureText(probe).width;
      cv.font = SIZE + 'px "' + name + '",monospace';
      const c = cv.measureText(probe).width;
      cv.font = SIZE + 'px "' + name + '",serif';
      const d = cv.measureText(probe).width;
      return c !== a || d !== b;
    };

    const rows = [];
    for (const name of fams) {
      if (!installed(name)) { rows.push({ name, installed: false }); continue; }
      const v = {};
      for (const [k, ch] of Object.entries(CH)) v[k] = advance('"' + name + '",monospace', ch);
      const nat = {};
      for (const k of ['box', 'block', 'braille', 'cjk']) nat[k] = nativeOf(name, CH[k]);
      const a = v.ascii;
      rows.push({
        name, installed: true, advance: v, native: nat,
        boxVsAscii: v.box - a,
        blockVsAscii: v.block - a,
        brailleVsAscii: v.braille - a,
        cjkRatio: v.cjk / a,
        // 「一格对齐」：ASCII、框线、方块、盲文都等于 a。
        oneCell: ['box', 'dash', 'corner', 'block', 'shade', 'braille']
          .every(k => Math.abs(v[k] - a) < 0.02),
        // 「两格对齐」：汉字正好 2 * a。
        twoCellCjk: Math.abs(v.cjk / a - 2) < 0.02,
      });
    }
    return { size_px: SIZE, sample: N, rows };
  })()`);

  ws.close();
  proc.kill();

  const ok = result.rows.filter(r => r.installed);
  console.log('已装并量到 ' + ok.length + ' / ' + result.rows.length + ' 个字体（' + result.size_px + 'px）\n');
  console.log('字体'.padEnd(26) + 'a\t框线-a\t方块-a\t盲文-a\t汉字/a\t一格齐\t两格汉字');
  for (const r of ok) {
    console.log(r.name.padEnd(26) + r.advance.ascii.toFixed(2) + '\t'
      + (r.boxVsAscii >= 0 ? '+' : '') + r.boxVsAscii.toFixed(2) + '\t'
      + (r.blockVsAscii >= 0 ? '+' : '') + r.blockVsAscii.toFixed(2) + '\t'
      + (r.brailleVsAscii >= 0 ? '+' : '') + r.brailleVsAscii.toFixed(2) + '\t'
      + r.cjkRatio.toFixed(2) + '\t' + (r.oneCell ? '是' : '否') + '\t'
      + (r.twoCellCjk ? '是' : '否'));
  }
  const both = ok.filter(r => r.oneCell && r.twoCellCjk);
  console.log('\n同时满足「一格齐」与「汉字两格」的字体：'
    + (both.length ? both.map(r => r.name).join('、') : '（一个也没有）'));

  if (out) {
    fs.mkdirSync(path.dirname(out), { recursive: true });
    fs.writeFileSync(out, JSON.stringify(result, null, 2), 'utf8');
    console.log('数据 ' + out);
  }
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
