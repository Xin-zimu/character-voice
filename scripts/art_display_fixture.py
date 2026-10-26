#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""art_display_fixture.py — 显示链路的**可控**假上游 + 真实应用服务。

用途（V-ART-02 的 B 类证据）：用可重复的样本核对「流式 → 完成 → 刷新重放 →
切世界 → 窄屏」这条链路，不用真实模型的随机输出来代替显示测试。

它和 scripts/stream_fixture.py 的区别：那个是按固定模式各返回一条，
这个由**用户消息**决定返回哪一段，并支持「先给一半再断流」，
所以能在真实页面里逐类核对空格、空行、前缀、缩进和截断。

用法：
    py scripts/art_display_fixture.py --serve
    # 打印 {"app": 端口, "upstream": 端口}
    # 前端设置里 base_url 填 http://127.0.0.1:<upstream>/v1，key 随便填

消息约定：
    @<case>            返回该样本的全部内容（一次帧）
    @split:<case>      逐字符发送（模拟任意 token 边界）
    @cut:<case>:<n>    发送前 n 个字符后直接断开（不写 [DONE]，模拟流中断）
"""
import argparse
import http.server
import json
import socket
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server  # noqa: E402

# 每条都对应 VALIDATION 里要求覆盖的一类形状。注释写清「要看什么」。
CASES = {
    # 单行字符表情：不该被当成多行图案，按文字显示即可。
    "kaomoji": "（ ..）\n\n嗯。",
    # 多行 ASCII 图案：左边距逐行不同，是图案本身的形状（不是错误）。
    "cat": "   /\\_/\\\n  ( o.o )\n   > ^ <\n\n看了一会儿。",
    # **顶格的脸**（2026-10-26 复采的真实输出 A1 rep2 原样）：
    # 脸那行没有缩进，最长连续符号串只有 1，旧判据让整只猫一块都拼不出来，
    # 界面按普通文字排版、行首空格被折叠，脸和嘴对不上耳朵。
    "cat_flat": " /\\_/\\\n( o.o )\n > ^ <\n\n大概是这样的。",
    # Unicode 制表线：─ │ ┌ 是画线符号，不是 Tab。
    "box": "┌──────┐\n│      │\n└──────┘\n\n雨声一直没停。",
    # 盲文点阵。
    "braille": "  ⣠⣾⣿⣦⡀\n  ⠙⠿⠿⠿⠋\n\n窗外没开灯。",
    # 方块点阵。
    "block": "████████\n█░░░░░░█\n█░████░█\n█░░░░░░█\n████████\n\n……画了一本书。",
    # 图案内部空行 + 前导空格 + 连续空格。
    "spaces": "   ┌────┐\n   │    │\n\n   └────┘\n\n下面还有一句。",
    # 纯图案回复（没有文字）。
    "pure": "  /\\_/\\\n ( o.o )\n  > ^ <",
    # 前缀与首行缩进同时出现：前缀要剥，缩进要留。
    "prefix_indent": "YUKI.N>   /\\_/\\\n  ( o.o )\n   > ^ <\n\n这样。",
    # 普通文本分段：必须仍报「分段」，不能被图案豁免误放。
    "prose": "看书。\n\n另外，温度在下降。",
    # 缩进的两行普通文字：不是图案。
    "indent_prose": "    你好呀\n    今天很好",
    # 代码样式文本：不是图案。
    "code": "def f(x):\n    return x + 1",
    # 长行 ASCII 图案：窄屏下的溢出与裁切。
    "wide": "┌──────────────────────────────────────────────┐\n│                                              │\n└──────────────────────────────────────────────┘\n\n一条很宽的框。",
    # 文字与图案交替：分块排版。
    "mixed": "先说一句。\n┌────┐\n│    │\n└────┘\n中间一句。\n  ⣠⣾⣿\n  ⠿⠿⠿\n最后一句。",
    # 中文标点排的「图案」：宽度最不可靠的一类。
    "cjk_art": "　　　／＼\n　　／　　＼\n　／　　　　＼",
    # 图案出现在**第四行之后**：打字机的前 3 行嗅探看不到它，
    # 于是图案的前两行会逐字吐出来，直到 isArt 成立才整块补齐。
    # 这是「尚未形成完整图案的中间状态」，要单独记录实际观感。
    "late_art": "第一句话。\n第二句话。\n第三句话。\n第四句话。\n\n┌────┐\n│    │\n└────┘",
    # Tab 与制表线字符是两回事（─│┌ 是画线符号）。这里只记录 Tab 的实际显示行为，
    # 不与画线符号混为一谈：Tab 在 pre 里的宽度由 tab-size 决定，不是字符宽度。
    "tab": "a\tb\n┌──┐\n└──┘",
}


class Upstream(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            request = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            request = {}
        messages = request.get("messages") or []
        user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user = (m.get("content") or "")
                break
        user = user.strip()
        mode, case, cut = "all", user, 0
        if user.startswith("@split:"):
            mode, case = "split", user[len("@split:"):]
        elif user.startswith("@cut:"):
            rest = user[len("@cut:"):]
            parts = rest.rsplit(":", 1)
            case = parts[0]
            cut = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 10
            mode = "cut"
        elif user.startswith("@"):
            case = user[1:]
        text = CASES.get(case, "已经收到。")

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        pieces = [text] if mode != "split" else list(text)
        if mode == "cut":
            pieces = [text[:cut]]
        try:
            for piece in pieces:
                frame = ("data: " + json.dumps(
                    {"choices": [{"delta": {"content": piece}}]}, ensure_ascii=False) + "\r\n\r\n")
                self.wfile.write(frame.encode())
                self.wfile.flush()
            if mode == "cut":
                # 模拟上游中途断开：不写 [DONE]，也不给 finish_reason。
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            self.wfile.write(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\r\n\r\n')
            self.wfile.write(b"data: [DONE]\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", action="store_true")
    ap.parse_args()
    try:
        server.load_corpus()
    except Exception as exc:
        sys.stderr.write(f"[fixture] 无检索索引，按「无原作片段」运行：{exc}\n")
    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    app = server.Server(("127.0.0.1", 0), server.Handler)
    for service in (upstream, app):
        threading.Thread(target=service.serve_forever, daemon=True).start()
    print(json.dumps({"app": app.server_address[1], "upstream": upstream.server_address[1]}),
          flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        for service in (app, upstream):
            service.shutdown()
            service.server_close()


if __name__ == "__main__":
    main()
