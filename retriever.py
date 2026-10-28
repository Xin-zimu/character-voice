#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
retriever.py — 剧情检索（零依赖）

把 data/index/docs.jsonl 装进内存, 用 BM25 打分, 世界感知排序。

设计要点:
  1. 不判断「谁在说话」, 只找「这段剧情里她在场」。
     字幕没有说话人标注, 硬猜会引入系统性错误 (见 data/README.md 的教训)。
  2. 世界感知: 主线世界的她在主线检索里优先, 崩溃世界的她在崩溃检索里优先。
     但两边都不过滤 —— 因为「另一个世界」的信息偶尔漏过来, 正是玩法本身。
  3. 「无尽八月」的七集内容高度重复, 检索时按文本去重, 免得同一句塞满结果。

用法:
    from retriever import Corpus
    c = Corpus(Path("data/index"))
    c.search("消失", "你为什么要把世界改回去", k=6)
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path

# 中文常用虚词: 不切 2-gram, 也参与停用
_STOP = set("的了是我你他她它们在有和就都而及与着或一个不没很也这那吗呢吧啊呀哦嘛")

# 否定 / 反义字。查询侧要留住它们的语义（Q-02）：
#   「不知道」被剥成「知道」、「不需要」被剥成「需要」，是语义反转。
#   语料里她的标志性回答正是「不需要」（消失@0:25:56）。
_NEG = set("不没未无非别否莫勿")

_CN = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]+")
_EN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """中日文按 2-gram 切, 拉丁词整体保留。

    **这是建索引用的分词，规则不要改。**

    它同时决定 BM25 的词表、df、IDF 和文档长度。任何对它的改动都会
    让全库 IDF 重算、所有文档分数整体漂移，从而改变排序 —— 实测把
    「不确定」保留成「不确」+「确定」这种替换式改法，会让首条查询的
    正确答案从 7.838 分掉到 6.011 分、main Recall 从 73.0% 崩到 4.0%。

    查询侧需要的增量（否定信号、短句兜底）放在 `tokenize_query()` 里，
    只影响查询，不动索引。
    """
    text = text.lower()
    toks: list[str] = []
    for m in _EN.finditer(text):
        toks.append(m.group())
    for m in _CN.finditer(text):
        s = m.group()
        # 去掉虚词, 避免「的」「我」这类高频字符拉高所有文档的分数
        s = "".join(ch for ch in s if ch not in _STOP)
        if not s:
            continue
        if len(s) == 1:
            toks.append(s)
        else:
            toks.extend(s[i: i + 2] for i in range(len(s) - 1))
    return toks


def tokenize_query(text: str) -> list[str]:
    """查询侧分词：在 `tokenize()` 的结果上**追加**两类信号。

    与 `tokenize()` 分开的原因是分工不同：
      - `tokenize()` 定索引，改它 = 改全库 IDF = 改排序；
      - 这个函数只作用于用户这句话，不影响任何文档的分数尺度。

    追加的两类信号：

    1. **否定双字**（Q-02）。`不知道` 在 `tokenize()` 里只剩 `知道`，
       于是「不知道」和「知道」会得到完全相同的检索结果。这里额外追加
       `不知`，两者即可区分。只在否定字**紧挨实词**时追加；孤立否定字
       （「在不在」里的 `不`）没有区分力，不加。

    2. **整段虚词的短句兜底**（Q-01）。`在吗`/`是的`/`那个` 经
       `tokenize()` 后为空，检索会静默跳过。这里退回用原串，
       让检索真的跑起来。原串 token 的 IDF 很高、只在原句出现过时
       才命中，所以不会把短句硬凑出无关片段。
       **单个虚词不退**（`的`/`了`）—— 那本来就不该检索。

    纯标点 / 空白仍返回 `[]`。
    """
    text = text.lower()
    toks = tokenize(text)

    for m in _CN.finditer(text):
        raw = m.group()
        if not any(ch not in _STOP for ch in raw):
            # 整段全是虚词。与 `tokenize()` 的做法保持一致：
            # 长度 >=2 退原串，单字不退。
            if len(raw) >= 2:
                toks.append(raw)
            continue
        for i, ch in enumerate(raw):
            if ch not in _NEG:
                continue
            if i > 0 and raw[i - 1] not in _STOP:
                toks.append(raw[i - 1: i + 1])   # 前字 + 否定字：不确 / 没说
            if i + 1 < len(raw) and raw[i + 1] not in _STOP:
                toks.append(raw[i: i + 2])       # 否定字 + 后字：不知 / 不需

    seen: set[str] = set()
    out: list[str] = []
    for t in toks:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


# 检索结果状态。修复前「切不出 token」和「语料里没有」都只是返回 []，
# 调用方与评测都无法区分，导致把分词失败记成「正确的空召回」。
STATUS_OK = "ok"                    # 有命中
STATUS_NO_TOKENS = "no_tokens"      # 切不出 token —— 检索没有启动
STATUS_NO_MATCH = "no_match"        # 检索跑了，但没有够格的记录


