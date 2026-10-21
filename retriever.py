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

_CN = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]+")
_EN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """中日文按 2-gram 切, 拉丁词整体保留。"""
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
        """世界感知检索。

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
        """
        toks = tokenize(query)
        if not toks:
            return []
        raw = self.bm25.search(toks, limit=160)
        if not raw:
            return []
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
                "anchor": d.get("anchor", 0.0),
                "score": round(score, 3),
            })
            if len(out) >= k:
                break
        return out

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
