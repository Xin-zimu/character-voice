#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
server.py — YUKI.N> 终端（两个世界 · 一个她）

零依赖: 只用 Python 标准库。

启动:
    python server.py
    # 浏览器打开 http://127.0.0.1:8765

它在做什么:
    浏览器不能直连大模型 API (CORS), 所以这里做一个极薄的本地服务:
      - 静态文件
      - 剧情检索 (读 data/index/, 13070 条原作语料)
      - 组装 prompt (两个人格卡 + 世界线 + 你的记录 + 检索到的原文)
      - 把上游的 SSE 原样透传给你

    API Key 只存在你自己的浏览器 localStorage 里, 不落盘、不上报。

接口:
    GET  /                     终端界面
    GET  /api/personas         人格卡 (两张, 对应两条世界线)
    GET  /api/worlds           世界线元信息 + 语料统计
    GET  /api/backends         本机可用的可选后端 (例如 Qoder 桥)
    GET  /api/corpus?ep=03     资料终端: 按集翻原文
    POST /api/preview          看这次请求真正发出去的 prompt (不花钱)
    POST /api/chat             对话, SSE 流式透传
"""
import http.server
import json
import os
import socket
import socketserver
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
PERSONAS = ROOT / "personas"
INDEX = ROOT / "data" / "index"
PORT = int(os.environ.get("PORT", "8766"))

# 可选后端的发现约定：qoder_backend.mjs 把地址与本地 token 写到这个文件。
QODER_STATE = Path.home() / ".dsh" / "qoder-backend" / "state.json"
QODER_PROVIDER = "qoder"

sys.path.insert(0, str(ROOT))
import prompt_builder as pb  # noqa: E402
from retriever import Corpus  # noqa: E402

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}

# 默认不走系统代理。
# 原因: 系统里设了 HTTP_PROXY 时, 连本地地址和国内 API 都会被塞进代理,
# 轻则变慢, 重则直接连不上。需要代理访问 OpenAI 的用户加 --use-env-proxy。
USE_ENV_PROXY = "--use-env-proxy" in sys.argv
OPENER = (urllib.request.build_opener() if USE_ENV_PROXY
          else urllib.request.build_opener(urllib.request.ProxyHandler({})))

CORPUS = None  # 延迟加载


def load_corpus() -> Corpus:
    global CORPUS
    if CORPUS is None:
        CORPUS = Corpus(INDEX)
    return CORPUS


def load_personas() -> dict:
    cards = {}
    for f in sorted(PERSONAS.glob("*.json")):
        try:
            card = json.loads(f.read_text(encoding="utf-8"))
            cards[card.get("id") or f.stem] = card
        except Exception as e:
            sys.stderr.write(f"[server] 人格卡解析失败 {f.name}: {e}\n")
    return cards


def probe_qoder_backend() -> dict:
    """读取 qoder_backend.mjs 的状态文件，判断这个可选后端现在能不能用。

    状态文件存在不等于能用：桥进程可能已经被杀掉，端口也就没了。
    所以这里真的连一次 /v1/models，连不上就报 unavailable。
    """
    out = {
        "id": QODER_PROVIDER,
        "name": "Qoder 连接器",
        "detail": "本机 Qoder 账号提供的模型，经 dsh-qoder-connect 转发",
        "available": False,
        "base_url": "",
        "token": "",
        "models": [],
        "reasoning_models": [],
        "recommended": "",
        "reason": "",
    }
    if not QODER_STATE.exists():
        out["reason"] = "桥未启动：先运行 node qoder_backend.mjs"
        return out
    try:
        doc = json.loads(QODER_STATE.read_text(encoding="utf-8"))
    except Exception as e:
        out["reason"] = f"状态文件无法解析：{e}"
        return out

    base_url = doc.get("base_url") or ""
    token = doc.get("token") or ""
    if not base_url or not token:
        out["reason"] = "状态文件缺少 base_url / token"
        return out

    out["base_url"] = base_url
    out["token"] = token
    out["models"] = doc.get("models") or []
    out["reasoning_models"] = doc.get("reasoning_models") or []
    out["recommended"] = doc.get("recommended") or ""

    # 真连一次，确认端口还活着。
    try:
        req = urllib.request.Request(
            base_url + "/v1/models",
            headers={"Authorization": "Bearer " + token},
        )
        with OPENER.open(req, timeout=3) as r:
            if r.status == 200:
                out["available"] = True
            else:
                out["reason"] = f"桥返回 {r.status}"
    except Exception as e:
        out["reason"] = f"连不上桥：{e}"
    return out


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if args and str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
            sys.stderr.write("[server] %s\n" % (fmt % args))

    # ------------------------------------------------------------------
    # 通用输出
    # ------------------------------------------------------------------
    def send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def json_err(self, code, msg):
        self.send_json({"error": msg}, code)

    # ------------------------------------------------------------------
    # GET
    # ------------------------------------------------------------------
    def do_GET(self):
        u = urlsplit(self.path)
        path = unquote(u.path)

        if path == "/api/personas":
            return self.send_json(list(load_personas().values()))
        if path == "/api/backends":
            return self.send_json({"backends": [probe_qoder_backend()]})
        if path == "/api/worlds":
            # 语料不入库（版权原因）。没有索引时不要让整个界面打不开：
            # 返回 200 + 语料状态，前端照样能聊天，只是没有原作片段。
            try:
                corpus = load_corpus()
                landing = corpus.landing()
                corpus_state = "ready"
                corpus_note = ""
            except Exception as e:
                landing = {"lines": 0, "episodes": [], "worlds": {}, "movie_cut": None}
                corpus_state = "missing"
                corpus_note = f"{e}。先运行 python build_corpus.py（需要 data/parsed 字幕）"
            return self.send_json({
                "worlds": pb.WORLD_META,
                "corpus": landing,
                "corpus_state": corpus_state,
                "corpus_note": corpus_note,
            })
        if path == "/api/corpus":
            qs = parse_qs(u.query)
            ep = (qs.get("ep") or [""])[0]
            try:
                offset = int((qs.get("offset") or ["0"])[0])
                limit = min(400, int((qs.get("limit") or ["200"])[0]))
            except ValueError:
                return self.json_err(400, "offset / limit 必须是整数")
            try:
                corpus = load_corpus()
            except Exception as e:
                # 资料终端在无语料时显示空列表并说明原因，而不是 500。
                return self.send_json({
                    "episodes": [],
                    "corpus_state": "missing",
                    "corpus_note": f"{e}。先运行 python build_corpus.py（需要 data/parsed 字幕）",
                })
            if ep == "":
                return self.send_json({"episodes": corpus.meta.get("episodes", [])})
            if ep not in corpus.episodes:
                return self.json_err(404, f"没有这一集: {ep}")
            return self.send_json(corpus.episode(ep, offset, limit))

        if path == "/favicon.ico":
            path = "/favicon.svg"   # 浏览器兜底会请求 .ico, 指到同一个图标
        if path in ("/", "/index.html"):
            return self.serve_file(WEB / "index.html")
        web_root = WEB.resolve()
        try:
            target = (web_root / path.lstrip("/")).resolve()
        except (OSError, ValueError):
            return self.send_error(404, "Not Found")
        if not target.is_relative_to(web_root) or not target.is_file():
            return self.send_error(404, "Not Found")
        return self.serve_file(target)

    def serve_file(self, p: Path):
        data = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(p.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ------------------------------------------------------------------
    # POST
    # ------------------------------------------------------------------
    def do_POST(self):
        path = unquote(urlsplit(self.path).path)
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self.json_err(400, "请求体不是合法 JSON")

        if path == "/api/preview":
            return self.api_preview(req)
        if path != "/api/chat":
            return self.send_error(404, "Not Found")
        return self.api_chat(req)

    def _compose(self, req):
        """把请求组装成上游 payload, 同时返回检索到的剧情。"""
        world = req.get("world") or "normal"
        if world not in pb.WORLD_META:
            world = "normal"
        user_text = (req.get("message") or "").strip()
        history = req.get("history") or []
        try:
            k = int(req.get("k") or 5)
        except (TypeError, ValueError):
            k = 5
        if not user_text:
            raise ValueError("消息为空")

        try:
            corpus = load_corpus()
            hits = corpus.search(world, user_text, k=k)
        except Exception as e:
            hits = []
            sys.stderr.write(f"[server] 检索不可用: {e}\n")

        cards = load_personas()
        if not cards:
            raise RuntimeError("personas 目录里没有可用的人格卡")
        persona = pb.persona_for(cards, world)

        messages = pb.build_messages(persona, world, hits, history, user_text)

        return {
            "world": world,
            "persona_id": persona.get("id"),
            "messages": messages,
            "hits": [
                {key: h[key] for key in
                 ("id", "ep", "ep_title", "start", "world", "text", "score")}
                for h in hits
            ],
        }

    def api_preview(self, req):
        try:
            out = self._compose(req)
        except (ValueError, RuntimeError) as e:
            return self.json_err(400, str(e))
        out["system_prompt"] = out["messages"][0]["content"]
        out["message_count"] = len(out["messages"])
        out.pop("messages")
        return self.send_json(out)

    def api_chat(self, req):
        backend = (req.get("backend") or "").strip()
        if backend == QODER_PROVIDER:
            # 可选后端：地址与本地 token 由服务端自己解析，
            # 不接受前端传来的 base_url / api_key，避免被指向别处。
            info = probe_qoder_backend()
            if not info["available"]:
                return self.json_err(503, f"Qoder 后端不可用：{info['reason']}")
            base_url = info["base_url"].rstrip("/")
            # 桥的地址是裸 origin（没有 /v1），与其他 OpenAI 兼容接口不同。
            # 这里补齐，否则会拼成 /chat/completions 而上游只认 /v1/chat/completions。
            if not base_url.endswith("/v1"):
                base_url += "/v1"
            api_key = info["token"]
            model = req.get("model") or (info["models"][0] if info["models"] else "auto")
        else:
            base_url = (req.get("base_url") or "https://api.deepseek.com/v1").rstrip("/")
            api_key = req.get("api_key") or ""
            model = req.get("model") or "deepseek-chat"
        try:
            temperature = float(req.get("temperature", 0.85))
            max_tokens = int(req.get("max_tokens", 400))
        except (TypeError, ValueError):
            return self.json_err(400, "temperature / max_tokens 必须是数字")

        if not api_key:
            return self.json_err(400, "缺少 API Key，请在右上角设置里填写")
        try:
            composed = self._compose(req)
        except (ValueError, RuntimeError) as e:
            return self.json_err(400, str(e))

        payload = {
            "model": model,
            "messages": composed["messages"],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        upstream = urllib.request.Request(
            base_url + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + api_key,
                "Accept": "text/event-stream",
            },
        )
        try:
            resp = OPENER.open(upstream, timeout=120)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:600]
            return self.json_err(e.code, f"上游 {e.code}: {body}")
        except Exception as e:
            return self.json_err(502, f"连接上游失败: {e}")

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("X-Yuki-World", composed["world"])
        self.send_header("X-Yuki-Hits", str(len(composed["hits"])))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        try:
            while True:
                chunk = resp.read(1)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # 用户关页面了
        except Exception as e:
            sys.stderr.write(f"[server] 流中断: {e}\n")
        finally:
            try:
                resp.close()
            except Exception:
                pass


class Server(socketserver.ThreadingTCPServer):
    # Windows 的 SO_REUSEADDR 允许多个进程绑定同一端口。
    allow_reuse_address = (os.name != "nt")
    daemon_threads = True


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def bind_server(start_port: int):
    """探测后仍捕获 bind 竞争；只顺延本次请求的十个端口。"""
    skipped = []
    last_error = None
    for port in range(start_port, min(start_port + 10, 65536)):
        if port_in_use(port):
            skipped.append(port)
            continue
        try:
            return Server(("127.0.0.1", port), Handler), skipped
        except OSError as exc:
            last_error = exc
            skipped.append(port)
    raise OSError(f"端口 {start_port}–{min(start_port + 9, 65535)} 均不可用。{last_error or ''}")


def main():
    if not (WEB / "index.html").exists():
        sys.exit(f"找不到界面文件: {WEB / 'index.html'}")

    try:
        httpd, skipped = bind_server(PORT)
    except OSError as e:
        sys.exit(f"启动失败：{e}")
    if skipped:
        print(f"  端口 {', '.join(map(str, skipped))} 已占用，自动顺延。")
    print("=" * 56)
    print("  长门有希 · 终端（两个世界 · 一个她）")
    try:
        c = load_corpus()
        print(f"  剧情库已载入: {c.size} 条 / {len(c.episodes)} 个片源")
    except Exception as e:
        print(f"  [!] 剧情库未就绪: {e}")
        print("      先运行: python build_corpus.py")
    print(f"  http://127.0.0.1:{httpd.server_address[1]}")
    print("  Ctrl+C 停止")
    print("=" * 56)

    with httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止。")


if __name__ == "__main__":
    main()
