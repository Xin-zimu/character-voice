#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
!!! DEPRECATED — 已废弃，结论为"方法论错误"，不要使用，不要引用其产出 !!!

废弃日期: 2026-10-21
废弃原因(实测):
    本脚本假设"长门的台词可以用字面特征识别"。这个假设被数据否证。
    人工抽查候选池 1118 条, 精度约 15%; 且错误不是随机分布, 而是系统性的。

    错误模式一: 奖励"短句"→ 同时命中三类人
        - 阿虚的内心独白 (第一人称叙述者, 同样短而平淡)
        - 朝比奈实玖瑠 ("对不起" / "呃…" / "我、我觉得" —— 短且软)
        - 春日的祈使短句 ("我才不要是也" / "哟 阿虚")
    长门只占其中一小部分。s>=6 最高分段 359 条里, "阿虚…" 出现 20+ 次,
    "对不起" "朝比奈学姐" "凉宫同学" 遍地 —— 全是在叫别人或被别人叫。

    错误模式二: 信号方向相反
        长门 = 短 + 平 + 无犹豫
        朝比奈 = 短 + 软 + 充满犹豫
        本脚本只要"短"就给分, 恰好把两者混为一谈。
        "呃…还是不明白" / "其实不大明白" 这类典型朝比奈台词拿到了 s6 高分。

    结论: 这不是"信噪比偏低可以再用", 而是打分函数的信号定义本身错了。
    产出 data/_deprecated/candidates_heuristic/ 已整体隔离, 禁止当作 few-shot
    或训练语料。保留代码仅为记录失败路径。

正确路径: extract_scenes.py (称呼锚点定位"她在场的片段") → annotate.py (LLM 标注
说话人) → 人工复核 → 才可进入 data/labeled/ 成为语料。

为什么需要它:
    华盟这套 .ass 的 Actor 字段全是 NTP, 没有任何说话人标注。
    13070 条对白里要找出长门的那几百句, 纯靠人看不现实。

思路:
    长门的台词在文本层面有极强的统计特征 (远比一般角色可识别):
      - 极短: 原作里大量是 2-6 字的应答
      - 句末几乎只有 。 和 ……, 极少 ！和 ?
      - 多用书面语/陈述句, 少用语气助词 (啊/呢/吧/嘛)
      - 自称"我"的频率远低于常人
    先用这些特征打一个分, 得到"高置信候选池",
    再交给 LLM 或人工在这个小池子里精判 — 召回不全, 但精度可控。

用法(不要用):
    python scripts/_deprecated_heuristic_filter.py --parsed data/parsed --out /tmp/x --min-score 5
"""
import argparse
import json
import re
import sys
from pathlib import Path

# 强特征: 长门的高频固定表达
STRONG_PATTERNS = [
    "是的", "不", "不知道", "没有", "了解", "明白", "大概", "应该",
    "不存在", "正在进行", "无法", "可以", "没有必要", "不需要",
    "情报", "资讯", "信息", "空间", "存在", "修正", "程序",
    "凉宫同学", "阿虚", "朝比奈", "古泉",
]
# 弱特征: 普通人常用、长门不用的语气词
CASUAL_PARTICLES = ["啊", "呀", "啦", "嘛", "哦", "咧", "捏", "哈", "呗"]

END_PUNCT = "。！？…"


def score(text: str) -> tuple[int, list[str]]:
    t = text.strip()
    n = len(t)
    s, why = 0, []

    if n <= 6:
        s += 4; why.append("极短")
    elif n <= 12:
        s += 3; why.append("短")
    elif n <= 18:
        s += 1
    else:
        s -= 2; why.append("过长")

    if "！" in t or "!" in t:
        s -= 4; why.append("感叹号")
    if "？" in t or "?" in t:
        s -= 1
    if "…" in t:
        s += 1; why.append("省略号")
    if t.endswith("。"):
        s += 1
    if any(p in t for p in CASUAL_PARTICLES):
        s -= 4; why.append("口语助词")
    if "我" in t:
        s -= 1
    if re.search(r"(吗|呢|吧)[。…]?$", t):
        s -= 2; why.append("反问/商量语气")
    if any(k in t for k in STRONG_PATTERNS):
        s += 2; why.append("强特征词")

    return s, why


def main():
    sys.stderr.write(
        "\n[DEPRECATED] 本脚本的筛选思路已被实测否证(精度约15%, 且错误系统性偏向\n"
        "阿虚旁白/朝比奈/春日的短句)。产出不得用作语料。仅作失败记录保留。\n\n"
    )

    ap = argparse.ArgumentParser()
    ap.add_argument("--parsed", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--min-score", type=int, default=5)
    ap.add_argument("--only", default=None)
    args = ap.parse_args()

    parsed, out = Path(args.parsed), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    agg = []
    for f in sorted(parsed.glob("*.dialogue.jsonl")):
        ep = f.stem.replace(".dialogue", "")
        if args.only and ep not in args.only.split(","):
            continue
        rows = [json.loads(l) for l in f.open(encoding="utf-8")]
        cands = []
        for r in rows:
            s, why = score(r["text"])
            if s >= args.min_score:
                cands.append({**r, "score": s, "why": why})
        agg.extend(cands)
        # 每集一份候选, 便于逐集人工/LLM 复核
        with (out / f"{ep}.cand.jsonl").open("w", encoding="utf-8") as fh:
            for c in sorted(cands, key=lambda x: -x["score"]):
                fh.write(json.dumps(c, ensure_ascii=False) + "\n")
        print(f"{ep:>4}: 对白 {len(rows):>4} -> 候选 {len(cands):>3} ({len(cands)/max(len(rows),1)*100:.0f}%)")

    with (out / "all.cand.jsonl").open("w", encoding="utf-8") as fh:
        for c in agg:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")
    print(f"\n候选池合计 {len(agg)} 条 -> {out/'all.cand.jsonl'}")


if __name__ == "__main__":
    main()
