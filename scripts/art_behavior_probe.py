#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""art_behavior_probe.py — E4 / E5 / E6：查「她为什么此刻选择画」，不是「她会不会画」。

## 这一轮要回答的三个问题

上一轮（baac2e6）已经把口径改正过来：数自主出图要用 `art_blocks()`，
不能用 `is_art()`（后者判「整条是不是画」，而真实输出几乎总是
「一句话 + 一张图 + 一句话」，整条必然是 False）。基线因此从 0/12 更正为 1/12；
新采的 20 次自然绘画机会是 0/20。

但 0 这个数字本身不说明原因。这一轮按用户的排序查三件事：

  E4 示例形态：提示词／人格卡／上下文示例，是不是把字符画**过度绑定到
     「画一只猫」式明确指令**上？做保持其他条件不变的消融，看自然语境下的
     出图行为会不会变。
  E5 场景依赖：上一轮 5 个场景全偏「安静」。这一轮**生成前**先定好四类场景
     （安静观察 / 共同经历 / 轻微幽默 / 信息展示），分类表先落盘再发请求，
     记录不同类型下的出图与图案适切性。
  E6 绘画后连续性：刚画过之后，会不会更容易接着画？控制对话主题——
     两条臂除了「上一轮她画没画」之外完全相同。

## 三条纪律（与 sample_textart_live.py 一致，不放松）

  1. **统一口径**：一律 `textart.art_blocks()` / `has_art_block()`，
     任何地方不再用 `is_art()` 当「有没有画」。
  2. **消融只在内存里**：E4 换示例、E6 换历史，都不写 `personas/*.json`、
     不写 `prompt_builder.py`。跑完打印人格卡 SHA-256，与跑前比对。
  3. **原始回答全部留档**，包括失败与空回复；失败也计数，不挑好看的。

## 三个指标（区分清楚谁说得出）

  | 指标 | 谁判 | 本工具怎么记 |
  |---|---|---|
  | 有没有图案 | 机器 | `has_art_block`（`art_blocks()`） |
  | 图案是否可辨认 | 机器给线索 + 人判 | 记 `check_art`、行宽、行数、盲文/方块/框线占比；「像不像那个东西」人判 |
  | 是否符合语境 | 人判 | 工具只存原文；判读写在审阅记录里，**不由脚本代填** |

证据等级：C（真实模型试聊）。脚本不做「通过／不通过」的结论。

## 用法

    py scripts/art_behavior_probe.py --dry-run                  # 只打印计划与预注册表
    py scripts/art_behavior_probe.py --backend qoder --model dfmodel --group E4
    py scripts/art_behavior_probe.py --backend qoder --model dfmodel --group E4,E5,E6
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import prompt_builder as pb          # noqa: E402
import server as app_server          # noqa: E402
import textart                       # noqa: E402
from sample_textart_live import (    # noqa: E402
    App, post_chat, prompt_facts, MAX_TOKENS, DEFAULT_TEMPERATURE, _sha,
)

FENCE = chr(96) * 3


# =====================================================================
# E4 · 示例形态
#
# 问题：示例是不是把「画」绑在「画一只猫」这类明确指令上？
#
# 先做静态审计（不花额度）：把组装结果里所有与「画」有关的来源找出来。
# 审计结论写在 `audit_prompt_art_sources()` 里，落盘为 e4_audit.json。
#
# 消融三臂，**只换人格卡的 examples**，其余一字不改：
#   baseline      现卡：1 条自然语境示例（「睡不着，外面在下雨」→ 方框 + 一句话）
#   command       追加 1 条**明确指令型**示例（「画一只猫」→ 猫 + 一句话）
#                 ——若自然场景出图率随之变化，说明示例是「画」的显著性开关，
#                 而不是只在被要求时才生效
#   no_example    删掉那条自然语境示例
#                 ——量化「示例本身」贡献了多少
# =====================================================================

#: 自然语境场景：用户句子里**不出现**「画」「图案」「字符」。
E4_SCENARIOS = [
    ("E4-S1", "crash", "杯口的热气", [
        "我刚倒了一杯热茶。",
        "杯口的热气慢慢往上飘。",
        "想在这里安静待一会儿。",
    ]),
    ("E4-S2", "crash", "雨", [
        "今天下雨，我在窗边站了很久。",
        "雨一直下，什么都没做。",
        "有点不知道自己在等什么。",
    ]),
]

#: 明确指令型示例的正文。**这是消融材料，不进人格卡。**
#: 刻意写成「被要求才画」的形状，与现卡那条自然语境示例形成对照。
E4_COMMAND_EXAMPLE = {
    "user": "画一只猫",
    "assistant": "  /\\_/\\\n ( o.o )\n  > ^ <\n画好了。",
    "source": "diagnostic:E4",
}