class SearchResult:
    """`Corpus.search_detailed()` 的返回值。

    保留 `hits` 让调用方可以直接替换 `search()`；
    额外带 `status` / `tokens` / `query` 便于分辨「为什么没结果」。
    """

    __slots__ = ("hits", "status", "tokens", "query")

    def __init__(self, hits: list[dict], status: str,
                 tokens: list[str], query: str):
        self.hits = hits
        self.status = status
        self.tokens = tokens
        self.query = query

    def __bool__(self) -> bool:
        """`if result:` 等价于「有命中」。"""
        return bool(self.hits)

    def __len__(self) -> int:
        return len(self.hits)

    def __iter__(self):
        return iter(self.hits)

    def __repr__(self) -> str:
        return (f"SearchResult(status={self.status!r}, n={len(self.hits)}, "
                f"query={self.query!r})")


def search_detailed(corpus, world: str, query: str, k: int = 6,
                    min_score: float = 5.0) -> SearchResult:
    """模块级快捷方式。

    `corpus` 为 None 时只做分词判断、不加载索引 —— 用于不依赖语料库的
    契约测试，也方便调用方先确认「这条查询能不能进检索」再决定是否加载。
    """
    if corpus is None:
        query = query or ""
        toks = tokenize_query(query)
        if not toks:
            return SearchResult([], STATUS_NO_TOKENS, [], query)
        return SearchResult([], STATUS_NO_MATCH, toks, query)
    return corpus.search_detailed(world, query, k, min_score)


class BM25:
    """标准 BM25, k1/b 用常见取值。"""

    __slots__ = ("k1", "b", "tfs", "lens", "n", "avgdl", "df", "idf")

    def __init__(self, docs: list[list[str]], k1: float = 1.4, b: float = 0.72):
        self.k1 = k1
        self.b = b
        self.tfs = [Counter(d) for d in docs]
        self.lens = [len(d) for d in docs]
        self.n = len(docs)
        self.avgdl = (sum(self.lens) / self.n) if self.n else 1.0
        self.df: Counter[str] = Counter()
        for tf in self.tfs:
            for w in tf:
                self.df[w] += 1
        self.idf: dict[str, float] = {}
        for w, n in self.df.items():
            self.idf[w] = math.log(1 + (self.n - n + 0.5) / (n + 0.5))

    def score(self, query_tokens: list[str], i: int) -> float:
        tf = self.tfs[i]
        dl = self.lens[i]
        norm = self.k1 * (1 - self.b + self.b * dl / self.avgdl)
        s = 0.0
        for w in query_tokens:
            f = tf.get(w)
            if not f:
                continue
            s += self.idf.get(w, 0.0) * (f * (self.k1 + 1)) / (f + norm)
        return s

    def search(self, query_tokens: list[str], limit: int = 64) -> list[tuple[int, float]]:
        """只要 top-limit, 不全量排序 (13070 条文档这样快得多)。"""
        scored = []
        for i in range(self.n):
            s = self.score(query_tokens, i)
            if s > 0:
                scored.append((i, s))
        scored.sort(key=lambda x: -x[1])
        return scored[:limit]


