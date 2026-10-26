"""Server, prompt and retrieval regressions; uses only local sockets and stdlib."""
import http.client
import json
import re
import socket
import sys
import time
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import prompt_builder as pb
import server
import textart
sys.path.insert(0, str(ROOT / 'scripts'))
import check_persona

# 语料不入库（版权原因）。没有 data/index 时，**只有依赖字幕的用例**应当跳过，
# 其余用例（人格卡、记忆、场景、消息组装、字符画判据……）照常跑。
#
# 为什么必须分组（2026-10-24）：原来这里在 `BackendTests.setUpClass` 里
# `raise SkipTest`，于是**整个类**被跳过——克隆者跑出来是「后端 0 项」，
# 看起来像全绿，实际什么都没验。那比红更危险：红的测试会让人去修，
# 被跳过的测试只会让人以为没事。
#
# 现在的形状：
#   BackendTests         —— 不碰字幕，任何机器都能跑
#   CorpusTests(BackendTests) —— 依赖字幕，没有索引就只跳过这几个
CORPUS_READY = (ROOT / 'data' / 'index' / 'docs.jsonl').exists()
NO_CORPUS_REASON = (
    '这条用例依赖字幕索引（语料未包含在本仓库中，版权原因）。'
    '请先自备 data/parsed/*.dialogue.jsonl，再运行 py build_corpus.py。'
)


