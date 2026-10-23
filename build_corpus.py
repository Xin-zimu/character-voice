#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
build_corpus.py — 把全量原作语料建成可检索的剧情库

不剪集。13070 条对白全进, 包括《消失》后半段那个害羞的长门 ——
她不是「污染样本」, 她是另一个世界线的她, 本来就该在库里, 只是要标上世界。

产出 (data/index/):
    docs.jsonl       检索单元: 每条台词 + 上下文窗口 (前后各 CONTEXT 条)
    meta.json        统计信息、世界边界、集数目录

用法:
    python build_corpus.py
    python build_corpus.py --context 6
"""
from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PARSED = ROOT / "data" / "parsed"
OUT = ROOT / "data" / "index"

# ---------------------------------------------------------------
# 世界线划分。
# 主世界: 正常的北高, 有春日、有 SOS 团, 长门是终端界面。
# 崩溃世界: 12 月 18 日之后, 春日消失, 长门变成普通害羞少女。
#
# 《消失》的切点靠剧情锚点定位, 不写死 —— 不同片源时间轴会差。
# 锚点: 阿虚发现自己「不认识的长门」在图书馆 / 「这个房间和我在5月」。
# ---------------------------------------------------------------
WORLD_NORMAL = "normal"
WORLD_CRASH = "crash"

MOVIE_ANCHOR_TITLES = ["这样的选手变更是谁安排的？", "这个房间和我在5月"]


def ts_sec(t: str) -> float:
    try:
        h, m, s = t.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except Exception:
        return 0.0


def ts_show(t: str) -> str:
    return t[:7] if len(t) >= 7 else t


def norm(s: str) -> str:
    return re.sub(r"[\s\u3000…。．\.]", "", s)


def load_dialogue(p: Path) -> list[dict]:
    rows = []
    with p.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def detect_movie_cut(rows: list[dict]) -> float | None:
    """在《消失》里找世界线切换的时刻 (秒)。找不到返回 None。"""
    for i, r in enumerate(rows):
        if any(r["text"].startswith(a) for a in MOVIE_ANCHOR_TITLES):
            return ts_sec(rows[i]["start"])
    return None


# ---------------------------------------------------------------
# 长门是否在这段对话里「在场」的软信号。
# 字幕没有说话人标注, 这里不判断「谁说的」, 只给检索加权。
# ---------------------------------------------------------------
ANCHOR_RE = re.compile(r"(长门|有希|Nagato|Yuki|信息统合|思念体|终端界面)")


def anchor_score(window_text: str) -> float:
    n = len(ANCHOR_RE.findall(window_text))
    return min(1.0, n / 3.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--context", type=int, default=5, help="每侧上下文台词数")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    files = sorted(PARSED.glob("*.dialogue.jsonl"),
                   key=lambda f: (f.stem.startswith("消失"), f.stem))
    total_rows = 0
    total_docs = 0
    episodes = []
    movie_cut = None
    world_counts = Counter()

    with (out / "docs.jsonl").open("w", encoding="utf-8") as fh:
        for f in files:
            ep = f.stem.replace(".dialogue", "")
            rows = load_dialogue(f)
            if not rows:
                continue
            is_movie = ep == "消失"
            if is_movie:
                movie_cut = detect_movie_cut(rows)

            n = len(rows)
            ep_start = ts_show(rows[0]["start"])
            ep_end = ts_show(rows[-1]["end"])
            episodes.append({
                "ep": ep,
                "title": "凉宫春日的消失（剧场版）" if is_movie else f"第 {ep} 集",
                "lines": n,
                "start": ep_start,
                "end": ep_end,
            })

            for i, r in enumerate(rows):
                t_sec = ts_sec(r["start"])
                if is_movie and movie_cut is not None:
                    world = WORLD_CRASH if t_sec >= movie_cut else WORLD_NORMAL
                else:
                    world = WORLD_NORMAL

                lo = max(0, i - args.context)
                hi = min(n - 1, i + args.context)
                ctx_lines = []
                ctx_worlds = []
                for j in range(lo, hi + 1):
                    mark = "▶" if j == i else " "
                    ctx_lines.append(
                        f"{mark}{ts_show(rows[j]['start'])}  {rows[j]['text'].replace(chr(10), ' ')}"
                    )
                    # 逐行记世界归属（R-01 / WORLD-001-B）。
                    #
                    # 为什么要在**建索引时**标，而不是渲染时再算：
                    # 上下文窗口天然会跨过世界切点——《消失》切点附近的 10 个
                    # 窗口、30 行会横跨两个世界。渲染层只有拿到「这一行属于谁」
                    # 才能逐行裁掉；靠事后按时间码反查，一来要再扫全表，
                    # 二来一旦切点规则变了（WORLD-002-B 审计之后很可能变），
                    # 渲染层就会拿到旧结论。归属跟着数据走，规则只在一处。
                    if is_movie and movie_cut is not None:
                        ctx_worlds.append(
                            WORLD_CRASH if ts_sec(rows[j]["start"]) >= movie_cut
                            else WORLD_NORMAL)
                    else:
                        ctx_worlds.append(WORLD_NORMAL)
                ctx = "\n".join(ctx_lines)

                # 检索单元: 上下文窗口 + 本句。
                # 本句重复两遍是为了让 BM25 更偏向「这一句本身」而非上下文。
                #
                # **检索文本也按世界裁**：只留本世界的上下文行。
                # 否则另一世界的措辞会把记录「拽」进结果（检索层看到的
                # 是污染过的文本），那是渲染层再裁也补不回来的。
                keep = [k for k, w in enumerate(ctx_worlds) if w == world]
                bm25_ctx = "\n".join(ctx_lines[k] for k in keep)
                text = f"{bm25_ctx}\n{ctx_lines[i - lo].lstrip('▶ ').strip()}"
                doc = {
                    "id": f"{ep}:{i}",
                    "ep": ep,
                    "idx": i,
                    "start": ts_show(r["start"]),
                    "end": ts_show(r["end"]),
                    "t": round(t_sec, 1),
                    "world": world,
                    "text": r["text"],
                    # ctx 保留**全部**原文（原文一条不删，DEC-002），
                    # 只加一行 ctx_worlds 说明每行归谁。渲染时据此裁剪。
                    "ctx": ctx,
                    "ctx_worlds": ctx_worlds,
                    "anchor": round(anchor_score(ctx), 2),
                    "bm25": text,
                }
                fh.write(json.dumps(doc, ensure_ascii=False) + "\n")
                total_docs += 1
                world_counts[world] += 1
            total_rows += n
            print(f"{ep:>4}: {n:>5} 条")

    meta = {
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "lines": total_rows,
        "docs": total_docs,
        "context": args.context,
        "episodes": episodes,
        "worlds": {
            WORLD_NORMAL: {"name": "主线世界", "lines": world_counts[WORLD_NORMAL]},
            WORLD_CRASH: {"name": "崩溃世界", "lines": world_counts[WORLD_CRASH]},
        },
        "movie_cut": ts_show_pos(movie_cut) if movie_cut else None,
        "movie_cut_sec": movie_cut,
    }
    (out / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n合计 {total_rows} 条对白, 建索引 {total_docs} 条 -> {out}")
    print(f"主线世界 {world_counts[WORLD_NORMAL]} 条 / 崩溃世界 {world_counts[WORLD_CRASH]} 条")
    if movie_cut:
        print(f"《消失》世界线切点: {ts_show_pos(movie_cut)}")


def ts_show_pos(sec: float) -> str:
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = int(sec % 60)
    return f"{h}:{m:02d}:{s:02d}"


if __name__ == "__main__":
    main()