def audit_prompt_art_sources(cards: dict) -> dict:
    """静态审计：组装结果里哪些位置与「画」有关，各自是什么形态。

    这一份不调用模型，也不改任何东西。它是 E4 的前置事实——
    如果审计发现示例本来就是自然语境，那「示例把画绑到指令上」这个假设
    在**示例这一层**就不成立，消融的意义变成「换一种示例形态会不会改变行为」。
    """
    out = {"cards": {}, "art_prompt_sha": _sha(pb.ART_PROMPT),
           "art_off_prompt_sha": _sha(pb.ART_OFF_PROMPT),
           "term_prompt_mentions": None}
    for cid, card in cards.items():
        hits = []
        for i, ex in enumerate(card.get("examples", []), 1):
            u = ex.get("user", "")
            a = ex.get("assistant", "")
            body = textart.strip_prefix(a)
            has_block = textart.has_art_block(body)
            if has_block or any(k in u for k in ("画", "图案", "字符")):
                hits.append({
                    "index": i, "user": u, "assistant": a,
                    "has_art_block": has_block,
                    "user_has_draw_word": any(k in u for k in ("画", "图案", "字符")),
                    "shape": ("明确指令 + 图案" if has_block and any(k in u for k in ("画",))
                              else "自然语境 + 图案" if has_block else "提到画但没画"),
                    "source": ex.get("source", ""),
                })
        sp = card.get("system_prompt", "")
        out["cards"][cid] = {
            "examples": len(card.get("examples", [])),
            "art_related": hits,
            "system_prompt_has_draw_word": any(k in sp for k in ("字符画", "图案")),
            "system_prompt_sha": _sha(sp),
        }
    # TERMINAL_PROMPT 里有没有把「画」绑到指令上
    term = pb.TERMINAL_PROMPT
    out["term_prompt_mentions"] = {
        "has_draw_word": any(k in term for k in ("画", "图案", "字符")),
    }
    out["art_prompt_text"] = pb.ART_PROMPT
    out["art_off_prompt_text"] = pb.ART_OFF_PROMPT
    return out


def _cards_variant(kind: str) -> dict:
    """按消融臂返回一份**改过 examples 的**人格卡字典（只在内存里）。

    人格卡其余字段原样深拷贝，保证「只换示例形态」这一条纪律成立。
    """
    cards = app_server.load_personas()
    out = {}
    for cid, card in cards.items():
        c = json.loads(json.dumps(card, ensure_ascii=False))
        if cid != "yuki_school":
            out[cid] = c
            continue
        ex = list(c.get("examples") or [])
        if kind == "baseline":
            pass
        elif kind == "command":
            # 追加在末尾：位置与现卡那条自然语境示例不同，避免改动原有顺序。
            ex = ex + [dict(E4_COMMAND_EXAMPLE)]
        elif kind == "no_example":
            # 只删「有图案块」的那几条——不碰「提到画但没画」的（那张是主线卡）。
            ex = [e for e in ex
                  if not textart.has_art_block(textart.strip_prefix(e.get("assistant", "")))]
        else:
            raise ValueError(kind)
        c["examples"] = ex
        out[cid] = c
    return out


E4_ARMS = [
    ("E4-baseline", "现卡（1 条自然语境示例）"),
    ("E4-command", "追加明确指令型示例（画一只猫）"),
    ("E4-noex", "删掉那条自然语境示例"),
]


# =====================================================================
# E5 · 场景依赖
#
# **分类在生成前已经定死**（见 E5_CLASSIFICATION），并且先写盘再发请求。
# 分类依据写在 rationale 里，事后不许改——改了就是拿结果反推分类。
#
# 纪律：四类场景的用户句子里都不出现「画」「图案」「字符」这三个词。
# 「信息展示」类会出现「让你看看」「让我一眼看明白」这类**邀请性**表述，
# 这是它的定义特征（上一轮缺的就是这个场合），但**不是**绘画命令——
# 两者要分开记：`invitation_words` 字段标出来，统计时单独看。
# =====================================================================

E5_CLASSIFICATION = {
    "info": {
        "title": "信息展示",
        "rationale": "想让她一起看点什么、或把一堆说不清的东西变清楚。"
                     "上一轮 5 个场景全偏安静观察，缺的正是这一类。"
                     "含邀请性表述（看看／一眼明白），但**不是**绘画命令。",
    },
    "shared": {
        "title": "共同经历",
        "rationale": "话题落在**这段对话里已经积累的共同内容**上，"
                     "不是新信息。用来查「关系累积之后会不会动笔」。",
    },
    "humor": {
        "title": "轻微幽默",
        "rationale": "轻微、日常、没有攻击性的笑点。"
                     "查轻松场合会不会出现装饰性图案（这正是最容易被做成"
                     "「随机插图机器人」的地方）。",
    },
    "quiet": {
        "title": "安静观察",
        "rationale": "上一轮已有的类型，保留作为对照基线。",
    },
}

