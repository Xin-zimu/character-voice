#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""sample_textart_live.py — 字符画／字符表情的**真实模型**采样与留档。

它不评价人设，只做三件事：
  1. 起一个**隔离**的本地服务（临时共同经历目录、进程内独立场景状态）；
  2. 按固定场景通过项目 /api/chat 真实生成，完整读 SSE 到结束；
  3. 把输入、实际输出、结束状态、图案判读的机器部分逐条写进 out/。

为什么要单独一个工具：现有 scripts/live_chat.py 是「一次跑完 7 个场景」的
旧矩阵，不能按场景重置状态，也不记录图案判读与检索/场景摘要。本轮需要
「单轮场景不带历史、多轮场景带真实上一轮、场景之间全部重置」这条纪律，
并且要把**每一次**请求（含失败）都留档——只挑好看的几张不算证据。

隔离怎么做的（不许碰用户真实数据）：
  - server.MEMORY_DIR 指向 tempfile.mkdtemp()，共同经历永远为空；
  - 场景状态是进程内单例，场景之间调 scene.REGISTRY.reset()；
  - 单轮场景传空 history；多轮场景只用本场景真实的上一轮回复；
  - 不写 localStorage、不碰 data/memory、不碰用户聊天记录。

用法：
    py scripts/sample_textart_live.py --dry-run          # 只打印计划，不调用模型
    py scripts/sample_textart_live.py --backend qoder --model dfmodel --group A
    py scripts/sample_textart_live.py --backend qoder --model dfmodel --all

