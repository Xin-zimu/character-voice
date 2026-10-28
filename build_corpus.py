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

# 世界**恢复**锚点（客观世界口径，P0-4 改）。
#
# 语义定义 —— 这是本文件里最容易被误读的一处，写清楚：
#   本字段标注的是**客观世界状态**（画面里演的是哪个世界），
#   **不是**角色主观认知状态（阿虚什么时候意识到自己回来了）。
#   这两个时刻在《消失》里并不重合，实测相差 25 秒。
#
# 为什么锚在 2:33:01「我健康地出院了」而不是 2:33:26：
#   2:33:01 起阿虚已经身处原世界（医院走廊、能自己出院），
#   而 2:33:13「私立光阳园学院**又**变回了女校」、2:33:19「谷口**也**没有感冒」
#   这两句的「又 / 也」是对照副词——只有在「已经回到原世界、正逐项比对」的
#   语境下才成立，是客观世界已恢复的字面证据。
#   2:33:26「世界变回了原来的样子」是阿虚**逐项确认完毕后的总结句**，
#   属于主观认知的落点，不能当作客观世界在此刻才切换。
#
# 只认这一句，不做模糊匹配：这句在《消失》里出现一次且语义唯一。
# 若片源字幕有出入导致找不到，`detect_world_restore()` 返回 None，
# 行为退回到「全程 crash」——即 P0-3 之前的状态，不会比现在更差。
MOVIE_RESTORE_TITLES = ["我健康地出院了"]

# 主观确认句。**不参与 `world_at()` 计算**，只写进 meta 供下游/审计查阅，
# 用来说明「客观恢复点」与「角色确认点」是两个不同的时刻。
MOVIE_CONFIRM_TITLES = ["世界变回了原来的样子"]


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


def detect_world_restore(rows: list[dict], cut: float | None) -> float | None:
    """在《消失》里找**客观世界**恢复到主线的时刻 (秒)。

    只在切点之后找（`cut` 之前本来就是 normal，不需要恢复）。
    找不到返回 None —— 调用方据此保持「切点之后全是 crash」的旧行为。
    """
    if cut is None:
        return None
    for r in rows:
        if ts_sec(r["start"]) <= cut:
            continue
        if any(r["text"].startswith(a) for a in MOVIE_RESTORE_TITLES):
            return ts_sec(r["start"])
    return None


def detect_world_confirm(rows: list[dict], restore: float | None) -> float | None:
    """找角色**主观确认**已回到原世界的时刻 (秒)。仅记录，不参与归属判定。

    这从句是 `MOVIE_CONFIRM_TITLES` 里的总结句，落在客观恢复点之后。
    两个时刻之间的记录客观世界已恢复、但角色正在逐项确认，
    这批记录会被打上 `transition` 标记（见 `is_transition()`）。
    """
    if restore is None:
        return None
    for r in rows:
        if ts_sec(r["start"]) < restore:
            continue
        if any(r["text"].startswith(a) for a in MOVIE_CONFIRM_TITLES):
            return ts_sec(r["start"])
    return None


# ---------------------------------------------------------------
# 过渡段标记（P0-4）。
#
# `transition` 是一个**独立字段**，与 `world`、`speaker` 正交，不参与任何
# 既有判定：
#   - `world`  回答「客观世界是哪个」；
#   - `speaker` 回答「这句话谁说的」（本文件不产出该字段）；
#   - `transition` 回答「这条记录的归属是否有争议 / 是否处在切换瞬间」。
#
# 目前唯一来源是《消失》的客观恢复点与主观确认点之间那一段：
# 客观世界已在 2:33:01 恢复，但阿虚到 2:33:26 才确认，中间 6 条记录
# 无论标 normal 还是标 crash 都能找到理由（46 条方案 vs 40 条方案的
# 分歧点就在这里）。标 transition 让下游能自行取舍，而不是替下游决定。
#
# 下游约定（写进 docs/PRECISION_REVIEW.md 与新建的 docs/WORLD_BOUNDARY.md）：
#   - **事实检索**：不排除 transition 记录，但返回时需带上该标记，
#     让消费方知道这条证据的归属有争议；
#   - **角色表达参考**：transition 记录不得因其存在就被认定为长门台词；
#     说话者未知就是未知，`transition` 不提供任何说话者信息。
# ---------------------------------------------------------------
TRANSITION_REASON_SPAN = "restore_confirm_gap"