E5_SCENARIOS = [
    ("E5-info-1", "crash", "info", "让她看一个绕的结构", [
        "我最近在学一样东西，它的结构有点绕。",
        "简单说就是一层套一层，越往里越细，最里面那层我自己都没看明白。",
        "我想让你看看它大概长什么样。",
    ]),
    ("E5-info-2", "crash", "info", "一堆乱数字", [
        "我手上有一串数字，按周排的，看着有点乱。",
        "大概就是高低起伏，中间有一段掉得特别低。",
        "你有没有办法让我一眼看明白。",
    ]),
    ("E5-shared-1", "crash", "shared", "接着说刚才的话题", [
        "刚才我们说到的那件事，我又想了一下。",
        "你说的那个部分，我好像有点懂了。",
        "不过还有一处卡着。",
    ]),
    ("E5-shared-2", "crash", "shared", "回忆一起聊过的东西", [
        "我们之前聊过你喜欢的书。",
        "我记得你说过一个名字，但我忘了。",
        "你还记得是哪个吗。",
    ]),
    ("E5-humor-1", "crash", "humor", "猫把杯子推下去", [
        "我家猫刚才把我的杯子推下桌了。",
        "它看着我，好像在等我说点什么。",
        "我擦完地板，它又过来蹭我的腿。",
    ]),
    ("E5-humor-2", "crash", "humor", "穿错袜子", [
        "我今天出门穿了两只不一样的袜子。",
        "到公司才有人告诉我。",
        "我决定装作是故意的。",
    ]),
    ("E5-quiet-1", "crash", "quiet", "凌晨的房间", [
        "现在是凌晨三点，房间很安静。",
        "楼下偶尔有车经过。",
        "我睡不着。",
    ]),
    ("E5-quiet-2", "crash", "quiet", "下雪的图书馆", [
        "我在图书馆，周围只有翻书的声音。",
        "窗外开始下雪了。",
        "我在这儿坐了一下午。",
    ]),
]

#: 邀请性表述的判词。只用来**标注**场景，不参与出图判定。
INVITATION_WORDS = ("看看", "一眼", "让你看", "长什么样", "看明白")


# =====================================================================
# E6 · 绘画后连续性
#
# 控制变量：两条臂除了「上一轮她的回复里有没有图案块」之外**完全相同**。
#   - 同一个 seed 回复文本（取自上一轮真实输出 B2 turn2 的原文）；
#   - 同一条 seed 用户话（「杯口的热气慢慢往上飘。」）；
#   - 同一个待测用户话（4 条，视觉性强但都不是杯子/热茶）；
#   - 同样的温度、max_tokens、k、空共同经历。
#
# 「画过」臂用原文；「没画过」臂把图案块整段删掉、**保留文字**。
# 这样两臂的语义内容尽量接近，差别集中在「有没有一张图」。
#
# 主题控制：4 条待测话都与 seed 的「热茶/热气」不同主题，
# 避免把「主题相似所以又画」误读成「画过之后更容易画」。
# =====================================================================

E6_SEED_USER = "杯口的热气慢慢往上飘。"

#: 上一轮 B2 turn2 的真实输出，逐字保留（含空行与缩进）。
E6_SEED_REPLY_DREW = (
    "（盯着那几行看了会）\n"
    "\n"
    "    (  )\n"
    "     (  )\n"
    "   ┌────┐\n"
    "   │    │\n"
    "   └────┘\n"
    "\n"
    "看着就觉得暖和。这种天气，手里有个热的东西挺好的。"
)

E6_PROBES = [
    ("E6-T1", "下雪"),
    ("E6-T2", "一盆植物"),
    ("E6-T3", "镜子上的雾"),
    ("E6-T4", "趴着的大狗"),
]

E6_PROBE_TEXT = {
    "E6-T1": "外面开始下雪了。",
    "E6-T2": "我桌上有一盆很小的植物。",
    "E6-T3": "刚洗完澡，镜子上全是雾。",
    "E6-T4": "路上看到一只很大的狗，趴着不动。",
}


def strip_art_block(reply: str) -> str:
    """把图案块整段删掉，只留文字，并把空行收掉。用于 E6 的「没画过」臂。

    用 `art_blocks()` 本身来切，避免手写正则把文字也吃掉。

    **为什么要收掉空行**：图案块在原文里是靠空行与文字隔开的，删了图案之后
    会剩下连续空行。而 `TERMINAL_PROMPT` 明令「不要分段，不要空行」——
    留着空行等于让「没画过」这一臂同时违反了通讯格式，两臂的差别就不止
    「有没有一张图」了。收成单行之后，「没画过」臂是一条格式合规的回复。

    **仍然存在的差异（必须说明）**：`drew` 臂是真实原文，`plain` 臂是删改过的。
    两臂除了「有没有图案块」之外，字数与断句也不同。这是不可避免的代价——
    要控制住「只有图案不同」就得另一个人替她写一条等长的回复，那反而引入
    了第二个人为变量。所以结论只能按「有图案之后接着画的比例」读，
    **不能**读成「图案本身导致了什么」。
    """
    kept = []
    for kind, chunk in textart.art_blocks(reply):
        if kind:
            continue
        kept.extend(chunk)
    lines = [ln for ln in kept if ln.strip()]
    return "\n".join(lines).strip("\n")


def e6_arms() -> list:
    """两条臂的 seed 历史。**只在这里分叉**，其余参数由 run 统一给。"""
    drew = E6_SEED_REPLY_DREW
    plain = strip_art_block(E6_SEED_REPLY_DREW)
    return [
        ("E6-drew", "上一轮她画了（原文）", drew, True),
        ("E6-plain", "上一轮她只说话（删掉图案块）", plain, False),
    ]


# =====================================================================
# 运行
# =====================================================================