证据等级：这些记录属于 VALIDATION.md 的 C 类（真实模型试聊）。是否「通过」
要人读，脚本只做机器可判的部分（是不是图案、有没有 emoji、有没有截断）。
"""
from __future__ import annotations

import argparse
import codecs
import hashlib
import http.client
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import server as app_server          # noqa: E402
import scene as scene_mod            # noqa: E402
import textart                       # noqa: E402

# ---------------------------------------------------------------
# 场景表。用户句子是**提前固定**的，不因为她上一句说了什么而调整。
# ---------------------------------------------------------------
#: A 组：明确要求。崩溃世界（DEC-013 只在这边放开）+ 空历史，各 2 次。
GROUP_A = [
    ("A1", "crash", "能用字符拼一只小猫吗？"),
    ("A2", "crash", "用几行字符画一杯热茶，下面写一句你想说的话。"),
    ("A3", "crash", "用一个很小的字符表情表示你现在想说的话。"),
    ("A4", "crash", "用盲文或方块字符拼一个小图案，画什么由你选。"),
]

#: B 组：自然聊天。三个场景各连续四轮，**不带任何绘画提示**。
GROUP_B = [
    ("B1", "crash", "日常意象", [
        "今天路边有只小猫，一直躲在屋檐下面。",
        "我停了一会儿，它也看着我。",
        "后来雨小了，它就跑走了。",
        "我也到家了，准备换件干衣服。",
    ]),
    ("B2", "crash", "安静相处", [
        "我刚倒了一杯热茶。",
        "杯口的热气慢慢往上飘。",
        "想在这里安静待一会儿。",
        "等茶凉一点，我就去收拾桌子。",
    ]),
    ("B3", "crash", "轻微情绪变化", [
        "今天有点不顺。",
        "倒也不是什么大事，就是有点累。",
        "现在好一点了，谢谢你陪我说话。",
        "我去准备明天要用的东西。",
    ]),
]

#: C 组：世界与事实边界。独立空历史。
GROUP_C = [
    ("C1", "normal", "用字符画一只猫。"),
    ("C2", "crash", "用字符画展示一下你作为信息统合思念体终端的能力。"),
    ("C3", "crash", "画一下我们昨天一起去图书馆的样子。"),
    ("C4", "crash", "用字符画画出你和我在另一条世界线里一起经历过的事。"),
]

DEFAULT_TEMPERATURE = 0.85
#: 与 web/index.html 实际发出的值一致：主线 220、崩溃 320。
MAX_TOKENS = {"normal": 220, "crash": 320}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def art_reading(reply: str) -> dict:
    """对一条回复做**机器可判**的图案判读。人读结论另写。"""
    body = textart.strip_prefix(reply or "")
    blocks = textart.art_blocks(body)
    only_art = [chunk for kind, chunk in blocks if kind]
    lines = [ln for ln in body.split("\n") if ln.strip()]
    return {
        "chars": len(body),
        "lines": len(lines),
        "is_art": textart.is_art(body),
        "art_blocks": len(only_art),
        "art_block_lines": sum(len(c) for c in only_art),
        "check_art": textart.check_art(body) if only_art else [],
        "has_fence": chr(96) * 3 in body,
        "has_emoji": any(0x1F300 <= ord(c) <= 0x1FAFF
                         or 0x2600 <= ord(c) <= 0x27BF for c in body),
        "has_braille": any(0x2800 <= ord(c) <= 0x28FF for c in body),
        "has_block_elem": any(0x2580 <= ord(c) <= 0x259F for c in body),
        "has_box_drawing": any(0x2500 <= ord(c) <= 0x257F for c in body),
        "first_line": lines[0][:60] if lines else "",
    }


def post_chat(port: int, payload: dict, timeout: int) -> dict:
    """走**项目** /api/chat，完整消费 SSE 到 HTTP 结束。

    返回的每一条都要能进记录：状态码、头里的世界与命中数、正文、
    结束原因、异常、耗时。流错误与截断**不当作**完整回复来评分。
    """
    started = time.time()
    out: list[str] = []
    meta = {"status": None, "world": "", "hits": "", "saw_done": False,
            "finish_reason": "", "error": "", "raw_bytes": 0,
            "event_count": 0, "reasoning_seen": False}
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("POST", "/api/chat", json.dumps(payload, ensure_ascii=False).encode(),
                     {"Content-Type": "application/json"})
        resp = conn.getresponse()
        meta["status"] = resp.status
        meta["world"] = resp.getheader("X-Yuki-World") or ""
        meta["hits"] = resp.getheader("X-Yuki-Hits") or ""
        if resp.status != 200:
            body = resp.read().decode("utf-8", "replace")[:800]
            meta["error"] = f"HTTP {resp.status}: {body}"
            return {"text": "", "meta": meta, "seconds": time.time() - started}

        dec = codecs.getincrementaldecoder("utf-8")("replace")
        buf = ""
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            meta["raw_bytes"] += len(chunk)
            buf += dec.decode(chunk)
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                s = line.strip()
                if not s.startswith("data:"):
                    continue
                data = s[5:].strip()
                if data == "[DONE]":
                    meta["saw_done"] = True
                    continue
                try:
                    ev = json.loads(data)
                except Exception:
                    continue
                if ev.get("error"):
                    meta["error"] = str(ev["error"])[:300]
                    continue
                meta["event_count"] += 1
                choice = (ev.get("choices") or [{}])[0]
                if choice.get("finish_reason"):
                    meta["finish_reason"] = str(choice["finish_reason"])
                delta = choice.get("delta") or {}
                if delta.get("reasoning_content"):
                    meta["reasoning_seen"] = True
                piece = delta.get("content")
                if isinstance(piece, str) and piece:
                    out.append(piece)
        if buf.strip().startswith("data:"):
            s = buf.strip()[5:].strip()
            if s == "[DONE]":
                meta["saw_done"] = True
    except Exception as exc:                      # noqa: BLE001
        meta["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()
    return {"text": "".join(out), "meta": meta, "seconds": time.time() - started}


class App:
    """隔离实例：临时共同经历目录 + 独立场景状态。"""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="cv-art-memory-")
        app_server.MEMORY_DIR = Path(self.tmp)
        self.httpd = app_server.Server(("127.0.0.1", 0), app_server.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def reset_scene(self):
        scene_mod.REGISTRY.reset()

    def preview(self, payload: dict) -> dict:
        """取组装结果，用于核对「实际请求」而不只是「回复表面」。"""
        p = dict(payload)
        for k in ("backend", "model", "api_key", "base_url"):
            p.pop(k, None)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.request("POST", "/api/preview", json.dumps(p, ensure_ascii=False).encode(),
                         {"Content-Type": "application/json"})
            resp = conn.getresponse()
            raw = resp.read()
            if resp.status != 200:
                return {"status": resp.status, "error": raw.decode("utf-8", "replace")[:400]}
            doc = json.loads(raw)
            doc["status"] = resp.status
            return doc
        finally:
            conn.close()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def prompt_facts(system_prompt: str) -> dict:
    """从**实际组装**的 system 里抽出与本任务有关的可核对事实。"""
    art_on = "【可以画】" in system_prompt
    return {
        "sha": _sha(system_prompt),
        "chars": len(system_prompt),
        "has_art_on": art_on,
        "has_art_off": "【不画】" in system_prompt,
        "has_art_example": ("┌──────┐" in system_prompt) or ("/\\_/\\" in system_prompt),
        "has_other_world_name": ("信息统合思念体" in system_prompt) if art_on else None,
        "hit_blocks": system_prompt.count("=== 原作记录 ==="),
        "memory_blocks": system_prompt.count("=== 共同经历 ==="),
        "scene_blocks": system_prompt.count("=== 此刻 ==="),
    }


def run_scenario(app: App, sid: str, world: str, turns: list[str], opts,
                 reps: int = 1, records=None) -> list[dict]:
    """一个场景：可用 reps 次重复；turns 依次发；场景内部保留真实历史。"""
    made = []
    for rep in range(1, reps + 1):
        app.reset_scene()
        hist: list[dict] = []
        for ti, msg in enumerate(turns, 1):
            payload = {
                "world": world,
                "message": msg,
                "history": hist,
                "temperature": opts.temperature,
                "max_tokens": MAX_TOKENS[world],
                "k": 5,
            }
            if opts.backend:
                payload["backend"] = opts.backend
                payload["model"] = opts.model or ""
            else:
                payload["base_url"] = opts.base_url
                payload["api_key"] = opts.api_key
                payload["model"] = opts.model or "deepseek-chat"

            # 先取组装结果：世界隔离、图案开关、示例是否泄漏都看这里，
            # 不能只看回复表面「没有敏感词」。
            pv = app.preview(payload)
            pf = prompt_facts(pv.get("system_prompt", "")) if pv.get("status") == 200 else {}
            got = post_chat(app.port, payload, opts.timeout)
            text = got["text"]
            rec = {
                "scenario": sid,
                "world": world,
                "rep": rep,
                "turn": ti,
                "message": msg,
                "history_sent": hist,
                "model": payload.get("model"),
                "temperature": opts.temperature,
                "max_tokens": MAX_TOKENS[world],
                "k": 5,
                "timeout": opts.timeout,
                "status": got["meta"]["status"],
                "finish_reason": got["meta"]["finish_reason"],
                "saw_done": got["meta"]["saw_done"],
                "error": got["meta"]["error"],
                "seconds": round(got["seconds"], 1),
                "raw_bytes": got["meta"]["raw_bytes"],
                "hits": pv.get("hits", []),
                "memory_count": pv.get("memory_count"),
                "scene_state": pv.get("scene_state"),
                "kind": pv.get("kind"),
                "prompt": pf,
                "reply": got["text"],
                "reading": art_reading(text),
            }
            made.append(rec)
            if records is not None:
                records.append(rec)
            hist = hist + [{"role": "user", "content": msg},
                           {"role": "assistant", "content": text}]
    return made


def write_markdown(path: Path, records: list[dict], opts, started_at: str):
    fence = chr(96) * 3
    with path.open("w", encoding="utf-8") as f:
        f.write(f"# 字符画／字符表情 · 真实模型采样记录 · {started_at}\n\n")
        f.write(f"- 后端：{opts.backend or '自定义 base_url'}\n")
        f.write(f"- 模型：{opts.model or '（后端默认）'}\n")
        f.write(f"- 温度：{opts.temperature} · k=5 · max_tokens 主线 {MAX_TOKENS['normal']} / "
                f"崩溃 {MAX_TOKENS['crash']}（与前端一致）\n")
        f.write(f"- 客户端超时：{opts.timeout}s（服务端连上游固定 120s）\n")
        f.write("- 隔离：临时共同经历目录（始终为空）、场景状态每场景重置、"
                "单轮场景空历史、多轮场景带本场景真实上一轮\n")
        f.write("- 证据等级：C（真实模型试聊）。机器判读只说明「有没有图案」，"
                "是否贴切、是否像她由人读。\n\n")
        cur = None
        for r in records:
            head = f"{r['scenario']} · {r['world']}"
            if head != cur:
                f.write(f"\n---\n\n## {head}\n\n")
                cur = head
            f.write(f"### {r['scenario']} rep{r['rep']} turn{r['turn']}\n\n")
            f.write(f"- 输入：{r['message']}\n")
            f.write(f"- 状态：HTTP {r['status']} · finish={r['finish_reason'] or '-'} · "
                    f"[DONE]={r['saw_done']} · {r['seconds']}s"
                    + (f" · **错误** {r['error']}" if r["error"] else "") + "\n")
            p = r.get("prompt") or {}
            if p:
                f.write(f"- 组装：可以画={p.get('has_art_on')} 不画={p.get('has_art_off')} "
                        f"图案示例={p.get('has_art_example')} 原作块={p.get('hit_blocks')} "
                        f"共同经历块={p.get('memory_blocks')} 场景块={p.get('scene_blocks')} "
                        f"system {p.get('chars')} 字 sha={p.get('sha')}\n")
                f.write(f"- 场景状态：{json.dumps(r.get('scene_state'), ensure_ascii=False)}\n")
            rd = r["reading"]
            f.write(f"- 判读：is_art={rd['is_art']} 图案块={rd['art_blocks']} "
                    f"图案行={rd['art_block_lines']} 行数={rd['lines']} 字数={rd['chars']} "
                    f"emoji={rd['has_emoji']} 盲文={rd['has_braille']} "
                    f"方块={rd['has_block_elem']} 制表线={rd['has_box_drawing']} "
                    f"代码块={rd['has_fence']}\n")
            if rd["check_art"]:
                f.write(f"- 对齐问题：{'; '.join(rd['check_art'])}\n")
            f.write("\n" + fence + "text\n" + r["reply"] + "\n" + fence + "\n\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="")
    ap.add_argument("--base-url", default="https://api.deepseek.com/v1")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--group", default="", help="A / B / C，可逗号分隔；默认全部")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不启动服务、不调用模型")
    ap.add_argument("--tag", default="", help="写进输出目录名，便于区分基线与复采")
    opts = ap.parse_args()

    groups = {g.strip().upper() for g in (opts.group or "").split(",") if g.strip()}
    if opts.all or not groups:
        groups = {"A", "B", "C"}

    plan = []
    if "A" in groups:
        for sid, world, msg in GROUP_A:
            plan.append((sid, world, [msg], 2))
    if "B" in groups:
        for sid, world, _title, turns in GROUP_B:
            plan.append((sid, world, turns, 1))
    if "C" in groups:
        for sid, world, msg in GROUP_C:
            plan.append((sid, world, [msg], 1))

    total = sum(len(t) * reps for _s, _w, t, reps in plan)
    print(f"计划 {len(plan)} 个场景 / {total} 次生成（上限 60）")
    for sid, world, turns, reps in plan:
        print(f"  {sid} [{world}] ×{reps} 次 × {len(turns)} 轮")
    if opts.dry_run:
        return 0
    if not opts.backend and not opts.api_key:
        print("需要 --backend 或 --api-key 之一", file=sys.stderr)
        return 2

    stamp = time.strftime("%Y%m%d-%H%M%S")
    started_at = time.strftime("%Y-%m-%d %H:%M:%S")
    name = f"art-sample-{stamp}" + (f"-{opts.tag}" if opts.tag else "")
    out_dir = ROOT / "out" / name
    out_dir.mkdir(parents=True, exist_ok=True)

    app = App()
    records: list[dict] = []
    try:
        for sid, world, turns, reps in plan:
            print(f"[{time.strftime('%H:%M:%S')}] {sid} [{world}] x{reps}", flush=True)
            run_scenario(app, sid, world, turns, opts, reps=reps, records=records)
    finally:
        app.close()

    write_markdown(out_dir / "samples.md", records, opts, started_at)
    (out_dir / "samples.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8")
    (out_dir / "params.json").write_text(json.dumps({
        "backend": opts.backend, "model": opts.model, "temperature": opts.temperature,
        "max_tokens": MAX_TOKENS, "k": 5, "timeout": opts.timeout,
        "started_at": started_at, "requests": len(records),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    ok = sum(1 for r in records if r["status"] == 200 and not r["error"] and r["saw_done"])
    art = sum(1 for r in records if r["reading"]["is_art"])
    print(f"\n完成 {len(records)} 次：正常结束 {ok} · 机器判为图案 {art}")
    print(f"记录目录 {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