class BackendTests(unittest.TestCase):
    """不依赖字幕的用例：起一个本地服务，跑 HTTP 与组装逻辑。

    **这里的 setUpClass 不检查语料** —— 没有字幕时这些用例一样有意义。
    """

    @classmethod
    def setUpClass(cls):
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

    # ---------------------------------------------------------------
    # 「给建议」判据（AUD-04 第二层）
    #
    # 判的是**祈使句**：中文里「让对方做某事」最常见的形状是动词开头、无主语。
    #
    # 边界要说清楚，别把它当成语义判定器：
    #   - `先别急`、`休息一下吧` 与 `别熬夜了` 形状相同，前者是安抚、后者是要求，
    #     正则分不出来。所以它只是粗筛，只用于防「示例里出现明确的行动指令」。
    #   - 抓不到的（「你会好起来的」这类不含动作的安慰、名词化建议）不在这里兜，
    #     靠 `V-CHAR-02` 试聊人工看。
    #   - 被误报时先确认是真建议还是安抚；真是测试太笨就改测试，
    #     不要为了过测试去改人格卡的写法。
    #
    # 第一版只认「把/你可以/先/然后」几种构式，复审实测漏掉 5 条
    # （「吃点甜的会好些」「别再熬夜了」「放下手机」「面试前把公司查一遍」
    # 「选那个离家近的」），换成动词开头之后都抓得住。
    # ---------------------------------------------------------------
    _LEAD = r'(?:那|那就|要不|不如|最好|还是|所以)?(?:你)?(?:可以|应该|需要|得)?'
    _VERB = (r'(?:别|不要|不用|不必|先|再|赶紧|快|多|少|早点|记得|记住|注意|'
             r'吃|喝|睡|休息|去|来|看|读|写|学|做|买|换|选|放|停|把|用|查|问|'
             r'找|打|给|拿|带|送|告诉|试试|看看|想想|问问|整理|复习|检查|准备|'
             r'参加|申请|保持|坚持|控制|确认|判断)')
    # 引导词与动词都必须是**必需的**：整条写成可选会让空串也匹配，
    # 于是任何句子都算「给建议」（本轮就踩过，一次误报 24 条）。
    IMPERATIVE = re.compile('^' + _LEAD + _VERB)

    # 句首祈使但其实是安抚/表态的固定说法。宁可少列，只收确定的。
    SOOTHE = re.compile(
        r'^(不用担心|不需要|没有必要|不用谢|不用客气|别在意|做不到|'
        r'知道了|不确定|不知道|没有了|不是)')

    @staticmethod
    def advice_hits(text):
        """返回回答里「要求对方做事」的小句片段；空列表表示没给建议。"""
        # 先剥掉括号动作再判。括号里写的是她自己的动作（「把书合上」），
        # 不是对对方的要求——不剥就会把纯动作回复全判成给建议。
        body = re.sub(r'（[^）]*）|\([^)]*\)', '', text)
        hits = []
        for clause in re.split(r'[，。！？；\n]', body):
            clause = clause.strip()
            if not clause or BackendTests.SOOTHE.search(clause):
                continue
            # 句首时间状语先剥掉：「面试前把公司查一遍」的指令在「把」上。
            # 必须锚在「…前/…后」这种**名词性**时间词上，且剥完要以动词开头；
            # 否则「你说的时候停了两次」会被剥成「停了两次」，把陈述读成指令
            # （复审的误判样例就是这么来的）。
            stripped = re.sub(r'^(?:[一二三四五六七八九十]?[0-9]*[点号天周月年]?|面试|考试|'
                              r'开会|上班|出门|回家|吃饭)?(前|后|之前|之后|的时候)',
                              '', clause)
            if not BackendTests.IMPERATIVE.match(stripped):
                stripped = clause
            m = (BackendTests.IMPERATIVE.match(stripped)
                 or BackendTests.IMPERATIVE.match(clause))
            if not m:
                continue
            # 动词后紧跟「了/到/过」是已发生或结果补语（「停了两次」「读到一半」），
            # 不是要求对方做。祈使句不会这么写。
            rest = clause[m.end():]
            if rest[:1] in ('了', '到', '过', '着', '完'):
                continue
            # 「动词 + 久了 / 长了 / 多了 / 少了」是**她自己**的状态或程度，
            # 不是让对方做。新加的示例「看久了有点压鼻梁」就是被这条误伤的：
            # 「看」被当成祈使句的动词，后面「久了」是时长补语。
            # 祈使句里这个构式几乎不出现（「等久了再说」是条件句，不是指令），
            # 所以放行。**不要**因此把单字动词一概放行——「放下手机」还得抓住。
            if rest.startswith(('久了', '长了', '多了', '少了', '起来', '出来')):
                continue
            # 「V 得/不得 + 补语」同属结果补语（「读得下去」）。
            if rest[:1] in ('得',) and len(rest) > 1:
                continue
            # 三字以内的自述（「看书。」）不是指令，是回答「你在干什么」。
            # 只兜最短的：放太宽会把「放下手机」也算进来，那是要对方做的事。
            if len(clause) <= 3:
                continue
            hits.append(m.group(0))
        return hits

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

    # ------------------------------------------------------------------
    # 依赖字幕的用例（2026-10-24 起按下述方式分组）
    #
    # 仍然是 BackendTests 的方法，但**各自**在开头调用 `_need_corpus()`。
    # 这样没有索引时只跳过这几个，而不是整个类消失。
    #
    # 为什么不用单独的子类装它们：那些用例要复用 `request()`、`memory_dir()`
    # 等一批 helper，拆出去要么复制 helper、要么多重继承，两种都比现在难读。
    # 而且拆类会把这些用例从 `BackendTests` 的其它 setUp/tearDown 约定里剥离，
    # 属于为了「整齐」付出真实风险。这里要的是**跳过粒度**，不是类的美观。
    # ------------------------------------------------------------------
    @classmethod
    def _need_corpus(cls):
        if not CORPUS_READY:
            raise unittest.SkipTest(NO_CORPUS_REASON)
        if not hasattr(cls, 'corpus') or cls.corpus is None:
            cls.corpus = server.load_corpus()

    def test_03_complete_corpus(self):
        self._need_corpus()
        self.assertEqual(self.corpus.size, 13070)
        self.assertEqual(len(self.corpus.episodes), 29)
        self.assertEqual(sum(d['world'] == 'crash' for d in self.corpus.docs), 1574)
        self.assertEqual(len({d['id'] for d in self.corpus.docs}), 13070)

    def test_03b_corpus_load_is_locked(self):
        """`load_corpus()` 必须只构造**一份**索引（2026-10-26 复现的竞态）。

        原来是「先查后建」：

            if CORPUS is None:
                CORPUS = Corpus(INDEX)

        `ThreadingTCPServer` 每个请求一个线程，窗口里进来的线程都会看到
        `CORPUS is None`，于是同时构造多份 13070 条索引。实测 6 个线程
        同时进来**构造了 6 次**、拿到 6 个互不相同的对象。

        后果不只是浪费内存：多份索引在多个线程里同时构建，本机 CPython
        3.13.2 上会偶发解释器级崩溃（`Fatal Python error:
        _PyEval_EvalFrameDefault: Executing a cache` 或 0xC0000005）。
        全量变异测试连跑两遍各崩一次、崩的位置还是随机的——根因就是这个竞态。

        这里不真建索引（太慢），而是换成立即返回的假构造器，只数次数。
        """
        import threading as _threading

        real_corpus = server.Corpus
        real_global = server.CORPUS
        calls = []
        gate = _threading.Barrier(6)

        class _SlowCorpus:
            def __init__(self, index_dir):
                calls.append(index_dir)
                # **必须真的慢下来**：真实索引构造要几秒，竞态窗口就有那么大。
                # 第一版写成立即返回的假构造器，结果**无锁也能过**——
                # 六个线程还没来得及相遇，第一个就已经把 CORPUS 填好了，
                # 于是用例测不到它声称要测的东西（M79 就是这么漏掉的）。
                time.sleep(0.2)

        try:
            server.Corpus = _SlowCorpus
            server.CORPUS = None

            def worker():
                gate.wait()         # 尽量让所有线程同时进 load_corpus
                server.load_corpus()

            threads = [_threading.Thread(target=worker) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(len(calls), 1,
                             f'并发调用构造了 {len(calls)} 份索引，应当是 1 份')
        finally:
            server.Corpus = real_corpus
            server.CORPUS = real_global

    def test_04_retrieval_anchors(self):
        self._need_corpus()
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
        self._need_corpus()
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

    def test_05c_context_lines_are_filtered_per_world(self):
        """上下文窗口里**每一行**都不能属于另一世界（R-01，WORLD-001-B）。

        为什么单独立一条：`test_05` 的注入块检查只收集以 `▶` 开头的**中心句**
        时间点，普通上下文行一条都没查。实测索引里有 10 个窗口、30 行上下文
        属于另一世界（全部在《消失》切片附近），而 test_05 一直是绿的。

        所以这里做两件事，都不看 `▶`：
          1. **索引层**：`ctx_worlds` 必须逐行存在，且长度与 `ctx` 行数一致；
             并且全量扫描 13070 条，不许出现「本行世界 ≠ 记录世界」的残留。
          2. **渲染层**：对**所有**命中做一次真实 prompt 组装，注入块里出现的
             每一个时间码，都必须能在本世界找到对应记录。

        第 2 点是关键：它不假设哪几行会被裁掉，而是拿「出现的每个时间码」
        去反查世界。裁多了不算错（宁可缺），出现另一世界的行才算错。
        """
        self._need_corpus()
        # 1) 索引层：逐行世界标注存在且自洽
        for d in self.corpus.docs:
            self.assertIn('ctx_worlds', d,
                          f'{d["id"]} 缺少逐行世界标注，无法按行过滤')
            n_lines = len(d['ctx'].splitlines())
            self.assertEqual(
                len(d['ctx_worlds']), n_lines,
                f'{d["id"]} 的 ctx_worlds 与 ctx 行数不一致：'
                f'{len(d["ctx_worlds"])} vs {n_lines}')

        # 全量扫一遍：有没有「窗口内出现另一世界行」的记录。
        # 这不是要求索引被裁干净（原文必须全留），而是要求**标注**是准的，
        # 且渲染层能据此裁掉。所以这里只统计，真正的断言在下面渲染层。
        world_of = {(d['ep'], d['start']): d['world'] for d in self.corpus.docs}
        for d in self.corpus.docs:
            for ts, w in zip(
                    [line.strip().lstrip('▶').strip().split(None, 1)[0]
                     for line in d['ctx'].splitlines() if line.strip()],
                    d['ctx_worlds']):
                expect = world_of.get((d['ep'], ts))
                if expect is not None:
                    self.assertEqual(
                        w, expect,
                        f'{d["id"]} 的上下文行 {ts} 标成 {w}，实际是 {expect}')

        # 2) 渲染层：真实组装 prompt，注入块里的每个时间码都得属于本世界
        #
        # **这里踩过一次空断言**：第一版按 `[1] 标题 · 0:37:54` 里的标题反查集数，
        # 但那是 `ep_title`（「凉宫春日的消失（剧场版）」），不是 `ep`（`消失`），
        # 于是 `(ep, ts)` 永远查不到，`other_starts` 里也就永远不命中——
        # 用例恒绿。变异 M52 把逐行过滤整个删掉都没红，才暴露出来。
        #
        # 改成不依赖集名：**`(集, 时间码)` 只在《消失》里跨世界**，所以只要看
        # 「这个时间码在本世界有没有对应记录、在另一世界有没有」，两边一比就清楚。
        # 具体做法：把每行的 `时间码` 摊平成集合，分别求它与两个世界的交集；
        # 若某行只属于另一世界，就是跨界。
        blocks_checked = 0
        lines_checked = 0
        for world, other in (('normal', 'crash'), ('crash', 'normal')):
            mine_starts = {d['start'] for d in self.corpus.docs
                           if d['world'] == world and d['ep'] == '消失'}
            other_starts = {d['start'] for d in self.corpus.docs
                            if d['world'] == other and d['ep'] == '消失'}
            # 用会命中《消失》切片附近的查询，才能覆盖到跨界窗口
            for query in ('不可能有那回事', '这样的选手变更是谁安排的',
                          '你为什么戴眼镜', '信息统合思念体'):
                hits = self.corpus.search(world, query, k=6)
                block = pb.render_hits(hits, world)
                if not block:
                    continue
                blocks_checked += 1
                for line in block.splitlines():
                    body = line.strip().lstrip('▶').strip()
                    if not body or body.startswith('['):
                        continue
                    # **按空白取第一个字段**，不要用 `[:8]` 切片：
                    # 「0:38:13  这样的选手…」的前 8 个字符是 `'0:38:13 '`
                    # （带尾随空格），跟 `start` 里的 `'0:38:13'` 不相等，
                    # 于是整个判据恒为真——变异 M52 删掉逐行过滤都没红，
                    # 就是这么来的。切片看着「时间码总是 7 位」，实际会带空格。
                    ts = body.split(None, 1)[0]
                    lines_checked += 1
                    # 这个时间码只属于另一世界 → 不该出现在本世界的块里
                    if ts in other_starts and ts not in mine_starts:
                        self.fail(
                            f'{world} 的注入块含 {other} 的上下文行 {ts}\n'
                            f'查询：{query}\n--- 块 ---\n{block}')
        self.assertGreater(blocks_checked, 0, '没有覆盖到任何注入块，用例失效')
        # 防止「一块都没解析出行」这种静默失效：必须真的检查过上下文行。
        self.assertGreater(lines_checked, 20,
                           f'只检查了 {lines_checked} 行，覆盖不足，用例可能已失效')

    def test_05b_retriever_filters_by_world(self):
        """检索层必须硬过滤，而不是降权。"""
        self._need_corpus()
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
        # 截断说明已从 format_history 移到 build_messages 的 system 里
        # （插进对话流会造出第二条 system，各家实现处理不一致，见 test_09b）。
        # 所以这里改断言两件事，一条都不放宽：
        #   - format_history 不再产生任何 system 条目；
        #   - 省略条数仍算得对（38 条），并真的出现在组装后的 system 里。
        self.assertFalse([m for m in result if m['role'] == 'system'],
                         'format_history 不应再输出 system 条目')
        # 口径是「轮」：38 条被截掉 → 19 轮（一条 user 算一轮开头）
        self.assertEqual(pb.count_omitted(history), 19)
        card = pb.persona_for(server.load_personas(), 'normal')
        msgs = pb.build_messages(card, 'normal', [], history, '测试')
        self.assertIn('19 轮', msgs[0]['content'])
        self.assertEqual([m['role'] for m in msgs].count('system'), 1)

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

        **本用例已按 AUD-14 显式重写（2026-10-23），保留旧断言与改写理由。**

        旧断言（已被替代）：
            sent = [m for m in msgs if m['role'] == 'assistant']
            self.assertEqual([m['content'] for m in sent],
                             [pb.strip_prefix(e['assistant']) for e in p['examples']])

        它锁的是「示例以 assistant 消息的形式发出，且一条不少」。
        **为什么必须改写**：2026-10-23 实测发现，正是「示例铺成消息对」这个
        结构让模型把它们当成真实对话——空历史问「你知道信息统合思念体吗」，
        13～14 / 20 次答「你刚才也问过」。对比实验见 `test_09d` 的说明。
        现在示例折进 system 的【语气示例】段，消息序列里不再有示例消息。

        新断言仍然锁「一条不少」这个**实质**（防止回到 `[:14]` 静默截断），
        但改为在 system 文本里逐条核对，而不是数 assistant 消息的条数。
        """
        cards = server.load_personas()
        for world in ('normal', 'crash'):
            p = pb.persona_for(cards, world)
            msgs = pb.build_messages(p, world, [], [], '测试')
            system = msgs[0]['content']

            # 1) 示例区在 system 里，条数写对
            self.assertIn('【语气示例', system, f'{p["id"]} 的示例段不见了')
            self.assertIn(f'共 {len(p["examples"])} 组', system,
                          f'{p["id"]} 的示例组数与卡里不一致')

            # 2) 每一条都真的在里面（这正是旧断言要防的「静默截断」）
            for i, e in enumerate(p['examples'], 1):
                u = e['user']
                a = pb.strip_prefix(e['assistant'])
                self.assertIn(f'对方：{u}', system,
                              f'{p["id"]} 第 {i} 组的提问没进 prompt')
                self.assertIn(f'你：{a}', system,
                              f'{p["id"]} 第 {i} 组的回答没进 prompt')

            # 3) 消息序列里不能再有示例消息（结构修复本身）
            self.assertNotIn('assistant', [m['role'] for m in msgs],
                             f'{p["id"]} 又把示例铺成消息对了（AUD-14 回退）')

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
                         '对方明确问该怎么办时', '给建议只看一件事'],
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

    def test_12b_examples_respect_advice_boundary(self):
        """示例必须示范「问了才给建议」，而不是「一律给方案」（AUD-04 第二层）。

        为什么单独一条：`test_12` 只认**文案**——把已废止的句子搬走就通过。
        实测两个盲点都能骗过它：

        1. 「我明天要考试，好慌 → 现在看三章」和「……怎么办 → 现在看三章」
           两条并排存在时，旧断言仍全绿（它只查「好慌」这三个字在不在）。
        2. 新增任意一条「用户只说情绪，她直接给方案」的新示例，
           旧断言完全不报（它只黑名单了那一条具体问句）。

        ## 判据的边界（重要，别把它当成语义判定）

        这里判的是**祈使句**：中文里「让对方做某事」最常见的形状是动词开头、
        没有主语。第一批判据只认「把/你可以/先/然后」这几种构式，实测漏得厉害
        （复审指出并复现）：`吃点甜的会好些`、`别再熬夜了`、`放下手机`、
        `面试前把公司查一遍`、`选那个离家近的` 全部漏判。换成动词开头之后
        这五条都抓得住。

        但**它不是语义判断器**。`先别急`、`休息一下吧` 和 `别熬夜了` 形状一样，
        前者是安抚、后者是要求，正则分不出来。所以：

        - 这条断言**只用于防止「示例里出现明确的行动指令」**，是粗筛；
        - 它抓不到的（例如「你会好起来的」这种不含动作的安慰、或名词化建议）
          不在这里兜，靠 `V-CHAR-02` 试聊人工看；
        - 被它误报时，先确认那是真的建议还是安抚。真憋屈就改示例的写法，
          不要为了过测试往正则里堆特例——堆到最后没人能说清它到底在查什么。

        判据刻意做窄：漏判的代价是这条测试不响（有别的层兜），误判的代价
        是逼人改人格卡去迎合一个笨正则。两害相权，选前者。
        """
        # 求助判据：问句式的求助。「帮我看看这个报错」是；「还有三章没看完，怎么办」是；
        # 「我有点难过」「我搞砸了」不是。
        ASK = re.compile(r'怎么办|该不该|要不要|怎么才能|你说我|帮我|能不能帮|给我个建议')

        def is_asking(user_text):
            """求助必须落在用户**最后一句**上。

            只看全文会把「帮你问一下，我搞砸了」这种顺带提到认成求助，
            从而让「没求助却给方案」悄悄漏过去，所以宁可要求问句在末尾。
            """
            tail = [c for c in re.split(r'[，。！？；\n]', user_text) if c.strip()]
            return bool(tail) and bool(ASK.search(tail[-1]))

        # 动作动词表。不求全 —— 漏一个的后果是这条测试不响，不是判错。
        IMPERATIVE = re.compile(
            r'^(别|不要|不用|不必|先|再|赶紧|快|多|少|早点|记得|记住|注意|'
            r'吃|喝|睡|休息|去|来|看|读|写|学|做|买|换|选|放|停|把|用|查|问|'
            r'找|打|给|拿|带|送|告诉|试试|看看|想想|问问|整理|复习|检查|准备|'
            r'参加|申请|保持|坚持|控制|确认|判断)')
        # 句首祈使但其实是安抚/表态的固定说法。宁可少列，只收确定的。
        SOOTHE = re.compile(
            r'^(不用担心|不需要|没有必要|不用谢|不用客气|别在意|做不到|'
            r'知道了|不确定|不知道|没有了|不是)')

        advice_hits = self.advice_hits


        cards = server.load_personas()
        for world in ('normal', 'crash'):
            p = pb.persona_for(cards, world)
            for e in p['examples']:
                if is_asking(e['user']):
                    continue
                with self.subTest(world=world, user=e['user']):
                    self.assertEqual(
                        advice_hits(e['assistant']), [],
                        f'{p["id"]}：用户「{e["user"]}」没有求助，'
                        f'回答却是祈使句「{e["assistant"]}」——示范了无条件给建议')

            # 按卡逐张断言：合并统计会被另一张卡掩盖（变异脚本 M3 复现过）。
            asked = [e for e in p['examples'] if is_asking(e['user'])]
            with self.subTest(world=world, check='ask-example'):
                self.assertTrue(asked, f'{p["id"]}：缺少「用户明确求助」的示例，'
                                       f'模型无从知道被问到时可以给建议')
                self.assertTrue(len(asked) < len(p['examples']),
                                f'{p["id"]}：示例全都带求助，缺少「没求助」的对照')
            # 求助示例本身也要守边界：给一步就停，不是一串建议。
            for e in asked:
                with self.subTest(world=world, asked=e['user']):
                    self.assertLessEqual(
                        len(advice_hits(e['assistant'])), 1,
                        f'{p["id"]}：求助示例「{e["user"]}」给了一串建议，'
                        f'与「给一步能做的就停」冲突')

        # 台词层的硬边界：主线卡里「给建议」的权威表述只能有一处。
        p = pb.persona_for(cards, 'normal')
        system = pb.build_messages(p, 'normal', [], [], '测试')[0]['content']
        self.assertTrue(
            '对方明确问该怎么办时' in system or '问了，给一步能做的就停' in system,
            'AUD-04：缺少「只在被问到时给建议」的权威表述')

    def test_12b2_advice_predicate_catches_known_leaks(self):
        """判据本身要有用例：漏判过的说法必须被抓住。

        复审给出了 5 条能骗过旧判据的示例。如果只把它们写进 docstring，
        下次有人改动正则、又把它们放跑，没人会发现。这里直接钉住。

        **判据只在这一处定义**（`advice_hits` 在本类里作为静态方法），
        测试与用例共享同一份实现——两处各写一份，改了一处另一处会悄悄分叉，
        这正是本文件先前出过的问题（同一段判据出现过三个版本）。
        """
        must_hit = ['吃点甜的会好些。', '别再熬夜了。', '放下手机，闭眼躺着。',
                    '面试前把公司查一遍。', '选那个离家近的。']
        for t in must_hit:
            with self.subTest(text=t):
                self.assertTrue(self.advice_hits(t),
                                f'判据漏判：「{t}」是明确的行动指令')

        # 不该被误判的：安抚、自述、纯动作、陈述。
        must_not = ['不用担心。', '知道了。', '原因。', '（把书合上）', '看书。',
                    '比较喜欢。', '不夸张。你说的时候停了两次，是真的在意。',
                    '不是冷淡。是说不出来。', '……']
        for t in must_not:
            with self.subTest(text=t):
                self.assertFalse(self.advice_hits(t),
                                 f'判据误判：「{t}」不是给建议')

    def test_12e_unsupported_persona_claims_removed(self):
        """PEND-012/013：无来源的设定不能经人格卡或示例重新进入实际 prompt。

        这里锁住已删除的具体主张，不声称能判断任意同义改写或角色自然度。
        同时保留不确定性约束和阅读爱好，避免整段删空也通过。
        只检查没有历史、检索和记忆的默认配置；不禁止真实聊天谈甜食或夸奖。
        """
        cards = server.load_personas()
        normal = pb.persona_for(cards, 'normal')
        crash = pb.persona_for(cards, 'crash')
        for world, persona, removed in (
            ('normal', normal, ('推断常出错', '自我评价偏低',
                                '被夸奖时不接受也不反驳，直接跳过')),
            ('crash', crash, ('喜欢甜食', '忍不住提到吃的', '做过巧克力')),
        ):
            sent = pb.build_messages(persona, world, [], [], '在吗')[0]['content']
            for claim in removed:
                with self.subTest(world=world, removed=claim):
                    self.assertNotIn(claim, sent)
        with self.subTest(check='uncertainty-preserved'):
            self.assertRegex(normal['system_prompt'], r'推测[^\n]*不当成事实')
            self.assertIn('不确定', normal['system_prompt'])
        with self.subTest(check='reading-preserved'):
            self.assertIn('喜欢看书', crash['system_prompt'])
        with self.subTest(check='praise-example-replaced'):
            praise = [e for e in normal['examples'] if e['user'] == '你好厉害']
            self.assertEqual(len(praise), 1)
            self.assertNotEqual(praise[0]['assistant'].strip(), '……',
                                '旧的「被夸直接跳过」示例仍在教模型固定回避')
            self.assertTrue(praise[0]['source'].startswith('handwritten:'),
                            '新示例是项目写法，不应继续冒充人物解读的出处')
            self.assertNotIn('self-deprecating-skip', json.dumps(normal, ensure_ascii=False))

    def test_12f_unverified_self_history_is_not_asserted(self):
        """CHAT-019 / DEC-017：关于她自己的经历，没有依据不许说成事实。

        为什么单独一条：`test_12e` 只管「卡里不能写没有依据的设定」，管不到
        「没写也不能现场编」。36 次采样（docs/reviews/2026-10-24-self-facts-sampling.md）
        里，问「你以前做过巧克力吗」2/3 承认并补细节，问「最近一次做饭」3/3 作答——
        卡里已经没有这些内容，说明缺的是**规则**，不是卡里的文字。

        判据（丙）：禁可核查的具体项，允许不承诺的倾向，想不起就说记不清。
        这里只锁「规则确实进了实际组装结果」和「没有退化成关键词禁令」；
        不声称能判断任意改写，也不判断她说话自不自然（那是 C 类证据）。
        """
        cards = server.load_personas()
        fake_hit = {'ep': '消失', 'ep_title': '消失', 'start': '0:38:13',
                    'world': 'normal', 'ctx': '▶测试句', 'ctx_worlds': ['normal']}
        for world in ('normal', 'crash'):
            persona = pb.persona_for(cards, world)
            hit = dict(fake_hit, world=world, ctx_worlds=[world])
            sent = pb.build_messages(persona, world, [hit], [], '在吗')[0]['content']
            with self.subTest(world=world, check='self-history-rule'):
                self.assertIn('关于你自己的经历', sent)
            with self.subTest(world=world, check='specifics-banned'):
                # 四个词必须一起出现，避免只留「不要编」这种没有边界的写法。
                self.assertIn('时间、地点、次数、具体情节', sent)
            with self.subTest(world=world, check='vague-allowed'):
                self.assertIn('不承诺的倾向', sent)
            with self.subTest(world=world, check='recall-answer'):
                self.assertIn('记不清', sent)
            with self.subTest(world=world, check='hits-are-not-evidence'):
                # 检索片段没讲到这件事就等于没有依据。这两句要分开断言：
                # 「等于没有依据」在自述规则末尾，「拼一个答案」在检索块里，
                # 只锁其中一句，删掉另一句时测试仍然全绿（M65 一开始就是这么漏的）。
                # 采样里巧克力/做饭两组的 k=5 命中全是噪声，她照样答了。
                self.assertIn('等于没有依据', sent)
                self.assertIn('不要拿它们拼一个答案', sent)
            with self.subTest(world=world, check='grounded-history-still-allowed'):
                # 只有禁令会过度抑制：第一版规则上线后，检索里**确实有**的借书卡
                # 往事也被推成「想不起来了」。所以要同时锁「有依据的照常说」。
                self.assertIn('有依据的往事照常讲', sent)
            with self.subTest(world=world, check='present-tense-not-restricted'):
                # 「选哪本书」「怕什么」不是往事，不该被这条规则压成「记不清」。
                self.assertIn('这条只管', sent)
        with self.subTest(check='persona-stance'):
            self.assertIn('包括关于你自己以前的事', cards['yuki']['system_prompt'])
            self.assertIn('只说你确实记得的', cards['yuki_school']['system_prompt'])
        with self.subTest(check='no-keyword-ban'):
            # DEC-017 明确不新增「甜食」「巧克力」这类词禁：正常聊天会谈到吃的，
            # 拦词会把「我不太喜欢甜的」这种合法回答也标成违例。
            guard = cards['yuki_school'].get('guard') or {}
            for reply, user in (
                ('甜的东西偶尔会吃。', '你平时是不是特别喜欢甜食？'),
                ('我不记得做过巧克力。', '你以前自己做过巧克力吗？'),
            ):
                self.assertEqual(
                    check_persona.simulate_check_reply(reply, user, guard), '',
                    f'正常回答被误判为违例：「{reply}」')

    def test_12d_school_card_identity(self):
        """崩溃世界卡的 AUD-13：不得把「任务助手」写进身份，也不得假定用户性别。

        为什么单独一条：`test_12b` 只查主线卡，崩溃卡一直是**零覆盖**。
        变异脚本（`scripts/mutation_check.py` 的 M7）把「帮用户处理他提出的问题」
        加回崩溃卡之后，全部回归仍然全绿 —— 这就是零覆盖的证据。

        要求：废止表述不在（助手定位 + 「他」），改写表述在（持续通讯），
        且「没有依据的强制反应」不能再写成规定（AUD-07 同类问题）。
        """
        cards = server.load_personas()
        p = pb.persona_for(cards, 'crash')
        self.assertEqual(p['world'], 'crash')
        system = pb.build_messages(p, 'crash', [], [], '测试')[0]['content']

        for gone in ('帮用户处理', '你在帮用户', '他提出的问题'):
            self.assertNotIn(gone, system,
                             f'AUD-13：崩溃卡仍含任务助手定位「{gone}」')
        for kept in ('持续的通讯', '你不太有自信'):
            self.assertIn(kept, system,
                          f'AUD-13：崩溃卡缺少改写后的「{kept}」——删掉不等于改写')

        # AUD-07：强制反应模板。这些是「每次都会」的规定，不是情境允许的反应。
        for gone in ('被人夸奖会立刻否认', '会道歉，即使自己没错'):
            self.assertNotIn(gone, system,
                             f'AUD-07：崩溃卡仍规定强制反应「{gone}」')

        # 世界隔离的台词层：崩溃卡不得**声称**自己是主线那边的身份。
        # 注意不能用「人形终端界面」这个词本身做判据 —— WORLD_PROMPT["crash"]
        # 里有一句「你不再是人形终端界面」，那是对身份的否定，正是要保留的。
        # 所以只禁「肯定式」的说法。
        for gone in ('你是信息统合思念体', '你的任务是观测', '信息统合思念体派往',
                     '你是人形终端'):
            self.assertNotIn(gone, system,
                             f'WORLD-001：崩溃卡出现主线身份的肯定式表述「{gone}」')
        self.assertIn('你不再是人形终端界面', system,
                      'WORLD-001：崩溃世界必须明确否定主线身份，删掉这句就失去了隔离')

    def test_09b_single_system_message(self):
        """消息序列形状：单条 system + 严格交替 + 以 user 收尾。

        **本用例已按 AUD-14 显式重写（2026-10-23），保留旧断言与改写理由。**

        旧断言（已被替代）里有两条现在不成立：
            self.assertEqual(msgs[1]['content'], p['examples'][0]['user'])
            self.assertEqual(msgs[-2]['content'].strip(),
                             pb.strip_prefix(p['examples'][-1]['assistant']))
        它们断言「system 之后第一条是示例首句」「末条之前是示例最后一条回复」——
        即**示例以消息对的形式出现在序列里**。

        **为什么必须改写**：2026-10-23 实测，正是这个结构让模型把示例当成
        真实对话（空历史答「你刚才也问过」，13～14 / 20 次）。示例改成折进
        system 之后实测降到 0/10，见 `test_09d`。所以「示例必须是消息对」
        这条旧契约本身就是要移除的缺陷，不是被弄坏的实现。

        仍然保留、且**一条都不放宽**的实质约束：
          - 有且只有一条 system，且在第 0 位（非首条 system 各家处理不一致）；
          - 最后一条必须是 user；
          - 除首条 system 外严格 user/assistant 交替、以 user 起；
          - 正文出现在最后一条 user 里；
          - 各种畸形 history（空白、tool、None、system）都不能弄坏序列。

        原来那套「切示例段 / 切历史段」的复杂切法也随之简化：
        示例既然不在序列里，就不需要再切它们了——但历史与正文的合并规则
        仍然用同样的形状断言覆盖，这部分是它的核心价值，没有减少。
        """
        cards = server.load_personas()
        bad_histories = [
            [],
            [{'role': 'user', 'content': '在吗'}, {'role': 'assistant', 'content': '在。'}],
            # 空白 content：会被过滤，实际保留 1 条而 len() 是 2
            [{'role': 'user', 'content': 'x'}, {'role': 'assistant', 'content': '   '}],
            # 夹一条 tool：过滤后会留下相邻两条 user，必须被合并
            [{'role': 'user', 'content': 'u0'}, {'role': 'assistant', 'content': 'a0'},
             {'role': 'user', 'content': 'u1'}, {'role': 'tool', 'content': 't'},
             {'role': 'user', 'content': 'u2'}, {'role': 'assistant', 'content': 'a2'}],
            [{'role': 'system', 'content': '不该进来'}],
            [None, {'sys': True}, {'role': 'user', 'content': '仍然算一条'}],
        ]
        for world in ('normal', 'crash'):
            p = pb.persona_for(cards, world)
            for i, history in enumerate(bad_histories):
                msgs = pb.build_messages(p, world, [], history, '测试')
                roles = [m['role'] for m in msgs]
                with self.subTest(world=world, history=i):
                    self.assertEqual(roles.count('system'), 1,
                                     f'{p["id"]}：出现 {roles.count("system")} 条 system，'
                                     f'非首条 system 在各家实现里处理不一致')
                    self.assertEqual(roles[0], 'system')
                    self.assertEqual(roles[-1], 'user',
                                     f'{p["id"]}：消息以 {roles[-1]} 收尾')

                    # 除首条 system 外，整体必须严格交替的 user/assistant，
                    # 且以 user 起、以 user 止（正文总是 user）。若出现相邻同角色
                    # 或 assistant 起头，说明合并或截断把序列弄坏了。
                    body = roles[1:]
                    self.assertEqual(body, ['user', 'assistant'] * (len(body) // 2)
                                     + (['user'] if len(body) % 2 else []),
                                     f'{p["id"]}：出现相邻同角色或角色错位：{body}')
                    self.assertIn('测试', msgs[-1]['content'],
                                  f'{p["id"]}：正文没有出现在最后一条 user 里')

                    # 示例必须在 system 里（不在序列里，但不能凭空消失）
                    self.assertIn('【语气示例', msgs[0]['content'],
                                  f'{p["id"]}：示例段不在 system 里')
                    self.assertEqual(
                        msgs[0]['content'].count('对方：'), len(p['examples']),
                        f'{p["id"]}：system 里的示例条数与卡不一致')

    def test_09d_examples_are_not_message_pairs(self):
        """示例必须折进 system，**不能**铺成消息对（AUD-14）。

        这是 2026-10-23 用真实模型查出来的，证据在这条用例里写清楚：

        现象：空历史问「你知道信息统合思念体吗」，她答「你刚才也问过」——
              凭空造出一段不存在的对话。同理，「你还记得我上次说的那件事吗」
              会答「记得。面试那件。」

        对比实验（dfmodel，同一句话，温度 0.3，每档 10～20 次）：

            | 做法                             | 出现「刚才问过」|
            |----------------------------------|----------------|
            | 示例铺成 user/assistant 消息对    | 13～14 / 20    |
            | 只改 system 里那句说明的措辞      | 14 / 20        |
            | 给示例消息加 `name: example`      | 7 / 10         |
            | 示例折叠进 system                 | 0 / 10         |
            | 完全不给示例（对照）              | 0 / 10         |

        结论：这不是措辞问题，是**结构问题**。只要示例和真实对话在消息序列里
        长得一样，模型就分不开；在 system 里加多少句「这不是真的事」都压不住。

        所以这条用例锁的是结构：**序列里不许出现 assistant 消息**。
        它挡的是「有人为了恢复 few-shot 形状又把示例铺回去」——
        那样做会让缺陷原样回来，而且在工程测试里完全看不出来。
        """
        cards = server.load_personas()
        for world in ('normal', 'crash'):
            p = pb.persona_for(cards, world)
            self.assertTrue(p['examples'], f'{p["id"]} 没有示例，用例失去意义')
            for history in ([], [{'role': 'user', 'content': '在吗'},
                                 {'role': 'assistant', 'content': '在。'}]):
                with self.subTest(world=world, hist=len(history)):
                    msgs = pb.build_messages(p, world, [], history, '测试')
                    roles = [m['role'] for m in msgs]
                    # 只允许真实历史带来的 assistant；没有历史时一条都不许有。
                    expected = sum(1 for m in history if m['role'] == 'assistant')
                    self.assertEqual(
                        roles.count('assistant'), expected,
                        f'{p["id"]}：序列里有 {roles.count("assistant")} 条 assistant，'
                        f'期望 {expected} 条。示例可能又被铺成消息对了（AUD-14 回退）')
                    # 示例内容必须在 system 里，不能靠「删掉示例」来通过
                    self.assertIn(p['examples'][0]['user'], msgs[0]['content'],
                                  f'{p["id"]}：示例既不在序列里也不在 system 里')

    def test_09c_history_merges_adjacent_same_role(self):
        """过滤掉中间条目后留下的相邻同角色必须合并。

        复审给出的反例：`[user, assistant, user, tool, user, assistant]`
        过滤 tool 后得到 `[..., user, user, ...]`。部分上游对这个序列直接 400，
        另一些会把它读成同一句话的两段。合并比丢弃安全——丢弃会少内容。
        """
        h = [{'role': 'user', 'content': 'u0'}, {'role': 'assistant', 'content': 'a0'},
             {'role': 'user', 'content': 'u1'}, {'role': 'tool', 'content': 't'},
             {'role': 'user', 'content': 'u2'}, {'role': 'assistant', 'content': 'a2'}]
        out = pb.format_history(h)
        self.assertEqual([m['role'] for m in out],
                         ['user', 'assistant', 'user', 'assistant'])
        # 两段都留着，不能丢内容
        self.assertIn('u1', out[2]['content'])
        self.assertIn('u2', out[2]['content'])

    def test_09d_count_omitted_reports_turns(self):
        """截断说明报的是**轮数**，不是条数。

        口径写错会让提示差一倍：61 条消息 = 30 轮，实际截掉 19 轮。
        改前写「还有 38 条」——模型可能读成 38 轮，也可能读成 38 句。
        """
        h = [{'role': 'user', 'content': f'q{i}'} if i % 2 == 0 else
             {'role': 'assistant', 'content': f'a{i}'} for i in range(61)]
        self.assertEqual(pb.count_omitted(h), 19)
        # 没截断时为 0
        self.assertEqual(pb.count_omitted(h[:10]), 0)
        # 提示语用的是「轮」
        card = pb.persona_for(server.load_personas(), 'normal')
        system = pb.build_messages(card, 'normal', [], h, '测试')[0]['content']
        self.assertIn('19 轮', system)
        self.assertNotIn('条更早', system)

    def test_12c_action_brackets_enabled(self):
        """CHAT-007：prompt 必须允许括号动作，且说明「可以完全不带动作」。

        旧行为是 `TERMINAL_PROMPT` 写死「不要描述动作、表情」，与规则相反。
        这里锁三件事：
        1. 禁止句不在，允许句在 —— 只查前者会被「整段删掉」绕过；
        2. 两种正常形态都要说明（只写动作 / 完全没有动作），
           否则模型会把「允许动作」读成「必须带动作」，又把自然表达压成模板；
        3. 两张卡都要有，不能只改主线。
        """
        cards = server.load_personas()
        for world in ('normal', 'crash'):
            p = pb.persona_for(cards, world)
            system = pb.build_messages(p, world, [], [], '测试')[0]['content']
            with self.subTest(world=world):
                self.assertNotIn('不要描述动作', system,
                                 f'{p["id"]}：prompt 仍禁止动作描写，与 CHAT-007 相反')
                self.assertIn('（）', system, f'{p["id"]}：prompt 没有说明括号用法')
                # 「只有动作」与「没有动作」两种都要出现，缺哪一边都会让模型走极端。
                self.assertTrue('只有' in system and '动作' in system,
                                f'{p["id"]}：没有说明「可以整条只有动作」')
                self.assertRegex(system, r'(完全没有动作|不需要动作|不带动作|没有动作)',
                                 f'{p["id"]}：没有说明「也可以完全没有动作」')

    # ---------------------------------------------------------------
    # 长期记忆（CHAT-016 / CHAT-018，验收 V-MEM-01～05）
    #
    # 这些用例**不碰真实 data/memory**：`server.MEMORY_DIR` 在用例里被指到临时
    # 目录。忘了指的话测试会往用户真实记忆里写东西，而且彼此干扰。
    # ---------------------------------------------------------------
    def memory_dir(self):
        """把记忆目录指到临时目录，返回 patch 上下文。"""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return patch.object(server, 'MEMORY_DIR', Path(temp.name)), Path(temp.name)

    def test_13_memory_roundtrip_and_world_isolation(self):
        """V-MEM-01/02：增删改查能走通，且两个世界互不可见。"""
        ctx, root = self.memory_dir()
        with ctx:
            status, raw = self.request('/api/memory', {
                'world': 'normal', 'op': 'add', 'text': '你上周说在赶一个项目'})
            self.assertEqual(status, 200, raw)
            normal_id = json.loads(raw)['record']['id']

            status, raw = self.request('/api/memory', {
                'world': 'crash', 'op': 'add', 'text': '你在天台说过怕考试'})
            self.assertEqual(status, 200, raw)

            normal = json.loads(self.request('/api/memory?world=normal')[1])
            crash = json.loads(self.request('/api/memory?world=crash')[1])
            self.assertEqual([r['text'] for r in normal['records']], ['你上周说在赶一个项目'])
            self.assertEqual([r['text'] for r in crash['records']], ['你在天台说过怕考试'])
            # 落盘也必须是分开的两个文件，不是同一个文件里按字段过滤。
            self.assertTrue((root / 'memory-normal.json').exists())
            self.assertTrue((root / 'memory-crash.json').exists())

            # 跨世界改：必须 400，而且原记录不能被动到。
            status, raw = self.request('/api/memory', {
                'world': 'crash', 'op': 'update', 'id': normal_id, 'text': '被改了'})
            self.assertEqual(status, 400, raw)
            again = json.loads(self.request('/api/memory?world=normal')[1])
            self.assertEqual(again['records'][0]['text'], '你上周说在赶一个项目')

    def test_13a_memory_store_filters_by_world_at_storage_layer(self):
        """隔离必须在**存储层**成立，而不只是接口层。

        M12 变异（去掉 `search()` 里的世界过滤）第一版**没被拦住**。原因是
        测试只走 HTTP，而接口层按世界分文件——每条记录本身都带着自己的 world，
        过滤掉之后也查不出差别。要拦住它，必须造出「`self.world` 与记录里的
        world 不一致」的局面，也就是：

          - 同一个根目录、同一个 `self.world`，但另一个世界也往同名文件里写过；
          - 记录里的 `world` 字段缺失或被写坏。

        这两条都是「真出问题时」会出现的形状：目录被拷错、文件被外部脚本改过。
        所以这里直接对 MemoryStore 断言，绕过 HTTP。
        """
        import memory as memory_mod
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        normal = memory_mod.MemoryStore(root, 'normal')
        crash = memory_mod.MemoryStore(root, 'crash')
        normal.add('你不吃香菜', subject='user')
        crash.add('你不吃香菜', subject='user')

        # 正常路径：一个词两边都有，各搜自己那份。
        self.assertEqual([r['world'] for r in normal.search('不吃香菜')], ['normal'])
        self.assertEqual([r['world'] for r in crash.search('不吃香菜')], ['crash'])

        # 异常路径 1：同名文件里混进了另一世界的记录（目录拷错会这样）。
        path = root / 'memory-normal.json'
        doc = json.loads(path.read_text(encoding='utf-8'))
        doc['records'].append({
            'id': 'm-foreign-1', 'world': 'crash', 'text': '你不吃香菜，天台那次说过',
            'subject': 'user', 'source': {'kind': 'user-stated'},
            'created': '2026-10-21T09:00:00', 'revision': 1, 'revises': None,
            'status': 'active', 'tags': []})
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')
        got = normal.search('不吃香菜')
        self.assertEqual([r['world'] for r in got], ['normal'] * len(got), got)
        self.assertNotIn('m-foreign-1', [r['id'] for r in got])
        for r in got:
            self.assertEqual(r['world'], 'normal', f'混进了另一世界的记录：{r["id"]}')

        # 异常路径 2：记录缺 world 字段（外部脚本或老版本写的）。
        doc['records'].append({
            'id': 'm-noworld', 'world': 'crash', 'text': '你不吃香菜（缺字段版）',
            'subject': 'user', 'source': {'kind': 'user-stated'},
            'created': '2026-10-22T09:00:00', 'revision': 1, 'status': 'active'})
        del doc['records'][-1]['world']
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')
        got = normal.search('不吃香菜')
        self.assertEqual([r['world'] for r in got], ['normal'] * len(got), got)
        for r in got:
            self.assertEqual(r['world'], 'normal', f'缺字段的记录被当成本世界的：{r["id"]}')

        # read() 的弱召回兜底同理：不命中的句子走「补最近几条」，
        # 那条路同样只能捡本世界的记录。
        weak = normal.read('完全无关的话')
        self.assertTrue(weak, '弱召回应当补出本世界的记录')
        for r in weak:
            self.assertEqual(r['world'], 'normal', f'弱召回越界：{r["id"]}')

        # 世界名必须是枚举里的，别的新建 store 直接报错，而不是默默当 normal。
        with self.assertRaises(memory_mod.MemoryError_):
            memory_mod.MemoryStore(root, 'Normal')

    def test_13b_memory_world_is_required(self):
        """世界名缺省或拼错都要报错，不能静默落到 normal。

        静默回退就是 WORLD-001 的漏洞：崩溃世界里的对话会被记成主线的经历。
        """
        ctx, _ = self.memory_dir()
        with ctx:
            for body in ({'op': 'add', 'text': 'x'},
                         {'world': '', 'op': 'add', 'text': 'x'},
                         {'world': 'Normal', 'op': 'add', 'text': 'x'}):
                with self.subTest(body=body):
                    status, raw = self.request('/api/memory', body)
                    self.assertEqual(status, 400, raw)
            status, raw = self.request('/api/memory?world=neither')
            self.assertEqual(status, 400, raw)

    def test_14_memory_reaches_prompt_with_separate_source(self):
        """V-MEM-02 的可自动验证部分：记忆进了 prompt，且与原作片段来源分开。"""
        ctx, _ = self.memory_dir()
        with ctx:
            self.request('/api/memory', {'world': 'normal', 'op': 'add',
                                         'text': '你说过你在学做饭'})
            self.request('/api/memory', {'world': 'crash', 'op': 'add',
                                         'text': '天台那只猫是你先发现的'})
            status, raw = self.request('/api/preview', {'world': 'normal',
                                                        'message': '我今天学做饭了'})
            self.assertEqual(status, 200, raw)
            doc = json.loads(raw)
            system = doc['system_prompt']
            self.assertIn('你说过你在学做饭', system)
            self.assertIn('=== 共同经历 ===', system)
            # 另一世界的记录一个字都不能出现在主线的 system 里。
            self.assertNotIn('天台那只猫', system)
            self.assertEqual(doc['memory_count'], 1)

    def test_14b_memory_correction_retires_old_version(self):
        """V-MEM-03：纠正之后，旧说法不再进 prompt；恢复旧版则新版退下。"""
        ctx, _ = self.memory_dir()
        with ctx:
            first = json.loads(self.request('/api/memory', {
                'world': 'normal', 'op': 'add', 'text': '你在准备考研'})[1])['record']
            second = json.loads(self.request('/api/memory', {
                'world': 'normal', 'op': 'add', 'text': '你改主意了，不考研了',
                'revises': first['id']})[1])['record']

            def system_for(msg):
                return json.loads(self.request('/api/preview', {
                    'world': 'normal', 'message': msg})[1])['system_prompt']

            self.assertNotIn('你在准备考研', system_for('考研的事怎么样了'))
            self.assertIn('你改主意了，不考研了', system_for('考研的事怎么样了'))

            # 恢复旧版：新版必须同时退下，不能两个互斥版本同时生效。
            status, raw = self.request('/api/memory', {
                'world': 'normal', 'op': 'restore', 'id': first['id']})
            self.assertEqual(status, 200, raw)
            self.assertEqual(json.loads(raw)['record']['status'], 'active')
            view = json.loads(self.request('/api/memory?world=normal')[1])
            active = [r['text'] for r in view['records'] if r['status'] == 'active']
            self.assertEqual(active, ['你在准备考研'])
            self.assertNotIn('你改主意了', system_for('考研的事怎么样了'))
            self.assertEqual(second['status'], 'active')  # 刚建时是 active

    def test_14e_restore_keeps_one_active_version_in_chain(self):
        """恢复必须让**整条修订链**只剩一个 active（R-03，V-MEM-03）。

        `test_14b` 只覆盖了「恢复直接后继」这一种形状，所以一直是绿的。
        实测能复现两个反例（见 reviews/2026-10-23-zip-merge-review.md R-03）：

          1. 三代链 A→B→C，恢复 A：原实现只把 B 退下，C 仍是 active，
             于是 active 里同时有 A 和 C —— 两个互斥版本都进了 prompt。
          2. A→B 往返恢复：恢复 A 时把 A 的 `retired_by` 抹掉了，
             再恢复 B 也抹掉 B 的，最后 A、B 一起 active。

        判据不看实现细节，只看「同一条链上有几个 active」：
        **任一时刻，同一条修订链必须恰好只有一个 active**。
        """
        ctx, _ = self.memory_dir()
        with ctx:
            def post(body):
                status, raw = self.request('/api/memory', body)
                self.assertEqual(status, 200, raw)
                return json.loads(raw)['record']

            def active_texts():
                view = json.loads(self.request('/api/memory?world=normal')[1])
                return [r['text'] for r in view['records'] if r['status'] == 'active']

            def chain_of(records):
                """按 revises 把记录分成链。返回 id -> 链根 id。"""
                parent = {r['id']: r.get('revises') for r in records}

                def root(rid):
                    seen = set()
                    cur = rid
                    while parent.get(cur) and cur not in seen:
                        seen.add(cur)
                        cur = parent[cur]
                    return cur
                return {r['id']: root(r['id']) for r in records}

            # --- 情形 1：三代链，恢复最早的一代 ---
            a = post({'world': 'normal', 'op': 'add', 'text': '我不吃辣'})
            b = post({'world': 'normal', 'op': 'add', 'text': '我能吃一点辣',
                      'revises': a['id']})
            c = post({'world': 'normal', 'op': 'add', 'text': '我能吃重辣',
                      'revises': b['id']})
            self.assertEqual(active_texts(), ['我能吃重辣'], '三代链建好后只该有一条 active')

            post({'world': 'normal', 'op': 'restore', 'id': a['id']})
            records = json.loads(self.request('/api/memory?world=normal')[1])['records']
            roots = chain_of(records)
            by_root = {}
            for r in records:
                if r['status'] == 'active':
                    by_root.setdefault(roots[r['id']], []).append(r['text'])
            dup = {k: v for k, v in by_root.items() if len(v) > 1}
            self.assertFalse(
                dup, f'恢复最早一代后，同一条链上出现多个 active：{dup}')
            self.assertEqual(active_texts(), ['我不吃辣'],
                             '恢复 A 之后只剩 A 生效，C 必须一起退下')

            # --- 情形 2：往返恢复 ---
            d = post({'world': 'normal', 'op': 'add', 'text': '我养了一只猫'})
            e = post({'world': 'normal', 'op': 'add', 'text': '我不养猫了',
                      'revises': d['id']})
            post({'world': 'normal', 'op': 'restore', 'id': d['id']})
            self.assertIn('我养了一只猫', active_texts())
            self.assertNotIn('我不养猫了', active_texts())
            post({'world': 'normal', 'op': 'restore', 'id': e['id']})
            self.assertIn('我不养猫了', active_texts(),
                          '恢复 B 之后 B 生效')
            self.assertNotIn('我养了一只猫', active_texts(),
                             '往返恢复后 A 必须退下，不能两条同时 active')

            # --- 情形 3：prompt 里也不能同时出现两个互斥版本 ---
            sp = json.loads(self.request('/api/preview', {
                'world': 'normal', 'message': '你还吃辣吗，猫呢'})[1])['system_prompt']
            self.assertIn('我不吃辣', sp)
            self.assertNotIn('我能吃重辣', sp,
                             '恢复最早一代后，第三代不该还在 prompt 里')
            self.assertNotIn('我能吃一点辣', sp)

    def test_16b_memory_concurrent_writes_do_not_lose_records(self):
        """并发写不能静默丢记录（R-04，CHAT-016-A）。

        `test_16` 只测 `os.replace` 失败，那是**半文件**问题。这里测的是另一件：
        两个 MemoryStore 指向同一个文件、同时「读—改—写」，后写的那次会把
        前一次的修改整个覆盖掉——两个请求都返回成功，`warnings` 都是空，
        但最终文件里只剩一条。

        为什么 Windows 上一定会发生：`server.py` 每次请求都新建一个
        `MemoryStore`，而锁是**实例字段**；跨进程那层用的是 `fcntl`，
        Windows 根本没有。于是两个实例的锁互不相干。

        判据不看用了什么锁，只看结果：N 条并发写入，最后必须 N 条都在。
        「都报成功」和「文件里都有」两者必须一致，否则就是**静默**丢数据。
        """
        ctx, root = self.memory_dir()
        with ctx:
            import memory as memory_mod
            from pathlib import Path

            n = 6
            stores = [memory_mod.MemoryStore(root, world='normal') for _ in range(n)]
            barrier = threading.Barrier(n)
            errors = []
            results = []

            # 让所有实例在「读完、还没写」的时刻同步，把竞态放大到必然发生。
            original_load = memory_mod.MemoryStore._load_raw

            def synced_load(self, *a, **kw):
                data = original_load(self, *a, **kw)
                try:
                    barrier.wait(timeout=10)
                except threading.BrokenBarrierError:
                    pass   # 已经被串行化时屏障会超时，那正是修好之后的正常情况
                return data

            memory_mod.MemoryStore._load_raw = synced_load
            try:
                def work(i):
                    try:
                        rec, warns = stores[i].add(f'并发写入第 {i} 条')
                        results.append((i, rec['id'], list(warns)))
                    except Exception as e:            # noqa: BLE001
                        errors.append((i, repr(e)))

                threads = [threading.Thread(target=work, args=(i,)) for i in range(n)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=30)
            finally:
                memory_mod.MemoryStore._load_raw = original_load

            self.assertFalse(errors, f'并发写入抛错：{errors}')
            self.assertEqual(len(results), n, '每次写入都应返回成功')
            self.assertFalse([r for r in results if r[2]],
                             f'写入返回了警告，说明已经察觉异常却不报错：{results}')

            final = memory_mod.MemoryStore(root, world='normal').load()[0]
            text_in_file = {r['text'] for r in final}
            expected = {f'并发写入第 {i} 条' for i in range(n)}
            missing = expected - text_in_file
            self.assertFalse(
                missing,
                f'{n} 条并发写入全部报成功，但文件里少了 {len(missing)} 条：'
                f'{sorted(missing)}\n实际内容：{sorted(text_in_file)}')

    def test_16c_memory_visible_backup_failure(self):
        """备份失败必须看得见，且不能覆盖掉唯一的好备份（R-06，V-MEM-04）。

        原实现 `except OSError: pass`，注释却写「会在 log 里留痕」。
        实测：让 `shutil.copy2` 抛错之后，新增照常成功、`warnings` 是空的、
        日志里也没有任何记录、`.bak` 根本没生成——用户以为有备份，其实没有。

        这里连**恢复风险**一起锁住：主文件是坏的、`.bak` 是好的时，
        下一次保存不能拿坏的主文件去覆盖那个好备份。否则随后的 replace
        一旦失败，唯一可用的备份也没了。
        """
        ctx, root = self.memory_dir()
        with ctx:
            import memory as memory_mod

            store = memory_mod.MemoryStore(root, world='normal')
            store.add('第一条')
            store.add('第二条')   # 第二次写入才会产生 .bak（第一次时还没有主文件）

            original = memory_mod.shutil.copy2
            try:
                def boom(*a, **kw):
                    raise OSError('模拟磁盘写满')
                memory_mod.shutil.copy2 = boom
                rec, warns = store.add('备份应当失败的那一条')
            finally:
                memory_mod.shutil.copy2 = original

            self.assertEqual(rec['text'], '备份应当失败的那一条')
            self.assertTrue(warns, '备份失败必须有可见警告，不能静默吞掉')
            self.assertTrue(
                any('备份' in w for w in warns),
                f'警告里要说明是备份出了问题：{warns}')
            events = store.log_events(20)
            self.assertTrue(
                any('备份' in str(e) for e in events),
                f'备份失败也要写进操作日志：{events}')

            # --- 恢复风险：坏主文件 + 好备份，下一次保存不能覆盖好备份 ---
            good = store.path.with_suffix('.json.bak')
            self.assertTrue(good.exists(), '正常写入之后应当有 .bak')
            before = good.read_bytes()
            store.path.write_text('{ 这不是合法 JSON', encoding='utf-8')
            # 从坏文件恢复：读的时候会退到 .bak，并把警告带出来
            loaded, load_warns = store.load()
            self.assertTrue(load_warns, '主文件坏了必须给出警告')
            self.assertTrue(loaded, '应当能从 .bak 读回记录')
            # 再写一次：此时主文件是坏的，绝不能拿它覆盖 .bak
            store.add('恢复之后再写一条')
            self.assertEqual(
                good.read_bytes(), before,
                '主文件损坏时不能用它覆盖 .bak —— 那会把唯一可用的备份也毁掉')

    def test_14f_memory_subject_and_source_reach_the_prompt(self):
        """主体与来源必须传到消费端（R-07，CHAT-018）。

        实测过的问题：同一句「不吃香菜」，`subject=user/source=manual`
        与 `subject=yuki/source=summary` 渲染出的共同经历块**逐字相同**，
        只有日期和文本。模型因此分不清这是「对方说过的事」还是
        「她自己做过的事」，也分不清是用户亲口说的还是模型摘要。

        现在还没有自动摘要，所以「摘要污染」尚未发生；但接自动提取
        （PEND-008）之前必须先把这条通路建起来，否则第一版就会混。

        判据：**两种主体渲染出的块必须不同**，且块里能看出是谁的事、
        来源是什么。只要求「可区分」，不规定具体措辞。
        """
        def block_for(records):
            return pb.render_shared_memory(records, 'normal')

        user_manual = [{
            'id': 'm-u', 'world': 'normal', 'text': '不吃香菜',
            'subject': 'user', 'status': 'active', 'created': '2026-10-20T10:00:00',
            'source': {'kind': 'user-stated'}}]
        yuki_summary = [{
            'id': 'm-y', 'world': 'normal', 'text': '不吃香菜',
            'subject': 'yuki', 'status': 'active', 'created': '2026-10-20T10:00:00',
            'source': {'kind': 'summary'}}]

        a = block_for(user_manual)
        b = block_for(yuki_summary)
        self.assertTrue(a and b, '两块都不该为空')
        self.assertNotEqual(
            a, b,
            f'主体与来源不同，渲染出的块却一模一样：\n{a}')

        # 主体要能看出来：用户的事 / 她的事，措辞不同
        self.assertIn('对方', a, f'用户的事应当标成对方说的：{a}')
        self.assertIn('你', b, f'她自己的事应当用她的视角写：{b}')

        # 来源要能看出来，且不能把摘要当成亲口说过
        self.assertNotEqual(a, block_for([{**user_manual[0],
                                          'source': {'kind': 'summary'}}]),
                            '来源不同，渲染必须不同')

    def test_14g_memory_subject_and_source_reach_the_ui(self):
        """界面拿到的记录要带主体与来源（R-07）。"""
        ctx, _ = self.memory_dir()
        with ctx:
            self.request('/api/memory', {
                'world': 'normal', 'op': 'add', 'text': '怕黑',
                'subject': 'yuki'})
            view = json.loads(self.request('/api/memory?world=normal')[1])
            rec = view['records'][0]
            self.assertEqual(rec['subject'], 'yuki',
                             '写法要能指定主体，不能一律记成用户的')
            self.assertIn('source', rec, '记录要带来源，界面才能显示')
            self.assertIsInstance(rec['source'], dict)

    def test_14c_memory_log_is_traceable(self):
        """V-MEM-05：改了什么要查得到，日志里带旧文本。"""
        ctx, _ = self.memory_dir()
        with ctx:
            rec = json.loads(self.request('/api/memory', {
                'world': 'normal', 'op': 'add', 'text': '你在学吉他'})[1])['record']
            self.request('/api/memory', {'world': 'normal', 'op': 'update',
                                         'id': rec['id'], 'text': '你在学贝斯'})
            self.request('/api/memory', {'world': 'normal', 'op': 'delete',
                                         'id': rec['id']})
            events = json.loads(self.request('/api/memory?world=normal')[1])['events']
            self.assertEqual([e['op'] for e in events], ['add', 'update', 'delete'])
            self.assertEqual(events[1]['from'], '你在学吉他')
            self.assertEqual(events[1]['to'], '你在学贝斯')
            self.assertTrue(all(e['world'] == 'normal' for e in events))

    def test_14d_deleted_memory_leaves_prompt(self):
        """删掉之后不能再出现在 prompt 里——进回收站不是只藏起来。"""
        ctx, _ = self.memory_dir()
        with ctx:
            rec = json.loads(self.request('/api/memory', {
                'world': 'normal', 'op': 'add', 'text': '你不吃香菜'})[1])['record']
            self.request('/api/memory', {'world': 'normal', 'op': 'delete',
                                         'id': rec['id']})
            system = json.loads(self.request('/api/preview', {
                'world': 'normal', 'message': '晚饭吃香菜吗'})[1])['system_prompt']
            self.assertNotIn('你不吃香菜', system)
            # 回收站里还在，界面能恢复。
            view = json.loads(self.request('/api/memory?world=normal')[1])
            self.assertEqual(view['records'][0]['status'], 'retired')

    def test_15_broken_memory_does_not_break_chat(self):
        """V-MEM-04：记忆文件坏掉时，对话照常进行，错误写进界面而不是吞掉。"""
        ctx, root = self.memory_dir()
        with ctx:
            self.request('/api/memory', {'world': 'normal', 'op': 'add', 'text': '旧事'})
            (root / 'memory-normal.json').write_text('{不是 JSON', encoding='utf-8')
            (root / 'memory-normal.json.bak').unlink(missing_ok=True)

            status, raw = self.request('/api/preview', {'world': 'normal',
                                                        'message': '还记得吗'})
            self.assertEqual(status, 200, raw)          # 聊天不受影响
            self.assertEqual(json.loads(raw)['memory_count'], 0)
            # 读接口要明确报错，不能显示成「没有任何记忆」。
            status, raw = self.request('/api/memory?world=normal')
            self.assertEqual(status, 500, raw)
            self.assertIn('损坏', json.loads(raw)['error'])

    def test_15b_memory_backup_recovers(self):
        """.bak 可用时从备份恢复，并把「可能丢了最后一次修改」说出来。"""
        ctx, root = self.memory_dir()
        with ctx:
            self.request('/api/memory', {'world': 'normal', 'op': 'add', 'text': '第一句'})
            self.request('/api/memory', {'world': 'normal', 'op': 'add', 'text': '第二句'})
            (root / 'memory-normal.json').write_text('{坏的', encoding='utf-8')
            status, raw = self.request('/api/memory?world=normal')
            self.assertEqual(status, 200, raw)
            doc = json.loads(raw)
            self.assertEqual([r['text'] for r in doc['records']], ['第一句'])  # 备份是上一次
            self.assertTrue(any('备份' in w for w in doc['warnings']), doc)

    def test_16_memory_write_is_atomic(self):
        """写入失败不留半个文件：os.replace 之前挂掉，主文件仍是旧的完整内容。"""
        import memory as memory_mod
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = memory_mod.MemoryStore(Path(temp.name), 'normal')
        store.add('原有的那条', subject='user')
        before = (Path(temp.name) / 'memory-normal.json').read_text(encoding='utf-8')
        with patch('memory.os.replace', side_effect=OSError('磁盘满了')):
            with self.assertRaises(OSError):
                store.add('写不进去的那条', subject='user')
        after = (Path(temp.name) / 'memory-normal.json').read_text(encoding='utf-8')
        self.assertEqual(before, after)
        self.assertEqual([r['text'] for r in store.load()[0]], ['原有的那条'])
        # 临时文件也不能留在目录里。
        self.assertEqual([p.name for p in Path(temp.name).glob('.tmp-*')], [])

    # ---------------------------------------------------------------
    # 场景状态（CHAT-013 / CHAT-008，验收 V-CONT-04）
    #
    # 场景状态是**进程内**的，不像记忆那样能指到临时目录。
    # 所以每个用例前后都要 reset，否则前一个用例的眼镜/书状态会漏到下一个。
    # ---------------------------------------------------------------
    def setUp(self):
        server.scene_mod.REGISTRY.reset()

    def tearDown(self):
        server.scene_mod.REGISTRY.reset()

    def turn(self, world, message, history=None):
        """走一次**真实的**一轮：和 `/api/chat` 一样会写状态，但不发到上游、不花钱。

        为什么不直接用 `/api/preview`：预览**不**改真实状态（见 `test_23a`），
        拿它当「发送」用会让这批用例全都测不到东西——改状态那条路根本没走到。
        这个坑是修预览污染之后才暴露的：在此之前预览会顺带改状态，
        用例一直绿着，但测的其实是错的路径。
        """
        # _compose 只用到实例方法 send / json 之类，构造一份不绑 socket 的实例即可。
        handler = server.Handler.__new__(server.Handler)
        out = handler._compose({'world': world, 'message': message,
                                'history': history or []})
        # 与 /api/preview 对齐，省得每条用例都写 messages[0]["content"]。
        out['system_prompt'] = out['messages'][0]['content']
        return out

    def test_17_scene_state_reaches_prompt(self):
        """V-CONT-04：括号动作改变她的状态，并写进下一次请求的 system。"""
        doc = self.turn('normal', '（把书翻开）')
        self.assertIn('=== 此刻 ===', doc['system_prompt'])
        self.assertIn('手里拿着一本打开的书', doc['system_prompt'])
        self.assertIs(doc['scene_state']['book'], True)

    def test_18_scene_state_belongs_to_her_not_user(self):
        """CHAT-013-B：用户说「我摘了眼镜」不改她的状态。

        这是整套状态里最容易错的一条：主语弄反，她会一直按错的设定说话。
        """
        # 崩溃世界的她本来戴着眼镜
        status, raw = self.request('/api/preview', {'world': 'crash', 'message': '我摘了眼镜'})
        self.assertEqual(status, 200, raw)
        doc = json.loads(raw)
        self.assertIs(doc['scene_state']['glasses'], True)
        self.assertIn('戴着眼镜', doc['system_prompt'])

        # 她说「（把眼镜摘下来）」才改
        status, raw = self.request('/api/preview', {
            'world': 'crash', 'message': '（把眼镜摘下来）'})
        doc = json.loads(raw)
        self.assertIs(doc['scene_state']['glasses'], False)
        self.assertIn('没有戴眼镜', doc['system_prompt'])

    def test_19_scene_state_rejects_negation_and_questions(self):
        """CHAT-013-B：否定、假设、疑问都不更新。

        这里用的句子是**能区分有豁免和没豁免**的那些：
        「（别摘眼镜）」这类否定词本身就是动作词，去掉豁免也匹配不上，
        拿它当断言等于什么都没查（M18 第一版就是这么漏过去的）。
        """
        # 崩溃世界的她本来戴着眼镜；书的状态未记录。
        for text in ('（别把书合上）', '（有没有翻开书）', '（是不是合上了）',
                     '（如果合上书）', '（把书合上了吗）', '你戴着眼镜吗'):
            with self.subTest(text=text):
                status, raw = self.request('/api/preview', {'world': 'crash', 'message': text})
                self.assertEqual(status, 200, raw)
                doc = json.loads(raw)
                self.assertIs(doc['scene_state']['glasses'], True,
                              f'「{text}」不该改眼镜状态，实际改动了')
                self.assertIsNone(doc['scene_state'].get('book'),
                                  f'「{text}」不该把书的状态定下来，实际定成了 '
                                  f'{doc["scene_state"].get("book")}')
                self.assertNotIn('手里拿着一本打开的书', doc['system_prompt'])
                self.assertNotIn('书是合上的', doc['system_prompt'])

    def test_20_scene_state_does_not_leak_across_worlds(self):
        """CHAT-013-B：两个世界分别维护，互不继承。

        除了「另一边没被改」，还要查「这一边真的改了」——
        只查前者的话，两边共用一份状态也能混过去（M19 第一版就这么漏的）。
        """
        # 必须用 turn（真实一轮）。预览不写状态，拿它推进状态会一条也改不动。
        self.turn('normal', '（把眼镜摘下来）我在部室门口')
        # 主线这边确认改成功
        self.assertIs(server.scene_mod.REGISTRY.get('normal').state['glasses'], False)
        self.assertIs(server.scene_mod.REGISTRY.get('normal').state['together'], True)

        status, raw = self.request('/api/preview', {'world': 'crash', 'message': '在吗'})
        doc = json.loads(raw)
        self.assertIs(doc['scene_state']['glasses'], True, '崩溃世界继承了主线摘下的眼镜')
        self.assertIs(doc['scene_state']['together'], False, '崩溃世界继承了主线的共同场景')
        self.assertIn('你们不在同一个空间', doc['system_prompt'])
        self.assertIn('戴着眼镜', doc['system_prompt'])
        self.assertNotIn('没有戴眼镜', doc['system_prompt'])

        # 反过来：崩溃世界改，主线不受影响，且崩溃世界也要真的改成 false。
        self.turn('crash', '（把眼镜摘下来）')
        self.assertIs(server.scene_mod.REGISTRY.get('crash').state['glasses'], False)
        normal_sp = self.turn('normal', '嗯')['system_prompt']
        block = normal_sp[normal_sp.index('=== 此刻 ==='):
                          normal_sp.index('=== 状态结束 ===')]
        # 注意别写 assertNotIn('戴着眼镜')：「没有戴眼镜」也含这三个字。
        self.assertIn('- 没有戴眼镜', block)
        self.assertNotIn('- 戴着眼镜', block)

    def test_21_together_scene_needs_explicit_place(self):
        """CHAT-008：「一起看书吧」不算同处一室，明确的地点才算。"""
        status, raw = self.request('/api/preview', {'world': 'normal', 'message': '一起看书吧'})
        self.assertIs(json.loads(raw)['scene_state']['together'], False)

        status, raw = self.request('/api/preview', {'world': 'normal', 'message': '我在活动室了'})
        self.assertIs(json.loads(raw)['scene_state']['together'], True)

    def test_22_scene_failure_does_not_break_chat(self):
        """状态出错按「没有状态」处理：少说一句，好过让对话打不开。"""
        # 让整条状态路径失效（update 与 render 都抛），才能看出「按无状态处理」。
        with patch.object(server.scene_mod.REGISTRY, 'get',
                          side_effect=RuntimeError('坏了')):
            status, raw = self.request('/api/preview', {'world': 'normal', 'message': '在吗'})
        self.assertEqual(status, 200, raw)
        doc = json.loads(raw)
        self.assertNotIn('=== 此刻 ===', doc['system_prompt'])
        self.assertTrue(doc['system_prompt'].strip())
        self.assertEqual(doc['scene_state'], {}, '状态不可用时不该编一个出来')

    def test_23_unrecorded_state_is_not_invented(self):
        """CHAT-013 末条：没有记录就不提，不猜。

        书和眼镜之外的东西不该有输出；书未记录时既不能说「开着」也不能说
        「合着」，更不能用键名占位（「book：没有记录」）——那还是把没记录的
        项摆到了模型面前，模型很容易顺着它编。
        """
        status, raw = self.request('/api/preview', {'world': 'normal', 'message': '在吗'})
        self.assertEqual(status, 200, raw)
        text = json.loads(raw)['system_prompt']
        start = text.index('=== 此刻 ===')
        block = text[start:text.index('=== 状态结束 ===')]
        lines = [ln for ln in block.splitlines() if ln.startswith('- ')]
        self.assertEqual(lines, ['- 没有戴眼镜', '- 你们不在同一个空间，隔着终端说话'],
                         f'此刻块里出现了没有记录的内容：{block!r}')
        for word in ('书', 'book', '没有记录'):
            self.assertNotIn(word, block, f'未记录的项被写出来了：{word}')

    def test_23a_preview_does_not_touch_real_state(self):
        """预览是假设，不是发生：点一下调试面板不该改变她的状态。

        这是复审查出来的真 bug：`/api/preview` 复用 `_compose`，而 `_compose`
        先更新再渲染。于是「在调试面板里输入『（把眼镜摘下来）我到活动室了』，
        看一眼就关掉、什么也没发」，她的眼镜就真的摘了、场景也真的变了。
        预览按钮用户会反复点，这个污染是累积的。

        同时要保证：预览**显示**的必须是试算结果，否则「面板说戴着眼镜、
        注入的 system 说没戴」，两者对不上比不显示更糟。
        """
        server.scene_mod.REGISTRY.reset()
        before = server.scene_mod.REGISTRY.get('crash').known()
        self.assertIs(before['glasses'], True)

        status, raw = self.request('/api/preview', {
            'world': 'crash', 'message': '（把眼镜摘下来）我到活动室了'})
        self.assertEqual(status, 200, raw)
        doc = json.loads(raw)
        # 1) 预览里显示的是「如果现在发」的结果
        self.assertIs(doc['scene_state']['glasses'], False,
                      '预览应当显示试算后的状态')
        self.assertIn('没有戴眼镜', doc['system_prompt'])
        # 2) 真实状态一动没动
        after = server.scene_mod.REGISTRY.get('crash').known()
        self.assertEqual(after, before, '预览改动了真实场景状态')

        # 3) 再预览一次、问一句「在吗」——仍然不该带上次预览的痕迹
        status, raw = self.request('/api/preview', {'world': 'crash', 'message': '在吗'})
        doc = json.loads(raw)
        self.assertIs(doc['scene_state']['glasses'], True, '上一次预览的痕迹漏进来了')
        # 崩溃世界默认戴着眼镜，所以「此刻」块本来就该有——要查的是它的**内容**
        # 没被上一次预览改掉。断言「没有这一块」是错的，那样连默认状态都查不出来。
        sp = doc['system_prompt']
        block = sp[sp.index('=== 此刻 ==='):sp.index('=== 状态结束 ===')]
        self.assertIn('戴着眼镜', block)
        self.assertNotIn('- 没有戴眼镜', block, '上一次预览摘下的眼镜漏进来了')
        # 不能写 assertNotIn('同一个空间')：默认那句「你们**不在**同一个空间」也含它。
        self.assertIn('你们不在同一个空间', block, '上一次预览的共同场景漏进来了')

    def test_23b_scene_snapshot_matches_prompt(self):
        """接口回传的 scene_state 必须和 system 里那一块是同一份。

        两者不一致时，调试面板会骗人：面板说「戴着眼镜」，模型看到的却是
        「没有戴眼镜」。这种情况比不显示更糟——用户会按面板去判断对错。
        """
        status, raw = self.request('/api/preview', {
            'world': 'normal', 'message': '（把书翻开）'})
        doc = json.loads(raw)
        sp = doc['system_prompt']
        block = sp[sp.index('=== 此刻 ==='):sp.index('=== 状态结束 ===')]
        snap = doc['scene_state']
        self.assertIs(snap['book'], True)
        self.assertIn('手里拿着一本打开的书', block)
        self.assertIn('没有戴眼镜', block)   # 主线默认
        self.assertIs(snap['glasses'], False)

    def _run_reply(self, world, reply_text):
        """走 `/api/chat` 本身，只是把上游换成假的（R-05）。

        为什么不能用更轻的写法：最初的版本自己调
        `_commit_reply_scene()`，于是把 `api_chat()` 里那一行接线删掉，
        用例照样绿——测的是「函数会算」，不是「它被调用」。
        这里 patch `server.OPENER`，让 `api_chat()` 完整跑一遍：
        组装 → 连上游 → 逐字节透传 → 收尾更新状态。
        """
        payload = json.dumps(
            {"choices": [{"delta": {"content": reply_text}}]},
            ensure_ascii=True)
        sse = f"data: {payload}\n\ndata: [DONE]\n".encode("ascii")

        class FakeResp:
            def __init__(self, data):
                self.data = [bytes([b]) for b in data]   # 与真实链路一样逐字节

            def read(self, n=1):
                return self.data.pop(0) if self.data else b""

            def close(self):
                pass

        class FakeOpener:
            def open(self, req, timeout=None):
                return FakeResp(sse)

        # 真的发一次 HTTP 请求，只是上游被换掉了。
        with patch.object(server, 'OPENER', FakeOpener()):
            status, raw = self.request('/api/chat', {
                'world': world, 'message': '在吗',
                'api_key': 'test-key-not-real', 'base_url': 'http://fake.invalid/v1',
                'model': 'test'})
        self.assertEqual(status, 200, raw[:300])
        # `raw` 是 SSE 原文（bytes）。上游是按 ensure_ascii 输出的，
        # 中文会变成 \uXXXX，所以断言查的是**解析后的正文**，不是原字节。
        decoded = []
        tmp = []
        for byte in raw:
            server._collect_sse_text(bytes([byte]), decoded, tmp)
        self.assertEqual(''.join(decoded), reply_text,
                         f'回复没有完整透传到客户端：{raw[:200]!r}')
        return ''.join(decoded)

    def test_23c_her_own_reply_updates_scene_state(self):
        """她说的话要能改状态，下一轮必须看见（R-05，V-CONT-04）。

        这是 R-05 里最关键的一条：状态原来只从**用户这句话**更新，
        所以她说「（把眼镜摘下来）」之后，下一轮 prompt 里仍是「戴着眼镜」。

        断言落在**下一轮的 system** 上，而不是「函数被调用过」。
        用例走的是 `api_chat()` 的同一条路径（`_pipe_stream` + `_commit_reply_scene`），
        所以把接线删掉它就会红——这一点实测过。
        """
        world = 'crash'
        server.scene_mod.REGISTRY.reset(world)
        self.assertIs(server.scene_mod.REGISTRY.get(world).state['glasses'], True,
                      '前置：崩溃世界默认戴着眼镜')

        # 1) 上游流式回复里她摘了眼镜，走真实的透传路径
        reply = self._run_reply(world, '（把眼镜摘下来）')
        self.assertIn('把眼镜摘下来', reply, f'SSE 累积失败：{reply!r}')

        # 2) 下一轮真的看得见
        doc = self.turn(world, '眼镜呢')
        self.assertIs(doc['scene_state']['glasses'], False,
                      '上一轮她摘了眼镜，这一轮状态仍是戴着')
        self.assertIn('没有戴眼镜', doc['system_prompt'])
        self.assertNotIn('戴着眼镜', doc['system_prompt'])

    def test_23c2_reply_uses_last_action_in_message(self):
        """一条回复里「摘下又戴上」，按最后那个算（R-05 的边界）。

        累积中途就算，会被读成「摘下」；必须等整条说完再判断。
        """
        world = 'crash'
        server.scene_mod.REGISTRY.reset(world)
        self._run_reply(world, '（把眼镜摘下来）……（又戴上了）')
        doc = self.turn(world, '眼镜呢')
        self.assertIs(doc['scene_state']['glasses'], True,
                      '最后一步是戴上，状态就该是戴着')

    def test_23d_reply_does_not_move_the_user(self):
        """她说「我到活动室了」不代表用户到了（R-05 第 ② 条）。"""
        server.scene_mod.REGISTRY.reset('normal')
        self._run_reply('normal', '我到活动室了。')
        doc = self.turn('normal', '在吗')
        self.assertIs(doc['scene_state']['together'], False)

    def test_24_history_keeps_what_pronouns_point_to(self):
        """CHAT-010 的机制层：指代要被接住，前提是前几轮真的在请求里。

        这条不能证明她答对了（那是 C 类证据，见 VALIDATION），
        但能证明「后来呢」发出去时，上一轮说的东西**确实随请求带上了**。
        历史被截断或丢光时，任何模型都只能反问「你指哪个」。
        """
        history = [
            {'role': 'user', 'content': '刚才那个锅是不是不能用'},
            {'role': 'assistant', 'content': '不能用。涂层掉了。'},
        ]
        status, raw = self.request('/api/preview',
                                   {'world': 'normal', 'message': '后来呢',
                                    'history': history})
        self.assertEqual(status, 200, raw)
        doc = json.loads(raw)
        sp = doc['system_prompt']
        # 历史不进 system，而是作为对话消息随请求发送。
        self.assertNotIn('刚才那个锅是不是不能用', sp,
                         '历史不该被塞进 system（会随每轮累积）')
        # 条数里含人格卡的示例（每组合 user+assistant 两条），所以不写死数字：
        # 直接数「末尾是不是 历史两条 + 本轮一句」，这才是这条用例要查的形状。
        status, raw2 = self.request('/api/preview',
                                    {'world': 'normal', 'message': '后来呢'})
        without_history = json.loads(raw2)['message_count']
        self.assertEqual(doc['message_count'], without_history + 2,
                         '带上两条历史后消息数应当正好多两条')

    def test_25_history_cannot_smuggle_across_worlds(self):
        """WORLD-001 的另一个入口：历史是前端传来的，不能拿它绕过世界隔离。

        场景：在主线对话里聊过崩溃世界的内容，切到崩溃世界时前端理论上只传
        本世界的 hist。即便前端传错了（或者有人直接打接口），
        服务端也只能按本次请求的 world 组装，不能把另一世界的人格换掉。
        """
        history = [
            {'role': 'user', 'content': '在崩溃世界里你戴眼镜'},
            {'role': 'assistant', 'content': '有。'},
        ]
        status, raw = self.request('/api/preview',
                                   {'world': 'normal', 'message': '嗯',
                                    'history': history})
        self.assertEqual(status, 200, raw)
        doc = json.loads(raw)
        self.assertEqual(doc['persona_id'], 'yuki', '被历史里的内容改了人格卡')
        # 人格必须是主线的：崩溃卡的特征句不能因为历史出现过就混进来。
        self.assertNotIn('你就是一个普通高中生', doc['system_prompt'])
        # 场景状态也不能被历史带动：主线默认不戴眼镜，历史里那句「你戴眼镜」
        # 不该让它变成戴着。
        self.assertIs(doc['scene_state']['glasses'], False)
        block = doc['system_prompt'][doc['system_prompt'].index('=== 此刻 ==='):
                                     doc['system_prompt'].index('=== 状态结束 ===')]
        self.assertIn('- 没有戴眼镜', block)

    # ---------------- 字符画（CHAT-017 / DEC-013） ----------------
    # 这一批测的是**组装与显示契约**：哪个世界放开、回复里多行图案会不会被
    # 当成违例、前端拿什么依据决定保空白。**不测**「她该不该画」——
    # 那是 V-ART-01，要真实试聊（PEND-001）。

    def test_26_art_allowed_only_in_crash_world(self):
        """字符画只在崩溃世界放开（DEC-013）。

        这条锁的是**决定本身**，不是实现细节：主线世界那句话还写着
        「不要分段」，同一条回复里既不分段又画一只猫，会把形式规则做成两套
        互相打架的规矩。要放开就先把 DEC-013 改了，再改这里。
        """
        self.assertFalse(pb.ART_ALLOWED_WORLDS['normal'])
        self.assertTrue(pb.ART_ALLOWED_WORLDS['crash'])
        # 两张卡的世界名必须都能在表里查到，否则拼错了会静默按「不开」处理。
        for world in ('normal', 'crash'):
            self.assertIn(world, pb.ART_ALLOWED_WORLDS)

    def test_27_prompt_carries_the_matching_instruction(self):
        """放开的那个世界拿到「可以画」，另一个拿到「不画」。

        只查「有没有这段」，不查措辞好不好——措辞是人格问题，得试聊看。
        """
        cards = server.load_personas()
        crash = pb.build_messages(pb.persona_for(cards, 'crash'), 'crash', [], [], '测试')[0]
        normal = pb.build_messages(pb.persona_for(cards, 'normal'), 'normal', [], [], '测试')[0]
        self.assertIn(pb.ART_PROMPT, crash['content'])
        self.assertNotIn(pb.ART_PROMPT, normal['content'])
        self.assertIn(pb.ART_OFF_PROMPT, normal['content'])
        self.assertNotIn(pb.ART_OFF_PROMPT, crash['content'])

    def test_28_art_instruction_does_not_become_a_quota(self):
        """「可以画」这一段里不能出现配额式的说法（DEC-007）。

        这是防回流：以后有人想「让她多画一点」，最顺手的写法就是加一句
        「每几轮画一次」。那不是产品规则。
        """
        for bad in ('每次', '每轮', '每条', '都要', '必须画', '至少', '频率'):
            self.assertNotIn(bad, pb.ART_PROMPT,
                             f'ART_PROMPT 里出现配额式说法「{bad}」，与 DEC-007 冲突')
        # 反过来，「不画也正常」必须写着，否则模型会以为不画是没做到。
        self.assertIn('只说话永远', pb.ART_PROMPT)

    def test_28b_art_format_is_an_explicit_exception(self):
        """字符画的格式要求必须**明确写成例外**（R-08，CHAT-017）。

        崩溃世界的 system 里同时有两段：
          - `TERMINAL_PROMPT`：「不要分段，不要空行，不要列表」
          - `ART_PROMPT`：「图案单独占几行，文字写在图案下面」
        第二段没有说明自己是第一段的例外，模型只能自己猜哪条优先。
        现有用例只查「两段都在不在」，查不出这个矛盾。

        判据不看措辞，看**关系**：ART_PROMPT 里必须出现「例外 / 只对图案」
        这类限定，并且点名它放宽的是「分段」这条规则。只加一句
        「可以画」不算修好——那等于又多了一条规定。
        """
        self.assertIn('例外', pb.ART_PROMPT,
                      f'ART_PROMPT 必须说明自己是基础格式的例外：{pb.ART_PROMPT}')
        self.assertIn('图案', pb.ART_PROMPT)
        # 必须点名放宽的是哪一条，否则模型不知道「不要分段」还算不算数。
        self.assertTrue(
            any(k in pb.ART_PROMPT for k in ('分段', '空行')),
            f'ART_PROMPT 要点名放宽的是「分段 / 空行」那条：{pb.ART_PROMPT}')
        # 并且要说清：除了图案块，其余文字仍然守原规则。
        self.assertTrue(
            any(k in pb.ART_PROMPT for k in ('其余', '其他', '除了', '文字部分')),
            f'ART_PROMPT 要限定适用范围，不能整条放宽：{pb.ART_PROMPT}')

    def test_28c_art_exception_reaches_crash_prompt_only(self):
        """例外只出现在放开画的世界；主线不许出现这条例外（DEC-013）。"""
        msgs_crash = pb.build_messages(
            pb.persona_for(server.load_personas(), 'crash'),
            'crash', [], [], '在吗')
        sp_crash = msgs_crash[0]['content']
        self.assertIn('例外', sp_crash)
        self.assertIn('不要分段', sp_crash)   # 基础规则仍在，只是有了例外

        msgs_normal = pb.build_messages(
            pb.persona_for(server.load_personas(), 'normal'),
            'normal', [], [], '在吗')
        sp_normal = msgs_normal[0]['content']
        self.assertNotIn('图案单独占几行', sp_normal,
                         '主线世界不该出现字符画的格式例外')

    def test_29_preview_reports_kind_per_world(self):
        """`/api/preview` 按世界回报 kind：前端不用自己再猜一遍规则。"""
        for world, expect in (('normal', 'plain'), ('crash', 'chat')):
            status, raw = self.request('/api/preview', {'world': world, 'message': '在吗'})
            self.assertEqual(status, 200, raw)
            self.assertEqual(json.loads(raw)['kind'], expect, f'{world} 的 kind 不对')

    def test_30_multiline_art_is_not_a_violation(self):
        """字符画不该被标 △「分段」；普通空行分段仍然要标。

        两半都要测：只测前一半，把分段检查整个删掉也会绿。
        """
        art = '┌────┐\n└────┘\n雨声一直没停。'
        self.assertEqual(check_persona.simulate_check_reply(art, '在下雨', {}), '')
        prose = '看书。\n\n另外，温度在下降。'
        self.assertIn('分段', check_persona.simulate_check_reply(prose, '', {}))

    def test_31_example_art_is_aligned(self):
        """人格卡示例里的图案必须自己对齐。

        这条测试存在的理由和 `test_12` 一样：示例是模型唯一的写法参照，
        示例里的图案歪着，她画出来的就会歪。
        """
        cards = server.load_personas()
        found = 0
        for world in ('normal', 'crash'):
            if not pb.ART_ALLOWED_WORLDS[world]:
                continue
            for e in pb.persona_for(cards, world)['examples']:
                if not textart.is_art(e['assistant']):
                    continue
                found += 1
                with self.subTest(example=e['user']):
                    self.assertEqual(textart.check_art(e['assistant']), [],
                                     f'示例「{e["user"]}」的图案对齐有问题')
        # 没有示例的话，上面那个循环什么都没测——必须至少有 1 条。
        self.assertGreaterEqual(found, 1, '放开字符画的世界至少要有一条示例，'
                                         '否则模型没有写法参照')

    def test_32_art_does_not_leak_across_worlds(self):
        """图案里的内容也守世界隔离：主线世界不能因为「画得像」就拿到放开。

        做法和 WORLD-001 的其他用例一致：同一句话分别问两个世界，
        看组装结果。这里问的是**画**，不是文字。
        """
        status, raw = self.request('/api/preview',
                                   {'world': 'normal', 'message': '画只猫给我看'})
        self.assertEqual(status, 200, raw)
        sysprompt = json.loads(raw)['system_prompt']
        # 主线世界：不画，而且不能出现「可以画」那一段（哪怕是作为反例）。
        self.assertNotIn(pb.ART_PROMPT, sysprompt)

        status, raw = self.request('/api/preview',
                                   {'world': 'crash', 'message': '画只猫给我看'})
        self.assertEqual(status, 200, raw)
        crash_prompt = json.loads(raw)['system_prompt']
        self.assertIn(pb.ART_PROMPT, crash_prompt)
        # 主线卡的特征句不能因为请求里提到「画」就混进来（双向都要查）。
        self.assertNotIn('你没有戴眼镜', crash_prompt)


    def test_37_seed_notes_are_not_runtime_rules(self):
        """历史种子里作废的「硬约束」不能作为规则回到文件里（AUD-11）。

        起因：`data/seed_nagato_lines.md` 把人工核定的 10 句台词，和当时作者从
        这 10 句里推出的统计与「可执行的硬约束」混排在一起：

        > 日常对话单句 ≤ 15 字；只有解释超自然现象时才允许长句，且必须零情绪词。

        这条与现有两条已确认决定**直接冲突**——「≤ 15 字」是被 DEC-001 取消的
        字数上限，「零情绪词」是被 DEC-007 否决的配额指标。但它以「可执行」的
        口吻写在数据目录里，任何人（包括以后的我）读到这里都可能当成运行规则。

        这份文件没有任何代码读取，所以删它不会让测试变红——正因如此，它只能靠
        这条断言保护。判据是**作废的结论必须以删除线或「作废」字样出现**，
        不能光秃秃地留在正文里。
        """
        seed = ROOT / 'data' / 'seed_nagato_lines.md'
        if not seed.exists():
            self.skipTest('种子文件不存在（不影响运行，因为它不被代码读取）')
        text = seed.read_text(encoding='utf-8')

        # ① 必须明确声明「不是运行规则」，否则读者只能靠猜。
        self.assertIn('这份文件是历史记录，不是运行规则', text,
                      '种子文件开头必须声明它不是运行规则（AUD-11）')

        # ② 不要再用「某几个字出现在这一行」当判据。第一版这么写，两个方向都错：
        #    · 误报——解释「它为什么作废」的句子会引用它，被当成旧规则本身；
        #    · 漏报（独立复核指出的）——同义改写 `单句控制在十五个字以内` 就绕过了，
        #      而 M50 恰好只测了原字面，于是测试报「已拦住」，实际只拦住了一种写法。
        #
        #    改法：不认字面，认**这一行是不是在文件里生效**。
        #    Markdown 里让一行失效只有两种写法：划掉（`~~…~~`）或明确标注作废。
        #    所以判据变成——**凡是同时出现「字数限制」与「情绪词限制」这两类
        #    意思的行，只要不在这两种作废标记里，就算它还在下达旧规则**。
        #    这样同义改写也拦得住，因为它们表达的还是同一对限制。
        # 模式要小心：只匹配「限制」，不匹配「统计」。
        #   第一版把 `字数` 与 `单句.{0,6}字` 都算进去，于是
        #   `| 平均字数 | **5.8 字**（含标点）…` 这张统计表也被当成字数上限——
        #   描述「她平均说多长」和规定「她不许超多长」是两件事。
        LIMIT_WORDS = [
            # 字数上限：必须有比较级或范围词
            (r'≤\s*\d+\s*字', r'限\s*\d+\s*字', r'不超过\s*\d+\s*字',
             r'\d+\s*字以内', r'\d+\s*字以下', r'最多\s*\d+\s*字',
             r'控制在.{0,8}字', r'单句.{0,8}(≤|以内|以下|不超过|控制在)'),
            # 情绪词配额（任何形式的「把情绪降到零/不许带情绪」）
            (r'零情绪', r'没有情绪词', r'情绪词.{0,4}(为|是)?\s*零',
             r'不带情绪', r'不要有情绪', r'情绪.{0,4}(为零|归零)',
             r'零.{0,3}情绪', r'一点情绪都不'),
        ]
        # 作废标记要把所有**同义说法**都列上。第一版只写了「作废」「已取消」，
        # 而文件里那句写的是「已被 DEC-001 整体取消」——少一个字就漏，
        # 于是「解释这条为什么不算」的句子被判成了「这条还在生效」。
        # `已由代码实现` / `不需要指令` 也是「谈论它」的写法：表格里那一行说的
        # 是「这件事现在由 build_corpus 负责，不用再写指令了」，不是在下达命令。
        DEAD_MARKS = ('~~', '作废', '取消', '不执行', '禁止', '不算规则',
                      '已失效', '不再', '否决', '已由代码实现', '不需要指令')

        def live_lines(patterns):
            out = []
            for ln in text.splitlines():
                low = ln.strip()
                if not low or low.startswith('#'):
                    continue            # 标题不是规则
                if any(m in ln for m in DEAD_MARKS):
                    continue            # 已明确标为作废，是在谈论它
                if any(re.search(p, ln) for p in patterns):
                    out.append(ln)
            return out

        for i, pats in enumerate(LIMIT_WORDS):
            name = '字数上限' if i == 0 else '情绪词配额'
            hits = live_lines(pats)
            self.assertFalse(
                hits,
                f'种子文件里有没标作废的{name}：{hits}\n'
                f'这两类限制分别撞 DEC-001（取消字数上限）与 DEC-007'
                f'（不把自然表达做成配额指标）。要么删掉，要么写成 ~~删除线~~ '
                f'或明说「作废」。')

        # ③ 「只保留 1 集」「必须按时间轴切分剔除」这类**剪裁指令**同样不行。
        #    第一版写的是 `'必须按时间轴切分剔除**'`——带两个星号，
        #    而文件里那一行是 `必须按时间轴切分剔除**` 结尾，但整行已经被
        #    `~~` 包住、且含「不执行」；判据里又有「含 ~~ 就跳过」，
        #    所以它一条都匹配不到。**那是一条永远为真的断言**（复核实测过）。
        #    现在改成按「剪裁动作 + 命令口气」查，且必须真的存在至少一条候选行，
        #    否则判据自身失效要说出来，不能靠空集通关。
        TRIM_ACTS = (r'只保留', r'剔除', r'删掉.*集', r'去重.*集', r'切分剔除')
        # `**` 必须转义：裸写 `r'**'` 是非法正则（报 nothing to repeat），
        # 而它混在 other patterns 里，出错点会指向整行，不容易看出是它。
        ORDER_WORDS = (r'必须', r'\*\*', r'^-\s', r'^\d', r'^>')

        trim_hits = []
        for ln in text.splitlines():
            if not ln.strip() or any(m in ln for m in DEAD_MARKS):
                continue
            if any(re.search(p, ln) for p in TRIM_ACTS) and \
                    any(re.search(p, ln) for p in ORDER_WORDS):
                trim_hits.append(ln)
        self.assertFalse(
            trim_hits,
            f'种子文件里有没标作废的语料剪裁指令：{trim_hits}\n'
            f'语料是全量保留的（13070 条），重复率不作为删除依据。')

        # ④ 判据不能靠空集通关。上面两组模式必须**在本文件里确实出现过**
        #    （只是都带上了作废标记）。否则说明模式写错了——文件改了、
        #    这些词一个都不剩，那两条断言就永远为真。
        #    这是复核给我的教训：③ 原先就是这么空掉的，却一直显示"通过"。
        for i, pats in enumerate(LIMIT_WORDS):
            self.assertTrue(
                any(any(re.search(p, ln) for p in pats) for ln in text.splitlines()),
                f'判据自身失效：种子文件里再也找不到第 {i + 1} 组限制的任何痕迹。'
                f'要么是文件被大改（那就要重看这条测试是否还有意义），'
                f'要么是模式写错了（那就成了永远为真的空断言）。')
        self.assertTrue(
            any(any(re.search(p, ln) for p in TRIM_ACTS) for ln in text.splitlines()),
            '判据自身失效：种子文件里找不到任何剪裁动作的词——'
            '要么它被整段删了（那这条测试就没对象了，应当删掉而不是留着空跑），'
            '要么模式写错了。')

    def test_36_rules_have_matching_examples(self):
        """写了规则就必须配示例，否则规则只靠一句话在支撑（AUD-08）。

        起因：主线卡规则里明明有「不同意就直接说不同意，并给理由……理由说完
        就停」，但 29 条示例里**一条意见分歧都没有**。规则是一句要求，示例是
        模型要模仿的完整形状；只有规则没有示例时，这条规则在实际输出里会
        明显弱于那些「规则＋好几条示例」的行为。

        这条测试只查**最容易观察到、也最容易漏配**的几条规则，用「用户说了
        带分歧信号的句子，而她**没有顺着附和**」来判——不查措辞，只看她是否
        表达了不同意见。
        """
        # (规则里的关键词, 用户侧的分歧信号, 她要表达的「不同意」信号)
        RULES = [
            ('不同意就直接说不同意', r'我觉得.*(败笔|不好|没意思|无聊)|你也觉得.*吧',
             r'不同意|不觉得|不这么|不赞同|我不认为'),
        ]
        for wid in ('normal', 'crash'):
            p = pb.persona_for(server.load_personas(), wid)
            system = p['system_prompt']
            for rule_key, user_sig, her_sig in RULES:
                if rule_key not in system:
                    continue        # 这条卡没写这条规则，不要求它有示例
                hit = [e for e in p['examples']
                       if re.search(user_sig, e['user'])]
                self.assertTrue(
                    hit,
                    f'{wid} 的规则里写了「{rule_key}」，但示例里没有任何一条'
                    f'是在示范它——只有规则没有示例，这条规则在实际输出里会'
                    f'明显弱于别的规则')
                self.assertTrue(
                    any(re.search(her_sig, e['assistant']) for e in hit),
                    f'{wid} 有「{rule_key}」的规则，也有对应的提问，'
                    f'但她的回答里看不出分歧——示例在教「附和」，和规则相反')

    def test_35_examples_do_not_share_a_filler_opening(self):
        """示例不能共用一个「犹豫前缀」，否则她学到的是套话（AUD-12）。

        独立扫出来的：崩溃卡 17 条里 7 条以「那个」开头。单看每条都自然，
        放在一起就是模板——模型从示例里学到的是「开口先来一个那个……」，
        而不是「犹豫的时候，说话会不顺」。AUD-12 的原话是「某一场景可用的
        候选不能自动成为固定口头禅」，这条测试就是它可自动化的那一半。

        **为什么只查填充词，不查所有重复**：主线卡有 3 条以「……」开头，
        那是「不说话」这个特质的直接表达，内容就是省略号本身——重复它不
        构成套话。所以判据只针对**不承载内容的犹豫填充词**（那个/呃/嗯/
        诶/啊），这类词出现在开头时，若多条共用，就是在教一种口癖。
        """
        FILLERS = ('那个', '呃', '嗯', '诶', '啊', '唔', '唉')
        for wid in ('normal', 'crash'):
            p = pb.persona_for(server.load_personas(), wid)
            answers = [e['assistant'] for e in p['examples']]
            with self.subTest(world=wid):
                openings = [a.lstrip('（(').lstrip() for a in answers]
                counts = {}
                for f in FILLERS:
                    n = sum(1 for a in openings if a.startswith(f))
                    if n:
                        counts[f] = n
                # **绝对阈值 3**，不用比例。一开始写的是「四成」，17 条里
                # 允许 6 条同开头——那已经是很明显的口癖了（变异 M47 把
                # 5 条改成「那个……」都没被拦住，就是这个阈值太宽）。
                # 一两条是语气，三条并排出现就已经在教一种固定的开口方式。
                limit = 2
                worst = max(counts.values()) if counts else 0
                self.assertLessEqual(
                    worst, limit,
                    f'{wid} 的示例里有 {worst} 条用同一个犹豫词开头'
                    f'（{counts}），这已经是一个固定口癖，不是在示范语气')

    def test_34_examples_do_not_invent_missing_facts(self):
        """示例不得示范「编造自己不可能知道的事实」（AUD-09）。

        为什么这条比它看起来重要：人格卡里明明写着「不确定就说「不确定」。
        绝不编造」，而**第一条示例**恰好是「今天天气怎么样 → 晴。气温二十三度。」
        示例与规则冲突时，模型学的是示例——规则是一句话，示例是它要模仿的
        完整问答形状。这条测试锁的是「规则和示例不能互相拆台」。

        判据刻意窄：只查「她没有信息来源的那类事实」——天气、温度、日期、
        时间、实时价格、新闻。别的领域的数字（代码行号、`O(n)`）不算编造，
        那些是推理结果，不是从外部读到的。所以用**问句主题**定位，
        不扫全文的数字。
        """
        # 用户问的是她无从得知的实时信息。
        UNKNOWABLE = re.compile(
            r'天气|气温|温度|几度|下雨|外面.*(冷|热)|新闻|股票|汇率|冠军|谁赢')
        # 示例若给出这些，就算编造：具体数值、天气词。
        SHOWS_FACT = re.compile(r'\d|[晴阴雨雪]|度|摄氏')

        def invented(answer, question):
            """回答里有没有**用户没提供过**的事实。

            「睡不着，外面在下雨 → 雨声一直没停」不算编造：雨是用户给的，
            她只是接着说了句听感。判据必须比这一层更细，否则会把
            「顺着对方的话说」误判成编造——那正是她在做的事。
            """
            for m in SHOWS_FACT.finditer(answer):
                ch = m.group(0)
                if ch not in question:      # 用户没提过这个字/数
                    return ch
            return ""

        cards = server.load_personas()
        checked = 0
        for wid in ('normal', 'crash'):
            p = pb.persona_for(cards, wid)
            for e in p['examples']:
                if not UNKNOWABLE.search(e['user']):
                    continue
                checked += 1
                with self.subTest(world=wid, user=e['user']):
                    leak = invented(e['assistant'], e['user'])
                    self.assertFalse(
                        leak,
                        f'{wid} 的示例「{e["user"]}」在没有信息来源的情况下给出了'
                        f'「{e["assistant"]}」（其中「{leak}」用户没提过）——这是在示范编造')

        # 没有样本的话这条测试什么都没测。两张卡都该有一条反问式回答。
        self.assertGreaterEqual(checked, 2,
                                '两张卡都应有「问天气」的示例，否则这条测试没有覆盖')
        # 反过来：只是「删掉数字」不算修好，必须真的表达「不知道」。
        for wid in ('normal', 'crash'):
            p = pb.persona_for(cards, wid)
            answers = [e['assistant'] for e in p['examples']
                       if UNKNOWABLE.search(e['user'])]
            self.assertTrue(
                any(re.search(r'不知道|看不见|没有.*(信息|数据)', a) for a in answers),
                f'{wid} 缺少「不知道」式的回答——删掉数字不等于承认不知道')

    def test_33_text_lines_beside_art_stay_plain(self):
        """一条回复里「文字 + 图案」并存时，文字行必须还能折行。

        这是实测截图暴露的问题：汉字与 ASCII 的宽度比是 1.66（不是 2），
        整条按 `pre` 排版时文字会被压成一行，句号被挤到下一行单独出现。
        修法是把「保空白」这个待遇只给图案那几行——分块由 `art_blocks` 决定，
        前端 `artBlocks` 逐条对应。
        """
        mixed = '雨声一直没停。\n┌──────┐\n│      │\n└──────┘\n窗外没开灯。'
        blocks = textart.art_blocks(mixed)
        self.assertEqual([k for k, _ in blocks], [False, True, False],
                         '文字行不能被并进图案块')
        # 拼回去必须一字不差：分块不能吃掉行。
        self.assertEqual([ln for _, ch in blocks for ln in ch], mixed.split('\n'))
        # 纯文字回复里一块图案都不该有——否则普通对话也会被换排版。
        self.assertFalse(any(k for k, _ in textart.art_blocks('看书。\n\n另外，温度在下降。')))


if __name__ == '__main__':
    # Windows + Python 3.13 上遇到过陈旧 __pycache__ 触发的 CPython 致命错误
    # （_PyEval_EvalFrameDefault: Executing a cache），表现是所有断言都通过、
    # 之后进程崩在 setUpClass 里。先禁写字节码，避免这个环境坑被误读成代码问题。
    sys.dont_write_bytecode = True
    unittest.main(verbosity=2)
