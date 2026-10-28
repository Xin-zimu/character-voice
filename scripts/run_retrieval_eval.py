#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""run_retrieval_eval.py — 对真实检索实现跑固定评测集，输出可复现的基线。

**只读**：不修改语料、索引、检索代码。只读 questions*.jsonl，只调 `Corpus.search()`。

## 两种评测集格式

`--eval legacy`（默认沿用旧文件）读 `questions.jsonl`，用 `ep@start` 做 key。
该 key **不唯一**：索引里 226 组 (集, 时间码) 对应多条记录，命中判定会失真。
保留这条路径只为**复现历史基线**，不要用它出新的结论。

`--eval v2` 读 `questions_v2.jsonl`，用 `record_id`（= 索引 doc 的 `id`，
形如 `03:83`）做 key，唯一且稳定。新结论一律用这个。

指标定义（写死在代码里，避免不同人算出不同数）：

  Precision@5   top-5 里属于 expected ∪ partial 的比例，不整除时按有效条数算分母
  Recall@5      expected 中被 top-5 命中的比例
  MRR           第一条属于 expected 的结果的倒数排名（top-5 内没有则 0）
  错角色率      在「可判定」的问题上，top-5 里 forbidden 条目占比
  世界串线率    top-5 里 world != 查询世界 的比例（检索层应恒为 0）
  重复率        同一集内相邻 idx 距离 < 12 的重复（检索器自己做近邻去重，
                这里独立复核它到底有没有生效）
  空召回率      absent 类问题返回 0 条的比例（越高越好）

两种汇总口径，必须分开看：
  all       全部 60 条
  main      只算 confidence=high 的 33 条 —— **主指标看这个**
  low       只算 confidence=low 的 27 条 —— 标注本身不确定，只看趋势不做结论

用法：
    python scripts/run_retrieval_eval.py                    # 打表到 stdout
    python scripts/run_retrieval_eval.py --eval v2          # 用 record_id 口径
    python scripts/run_retrieval_eval.py --json out.json    # 存基线
    python scripts/run_retrieval_eval.py --k 5 --verbose    # 逐条打印 top-k
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from retriever import Corpus  # noqa: E402

EVAL_DIR = ROOT / "data" / "retrieval_eval"
QUESTIONS_LEGACY = EVAL_DIR / "questions.jsonl"
QUESTIONS_V2 = EVAL_DIR / "questions_v2.jsonl"
INDEX = ROOT / "data" / "index"


def key(hit: dict) -> str:
    """旧格式的 key：`ep@start`。

    保留它只为复现历史基线。它**不唯一** —— 同一 (集, 时间码) 上的多条
    记录会算出同一个 key。新代码请用 `record_id`。
    """
    return f"{hit['ep']}@{hit['start']}"


def record_id(hit: dict) -> str:
    """稳定唯一标识：索引 doc 的 `id`（`ep:idx`）。"""
    return hit["id"]


def resolve_key(mode: str):
    return record_id if mode == "v2" else key


def load_questions(mode: str) -> tuple[list[dict], str]:
    """按格式读题，并把引用统一成「用于比对的 key 集合」。"""
    path = QUESTIONS_V2 if mode == "v2" else QUESTIONS_LEGACY
    if not path.exists():
        raise SystemExit(f"找不到 {path}")
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            q = json.loads(line)
            if mode == "v2":
                for fld in ("expected", "partial", "forbidden"):
                    q[fld] = [{"record_id": r["record_id"]} for r in q.get(fld, [])]
            rows.append(q)
    return rows, str(path.relative_to(ROOT))


def ref_keys(refs: list[dict], mode: str) -> set[str]:
    if mode == "v2":
        return {r["record_id"] for r in refs if r.get("record_id")}
    return {f'{r["ep"]}@{r["start"]}' for r in refs}


def evalone(corpus: Corpus, q: dict, k: int = 5, mode: str = "legacy") -> dict:
    """跑一条问题，返回逐条指标。"""
    hits = corpus.search(q["world"], q["query"], k=k)
    kf = resolve_key(mode)
    got = [kf(h) for h in hits]
    exp = ref_keys(q["expected"], mode)
    part = ref_keys(q["partial"], mode)
    forb = ref_keys(q["forbidden"], mode)

    # --- 命中判定 ---
    hit_idx = [i for i, g in enumerate(got) if g in exp]

    recall = (len({got[i] for i in hit_idx}) / len(exp)) if exp else None
    mrr = (1.0 / (hit_idx[0] + 1)) if hit_idx else 0.0

    pool = exp | part
    if q["expect_empty"]:
        # absent 类：不按 precision 算（没有正确答案池），只看召回了多少
        precision = None
    elif pool:
        precision = len([g for g in got if g in pool]) / len(got) if got else 0.0
    else:
        precision = None

    # --- 错误类型 ---
    forbidden_hits = [g for g in got if g in forb]
    wrong_world = [h for h in hits if h["world"] != q["world"]]

    # --- 近邻重复（独立复核检索器自己的去重） ---
    near_dup = 0
    for i, a in enumerate(hits):
        for b in hits[i + 1:]:
            if a["ep"] == b["ep"]:
                ia, ib = int(a["id"].split(":")[1]), int(b["id"].split(":")[1])
                if abs(ia - ib) < 12:
                    near_dup += 1
                    break

    row = {
        "id": q["id"],
        "category": q["category"],
        "world": q["world"],
        "confidence": q.get("confidence", "high"),
        "query": q["query"],
        "n_hits": len(hits),
        "expect_empty": q["expect_empty"],
        "has_expected": bool(exp),
        "recall": recall,
        "precision": precision,
        "mrr": mrr,
        "forbidden_hits": forbidden_hits,
        "wrong_world": len(wrong_world),
        "near_dup": near_dup,
        "top": [
            {"rank": i + 1, "id": got[i] if mode == "legacy" else key(h),
             "record_id": h["id"],
             "score": h["score"],
             "world": h["world"], "text": h["text"], "ep_title": h["ep_title"],
             "start": h["start"],
             "transition": h.get("transition"),
             "judge": ("expected" if got[i] in exp else
                       "partial" if got[i] in part else
                       "forbidden" if got[i] in forb else "-")}
            for i, h in enumerate(hits)
        ],
        "note": q.get("note", ""),
    }
    return row


