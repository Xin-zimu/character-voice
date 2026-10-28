#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""migrate_eval_record_id.py — 把评测集的记录引用迁移到稳定唯一 `record_id`。

## 为什么需要

旧格式用 `{"ep": "03", "start": "0:05:00"}` 引用一条记录，评测脚本再用
`key(hit) = f"{ep}@{start}"` 反查。但索引里 **(集, 时间码) 并不唯一**：

    13070 条记录里有 226 组 (ep, start) 冲突，涉及 462 条记录

于是 `key()` 落到冲突组上时，两条不同的记录会算出同一个 key，
命中判定、去重、forbidden 判定全部失真。实测评测集 67 条引用里
有 2 条踩到这个坑（`B05` 与 `A11` 的 `03@0:05:00`）。

## 旧格式的落点（脚本里的映射方式，写死不改语义）

- 该 (ep, start) **只对应 1 条**记录 → 直接映射到它的 `id`。
- 该 (ep, start) 对应 **多条**记录 → **不自动判断**，标 `ambiguous`，
  等人工在 `--decisions` 里指定。未指定时迁移脚本报错退出，不产出新文件。

这样做的原因和用户的要求一致：不得根据关键词匹配自动把候选判为正确，
两处歧义必须由人确认。机器能确定的 65 处静默迁移，机器不能确定的 2 处停下来问。

## 产物

    data/retrieval_eval/questions.jsonl        旧格式，原样保留，不覆盖
    data/retrieval_eval/questions_v2.jsonl     新格式，带 record_id
    data/retrieval_eval/migration_20261026.json  迁移报告（含逐条映射与差异）

用法：
    python scripts/migrate_eval_record_id.py --check        # 只体检，不写文件
    python scripts/migrate_eval_record_id.py --decisions decisions.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "data" / "index"
EVAL = ROOT / "data" / "retrieval_eval"
SRC = EVAL / "questions.jsonl"
DST = EVAL / "questions_v2.jsonl"
REPORT = EVAL / "migration_20261026.json"

REF_FIELDS = ("expected", "partial", "forbidden")


def md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


def load_index() -> list[dict]:
    docs = []
    with (INDEX / "docs.jsonl").open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                docs.append(json.loads(line))
    return docs