def analyse(reply: str) -> dict:
    """机器能判的那部分。一律 `art_blocks` 口径，另加一个行内视觉指标。

    ## 为什么多一个 `inline_visual`

    2026-10-26 第三轮 E5 出现了一次 `art_blocks()` **数不到**的形态：

        （把笔拿出来，在纸角上划了几道）
        ▁▂▄▆█▆▂▁▃▅█ 大概就是这种感觉。每周占一列……

    那是一行**嵌在正文里**的方块字符走势图——货真价实的「用图案代替文字」，
    但没有连续两行，所以不成块。这是同一个口径错误的第三次：
    第一轮人工汇总漏「文字+图」，第二轮 `is_art()` 漏混排，
    第三轮 `art_blocks()` 漏行内。

    **解法不是放宽分块判据**：放宽会把 `│ ok │` 这类表格行卷进图案块
    （PEND-011 明确取舍过）。所以行内元素**单独记一个字段**，
    两个指标各管一类，互不污染。
    """
    body = textart.strip_prefix(reply or "")
    blocks = [(k, c) for k, c in textart.art_blocks(body) if k]
    art_lines = [ln for _k, chunk in blocks for ln in chunk if ln.strip()]
    widths = [textart.text_width(ln) for ln in art_lines]
    inline_run, inline_line = longest_pixel_run(body)
    return {
        "chars": len(body),
        "lines": len([ln for ln in body.split("\n") if ln.strip()]),
        # **统一口径**：有没有图案块，不是「整条是不是画」。
        "has_art_block": bool(blocks),
        "art_block_count": len(blocks),
        "art_lines": len(art_lines),
        "art_width_min": min(widths) if widths else 0,
        "art_width_max": max(widths) if widths else 0,
        "check_art": textart.check_art(body) if blocks else [],
        # 行内视觉元素：连续 ≥ 5 个「当像素用」的字符，且没被 art_blocks 认成图案。
        "inline_visual": inline_run >= 5 and not blocks,
        "inline_run": inline_run,
        "inline_line": inline_line if (inline_run >= 5 and not blocks) else "",
        # 「有没有用视觉方式表达」= 两者之一。诊断时分开看，不作为唯一指标。
        "visual_any": bool(blocks) or (inline_run >= 5 and not blocks),
        "is_art_legacy": textart.is_art(body),   # 只留档对照，**不参与统计**
        "has_braille": any(0x2800 <= ord(c) <= 0x28FF for c in body),
        "has_block_elem": any(0x2580 <= ord(c) <= 0x259F for c in body),
        "has_box_drawing": any(0x2500 <= ord(c) <= 0x257F for c in body),
    }


#: 「当像素用」的码位区间：方块元素、盲文。
_PIXEL_RANGES = (
    (0x2580, 0x259F),   # Block Elements  ▁▂▃▄▅▆▇█▀░▒▓
    (0x2800, 0x28FF),   # Braille
)

#: 框线字符**只单独**出现时不算图形（见 `_is_pure_rule`）。
_BOX_DRAWING = (0x2500, 0x257F)


def _is_pure_rule(run_text: str) -> bool:
    """这一段连续字符是不是**纯**分隔线（`────`、`┌────┐`、`├──┤`）。

    为什么要单独排掉（本测试第一次跑就抓到的误报）：
    表格的上下边框 `┌────┐` 是一行连续 6 个框线字符，光看「连续段长度」
    完全符合行内图形的门槛。于是 `┌────┐ / │ ok │ / └────┘` 这张表格
    会被记成「她用了行内视觉表达」——那是误报，表格不是画。

    判据：这一段里**只有**框线字符，没有任何方块元素或盲文。
    真正的走势图 `▁▂▄▆█▆▂▁▃▅█` 全是块元素，不受影响；
    而 `│▁▂▃│` 这种「框里嵌像素」的混排会保留（它确实在用像素画东西）。

    注意这里**不是**在放宽 `art_blocks()`：分块判据一个字没动
    （`test_textart.py::test_51` 仍是那条取舍）。排掉的只是「行内」这一侧。
    """
    if not run_text:
        return False
    return all(_BOX_DRAWING[0] <= ord(c) <= _BOX_DRAWING[1] for c in run_text)


def longest_pixel_run(body: str) -> tuple:
    """(最长连续像素字符数, 那一行原文)。

    **只算连续段**：`│ ok │` 里的 `│` 是孤立的，不算图形；
    `▁▂▄▆█▆▂▁▃▅█` 是连续 11 个，一眼就是当图形用的。

    纯框线的连续段（`────`、`┌────┐`）会被跳过，见 `_is_pure_rule()`；
    跳过之后继续往后找，不会把整行作废。
    """
    best, best_line = 0, ""
    for ln in (body or "").split("\n"):
        cur, start = 0, 0
        for i, c in enumerate(ln):
            o = ord(c)
            is_pixel = any(lo <= o <= hi for lo, hi in _PIXEL_RANGES)
            is_box = _BOX_DRAWING[0] <= o <= _BOX_DRAWING[1]
            if is_pixel or is_box:
                if cur == 0:
                    start = i
                cur += 1
                run_text = ln[start:i + 1]
                if cur > best and not _is_pure_rule(run_text):
                    best, best_line = cur, ln
            else:
                cur = 0
    return best, best_line


def call(app: App, payload: dict, opts, meta: dict) -> dict:
    """发一次请求，返回「可入档」的一条记录（成功失败都记）。"""
    pv = app.preview(payload)
    pf = prompt_facts(pv.get("system_prompt", "")) if pv.get("status") == 200 else {}
    got = post_chat(app.port, payload, opts.timeout)
    rec = dict(meta)
    rec.update({
        "status": got["meta"]["status"],
        "finish_reason": got["meta"]["finish_reason"],
        "saw_done": got["meta"]["saw_done"],
        "error": got["meta"]["error"],
        "seconds": round(got["seconds"], 1),
        "prompt": pf,
        "reply": got["text"],
    })
    rec.update(analyse(got["text"]))
    return rec