def summarize(rows: list[dict]) -> dict:
    """按口径汇总。分母为 0 时返回 None，不写 0.0（0 和「没有样本」必须分开）。"""

    def avg(vals):
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    recall_rows = [r for r in rows if r["has_expected"]]
    prec_rows = [r for r in rows if r["precision"] is not None]
    absent_rows = [r for r in rows if r["expect_empty"]]
    judged_rows = [r for r in rows if not r["expect_empty"] and r["has_expected"]]

    total_hits = sum(r["n_hits"] for r in rows)
    return {
        "n_questions": len(rows),
        "n_with_expected": len(recall_rows),
        "n_absent": len(absent_rows),
        "n_judged": len(judged_rows),
        "recall_at_5": avg([r["recall"] for r in recall_rows]),
        "precision_at_5": avg([r["precision"] for r in prec_rows]),
        "mrr": avg([r["mrr"] for r in recall_rows]),
        "empty_recall_rate": (
            round(sum(1 for r in absent_rows if r["n_hits"] == 0) / len(absent_rows), 4)
            if absent_rows else None),
        "wrong_world_rate": (
            round(sum(r["wrong_world"] for r in rows) / total_hits, 4)
            if total_hits else None),
        "near_dup_rate": (
            round(sum(1 for r in rows if r["near_dup"]) / len(rows), 4)
            if rows else None),
        "forbidden_rate": (
            round(sum(len(r["forbidden_hits"]) for r in judged_rows) / total_hits, 4)
            if total_hits and judged_rows else None),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--json", default=None, help="把完整基线写到这个路径")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--eval", choices=("legacy", "v2"), default="legacy",
                    help="legacy=ep@start(不唯一, 仅复现历史) / v2=record_id(默认新结论用这个)")
    args = ap.parse_args()

    if not (INDEX / "docs.jsonl").exists():
        print(f"找不到索引 {INDEX}，先跑 python build_corpus.py")
        return 1

    corpus = Corpus(INDEX)
    questions, qpath = load_questions(args.eval)
    rows = [evalone(corpus, q, k=args.k, mode=args.eval) for q in questions]

    buckets = {
        "all": rows,
        "main": [r for r in rows if r["confidence"] == "high"],
        "low": [r for r in rows if r["confidence"] != "high"],
    }

    print(f"索引：{corpus.size} 条文档 · 问题：{len(rows)} 条 · k={args.k} "
          f"· 口径：{args.eval}（{qpath}）")
    if args.eval == "legacy":
        print("警告：legacy 口径的 key 是 `ep@start`，在 226 组冲突时间码上不唯一，"
              "只用于复现历史基线")
    print(f"\n{'口径':<6} {'n':>3} {'有期望':>6} {'Recall':>7} {'Prec':>7} "
          f"{'MRR':>7} {'空召回':>7} {'串线':>7} {'近邻重复':>8}")
    print("-" * 74)
    for name, rs in buckets.items():
        s = summarize(rs)
        def f(v, pct=True):
            if v is None:
                return "  n/a"
            return f"{v*100:6.1f}%" if pct else f"{v:7.3f}"
        print(f"{name:<6} {s['n_questions']:>3} {s['n_with_expected']:>6} "
              f"{f(s['recall_at_5'])} {f(s['precision_at_5'])} "
              f"{f(s['mrr'], False)} {f(s['empty_recall_rate'])} "
              f"{f(s['wrong_world_rate'])} {f(s['near_dup_rate'])}")

    # 分类拆解（主口径）
    print("\n按类别（main 口径）：")
    cats = {}
    for r in buckets["main"]:
        cats.setdefault(r["category"], []).append(r)
    for c, rs in sorted(cats.items()):
        s = summarize(rs)
        print(f"  {c:<10} n={s['n_questions']:>2}  Recall={s['recall_at_5']}  "
              f"Prec={s['precision_at_5']}  空召回={s['empty_recall_rate']}")

    if args.verbose:
        print("\n逐条：")
        for r in rows:
            tag = "主" if r["confidence"] == "high" else "低"
            print(f"\n[{tag}] {r['id']} ({r['category']}/{r['world']}) {r['query']!r}")
            print(f"     recall={r['recall']} prec={r['precision']} mrr={r['mrr']} "
                  f"hits={r['n_hits']} 串线={r['wrong_world']} 近邻重复={r['near_dup']}")
            for t in r["top"]:
                print(f"       {t['rank']}. [{t['judge']:<9}] {t['score']:>7.2f} "
                      f"{t['id']} {t['text'][:38]!r}")

    if args.json:
        payload = {
            "index_docs": corpus.size,
            "k": args.k,
            "eval_mode": args.eval,
            "questions_file": qpath,
            "summary": {name: summarize(rs) for name, rs in buckets.items()},
            "rows": rows,
        }
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print(f"\n基线已写入 {out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
