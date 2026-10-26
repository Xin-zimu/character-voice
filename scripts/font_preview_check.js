#!/usr/bin/env node
/**
 * font_preview_check.js — 用**真实浏览器**打开 web/font-preview.html 并留档。
 *
 * 为什么不靠人截图：PEND-010 要的是「换字体后图案究竟会不会歪」的**数值**，
 * 而这份数值取决于运行这台机器的字体环境。把它自动量下来，才有可复核的记录；
 * 截图只是给人看的附加物。
 *
 * 做法与 scripts/art_display_check.js 同源：起一个隔离的 headless Edge
 * （临时 user-data-dir，不碰用户配置与登录态），CDP 打开页面，
 * 等页面把 window.__FP_RESULT__ 算完，取出来写 report.json。
 *
 * 它**不改**页面字体、不安装字体、不写 web/index.html。
 *
 * 用法：
 *   py server.py                                   # 一个窗口（提供 http://127.0.0.1:8766）
 *   node scripts/font_preview_check.js --app http://127.0.0.1:8766 --shots out/font-preview-<时间戳>
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
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'cv-font-'));
  const child = spawn(bin, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-extensions', '--disable-background-networking',
    '--remote-debugging-port=0', '--user-data-dir=' + profile,
    '--window-size=1440,1200', 'about:blank',
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

async function main() {
  const app = arg('app', 'http://127.0.0.1:8766');
  const shots = arg('shots', path.join('out', 'font-preview-' + Date.now()));
  fs.mkdirSync(shots, { recursive: true });

  const { child, wsUrl } = await launch();
  const tabWs = await newTab(wsUrl);
  const ws = new WebSocket(tabWs);
  await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); });
  const cdp = new CDP(ws);
  await cdp.send('Page.enable');
  await cdp.send('Runtime.enable');

  await cdp.send('Page.navigate', { url: app + '/font-preview.html' });
  let data = null;
  for (let i = 0; i < 100; i++) {
    await sleep(150);
    try {
      // 页面没算完时这些标识还不存在，取到 false 就继续等。
      const st = await cdp.eval('({ready: !!window.__FP_READY__, has: !!window.__FP_RESULT__})');
      if (st && st.ready && st.has) { data = await cdp.eval('window.__FP_RESULT__'); break; }
    } catch (e) { /* 页面还在导航 */ }
  }
  if (!data) throw new Error('页面没有产出 __FP_RESULT__（脚本报错或没算完）');

  const consoleErrors = await cdp.eval(
    '(() => (window.__FP_ERRORS__ || []).slice(0, 20))()').catch(() => []);
  data.console_errors = consoleErrors;

  // 整页截图 + 窄屏截图，留给人工对照（D 类判断用）。
  const shot = async (name) => {
    const r = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
    fs.writeFileSync(path.join(shots, name + '.png'), Buffer.from(r.data, 'base64'));
  };
  await shot('full');
  await cdp.send('Emulation.setDeviceMetricsOverride',
    { width: 390, height: 900, deviceScaleFactor: 2, mobile: true });
  await sleep(700);
  await shot('narrow-390');
  await cdp.send('Emulation.clearDeviceMetricsOverride');

  fs.writeFileSync(path.join(shots, 'report.json'), JSON.stringify(data, null, 2), 'utf8');

  // 控制台打印一张给人看的摘要：只写测到的事实，不写推荐。
  console.log('字号 ' + data.size_px + 'px · 量于 ' + data.generated_at);
  console.log('\n字体\tASCII/框线\t方块\t盲文20格漂移\t汉字格数');
  for (const r of data.results) {
    const drift = Math.abs(r.brailleDelta) < 0.05 ? '0'
      : r.brailleDrift.toFixed(2) + 'px(' + r.brailleDriftCells.toFixed(1) + '格)';
    console.log(r.label + '\t' + (r.asciiAligned ? '对齐' : '不对齐') + '\t'
      + (r.blockAligned ? '1格' : '不对齐') + '\t' + drift + '\t' + r.cjkRatio.toFixed(2));
  }
  if (data.missing.length) console.log('\n未安装：' + data.missing.join('、'));
  console.log('\n报告 ' + path.join(shots, 'report.json'));

  cdp.close();
  child.kill();
}

main().catch(e => { console.error(e && e.stack || e); process.exit(1); });
