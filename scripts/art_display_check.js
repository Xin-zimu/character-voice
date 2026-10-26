#!/usr/bin/env node
/**
 * art_display_check.js — 用**真实浏览器**核对字符画的显示链路。
 *
 * 为什么要有这个：scripts/test_frontend.js 用的是模拟 DOM，能验判据与样式表，
 * 验不了「这段文字在真实字体下到底排成什么样」。V-ART-02 要求的是
 * 流式 / 完成 / 刷新重放 / 切世界 / 窄屏 五个时点的**实际观感**，
 * 那只能在真浏览器里量。
 *
 * 做法：起一个隔离的 Edge（临时 user-data-dir，不碰用户配置与登录态），
 * 通过 CDP 打开本地假上游驱动的页面，逐类样本发送、截图、量数据。
 *
 * 前置：py scripts/art_display_fixture.py --serve
 *   node scripts/art_display_check.js --app http://127.0.0.1:PORT --upstream http://127.0.0.1:PORT2 \
 *        --shots out/art-display-<时间戳>
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
      this.ws.send(JSON.stringify({ id, method, params: params || {} }));
      setTimeout(() => {
        if (this.pending.has(id)) { this.pending.delete(id); reject(new Error('CDP timeout: ' + method)); }
      }, 60000);
    });
  }
  async eval(expression) {
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
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'cv-art-'));
  const child = spawn(bin, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-extensions', '--disable-background-networking',
    '--remote-debugging-port=0', '--user-data-dir=' + profile,
    '--window-size=1280,900', 'about:blank',
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
  const doc = await r.json();
  return doc.webSocketDebuggerUrl;
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

const READ_STATE = String.raw`(() => {
  const out = { blocks: [], fonts: {}, rect: null };
  const txts = [...document.querySelectorAll('.msg.yuki .txt')];
  const el = txts[txts.length - 1];
  if (!el) return { empty: true };
  const cs = getComputedStyle(el);
  out.rect = el.getBoundingClientRect().toJSON();
  out.hostWhiteSpace = cs.whiteSpace;
  out.hostLetterSpacing = cs.letterSpacing;
  out.font = { family: cs.fontFamily, size: cs.fontSize, weight: cs.fontWeight };
  out.blocks = [...el.children].filter(c => /\bblk\b/.test(c.className)).map(c => {
    const s = getComputedStyle(c);
    return {
      cls: c.className,
      text: c.textContent,
      whiteSpace: s.whiteSpace,
      overflowX: s.overflowX,
      lineHeight: s.lineHeight,
      letterSpacing: s.letterSpacing,
      fontSize: s.fontSize,
      tabSize: s.tabSize,
      scrollWidth: c.scrollWidth,
      clientWidth: c.clientWidth,
      rect: c.getBoundingClientRect().toJSON(),
      lines: c.textContent.split('\n').length,
      leadingSpaces: c.textContent.split('\n').map(l => l.match(/^ */)[0].length),
    };
  });
  const flags = [...document.querySelectorAll('.msg.yuki .flag')];
  out.flag = flags.length ? flags[flags.length - 1].textContent : '';
  out.docOverflow = { scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth };
  const c = document.querySelector('#chat');
  out.chatOverflow = c ? { scrollWidth: c.scrollWidth, clientWidth: c.clientWidth } : null;
  out.fonts = { status: document.fonts ? document.fonts.status : 'n/a', size: document.fonts ? document.fonts.size : 'n/a' };
  return out;
})()`;

const MEASURE_FONT = String.raw`(() => {
  const probe = document.createElement('span');
  probe.style.cssText = 'position:absolute;visibility:hidden;white-space:pre;font-family:ui-monospace,SFMono-Regular,"JetBrains Mono",Menlo,Consolas,"Cascadia Mono",monospace;font-size:14.5px;letter-spacing:0';
  document.body.appendChild(probe);
  const chars = ['a', '│', '─', '┌', ' ', '雨', '⣿', '█', '　', '＼', '·'];
  const out = {};
  for (const ch of chars) {
    probe.textContent = ch.repeat(20);
    out[ch] = +(probe.getBoundingClientRect().width / 20).toFixed(2);
  }
  probe.remove();
  return out;
})()`;

async function main() {
  const app = arg('app');
  const upstream = arg('upstream');
  const shots = arg('shots', path.join('out', 'art-display-' + Date.now()));
  if (!app || !upstream) {
    console.error('用法: node scripts/art_display_check.js --app URL --upstream URL [--shots DIR]');
    process.exit(2);
  }
  fs.mkdirSync(shots, { recursive: true });
  const { child, wsUrl } = await launch();
  const tabWs = await newTab(wsUrl);
  const ws = new WebSocket(tabWs);
  await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); });
  const cdp = new CDP(ws);
  await cdp.send('Page.enable');
  await cdp.send('Runtime.enable');
  const results = { app, upstream, cases: [], font: null, viewport: null };

  // 刷新后必须等页面真的画完再量。原来固定等 1.3s，页面的两次启动 fetch
  // （personas / worlds）还没回来就读，于是拿到 empty——那是测量竞态，
  // 不是重放丢了（单独复验过：localStorage 里记录仍在、DOM 也画出来了）。
  const waitRendered = async (expectReply) => {
    for (let i = 0; i < 60; i++) {
      const st = await cdp.eval('(() => { const t = document.querySelectorAll(".msg.yuki .txt").length;' +
        'const empty = !!document.querySelector(".empty");' +
        'const busy = document.querySelector("#send") ? document.querySelector("#send").disabled : false;' +
        'return {t, empty, busy}; })()');
      if (!st.busy && (expectReply ? st.t > 0 : (st.empty || st.t > 0))) return;
      await sleep(150);
    }
  };
  const goto = async url => { await cdp.send('Page.navigate', { url }); await waitRendered(false); };
  const reload = async (expectReply) => { await cdp.send('Page.reload', {}); await sleep(300); await waitRendered(!!expectReply); };

  const seed = 'localStorage.setItem("yuki.settings.v2", JSON.stringify({backend:"",base_url:"' +
    upstream + '/v1",api_key:"fixture-key-not-real",model:"fixture",temperature:"0.85"}));' +
    'localStorage.setItem("yuki.hist.v2.normal","[]");localStorage.setItem("yuki.hist.v2.crash","[]");true';

  await goto(app + '/');
  await cdp.eval(seed);
  await reload(false);
  results.font = await cdp.eval(MEASURE_FONT);
  results.viewport = await cdp.eval('({w: innerWidth, h: innerHeight, dpr: devicePixelRatio})');

  async function send(text, waitMs) {
    await cdp.eval('(() => { const i = document.querySelector("#input"); i.value = ' +
      JSON.stringify(text) + '; document.querySelector("#send").click(); return true; })()');
    const deadline = Date.now() + (waitMs || 20000);
    while (Date.now() < deadline) {
      const st = await cdp.eval('(() => { const b = document.querySelector("#send");' +
        'const t = [...document.querySelectorAll(".msg.yuki .txt")].pop();' +
        'return { idle: !b.disabled, has: !!(t && t.textContent.trim()) }; })()');
      if (st.idle && st.has) break;
      await sleep(120);
    }
    await sleep(600);
  }
  async function screenshot(name) {
    const r = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
    fs.writeFileSync(path.join(shots, name + '.png'), Buffer.from(r.data, 'base64'));
  }
  async function clearWorld() {
    await cdp.eval('localStorage.setItem("yuki.hist.v2.normal","[]");' +
      'localStorage.setItem("yuki.hist.v2.crash","[]");true');
    await reload(false);
  }
  const clickTab = re => cdp.eval('(() => { const b=[...document.querySelectorAll("#tabs button")];' +
    'const t=b.find(x=>' + re + '.test(x.textContent)); if(t){t.click(); return t.textContent;} return ""; })()');

  async function runCase(name, payload, opts) {
    opts = opts || {};
    await clearWorld();
    if (opts.world === 'crash') { await clickTab('/CRASH|崩溃/'); await sleep(600); }
    const mid = [];
    let poll = null;
    if (opts.sampleMid) {
      poll = setInterval(async () => {
        try {
          const s = await cdp.eval('(() => { const t=[...document.querySelectorAll(".msg.yuki .txt")].pop(); return t ? t.textContent : ""; })()');
          if (s) mid.push(s);
        } catch (e) {}
      }, 60);
    }
    await send(payload, opts.waitMs);
    if (poll) clearInterval(poll);
    const done = await cdp.eval(READ_STATE);
    await screenshot(name + '-done');
    let replay = null, switched = null, narrow = null;
    await reload(true);
    replay = await cdp.eval(READ_STATE);
    await screenshot(name + '-replay');
    if (opts.narrow) {
      await cdp.send('Emulation.setDeviceMetricsOverride', { width: 375, height: 800, deviceScaleFactor: 2, mobile: true });
      await sleep(800);
      narrow = await cdp.eval(READ_STATE);
      await screenshot(name + '-narrow');
      await cdp.send('Emulation.clearDeviceMetricsOverride');
      await sleep(300);
    }
    if (opts.switchWorld) {
      await clickTab('/CRASH|崩溃/'); await sleep(700);
      await clickTab('/NORMAL|主线/'); await sleep(800);
      switched = await cdp.eval(READ_STATE);
      await screenshot(name + '-worldback');
    }
    results.cases.push({ name, payload, midStates: mid.slice(0, 60), done, replay, narrow, switched });
    console.log('[' + name + '] flag=' + JSON.stringify(done.flag || '') + ' blocks=' +
      (done.blocks || []).map(b => b.cls).join('|'));
  }

  const only = arg('only', '');
  const want = n => !only || only.split(',').includes(n);
  if (want('kaomoji')) await runCase('kaomoji', '@kaomoji', {});
  if (want('cat')) await runCase('cat', '@cat', { narrow: true, switchWorld: true });
  if (want('catflat')) await runCase('catflat', '@cat_flat', { narrow: true });
  if (want('box')) await runCase('box', '@box', { narrow: true });
  if (want('braille')) await runCase('braille', '@braille', { narrow: true });
  if (want('block')) await runCase('block', '@block', {});
  if (want('spaces')) await runCase('spaces', '@spaces', {});
  if (want('pure')) await runCase('pure', '@pure', {});
  if (want('prefix')) await runCase('prefix', '@prefix_indent', {});
  if (want('prose')) await runCase('prose', '@prose', {});
  if (want('indentprose')) await runCase('indentprose', '@indent_prose', {});
  if (want('code')) await runCase('code', '@code', {});
  if (want('wide')) await runCase('wide', '@wide', { narrow: true });
  if (want('mixed')) await runCase('mixed', '@mixed', { narrow: true, sampleMid: true });
  if (want('cjkart')) await runCase('cjkart', '@cjk_art', {});
  if (want('lateart')) await runCase('lateart', '@split:late_art', { sampleMid: true });
  if (want('tab')) await runCase('tab', '@tab', {});
  if (want('stream')) await runCase('stream', '@split:box', { sampleMid: true });
  if (want('cut')) await runCase('cut', '@cut:box:9', {});

  fs.writeFileSync(path.join(shots, 'report.json'), JSON.stringify(results, null, 2), 'utf8');
  cdp.close();
  child.kill();
  console.log('\n报告 ' + path.join(shots, 'report.json'));
}

main().catch(e => { console.error(e && e.stack || e); process.exit(1); });
