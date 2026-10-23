"""Local-only upstream + real application server for frontend HTTP regression tests."""
import http.server
import json
import re
import socket
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


def _examples_from_system(system_text):
    """从第一条 system 的收尾语里读出示例组数。

    没有这句就说明没发示例，返回 0 —— 此时整段都是真实对话。
    """
    m = re.search(r'上面 (\d+) 组是语气示例', system_text)
    return int(m.group(1)) if m else 0


class Upstream(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        mode = request['model']
        if mode == 'http-error':
            self.send_error(503, 'fixture unavailable')
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        # A larger declared length forces an incomplete upstream response.
        if mode == 'broken':
            self.send_header('Content-Length', '100000')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        if mode == 'empty-eof':
            return
        messages = request['messages']
        # 示例收尾语已并进第一条 system（见 prompt_builder.build_messages 注释），
        # 不能再靠那句文本定位示例段。但那条 system 里写着「上面 N 组是语气示例」，
        # 直接读这个数字最可靠——按消息形状去猜会把最后一条示例和历史首条搞混。
        n_examples = _examples_from_system(messages[0]['content'])
        real = [m for m in messages[1 + 2 * n_examples:] if m['role'] != 'system']
        if mode == 'inspect':
            content = json.dumps(real, ensure_ascii=False)
        elif mode == 'art':
            # 字符画的显示链路（CHAT-017 / V-ART-02）。四条都是真的会出现的形态：
            # 一个对齐好的方框、一行盲文点阵、一个歪掉的图案、一段带空行的普通话。
            # 用**逐字节**发出去，模拟上游把多行内容拆成任意边界的情况。
            content = ('YUKI.N>雨声一直没停。\n'
                       '┌──────┐   \n'
                       '│      │   \n'
                       '└──────┘   \n'
                       '\n'
                       '  ⣠⣾⣿⣦⡀  \n'
                       '  ⠙⠿⠿⠿⠋  \n'
                       '\n'
                       '窗外没开灯。')
        elif mode == 'art-misaligned':
            # 左边距不一致 + 行尾多空格：界面上应该标 △，不该安静通过。
            content = 'YUKI.N>  /\\_/\\\n ( o.o )   \n   > ^ <'
        elif mode == 'prose-break':
            # 普通分段：仍然要标「分段」，不能被字符画那条豁免误放。
            content = 'YUKI.N>看书。\n\n另外，温度在下降。'
        else:
            content = 'YUKI.N>已经收到。'
        frame = 'data: ' + json.dumps({'choices': [{'delta': {'content': content}}]}, ensure_ascii=False) + '\r\n\r\n'
        # Byte-sized writes exercise UTF-8 and SSE chunk boundaries.
        for byte in frame.encode():
            self.wfile.write(bytes([byte]))
            self.wfile.flush()
        if mode == 'error-frame':
            self.wfile.write(b'data: {"error":{"message":"fixture stream error"}}\n\n')
        elif mode == 'finish-only':
            self.wfile.write(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}')
        elif mode not in ('broken', 'eof'):
            self.wfile.write(b'data: [DONE]\n\n')
        self.wfile.flush()
        if mode == 'broken':
            self.connection.shutdown(socket.SHUT_RDWR)


def main():
    # 语料不入库（版权原因）。没有索引时仍然启动：检索会退化为「无原作片段」，
    # 流式与 HTTP 链路照样能测。否则克隆者连测试都跑不起来。
    try:
        server.load_corpus()
    except Exception as exc:
        sys.stderr.write(f'[fixture] 无检索索引，按「无原作片段」运行：{exc}\n')
    upstream = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    app = server.Server(('127.0.0.1', 0), server.Handler)
    for service in (upstream, app):
        threading.Thread(target=service.serve_forever, daemon=True).start()
    print(json.dumps({'app': app.server_address[1], 'upstream': upstream.server_address[1]}), flush=True)
    try:
        if '--serve' in sys.argv:
            threading.Event().wait()  # Manual browser verification; Ctrl+C stops it.
        else:
            sys.stdin.buffer.read()
    except KeyboardInterrupt:
        pass
    finally:
        for service in (app, upstream):
            service.shutdown()
            service.server_close()


if __name__ == '__main__':
    main()