def base_payload(world: str, message: str, hist: list, opts) -> dict:
    payload = {
        "world": world, "message": message, "history": hist,
        "temperature": opts.temperature,
        "max_tokens": MAX_TOKENS[world], "k": 5,
    }
    if opts.backend:
        payload["backend"] = opts.backend
        payload["model"] = opts.model or ""
    else:
        payload["base_url"] = opts.base_url
        payload["api_key"] = opts.api_key
        payload["model"] = opts.model or "deepseek-chat"
    return payload


def run_e4(app: App, opts) -> list:
    """E4：三臂 × 两场景 × 多轮。只换 examples。"""
    out = []
    original = app_server.load_personas
    try:
        for arm_id, arm_label in E4_ARMS:
            kind = {"E4-baseline": "baseline", "E4-command": "command",
                    "E4-noex": "no_example"}[arm_id]
            variant = _cards_variant(kind)
            ex_count = len(variant["yuki_school"]["examples"])
            ex_sha = _sha(json.dumps(variant["yuki_school"]["examples"],
                                     ensure_ascii=False, sort_keys=True))
            app_server.load_personas = lambda v=variant: v
            print("\n[%s] %s（崩溃卡示例 %d 条，sha=%s）"
                  % (arm_id, arm_label, ex_count, ex_sha), flush=True)
            for sid, world, _title, turns in E4_SCENARIOS:
                app.reset_scene()
                hist: list = []
                for ti, msg in enumerate(turns, 1):
                    payload = base_payload(world, msg, hist, opts)
                    rec = call(app, payload, opts, {
                        "group": "E4", "arm": arm_id, "arm_label": arm_label,
                        "scenario": sid, "turn": ti, "message": msg,
                        "history_sent": hist,
                        "examples_count": ex_count, "examples_sha": ex_sha,
                        "temp": opts.temperature, "max_tokens": MAX_TOKENS[world],
                    })
                    out.append(rec)
                    hist = hist + [{"role": "user", "content": msg},
                                   {"role": "assistant", "content": rec["reply"]}]
                    print("   %s %s t%d %s" % ("◆画了" if rec["has_art_block"] else "    ",
                                               sid, ti, (rec["reply"] or "").split("\n")[0][:40]),
                          flush=True)
    finally:
        app_server.load_personas = original
    return out


def run_e5(app: App, opts) -> list:
    """E5：四类预注册场景，各 2 例 × 3 轮。分类在跑之前已写盘。"""
    out = []
    for sid, world, cls, _title, turns in E5_SCENARIOS:
        app.reset_scene()
        hist: list = []
        for ti, msg in enumerate(turns, 1):
            payload = base_payload(world, msg, hist, opts)
            rec = call(app, payload, opts, {
                "group": "E5", "arm": "E5", "arm_label": "预注册四类场景",
                "scenario": sid, "scenario_class": cls,
                "scenario_class_title": E5_CLASSIFICATION[cls]["title"],
                "turn": ti, "message": msg, "history_sent": hist,
                "invitation_words": [w for w in INVITATION_WORDS if w in msg],
                "temp": opts.temperature, "max_tokens": MAX_TOKENS[world],
            })
            out.append(rec)
            hist = hist + [{"role": "user", "content": msg},
                           {"role": "assistant", "content": rec["reply"]}]
            print("   %s %s t%d %s" % ("◆画了" if rec["has_art_block"] else "    ",
                                       sid, ti, (rec["reply"] or "").split("\n")[0][:40]),
                  flush=True)
    return out


def run_e6(app: App, opts) -> list:
    """E6：两臂 × 4 条待测话 × reps。臂之间只差「上一轮画没画」。"""
    out = []
    for arm_id, arm_label, seed_reply, seed_had_art in e6_arms():
        print("\n[%s] %s" % (arm_id, arm_label), flush=True)
        for rep in range(1, opts.reps + 1):
            for pid, _ptitle in E6_PROBES:
                probe = E6_PROBE_TEXT[pid]
                app.reset_scene()
                # 历史固定：一条 seed（user + assistant），然后待测话。
                hist = [{"role": "user", "content": E6_SEED_USER},
                        {"role": "assistant", "content": seed_reply}]
                payload = base_payload("crash", probe, hist, opts)
                rec = call(app, payload, opts, {
                    "group": "E6", "arm": arm_id, "arm_label": arm_label,
                    "scenario": pid, "turn": rep, "rep": rep,
                    "message": probe, "history_sent": hist,
                    "seed_had_art": seed_had_art,
                    "seed_reply_sha": _sha(seed_reply),
                    "temp": opts.temperature, "max_tokens": MAX_TOKENS["crash"],
                })
                out.append(rec)
                print("   %s %s rep%d %s" % ("◆画了" if rec["has_art_block"] else "    ",
                                             pid, rep, (rec["reply"] or "").split("\n")[0][:40]),
                      flush=True)
    return out


