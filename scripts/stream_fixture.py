"""Local-only upstream + real application server for frontend HTTP regression tests."""
import http.server
import json
import socket
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


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
        sep = next(i for i, m in enumerate(messages) if m['content'].startswith('以上是语气示例'))
        real = [m for m in messages[sep + 1:] if m['role'] != 'system']
        if mode == 'inspect':
            content = json.dumps(real, ensure_ascii=False)
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