class Corpus:
    def __init__(self, index_dir: Path):
        self.dir = Path(index_dir)
        meta_p = self.dir / "meta.json"
        docs_p = self.dir / "docs.jsonl"
        if not meta_p.exists() or not docs_p.exists():
            raise FileNotFoundError(
                f"找不到检索索引: {self.dir}\n先运行: python build_corpus.py"
            )
        self.meta = json.loads(meta_p.read_text(encoding="utf-8"))
        self.docs: list[dict] = []
        with docs_p.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    self.docs.append(json.loads(line))
        self.bm25 = BM25([tokenize(d["bm25"]) for d in self.docs])
        self.episodes = {e["ep"]: e for e in self.meta.get("episodes", [])}

    # ------------------------------------------------------------------
    def search(self, world: str, query: str, k: int = 6,
               min_score: float = 5.0) -> list[dict]:
        """世界感知检索。返回命中列表（旧签名，主线依赖，保持不变）。

        world: "normal" | "crash"
        返回 [{id, ep, ep_title, start, world, text, ctx, anchor, score}]

        只在当前世界的记录里检索（WORLD-001，DEC-002）。
        以前的「另一世界降权 12% 但不过滤」已取消：降权不是隔离，
        它只会让泄漏变少、变难发现，而不是不发生。
        索引仍是全量 13070 条，过滤发生在查询时。

        关于 min_score：原来只有相对门槛 top*0.12，在「今天降温了」这种
        全体低分的查询上完全失效（top≈4，门槛≈0.5，噪声全过）。
        实测分数分布：真实依据 6–65，噪声 3–5。取 5.0 作绝对下限。
        「早上好」「好久不见」这类寒暄仍会命中（18 / 15 分），
        因为它们在原文里是别人的招呼语 —— 这靠提示词处理，不靠分数。

        需要区分「为什么没结果」时用 `search_detailed()`。
        """
        # 必须返回真正的 list：调用方会做切片、len、索引，
        # 不能返回 SearchResult 这种"像 list 但不是 list"的对象。
        return self.search_detailed(world, query, k, min_score).hits

    def search_detailed(self, world: str, query: str, k: int = 6,
                        min_score: float = 5.0,
                        corpus=None) -> "SearchResult":
        """同 `search()`，但把「为什么没结果」也返回。

        三种状态（Q-01 的配套修复）：

          STATUS_OK         有命中
          STATUS_NO_TOKENS  查询切不出任何 token —— **检索没有启动**
          STATUS_NO_MATCH   检索跑了，但语料里确实没有够格的记录

        修复前这两种情况都只是 `return []`，调用方无法分辨，
        评测把「分词失败」记成了「正确的空召回」（假通过）。
        现在寒暄/虚词输入会落到 no_match 或 ok，而真正的切词失败
        才会是 no_tokens —— 不再「所有短句一律强制召回」。
        """
        query = query or ""
        toks = tokenize_query(query)
        if not toks:
            return SearchResult([], STATUS_NO_TOKENS, [], query)
        raw = self.bm25.search(toks, limit=160)
        if not raw:
            return SearchResult([], STATUS_NO_MATCH, toks, query)
        top = raw[0][1] or 1.0


        out = []
        seen_near: list[tuple[str, int]] = []

        for i, s in raw:
            d = self.docs[i]
            # 硬过滤：不是当前世界的记录直接跳过。
            if d["world"] != world:
                continue
            weight = 1.15

            # 剧情段落的连贯性: 长门在对话中说的话基本都是陈述句/应答,
            # 带问号的大多是阿虚在问、春日在大喊。降权, 不归零。
            if ("？" in d["text"] or "?" in d["text"]):
                weight *= 0.55
            if ("！" in d["text"] or "!" in d["text"]):
                weight *= 0.45
            if d["text"].startswith(("（", "「")):
                weight *= 0.7

            # 她「在场」的片段优先。anchor 是称呼锚点密度, 0~1。
            weight *= 0.85 + 0.55 * float(d.get("anchor", 0.0))

            score = s * weight

            if score < min_score or score < top * 0.12:
                continue

            # 去重: 相邻台词共用一个上下文窗口, 会互相挤占名额。
            # 同一集里距离 < 12 条的, 只留分最高的那条。
            near_dup = False
            for kept_ep, kept_i in seen_near:
                if kept_ep == d["ep"] and abs(kept_i - d["idx"]) < 12:
                    near_dup = True
                    break
            if near_dup:
                continue
            seen_near.append((d["ep"], d["idx"]))

            ep_info = self.episodes.get(d["ep"], {})
            out.append({
                "id": d["id"],
                "ep": d["ep"],
                "ep_title": ep_info.get("title", d["ep"]),
                "start": d["start"],
                "world": d["world"],
                "text": d["text"],
                "ctx": d["ctx"],
                # 逐行世界归属（R-01 / WORLD-001-B）。渲染层据此裁掉
                # 窗口里属于另一世界的上下文行。旧索引没有这个字段时
                # 是 None，`filter_ctx_by_world()` 会走保守分支。
                "ctx_worlds": d.get("ctx_worlds"),
                "anchor": d.get("anchor", 0.0),
                "score": round(score, 3),
            })
            if len(out) >= k:
                break
        return SearchResult(out, STATUS_OK if out else STATUS_NO_MATCH, toks, query)

    # ------------------------------------------------------------------
    def episode(self, ep: str, start_idx: int = 0, limit: int = 200) -> dict:
        """资料终端: 按集翻原文。"""
        info = self.episodes.get(ep, {})
        rows = [d for d in self.docs if d["ep"] == ep]
        total = len(rows)
        rows = rows[start_idx: start_idx + limit]
        return {
            "ep": ep,
            "title": info.get("title", ep),
            "start": info.get("start", ""),
            "end": info.get("end", ""),
            "lines": total,
            "offset": start_idx,
            "items": [
                {
                    "idx": d["idx"],
                    "start": d["start"],
                    "text": d["text"],
                    "world": d["world"],
                    "anchor": d.get("anchor", 0.0),
                }
                for d in rows
            ],
        }

    def landing(self) -> dict:
        return {
            "lines": self.meta.get("lines", len(self.docs)),
            "episodes": self.meta.get("episodes", []),
            "worlds": self.meta.get("worlds", {}),
            "movie_cut": self.meta.get("movie_cut"),
        }

    @property
    def size(self) -> int:
        return len(self.docs)
