#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""radius_sweep.py — 近邻去重半径扫描（CORPUS_AUDIT §4.7 复现用）

用法:
    python3 scripts/radius_sweep.py

做法: 把 Corpus.search 里的去重半径参数化, 其余逻辑逐行照抄生产代码。
      半径 12 必须复现基线 73.0% —— 否则说明改写引入了偏差。

⚠️ 这是只读实验脚本, 不修改 retriever.py, 也不影响生产检索。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import retriever  # noqa: E402
from retriever import Corpus  # noqa: E402

QV = ROOT / "data" / "retrieval_eval" / "questions.jsonl"
RADII = (12, 10, 8, 6, 4, 3, 2, 1)


def search_with_radius(corpus: Corpus, radius: int, world: str, query: str, k: int = 5):
    """与 Corpus.search 逐行等价, 只把去重半径换成参数。"""
    toks = retriever.tokenize_query(query)
    if not toks:
        return []
    raw = corpus.bm25.search(toks, limit=160)
    if not raw:
        return []
    top = raw[0][1] or 1.0

    out: list[dict] = []
    seen_near: list[tuple[str, int]] = []

    for i, s in raw:
        d = corpus.docs[i]
        if d["world"] != world:
            continue
        weight = 1.15
        if "？" in d["text"] or "?" in d["text"]:
            weight *= 0.55
        if "！" in d["text"] or "!" in d["text"]:
            weight *= 0.45
        if d["text"].startswith(("（", "「")):
            weight *= 0.7
        weight *= 0.85 + 0.55 * float(d.get("anchor", 0.0))
        score = s * weight

        if score < 5.0 or score < top * 0.12:
            continue

        if any(e == d["ep"] and abs(j - d["idx"]) < radius for e, j in seen_near):
            continue
        seen_near.append((d["ep"], d["idx"]))

        out.append({"ep": d["ep"], "start": d["start"], "score": round(score, 3)})
        if len(out) >= k:
            break
    return out


def load_main_questions() -> list[dict]:
    qs = [json.loads(l) for l in QV.open(encoding="utf-8") if l.strip()]
    return [q for q in qs if q["confidence"] == "high" and q.get("expected")]


def recall_at_5(corpus: Corpus, radius: int, questions: list[dict]) -> float:
    total = 0.0
    for q in questions:
        want = {(e["ep"], e["start"]) for e in q["expected"]}
        hits = search_with_radius(corpus, radius, q["world"], q["query"], k=5)
        got = {(h["ep"], h["start"]) for h in hits}
        total += sum(1 for e in want if e in got) / len(want)
    return total / len(questions)


def main() -> int:
    corpus = Corpus(ROOT / "data" / "index")
    qs = load_main_questions()
    print(f"索引 {corpus.size} 条 · main 口径有 expected 的题 {len(qs)} 条 · k=5\n")
    print(f"{'半径':>4}  {'Recall@5':>9}")
    print("-" * 16)
    base = None
    for r in RADII:
        v = recall_at_5(corpus, r, qs)
        if r == 12:
            base = v
        print(f"{r:>4}  {v:>8.1%}")

    print()
    if base is not None and abs(base - 0.7302) > 0.0005:
        print(f"❌ 等价性校验失败: 半径 12 得 {base:.1%}, 基线应为 73.0%")
        return 1
    print(f"✅ 等价性校验通过: 半径 12 = {base:.1%}, 与基线一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
