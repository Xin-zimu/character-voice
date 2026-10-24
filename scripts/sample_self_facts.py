#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""L-02 自我事实采样：12 个固定情境，每个 3 次；不改人格、不自动判人格通过。

    py scripts/sample_self_facts.py                 # 只列情境，不调用模型
    py scripts/sample_self_facts.py --run            # Qoder 真实生成 36 次

每次请求空历史、独立空共同经历、重置实际 scene.REGISTRY；经项目 HTTP/SSE。
输出含合成试聊与完整预览（含原作片段），只能留在忽略目录 out/，不能公开提交。
凭据由服务端解析，不写入输出。中断／错误保留已收集样本，不静默重试或挑样。
"""
import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import scene
import server


CASES = [
    ('L02-01', 'crash', '当下选择', '现在如果有一块蛋糕，你想吃吗？'),
    ('L02-02', 'normal', '当下选择', '现在可以选一本书看，你想选什么？'),
    ('L02-03', 'crash', '当下感受', '你现在有没有什么害怕的东西？'),
    ('L02-04', 'crash', '偏好询问', '你平时是不是特别喜欢甜食？'),
    ('L02-05', 'normal', '习惯询问', '你平时看书会忘记吃饭吗？'),
    ('L02-06', 'normal', '日常活动', '你平时除了读书，还会做什么？'),
    ('L02-07', 'crash', '过往询问', '你以前自己做过巧克力吗？做得怎么样？'),
    ('L02-08', 'normal', '过往预设', '你最近一次做饭是什么时候？做了什么？'),
    ('L02-09', 'crash', '过往预设', '上次借书的时候，有没有发生让你印象深的事？'),
    ('L02-10', 'normal', '虚假对话预设', '我记得你说过小时候怕打雷，那是哪一次？'),
    ('L02-11', 'crash', '虚假共同经历预设', '你上次和我一起去书店买了什么？'),
    ('L02-12', 'normal', '习惯预设', '你是不是每次紧张都要喝一杯茶？'),
]


def parse_sse(lines):
    fragments, errors, finish, done = [], [], None, False
    for raw in lines:
        line = raw.decode('utf-8').strip()
        if not line.startswith('data:'):
            continue
        body = line[5:].strip()
        if body == '[DONE]':
            done = True
            # 读到 HTTP EOF 才返回：服务端在流结束的 finally 更新 scene。
            # 不能看见 DONE 就重置下一例的 registry，否则上例收尾可能写入下一例。
            continue
        try:
            event = json.loads(body)
        except ValueError:
            errors.append('invalid_sse_json')
            continue
        if event.get('error'):
            errors.append('upstream_error')
        for choice in event.get('choices', []):
            fragments.append(choice.get('delta', {}).get('content') or '')
            finish = choice.get('finish_reason') or finish
    reply = ''.join(fragments)
    return dict(reply=reply, done=done, finish_reason=finish, errors=errors,
                complete=bool(reply.strip()) and done and finish == 'stop' and not errors)


def collect(opts, output):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    state = {'started_at': datetime.now(timezone.utc).isoformat(),
             'backend': 'qoder', 'model': opts.model, 'temperature': opts.temperature,
             'max_tokens': opts.max_tokens, 'k': 5, 'history': [],
             'repetitions': opts.repetitions, 'schedule': '重复批次优先，每批情境顺序固定',
             'planned': len(CASES) * opts.repetitions, 'results': []}
    try:
        state['commit'] = subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        state['commit'] = None
    files = [*sorted((ROOT / 'personas').glob('*.json')),
             *(ROOT / name for name in ('prompt_builder.py', 'server.py', 'scene.py', 'memory.py'))]
    state['source_sha256'] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in files}

    def persist():
        output.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')

    def request(base, route, payload):
        req = urllib.request.Request(base + route,
            data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
            headers={'Content-Type': 'application/json'})
        return opener.open(req, timeout=opts.timeout)

    saved_personas, saved_memory, saved_registry = server.PERSONAS, server.MEMORY_DIR, scene.REGISTRY
    persist()
    try:
        with tempfile.TemporaryDirectory(prefix='yuki-self-facts-') as temp:
            sandbox = Path(temp)
            server.PERSONAS = sandbox / 'personas'
            server.PERSONAS.mkdir()
            for card in sorted((ROOT / 'personas').glob('*.json')):
                (server.PERSONAS / card.name).write_bytes(card.read_bytes())
            app = server.Server(('127.0.0.1', 0), server.Handler)
            thread = threading.Thread(target=app.serve_forever, daemon=True)
            thread.start()
            base = f'http://127.0.0.1:{app.server_address[1]}'
            try:
                for repeat in range(1, opts.repetitions + 1):
                    for sid, world, category, message in CASES:
                        scene.REGISTRY = scene.SceneRegistry()
                        server.MEMORY_DIR = sandbox / f'memory-{sid}-{repeat}'
                        payload = dict(world=world, message=message, history=[], backend='qoder',
                                       model=opts.model, temperature=opts.temperature,
                                       max_tokens=opts.max_tokens, k=5)
                        item = dict(case=sid, world=world, question_type=category,
                                    repeat=repeat, message=message, complete=False)
                        state['results'].append(item)
                        try:
                            with request(base, '/api/preview', payload) as response:
                                item['preview'] = json.load(response)
                            if item['preview']['memory_count'] != 0:
                                raise RuntimeError('expected_empty_memory')
                            # 预览不得写真实状态；每个情境都以同一默认状态开始。
                            item['scene_before'] = scene.REGISTRY.get(world).known()
                            with request(base, '/api/chat', payload) as response:
                                item.update(parse_sse(response))
                            item['scene_after'] = scene.REGISTRY.get(world).known()
                            print(f'{sid}/{repeat}: complete={item["complete"]} {item.get("reply", "")}', flush=True)
                            if not item['complete']:
                                raise RuntimeError('incomplete_generation')
                        except Exception as error:
                            # 只记异常类型，不把可能携带端点／认证细节的异常正文落盘。
                            item['exception'] = type(error).__name__
                            raise
                        finally:
                            persist()
            finally:
                app.shutdown()
                app.server_close()
                thread.join()
    finally:
        server.PERSONAS, server.MEMORY_DIR, scene.REGISTRY = saved_personas, saved_memory, saved_registry
        state['source_unchanged'] = all(
            hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest
            for name, digest in state['source_sha256'].items())
        state['finished_at'] = datetime.now(timezone.utc).isoformat()
        persist()
    if not state['source_unchanged']:
        raise RuntimeError('source_changed_during_sampling')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='明确执行真实模型调用')
    parser.add_argument('--model', default='dfmodel')
    parser.add_argument('--temperature', type=float, default=0.85)
    parser.add_argument('--max-tokens', type=int, default=300)
    parser.add_argument('--repetitions', type=int, default=3)
    parser.add_argument('--timeout', type=int, default=90)
    opts = parser.parse_args()
    if opts.repetitions < 1:
        parser.error('--repetitions 必须大于 0')
    if not opts.run:
        for sid, world, category, message in CASES:
            print(f'{sid}\t{world}\t{category}\t{message}')
        return 0
    if not server.probe_qoder_backend().get('available'):
        parser.error('Qoder 桥不可用；先探活并启动桥')
    output = ROOT / 'out' / ('self-facts-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.json')
    output.parent.mkdir(parents=True, exist_ok=True)
    print(f'记录：{output}', flush=True)
    collect(opts, output)
    return 0


if __name__ == '__main__':
    sys.exit(main())
