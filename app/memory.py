"""剧情记忆检索。

设计意图：事实层用检索，不用微调记。
  - 微调教「怎么说」，检索给「说什么」
  - 想改设定只要改文本文件，立刻生效，不用重训

默认实现是零依赖的中日文 2-gram BM25，
够跑通、够快、对原文逐字匹配很准。
有 GPU 或 API 后可换成向量检索，接口保持一致即可。
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path


def tokenize(text: str) -> list[str]:
    """中日文按 2-gram 切，拉丁词按空格切。

    2-gram 对中文/日文这种不分词的文本效果意外地好，
    而且完全不需要词典，短查询也能命中。
    """
    text = text.lower()
    tokens: list[str] = []
    for m in re.finditer(r"[a-z0-9]+", text):
        tokens.append(m.group())
    for m in re.finditer(r"[\u3040-\u30ff\u4e00-\u9fff]+", text):
        s = m.group()
        if len(s) == 1:
            tokens.append(s)
        else:
            tokens.extend(s[i : i + 2] for i in range(len(s) - 1))
    return tokens


class BM25:
    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.tokens = [tokenize(d) for d in docs]
        self.n = len(docs)
        self.avgdl = sum(len(t) for t in self.tokens) / max(1, self.n)
        self.df: Counter[str] = Counter()
        for t in self.tokens:
            for w in set(t):
                self.df[w] += 1

    def _idf(self, w: str) -> float:
        n = self.df.get(w, 0)
        return math.log(1 + (self.n - n + 0.5) / (n + 0.5))

    def scores(self, query: str) -> list[float]:
        q = tokenize(query)
        out: list[float] = []
        for toks in self.tokens:
            tf = Counter(toks)
            dl = len(toks)
            s = 0.0
            for w in q:
                f = tf.get(w)
                if not f:
                    continue
                s += self._idf(w) * (f * (self.k1 + 1)) / (
                    f + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
                )
            out.append(s)
        return out


class Memory:
    """剧情 + 对话历史，统一检索。"""

    def __init__(self, root: Path, sources: list[str], top_k: int = 4, min_score: float = 0.08):
        self.root = root
        self.top_k = top_k
        self.min_score = min_score
        self.chunks: list[dict] = []
        self._bm25: BM25 | None = None
        self._load(sources)

    # ---------------- 载入 ----------------
    def _iter_files(self, patterns: list[str]):
        for pat in patterns:
            yield from sorted(self.root.glob(pat))

    def _chunk_text(self, text: str, max_len: int = 220) -> list[str]:
        """按空行分段，过长的段再按句号切。

        段太大会稀释关键词命中，段太小会丢上下文。
        200 字左右是好用的经验值。
        """
        paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        out: list[str] = []
        for p in paras:
            if len(p) <= max_len:
                out.append(p)
                continue
            buf = ""
            for sent in re.split(r"(?<=[。！？!?\.])", p):
                if len(buf) + len(sent) > max_len and buf:
                    out.append(buf.strip())
                    buf = sent
                else:
                    buf += sent
            if buf.strip():
                out.append(buf.strip())
        return out

    def _load(self, sources: list[str]) -> None:
        for path in self._iter_files(sources):
            if path.suffix.lower() == ".jsonl":
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    text = rec.get("text") or rec.get("content") or ""
                    if text:
                        self.chunks.append({"text": text, "source": path.name})
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for c in self._chunk_text(text):
                self.chunks.append({"text": c, "source": path.name})

        # 长期对话记忆，聊过的事也算事实
        epi = self.root / "data" / "memory" / "episodic.jsonl"
        if epi.exists():
            for line in epi.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("user") and rec.get("assistant"):
                    self.chunks.append(
                        {
                            "text": f"（之前聊过）用户说：{rec['user']}　角色回答：{rec['assistant']}",
                            "source": "episodic",
                        }
                    )

        if self.chunks:
            self._bm25 = BM25([c["text"] for c in self.chunks])

    # ---------------- 检索 ----------------
    def search(self, query: str, k: int | None = None) -> list[dict]:
        if not self._bm25:
            return []
        k = k or self.top_k
        raw = self._bm25.scores(query)
        ranked = sorted(range(len(raw)), key=lambda i: raw[i], reverse=True)[:k]
        out = []
        for i in ranked:
            norm = 1 - math.exp(-raw[i] / 3)  # 压到 (0,1)
            if norm < self.min_score:
                continue
            out.append({**self.chunks[i], "score": round(norm, 3)})
        return out

    def remember(self, user: str, assistant: str) -> None:
        """把这一轮写进长期记忆，下回能想起来。"""
        p = self.root / "data" / "memory" / "episodic.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"user": user, "assistant": assistant}, ensure_ascii=False) + "\n")

    @property
    def size(self) -> int:
        return len(self.chunks)
