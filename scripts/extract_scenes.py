#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
extract_scenes.py — 用"称呼锚点"定位长门在场的对话场景

原理:
    字幕没有说话人标注, 但"长门""有希"出现在台词里时,
    大概率是别人在叫她和/或谈论她, 她本人极可能就在这个场景中。
    以锚点为中心取上下文窗口, 合并重叠区间, 得到"长门可能在场的场景池"。

    这比让模型从头判断 13070 条台词高效得多:
    标注范围从未知全量收敛到可人工核验的片段。

用法:
    python scripts/extract_scenes.py --parsed data/parsed --out data/scenes
"""
import argparse
import json
from pathlib import Path

ANCHOR_WORDS = ["长门", "有希", "Nagato", "Yuki"]
CTX_BEFORE = 8
CTX_AFTER = 8


def ts_key(t: str) -> float:
    try:
        h, m, s = t.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except Exception:
        return 0.0


def build_windows(rows):
    """返回 [(lo, hi), ...] 索引区间, 已合并重叠"""
    idxs = [i for i, r in enumerate(rows)
            if any(w in r["text"] for w in ANCHOR_WORDS)]
    if not idxs:
        return []
    spans = [(max(0, i - CTX_BEFORE), min(len(rows) - 1, i + CTX_AFTER)) for i in idxs]
    spans.sort()
    merged = [list(spans[0])]
    for lo, hi in spans[1:]:
        if lo <= merged[-1][1] + 4:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return [tuple(m) for m in merged]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--parsed", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default=None)
    args = ap.parse_args()

    parsed, out = Path(args.parsed), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    grand_lines = grand_scenes = 0
    for f in sorted(parsed.glob("*.dialogue.jsonl")):
        ep = f.stem.replace(".dialogue", "")
        if args.only and ep not in args.only.split(","):
            continue
        rows = [json.loads(l) for l in f.open(encoding="utf-8")]
        wins = build_windows(rows)
        covered = sum(hi - lo + 1 for lo, hi in wins)
        grand_lines += covered
        grand_scenes += len(wins)

        with (out / f"{ep}.scenes.txt").open("w", encoding="utf-8") as fh:
            fh.write(f"# {ep} | 场景片段 {len(wins)} 个 | 覆盖台词 {covered}/{len(rows)} 条\n")
            for si, (lo, hi) in enumerate(wins):
                fh.write(f"\n===== 片段 {si:03d}  {rows[lo]['start'][:7]} – {rows[hi]['end'][:7]} =====\n")
                for i in range(lo, hi + 1):
                    r = rows[i]
                    mark = "  *" if any(w in r["text"] for w in ANCHOR_WORDS) else "   "
                    fh.write(f"{mark}{i:>4} {r['start'][:7]}  {r['text']}\n")
        print(f"{ep:>4}: 片段 {len(wins):>3} / 覆盖 {covered:>4} 条 (占 {covered/max(len(rows),1)*100:.0f}%)")

    print(f"\n合计 片段 {grand_scenes} / 覆盖台词 {grand_lines}")


if __name__ == "__main__":
    main()