def load_questions(p: Path) -> list[dict]:
    rows = []
    with p.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def build_lookup(docs: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """(ep, start) -> 该时间码上的全部记录（保持索引内顺序）。"""
    by = defaultdict(list)
    for d in docs:
        by[(d["ep"], d["start"])].append(d)
    return by


def resolve(ref: dict, by, decisions: dict, where: str):
    """把一条旧引用解析成 record_id。

    返回 (record_id | None, info)。record_id 为 None 表示需要人工决策。
    """
    ep, start = ref["ep"], ref["start"]
    cands = by.get((ep, start), [])
    if not cands:
        return None, {"status": "missing", "cands": []}
    if len(cands) == 1:
        return cands[0]["id"], {"status": "unique", "cands": [cands[0]["id"]]}
    # 冲突：只有写在 decisions 里的才认
    pick = decisions.get(where)
    if pick is None:
        return None, {"status": "ambiguous",
                      "cands": [{"record_id": c["id"], "text": c["text"]}
                                for c in cands]}
    allowed = {c["id"] for c in cands}
    if pick not in allowed:
        raise SystemExit(
            f"{where}: 决策 {pick!r} 不在候选 {sorted(allowed)} 里，拒绝写文件")
    return pick, {"status": "ambiguous_resolved", "cands": [c["id"] for c in cands]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(SRC))
    ap.add_argument("--dst", default=str(DST))
    ap.add_argument("--report", default=str(REPORT))
    ap.add_argument("--decisions", default=None,
                    help='JSON 文件，形如 {"B05.expected[0]": "03:83"}')
    ap.add_argument("--check", action="store_true", help="只体检，不写任何文件")
    args = ap.parse_args()

    src = Path(args.src)
    if not src.exists():
        print(f"找不到 {src}")
        return 1

    docs = load_index()
    by = build_lookup(docs)
    questions = load_questions(src)
    decisions = {}
    if args.decisions:
        decisions = json.loads(Path(args.decisions).read_text(encoding="utf-8"))

    # 索引侧的全量唯一性体检
    dup_groups = {k: v for k, v in by.items() if len(v) > 1}

    out_rows = []
    mappings = []      # 逐条引用的迁移记录
    unresolved = []

    for q in questions:
        new_q = dict(q)
        ref_map = {}
        for fld in REF_FIELDS:
            new_refs = []
            for i, ref in enumerate(q.get(fld, [])):
                where = f"{q['id']}.{fld}[{i}]"
                rid, info = resolve(ref, by, decisions, where)
                if rid is None:
                    unresolved.append({"where": where, "ep": ref["ep"],
                                       "start": ref["start"],
                                       "status": info["status"],
                                       "cands": info["cands"]})
                    new_refs.append({"record_id": None})
                else:
                    new_refs.append({"record_id": rid})
                mappings.append({
                    "question": q["id"], "field": fld, "index": i, "where": where,
                    "old": {"ep": ref["ep"], "start": ref["start"]},
                    "record_id": rid, "status": info["status"],
                    "candidates": info["cands"],
                })
                ref_map[f"{fld}[{i}]"] = rid
            new_q[fld] = new_refs
        new_q["_legacy_id"] = q["id"]          # 便于与旧基线对照
        new_q["_ref_map"] = ref_map
        out_rows.append(new_q)

    # ---- 体检结论 ----
    print(f"索引 {len(docs)} 条 · (ep,start) 冲突组 {len(dup_groups)} 组 "
          f"（涉及 {sum(len(v) for v in dup_groups.values())} 条）")
    print(f"评测 {len(questions)} 题 · 引用 {len(mappings)} 条")

    stat = defaultdict(int)
    for m in mappings:
        stat[m["status"]] += 1
    for k in ("unique", "ambiguous", "ambiguous_resolved", "missing"):
        if stat[k]:
            print(f"  {k:<20} {stat[k]} 条")

    if unresolved:
        print("\n需要人工决策的引用（脚本不替你判断）：")
        for u in unresolved:
            print(f"  {u['where']}  {u['ep']}@{u['start']}  status={u['status']}")
            for c in u["cands"]:
                if isinstance(c, dict):
                    print(f"      候选 {c['record_id']:<10} {c['text'][:40]}")
                else:
                    print(f"      候选 {c}")
        print("\n用 --decisions 指定，例如：")
        print('  {"B05.expected[0]": "03:83", "A11.expected[2]": "03:84"}')
        if not args.check:
            print("\n未写文件（存在未决策的歧义引用）。")
        return 2

    if args.check:
        print("\n体检通过（--check 模式，未写文件）。")
        return 0

    # ---- 唯一性断言：新格式必须能不重复地指到具体记录 ----
    known = {d["id"] for d in docs}
    all_ids = [r["record_id"] for q in out_rows for fld0 in REF_FIELDS
               for r in q[fld0] if r["record_id"]]
    bad = [rid for rid in all_ids if rid not in known]
    if bad:
        raise SystemExit(f"断言失败：{bad[:5]} 不在索引里")
    for q in out_rows:
        for fld in REF_FIELDS:
            ids = [r["record_id"] for r in q[fld] if r["record_id"]]
            if len(ids) != len(set(ids)):
                raise SystemExit(f"断言失败：{q['id']}.{fld} 内部有重复 record_id")
    # 每一题内部 id 唯一
    qids = [q["id"] for q in out_rows]
    assert len(qids) == len(set(qids)), "题号重复"

    # ---- 写新格式（旧文件不动）----
    dst = Path(args.dst)
    src_md5 = md5(src)
    with dst.open("w", encoding="utf-8") as fh:
        for q in out_rows:
            core = {k: v for k, v in q.items() if not k.startswith("_")}
            fh.write(json.dumps(core, ensure_ascii=False) + "\n")

    report = {
        "generated_from": str(src.relative_to(ROOT)),
        "source_md5": src_md5,
        "source_md5_after": md5(src),          # 证明源文件未被改动
        "source_untouched": src_md5 == md5(src),
        "index_docs": len(docs),
        "index_dup_groups": len(dup_groups),
        "questions": len(questions),
        "refs_total": len(mappings),
        "refs_by_status": dict(stat),
        "output": str(dst.relative_to(ROOT)),
        "output_md5": md5(dst),
        "decisions": decisions,
        "mappings": mappings,
    }
    Path(args.report).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n新格式已写入 {dst}")
    print(f"迁移报告已写入 {args.report}")
    print(f"源文件未改动：{report['source_untouched']}（md5 {src_md5[:8]}…）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