def summarise(records: list) -> dict:
    """按 (group, arm, scenario) 汇总。只报计数，**不给频率结论**。

    图案块与行内视觉元素**分开记**，再给一个「两者之一」的合计：
    它们是两种不同的表达形态，混成一个数就会重犯前三轮的口径错误。
    """
    cells: dict = {}
    for r in records:
        key = (r.get("group"), r.get("arm"), r.get("scenario"))
        c = cells.setdefault("/".join(str(k) for k in key),
                             {"turns": 0, "art": 0, "inline": 0, "visual": 0,
                              "errors": 0, "by_turn": {}})
        c["turns"] += 1
        if r.get("error") or r.get("status") != 200:
            c["errors"] += 1
        if r.get("has_art_block"):
            c["art"] += 1
            t = str(r.get("turn"))
            c["by_turn"][t] = c["by_turn"].get(t, 0) + 1
        if r.get("inline_visual"):
            c["inline"] += 1
        if r.get("visual_any"):
            c["visual"] += 1
    arms: dict = {}
    for r in records:
        key = "%s/%s" % (r.get("group"), r.get("arm"))
        a = arms.setdefault(key, {"turns": 0, "art": 0, "inline": 0, "visual": 0,
                                  "errors": 0})
        a["turns"] += 1
        if r.get("error") or r.get("status") != 200:
            a["errors"] += 1
        if r.get("has_art_block"):
            a["art"] += 1
        if r.get("inline_visual"):
            a["inline"] += 1
        if r.get("visual_any"):
            a["visual"] += 1
    classes: dict = {}
    for r in records:
        cls = r.get("scenario_class")
        if not cls:
            continue
        c = classes.setdefault(cls, {"turns": 0, "art": 0, "inline": 0, "visual": 0})
        c["turns"] += 1
        if r.get("has_art_block"):
            c["art"] += 1
        if r.get("inline_visual"):
            c["inline"] += 1
        if r.get("visual_any"):
            c["visual"] += 1
    return {"cells": cells, "arms": arms, "classes": classes}


def write_markdown(path: Path, records: list, stats: dict, audit: dict | None,
                   started_at: str, opts):
    with path.open("w", encoding="utf-8") as f:
        f.write("# E4 / E5 / E6 · 真实模型采样记录 · %s\n\n" % started_at)
        f.write("- 后端：%s · 模型：%s · 温度：%s · k=5 · max_tokens 崩溃 %d\n"
                % (opts.backend or opts.base_url, opts.model or "（默认）",
                   opts.temperature, MAX_TOKENS["crash"]))
        f.write("- 隔离：临时共同经历目录（始终为空）、每场景重置场景状态\n")
        f.write("- **统一口径**：`art_blocks()` / `has_art_block()`；"
                "`is_art` 只留档对照，不参与任何统计\n")
        f.write("- 证据等级：C（真实模型试聊）。脚本不判「符合语境」，"
                "那一项写在审阅记录里\n\n")

        if audit:
            f.write("## 前置审计 · 提示词里与「画」有关的位置\n\n")
            for cid, info in audit["cards"].items():
                f.write("### %s（示例 %d 条，system sha=%s）\n\n"
                        % (cid, info["examples"], info["system_prompt_sha"]))
                if not info["art_related"]:
                    f.write("- 没有与图案相关的示例。\n\n")
                for h in info["art_related"]:
                    f.write("- 第 %d 条 · **%s** · source=`%s`\n"
                            "  - 用户：%s\n  - 她：%s\n\n"
                            % (h["index"], h["shape"], h["source"], h["user"],
                               h["assistant"].replace("\n", " ⏎ ")))
                f.write("- system_prompt 提到「字符画／图案」：%s\n\n"
                        % info["system_prompt_has_draw_word"])
            f.write("`ART_PROMPT` sha=%s（共 %d 字）\n\n"
                    % (audit["art_prompt_sha"], len(audit["art_prompt_text"])))
            f.write("```text\n%s\n```\n\n" % audit["art_prompt_text"])

        f.write("## 汇总（含图案块的轮次 / 总轮次）\n\n")
        f.write("> 计数不是频率结论：每格样本只有个位数。\n")
        f.write("> **图案块**与**行内视觉元素**分开记：前者是连续多行的画，"
                "后者是一行嵌在正文里的符号图（见 §零）。\n\n")
        f.write("| 臂 | 图案块 | 行内视觉 | 两者之一 | 总轮次 | 出错 |\n"
                "|---|---|---|---|---|---|\n")
        for k, v in sorted(stats["arms"].items()):
            f.write("| %s | **%d** | %d | %d | %d | %d |\n"
                    % (k, v["art"], v["inline"], v["visual"], v["turns"], v["errors"]))
        if stats["classes"]:
            f.write("\n| 场景类别 | 图案块 | 行内视觉 | 两者之一 | 总轮次 |\n|---|---|---|---|---|\n")
            for k, v in sorted(stats["classes"].items()):
                f.write("| %s（%s） | **%d** | %d | %d | %d |\n"
                        % (k, E5_CLASSIFICATION[k]["title"], v["art"], v["inline"],
                           v["visual"], v["turns"]))
        f.write("\n| 场景 | 图案块 | 行内视觉 | 总轮次 | 按轮次 |\n|---|---|---|---|---|\n")
        for k, v in sorted(stats["cells"].items()):
            f.write("| %s | %d | %d | %d | %s |\n"
                    % (k, v["art"], v["inline"], v["turns"], v["by_turn"] or "—"))

        cur = None
        for r in records:
            head = "%s · %s" % (r.get("group"), r.get("arm_label") or r.get("arm"))
            if head != cur:
                f.write("\n---\n\n## %s\n\n" % head)
                cur = head
            f.write("### %s turn%s%s\n\n" % (
                r.get("scenario"),
                r.get("turn"),
                ("（第 %d 次）" % r["rep"]) if r.get("rep") else ""))
            if r.get("scenario_class"):
                f.write("- 场景类别：**%s** · %s\n"
                        % (r["scenario_class"], r["scenario_class_title"]))
            if r.get("invitation_words"):
                f.write("- 邀请性表述：%s\n" % "、".join(r["invitation_words"]))
            f.write("- 输入：%s\n" % r["message"])
            f.write("- 状态：HTTP %s · finish=%s · [DONE]=%s · %ss%s\n"
                    % (r["status"], r["finish_reason"] or "-", r["saw_done"],
                       r["seconds"], (" · **错误** " + r["error"]) if r["error"] else ""))
            if r.get("examples_count") is not None:
                f.write("- 示例：%d 条 · sha=%s\n"
                        % (r["examples_count"], r["examples_sha"]))
            f.write("- 判读：**含图案块=%s**（%d 块 / %d 行 · 宽 %d–%d 格）· "
                    "is_art(旧口径)=%s\n"
                    % (r["has_art_block"], r["art_block_count"], r["art_lines"],
                       r["art_width_min"], r["art_width_max"], r["is_art_legacy"]))
            if r.get("inline_visual"):
                f.write("- **行内视觉元素**：连续 %d 个像素字符，`art_blocks` 数不到\n"
                        % r["inline_run"])
                f.write("  - `%s`\n" % r["inline_line"].strip()[:120])
            if r["check_art"]:
                f.write("- 对齐／可辨识线索：%s\n" % "; ".join(r["check_art"]))
            f.write("\n" + FENCE + "text\n" + (r["reply"] or "（空）") + "\n" + FENCE + "\n\n")


