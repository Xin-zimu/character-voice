#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
live_chat.py — 用真实模型跑验收试聊并留档。

工程回归（test_backend / test_frontend）只证明代码正确，不证明她像不像。
这个脚本补的是 [docs/VALIDATION.md] 里 C 类证据：真实模型的表现。

用法（需要先起 server.py，并配好可用的后端）：

    py server.py                                  # 另开一个窗口
    node qoder_backend.mjs                        # 可选：用本机 Qoder 账号
    py scripts/live_chat.py --backend qoder --model dfmodel
    py scripts/live_chat.py --base-url http://... --api-key sk-... --model deepseek-chat

输出写到 out/live-chat-<YYYYMMDD-HHMMSS>.md，包含每轮的原文与字数。

注意：
  - 不打印、不写盘 API Key。
  - 默认走 backend=qoder（服务端解析地址与 token），此时 --base-url/--api-key 被忽略。
"""
from __future__ import annotations

import argparse
import codecs
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 每个场景：(编号, 标题, 世界, 轮次)
SCENARIOS = [
    ("V-CHAT-01", "日常 + 安静陪伴", "normal",
     ["今天降温了", "你在做什么", "我有点累，不想说话"]),
    ("V-CONT-02", "意见分歧", "normal",
     ["我想辞职", "你觉得我该去吗"]),
    ("V-CHAT-03", "需要完整回答", "normal",
     ["帮我看看这个报错：IndexError: list index out of range", "那要怎么办"]),
    ("V-CONT-01", "连续追问与指代", "normal",
     ["我最近在学做饭", "昨天煎鱼糊了", "刚才那个锅是不是不能用"]),
    ("V-WORLD-02", "越界诱导（崩溃）", "crash",
     ["另一条世界线里你是不是有什么特别的身份"]),
    ("V-CHAT-02", "崩溃世界身份", "crash",
     ["你知道信息统合思念体吗", "你平时和谁一起玩"]),
    ("V-CONT-03", "崩溃世界日常", "crash",
     ["今天降温了", "我这次考试考砸了"]),
]


def chat(server, world, message, history, opts):
    payload = {
        "world": world,
        "message": message,
        "history": history,
        "temperature": opts.temperature,
        "max_tokens": opts.max_tokens,
        "k": 5,
    }
    if opts.backend:
        payload["backend"] = opts.backend
        payload["model"] = opts.model
    else:
        payload["base_url"] = opts.base_url
        payload["api_key"] = opts.api_key
        payload["model"] = opts.model

    req = urllib.request.Request(
        server.rstrip("/") + "/api/chat",
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"},
    )

    out: list[str] = []
    dec = codecs.getincrementaldecoder("utf-8")("replace")
    with urllib.request.urlopen(req, timeout=opts.timeout) as r:
        buf = ""
        while True:
            chunk = r.read(4096)
            if not chunk:
                break
            buf += dec.decode(chunk)
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body == "[DONE]":
                    return "".join(out)
                try:
                    ev = json.loads(body)
                except Exception:
                    continue
                piece = (ev.get("choices") or [{}])[0].get("delta", {}).get("content")
                if piece:
                    out.append(piece)
    return "".join(out)


def run_one(server, sid, title, world, turns, opts, sink):
    lines = [f"\n## {sid} · {title}  [{world}]\n"]
    hist: list[dict] = []
    for msg in turns:
        try:
            reply = chat(server, world, msg, hist, opts)
        except Exception as e:
            lines.append(f"\n**YOU>** {msg}\n\n**错误**：{e}\n")
            break
        n = len(reply.replace(" ", ""))
        lines.append(f"\n**YOU>** {msg}\n\n**YUKI.N>** {reply}\n\n`{n} 字`\n")
        hist.append({"role": "user", "content": msg})
        hist.append({"role": "assistant", "content": reply})
        hist = hist[-24:]
    text = "".join(lines)
    print(text)
    sink.write(text)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://127.0.0.1:8766")
    ap.add_argument("--backend", default="", help="可选后端 id，例如 qoder")
    ap.add_argument("--base-url", default="https://api.deepseek.com/v1")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--temperature", type=float, default=0.85)
    ap.add_argument("--max-tokens", type=int, default=400)
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--only", default="", help="只跑某个场景编号")
    opts = ap.parse_args()

    if not opts.backend and not opts.api_key:
        print("需要 --backend 或 --api-key 之一", file=sys.stderr)
        return 2

    out_dir = ROOT / "out"
    out_dir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"live-chat-{stamp}.md"

    head = (
        f"# 真实模型试聊 · {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"- 后端：{opts.backend or '自定义接口'}\n"
        f"- 模型：{opts.model or '（后端默认）'}\n"
        f"- 温度：{opts.temperature}\n"
        f"- server：{opts.server}\n\n"
        "> 这是 [docs/VALIDATION.md] 的证据类型 C。工程测试通过不等于这里通过。\n"
    )

    with path.open("w", encoding="utf-8") as sink:
        sink.write(head)
        print(head)
        for sid, title, world, turns in SCENARIOS:
            if opts.only and opts.only != sid:
                continue
            run_one(opts.server, sid, title, world, turns, opts, sink)

    print(f"\n已写入 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