def is_transition(t: float, restore: float | None, confirm: float | None) -> str | None:
    """判断某条记录是否落在「客观恢复点 → 主观确认点」之间。

    返回标记原因字符串，不属于过渡段则返回 None。
    `confirm` 为 None（片源找不到确认句）时退化为单点恢复，不产生过渡段。
    """
    if restore is None or confirm is None:
        return None
    if restore <= t < confirm:
        return TRANSITION_REASON_SPAN
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
    movie_restore = None
    movie_confirm = None
    world_counts = Counter()
    transition_counts = Counter()

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

            # 每一行「属于哪个世界」只在这一个地方算。
            # 记录归属和 ctx_worlds 都走 `world_at()`，避免两处规则不一致
            # （旧实现是两处各写一遍 `>= movie_cut`，加恢复点时必须同改）。
            if is_movie and movie_cut is not None:
                movie_restore = detect_world_restore(rows, movie_cut)
                movie_confirm = detect_world_confirm(rows, movie_restore)

            def world_at(t: float) -> str:
                if not (is_movie and movie_cut is not None):
                    return WORLD_NORMAL
                if t < movie_cut:
                    return WORLD_NORMAL
                # 切点之后：恢复点之前是 crash，恢复点之后回到 normal。
                if movie_restore is not None and t >= movie_restore:
                    return WORLD_NORMAL
                return WORLD_CRASH

            for i, r in enumerate(rows):
                t_sec = ts_sec(r["start"])
                world = world_at(t_sec)
                # 过渡标记：独立于 world / speaker，只描述「归属有争议」。
                transition = is_transition(
                    t_sec if is_movie else None, movie_restore, movie_confirm
                )

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
                        ctx_worlds.append(world_at(ts_sec(rows[j]["start"])))
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
                    # None = 非过渡记录；字符串 = 过渡原因。
                    # 注意这里存的是 str|None 而不是 bool：下游要区分
                    # 「不是过渡段」和「是过渡段，因为恢复/确认之间」，
                    # 未来还可能有别的过渡原因（如世界切点附近）。
                    "transition": transition,
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
                transition_counts[transition or "none"] += 1
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
        # 世界**客观**恢复到主线的时刻（P0-4 起锚在 2:33:01）。
        # None 表示没找到恢复点，此时切点之后全部按 crash 处理。
        # `movie_restore_basis` 明确口径，防止下游误读成「角色认知时刻」。
        "movie_restore": ts_show_pos(movie_restore) if movie_restore else None,
        "movie_restore_sec": movie_restore,
        "movie_restore_basis": "objective_world",
        "movie_restore_anchor": MOVIE_RESTORE_TITLES[0] if movie_restore else None,
        # 角色**主观**确认时刻（2:33:26「世界变回了原来的样子」）。
        # 仅作记录：不参与 world 判定，用于界定 transition 段的右端。
        "movie_confirm": ts_show_pos(movie_confirm) if movie_confirm else None,
        "movie_confirm_sec": movie_confirm,
        "movie_confirm_anchor": MOVIE_CONFIRM_TITLES[0] if movie_confirm else None,
        # 过渡段统计：transition 字段非空的记录数（当前 = 恢复点→确认点之间）。
        "transition": {
            "field": "transition",
            "independent_of": ["world", "speaker"],
            "total": total_docs - transition_counts["none"],
            "by_reason": {
                k: v for k, v in transition_counts.items() if k != "none"
            },
        },
    }
    (out / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n合计 {total_rows} 条对白, 建索引 {total_docs} 条 -> {out}")
    print(f"主线世界 {world_counts[WORLD_NORMAL]} 条 / 崩溃世界 {world_counts[WORLD_CRASH]} 条")
    if movie_cut:
        print(f"《消失》世界线切点: {ts_show_pos(movie_cut)}")
    if movie_restore:
        print(f"《消失》客观世界恢复点: {ts_show_pos(movie_restore)}  ({MOVIE_RESTORE_TITLES[0]})")
    if movie_confirm:
        print(f"《消失》主观确认时刻:   {ts_show_pos(movie_confirm)}  ({MOVIE_CONFIRM_TITLES[0]})")
    n_tr = total_docs - transition_counts["none"]
    if n_tr:
        print(f"过渡记录 (transition 非空): {n_tr} 条")


def ts_show_pos(sec: float) -> str:
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = int(sec % 60)
    return f"{h}:{m:02d}:{s:02d}"


if __name__ == "__main__":
    main()