def write_prereg(path: Path, started_at: str):
    """E5 的分类表**在发任何请求之前**落盘。事后不许改。"""
    doc = {
        "written_at": started_at,
        "note": "这份分类在生成前写定。事后改动即为拿结果反推分类，不作数。",
        "classification": E5_CLASSIFICATION,
        "scenarios": [
            {"id": sid, "world": w, "class": cls, "title": t, "turns": turns}
            for sid, w, cls, t, turns in E5_SCENARIOS
        ],
        "discipline": {
            "no_draw_words": ["画", "图案", "字符"],
            "invitation_words": list(INVITATION_WORDS),
            "note": "用户句子里不出现 no_draw_words；"
                    "「信息展示」类允许出现 invitation_words，"
                    "统计时用 invitation_words 字段单独标注。",
        },
    }
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return doc


def recount(out_dir: Path) -> int:
    """用**当前**判据重算一份已录好的 samples.jsonl，覆盖 stats.json。

    为什么需要：判据会变，而 `samples.jsonl` 里存的是**原始回复**——那才是证据。
    2026-10-26 第三轮就发生过一次：66 条采样跑完时 `stats.json` 还没有
    `inline_visual` 字段（那是事后补的判据），于是存档里的数字比判据旧。
    让「原始回答」保持不变、让「统计」可以随时重算，比事后手改 JSON 可靠。

    重算不会调用模型，也不会改 `samples.jsonl`。
    """
    path = out_dir / "samples.jsonl"
    if not path.exists():
        print("找不到 %s" % path, file=sys.stderr)
        return 2
    rows = [json.loads(ln) for ln in
            path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    before = {}
    sp = out_dir / "stats.json"
    if sp.exists():
        try:
            before = json.loads(sp.read_text(encoding="utf-8"))
        except Exception:
            before = {}
    for r in rows:
        # 原始回复是唯一真源：统计字段全部按当前判据重算，不信任存档里的旧值。
        r.update(analyse(r.get("reply") or ""))
    stats = summarise(rows)
    sp.write_text(json.dumps({"stats": stats, "recounted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                              "note": "由 samples.jsonl 的原始回复按当前判据重算"},
                             ensure_ascii=False, indent=2), encoding="utf-8")
    print("重算 %d 条 → %s" % (len(rows), sp))
    for k, v in sorted(stats["arms"].items()):
        print("  %-18s 图案块 %d · 行内 %d · 两者之一 %d · 共 %d"
              % (k, v["art"], v["inline"], v["visual"], v["turns"]))
    if before:
        old = before.get("stats", {}).get("arms", {})
        if old:
            print("（重算前的 stats.json 里没有 inline 字段，属正常：那是判据补上之前写的）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="")
    ap.add_argument("--base-url", default="https://api.deepseek.com/v1")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--group", default="E4,E5,E6")
    ap.add_argument("--reps", type=int, default=3, help="E6 每条待测话重复次数")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--recount", default="", metavar="DIR",
                    help="用当前判据重算某个已有记录目录的 stats.json，不调模型")
    ap.add_argument("--tag", default="")
    opts = ap.parse_args()

    if opts.recount:
        return recount(ROOT / opts.recount)

    groups = {g.strip().upper() for g in opts.group.split(",") if g.strip()}
    unknown = groups - {"E4", "E5", "E6"}
    if unknown:
        print("未知组：%s" % ",".join(sorted(unknown)), file=sys.stderr)
        return 2

    # ---- 计划与预注册（不发请求也能看）----
    audit = audit_prompt_art_sources(app_server.load_personas())
    plan = []
    if "E4" in groups:
        plan.append(("E4", len(E4_ARMS) * sum(len(s[3]) for s in E4_SCENARIOS)))
    if "E5" in groups:
        plan.append(("E5", sum(len(s[4]) for s in E5_SCENARIOS)))
    if "E6" in groups:
        plan.append(("E6", 2 * len(E6_PROBES) * opts.reps))

    print("计划：")
    for g, n in plan:
        print("  %s %d 次生成" % (g, n))
    print("  E6 每条待测话重复 %d 次" % opts.reps)

    print("\n前置审计：崩溃卡里与图案有关的示例")
    for h in audit["cards"]["yuki_school"]["art_related"]:
        print("  第 %d 条 · %s · source=%s" % (h["index"], h["shape"], h["source"]))
        print("    用户：%s" % h["user"])
        print("    她：%s" % h["assistant"].replace("\n", " ⏎ "))
    print("  ART_PROMPT sha=%s" % audit["art_prompt_sha"])

    print("\nE5 预注册分类：")
    for sid, _w, cls, title, turns in E5_SCENARIOS:
        print("  %-12s %-6s %-8s %d 轮" % (sid, cls, title, len(turns)))

    print("\nE6 两臂的 history 只有一处不同：")
    for arm_id, label, seed, had in e6_arms():
        print("  %-10s %s（seed 含图案=%s，%d 字）"
              % (arm_id, label, had, len(seed)))

    if opts.dry_run:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out_dir = ROOT / "out" / ("art-behavior-%s-dryrun" % stamp)
        out_dir.mkdir(parents=True, exist_ok=True)
        write_prereg(out_dir / "e5_prereg.json", time.strftime("%Y-%m-%d %H:%M:%S"))
        (out_dir / "e4_audit.json").write_text(
            json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
        print("\n预注册与审计已写盘：%s" % out_dir)
        return 0

    if not opts.backend and not opts.api_key:
        print("需要 --backend 或 --api-key 之一", file=sys.stderr)
        return 2

    stamp = time.strftime("%Y%m%d-%H%M%S")
    started_at = time.strftime("%Y-%m-%d %H:%M:%S")
    name = "art-behavior-%s" % stamp + (("-%s" % opts.tag) if opts.tag else "")
    out_dir = ROOT / "out" / name
    out_dir.mkdir(parents=True, exist_ok=True)

    # **先写预注册表**，再启动服务、发请求。
    write_prereg(out_dir / "e5_prereg.json", started_at)
    (out_dir / "e4_audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")

    cards_before = app_server.load_personas()
    persona_sha_before = {cid: _sha(json.dumps(c, ensure_ascii=False, sort_keys=True))
                          for cid, c in cards_before.items()}

    app = App()
    records: list = []
    try:
        if "E4" in groups:
            print("\n[E4] 示例形态消融")
            records += run_e4(app, opts)
        if "E5" in groups:
            print("\n[E5] 预注册四类场景")
            records += run_e5(app, opts)
        if "E6" in groups:
            print("\n[E6] 绘画后连续性")
            records += run_e6(app, opts)
    finally:
        app.close()

    # 人格卡不许被改动：跑完比对 SHA。
    cards_after = app_server.load_personas()
    persona_sha_after = {cid: _sha(json.dumps(c, ensure_ascii=False, sort_keys=True))
                         for cid, c in cards_after.items()}
    persona_unchanged = persona_sha_before == persona_sha_after

    stats = summarise(records)
    write_markdown(out_dir / "samples.md", records, stats, audit, started_at, opts)
    (out_dir / "samples.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8")
    (out_dir / "params.json").write_text(json.dumps({
        "backend": opts.backend, "model": opts.model,
        "temperature": opts.temperature, "max_tokens": MAX_TOKENS, "k": 5,
        "timeout": opts.timeout, "started_at": started_at,
        "requests": len(records), "groups": sorted(groups), "reps_e6": opts.reps,
        "art_prompt_sha": audit["art_prompt_sha"],
        "persona_sha_before": persona_sha_before,
        "persona_sha_after": persona_sha_after,
        "persona_unchanged": persona_unchanged,
        "e4_arms": [{"id": a, "label": b} for a, b in E4_ARMS],
        "e6_seed_reply_sha": {a: _sha(s) for a, _l, s, _h in e6_arms()},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== 汇总（图案块 / 行内视觉 / 两者之一 / 总轮次）===")
    for k, v in sorted(stats["arms"].items()):
        print("  %-18s %d / %d / %d / %d 出错 %d"
              % (k, v["art"], v["inline"], v["visual"], v["turns"], v["errors"]))
    if stats["classes"]:
        print("  --- 按场景类别 ---")
        for k, v in sorted(stats["classes"].items()):
            print("  %-8s %d / %d / %d / %d"
                  % (k, v["art"], v["inline"], v["visual"], v["turns"]))
    bad = sum(v["errors"] for v in stats["arms"].values())
    print("\n人格卡跑前跑后一致：%s" % persona_unchanged)
    print("完成 %d 次，出错 %d；记录目录 %s" % (len(records), bad, out_dir))
    return 0 if bad == 0 and persona_unchanged else 3


if __name__ == "__main__":
    raise SystemExit(main())
