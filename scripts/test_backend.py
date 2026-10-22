"""Server, prompt and retrieval regressions; uses only local sockets and stdlib."""
import http.client
import json
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import prompt_builder as pb
import server

# 语料不入库（版权原因）。没有 data/parsed 时这些测试无从谈起，
# 应当跳过并说明原因，而不是报红——否则克隆者会以为代码坏了。
CORPUS_READY = (ROOT / 'data' / 'index' / 'docs.jsonl').exists()
NO_CORPUS_REASON = (
    '语料未包含在本仓库中（版权原因）。请先自备 data/parsed/*.dialogue.jsonl，'
    '再运行 py build_corpus.py 生成索引。'
)


class BackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not CORPUS_READY:
            raise unittest.SkipTest(NO_CORPUS_REASON)
        cls.corpus = server.load_corpus()
        cls.app = server.Server(('127.0.0.1', 0), server.Handler)
        cls.thread = threading.Thread(target=cls.app.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.app.shutdown()
        cls.app.server_close()
        cls.thread.join()

    def request(self, path, body=None):
        conn = http.client.HTTPConnection(*self.app.server_address, timeout=5)
        try:
            if body is None:
                conn.request('GET', path)
            else:
                conn.request('POST', path, json.dumps(body).encode(), {'Content-Type': 'application/json'})
            reply = conn.getresponse()
            return reply.status, reply.read()
        finally:
            conn.close()

    def test_01_static_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            web = base / 'web'
            sibling = base / 'web2'
            web.mkdir(); sibling.mkdir()
            (web / 'index.html').write_text('safe')
            (sibling / 'private.txt').write_text('not public')
            (base / 'private.txt').write_text('not public')
            with patch.object(server, 'WEB', web):
                self.assertEqual(self.request('/?query=yes'), (200, b'safe'))
                for path in ('/../web2/private.txt', '/%2e%2e/web2/private.txt',
                             '/%2e%2e%2fweb2/private.txt', '/..%5cweb2%5cprivate.txt',
                             '/%252e%252e/web2/private.txt', '/%00',
                             '/C:/Windows/win.ini', '/..%2fprivate.txt?ignored=1'):
                    with self.subTest(path=path):
                        self.assertEqual(self.request(path)[0], 404)

    def test_02_port_rollover(self):
        # Reserve a real port. Select a pair whose second port is currently free.
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0)); occupied.listen()
            port = occupied.getsockname()[1]
            self.assertTrue(server.port_in_use(port))
            app, skipped = server.bind_server(port)
            try:
                self.assertIn(port, skipped)
                self.assertGreater(app.server_address[1], port)
                self.assertLessEqual(app.server_address[1], port + 9)
            finally:
                app.server_close()
        self.assertEqual(server.Server.allow_reuse_address, sys.platform != 'win32')

    def test_03_complete_corpus(self):
        self.assertEqual(self.corpus.size, 13070)
        self.assertEqual(len(self.corpus.episodes), 29)
        self.assertEqual(sum(d['world'] == 'crash' for d in self.corpus.docs), 1574)
        self.assertEqual(len({d['id'] for d in self.corpus.docs}), 13070)

    def test_04_retrieval_anchors(self):
        hits = self.corpus.search('normal', '信息统合思念体是什么', k=5)
        self.assertTrue(any(h['ep'] == '03' and '信息统合思念体意识的一部分' in h['text'] for h in hits))
        self.assertTrue(any(h['ep'] == '06' and 'YUKI.N>凉宫春日' in h['text'] for h in hits))
        for i, left in enumerate(hits):
            a = next(d for d in self.corpus.docs if d['id'] == left['id'])
            for right in hits[i + 1:]:
                b = next(d for d in self.corpus.docs if d['id'] == right['id'])
                if a['ep'] == b['ep']:
                    self.assertGreaterEqual(abs(a['idx'] - b['idx']), 12)

    def test_05_cross_world_isolation(self):
        """跨世界严格隔离（WORLD-001，DEC-002）。

        这个用例原来是 `test_05_cross_world_preview`，断言 prompt 里**必须**
        出现 `〔未被修改的时间线〕` 与 `【跨世界】` —— 那是为了锁住当时的
        「降权但不过滤」机制。行为反转后，断言也反转：这些字样必须不出现。

        注意「你为什么戴眼镜」在当前世界本来就能回答，
        所以标准不是「命中为空」，而是「不含另一世界的片段」。
        """
        for world, other in (('crash', 'normal'), ('normal', 'crash')):
            status, raw = self.request('/api/preview',
                                       {'world': world, 'message': '你为什么戴眼镜'})
            self.assertEqual(status, 200)
            preview = json.loads(raw)
            sp = preview['system_prompt']

            # 1) 旧机制的痕迹必须消失
            for marker in ('〔未被修改的时间线〕', '〔另一条世界线〕', '【跨世界】',
                           '像别的什么一样浮上来'):
                self.assertNotIn(marker, sp, f'{world} prompt 仍含旧跨世界标记：{marker}')

            # 2) 命中列表里不能有另一世界的条目
            for h in preview['hits']:
                self.assertEqual(h['world'], world,
                                 f'{world} 命中了另一世界的片段：{h["ep"]} {h["start"]}')

            # 3) 注入块的每一行都不能属于另一世界
            #    用「集数 + 时间码」定位，而不是用 text 做子串判断 ——
            #    像「为什么」这种短台词会误伤（它本来就能出现在别处）。
            if '=== 原作记录 ===' in sp:
                block = sp.split('=== 原作记录 ===')[1].split('=== 记录结束 ===')[0]
                # 不用 import re 的顶层名字：sys.path 上有同名模块会互相遮蔽，
                # 这里直接用字符串切分，避免与 retriever 的导入顺序纠缠。
                injected = set()
                for line in block.splitlines():
                    line = line.strip()
                    if line.startswith('▶'):
                        injected.add(line[1:9])
                other_docs = {d['start'][:8] for d in self.corpus.docs
                              if d['world'] == other and d['ep'] == '消失'}
                self.assertFalse(
                    injected & other_docs,
                    f'{world} 注入块的上下文行含 {other} 的时间点：{sorted(injected & other_docs)}')
                for marker in ('〔', '另一条世界线'):
                    self.assertNotIn(marker, block)

    def test_05b_retriever_filters_by_world(self):
        """检索层必须硬过滤，而不是降权。"""
        for world in ('normal', 'crash'):
            hits = self.corpus.search(world, '凉宫春日 信息统合思念体', k=8)
            self.assertTrue(hits, f'{world} 检索不应为空')
            for h in hits:
                self.assertEqual(h['world'], world)
        # 全量索引仍然保留 13070 条，只是查询时筛选。
        self.assertEqual(len({d['id'] for d in self.corpus.docs}), 13070)

    def test_06_world_and_corpus_endpoints(self):
        status, raw = self.request('/api/worlds')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['corpus']['lines'], 13070)
        status, raw = self.request('/api/corpus?ep=03&offset=0&limit=200')
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(raw)['items']), 200)
        self.assertEqual(self.request('/api/corpus?ep=missing')[0], 404)

    def test_07_history_odd_boundary(self):
        history = [{'role': 'user', 'content': f'q{i}'} if i % 2 == 0 else
                   {'role': 'assistant', 'content': f'YUKI.N>a{i}'} for i in range(61)]
        history += [{'sys': True}, {'role': 'assistant', 'content': ''},
                    {'role': 'system', 'content': 'must not enter prompt'}, None]
        result = pb.format_history(history)
        tail = [m for m in result if m['role'] != 'system']
        self.assertEqual(tail[0], {'role': 'user', 'content': 'q38'})
        self.assertEqual(tail[-1]['content'], 'q60')
        self.assertEqual(len(tail), 23)
        self.assertFalse(any('YUKI.N>' in m['content'] for m in tail))
        self.assertIn('38 条', result[0]['content'])

    def test_08_persona_fields_preserved(self):
        cards = server.load_personas()
        for world, delay in [('normal', 42), ('crash', 68)]:
            p = pb.persona_for(cards, world)
            self.assertEqual(p['world'], world)
            self.assertEqual(p['protocol']['type_delay_ms'], delay)
            self.assertIn('bubble_user', p['theme'])
            self.assertNotIn('metrics', p)
            # DEC-001：字数配额已取消，字段重新出现即为回归。
            self.assertFalse([k for k in p['guard'] if k.startswith('max_chars')],
                             f'{p["id"]} guard 含已废止的 max_chars 字段')
            # tech_keywords 原本只用于选择字数档位，档位取消后已无用途。
            self.assertNotIn('tech_keywords', p['guard'],
                             f'{p["id"]} guard 仍含无用途的 tech_keywords')
            self.assertTrue(p['guard']['banned_patterns'])
            self.assertTrue(p['examples'])
            for e in p['examples']:
                self.assertTrue(e.get('source'), f'{p["id"]} 示例缺 source')

    def test_09_all_examples_reach_prompt(self):
        """人格卡的示例必须全部进入 prompt。

        以前 build_messages 写死 [:14]，超出部分被静默丢弃 ——
        改了 JSON 却没效果，且没有任何提示。这里锁住该行为。
        """
        cards = server.load_personas()
        for world in ('normal', 'crash'):
            p = pb.persona_for(cards, world)
            msgs = pb.build_messages(p, world, [], [], '测试')
            sent = [m for m in msgs if m['role'] == 'assistant']
            self.assertEqual(len(sent), len(p['examples']),
                             f'{p["id"]} 示例被截断：卡内 {len(p["examples"])} 条，'
                             f'实际发送 {len(sent)} 条')
            self.assertEqual([m['content'] for m in sent],
                             [pb.strip_prefix(e['assistant']) for e in p['examples']])

    def test_10_backends_endpoint(self):
        """/api/backends 必须始终回答，且不泄露上游凭证。

        桥没启动时也要返回 200，只是 available=false ——
        前端据此把选项置灰，而不是整个设置面板打不开。
        """
        status, raw = self.request('/api/backends')
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertIn('backends', data)
        self.assertEqual(len(data['backends']), 1)
        q = data['backends'][0]
        for key in ('id', 'name', 'available', 'reason'):
            self.assertIn(key, q)
        self.assertIsInstance(q['available'], bool)
        # 不可用时必须给出原因，方便用户知道该跑什么。
        if not q['available']:
            self.assertTrue(q['reason'])

    def test_11_qoder_backend_rejects_when_unavailable(self):
        """桥不可用时，/api/chat 走 backend=qoder 必须明确报错，不能静默回退。"""
        info = server.probe_qoder_backend()
        if info['available']:
            self.skipTest('本机桥正在运行，跳过不可用分支')
        status, raw = self.request('/api/chat', {
            'world': 'normal', 'message': '测试', 'backend': 'qoder'})
        self.assertEqual(status, 503)
        self.assertIn('Qoder', json.loads(raw)['error'])

    def test_12_persona_no_superseded_framing(self):
        """人格卡里被审计判定为越界的表述不得回流（CHARACTER AUD-01/02/04）。

        - 「用词越少越好」与 CHAT-003 冲突：字数上限已取消，这句话等于把
          隐藏的配额写回提示词。
        - 「情绪恒定」把外在克制推成内在事实，SRC-01 不支持。
        - 「作为助手处理用户提出的问题」「不做安慰」「直接给建议」把任务助手
          定位写进身份，与 SCOPE-001 的聊天优先方向相反；给建议必须是应答
          对方的求助，不是默认动作，所以旧规则句和相关示例一起改。

        每一项都成对断言：废止的不在，改写的在。只查前者会被「整段删掉」
        绕过——那不算改写。只断言结构，不评价她说话像不像，后者属于
        VALIDATION 的 C 类证据。
        """
        AUD = {
            'AUD-01': {
                'gone': ['情绪恒定'],
                'kept': ['情绪外露很少'],
            },
            'AUD-02': {
                'gone': ['用词越少越好', '大多数时候用一两个词就够了'],
                'kept': ['一句能说完就不说两句'],
            },
            'AUD-04': {
                # 「直接给建议或直接说该做什么」是无条件给建议，与同段
                # 「对方没有求助时，不主动给建议」自相矛盾，必须一起清掉。
                'gone': ['作为助手处理用户提出的问题', '【作为助手的工作方式】',
                         '不做安慰', '直接给建议或直接说该做什么'],
                'kept': ['持续的通讯', '【相处方式】', '对方没有求助时',
                         '对方在问该怎么办时'],
            },
        }
        cards = server.load_personas()
        p = pb.persona_for(cards, 'normal')
        system = pb.build_messages(p, 'normal', [], [], '测试')[0]['content']
        # 成对断言：废止的说法必须不在，改写后的说法必须在。
        # 只做前者会被「整段删掉」绕过——删掉段落同样能让 assertNotIn 通过。
        for aud, rules in AUD.items():
            for phrase in rules['gone']:
                self.assertNotIn(phrase, system,
                                 f'{aud}：prompt 仍含已废止表述「{phrase}」')
            for phrase in rules['kept']:
                self.assertIn(phrase, system,
                              f'{aud}：prompt 缺少改写后的「{phrase}」'
                              f'——删掉段落不等于改写')

        # 示例也在示范：旧示例「我明天要考试，好慌 → 现在看三章」
        # 教的正是无条件给建议，与 AUD-04 的新规则相反。
        asks = [e['user'] for e in p['examples']]
        self.assertNotIn('我明天要考试，好慌', asks,
                         'AUD-04：示例仍在示范「对方没说怎么办就直接给方案」')
        self.assertTrue(any('怎么办' in u for u in asks),
                        'AUD-04：示例里缺少「对方明确问该怎么办」的样本，'
                        '无法示范给建议的边界')


if __name__ == '__main__':
    # Windows + Python 3.13 上遇到过陈旧 __pycache__ 触发的 CPython 致命错误
    # （_PyEval_EvalFrameDefault: Executing a cache），表现是所有断言都通过、
    # 之后进程崩在 setUpClass 里。先禁写字节码，避免这个环境坑被误读成代码问题。
    sys.dont_write_bytecode = True
    unittest.main(verbosity=2)
