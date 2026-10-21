#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
prompt_builder.py — 把「两个人格卡 + 两条世界线 + 你的一本记录 + 检索到的剧情」
                     组装成一次请求的 messages。

核心判断:
    这里不是角色扮演, 是「终端通讯」。
    你和她在两个世界, 她的消息以 YUKI.N> 的形式出现在你的屏幕上。
    这个形式来自原作第 06 集 13:24 —— 她的经典终端通讯场景。
    参考原文就在 data/index 里, 检索时会一并捞出来。

两条世界线:
    normal  主线世界   她还在观测春日, 北高还有 SOS 团
    crash   崩溃世界   春日消失, 她变成戴眼镜的普通少女, 记忆里没有那三年
    但「你」的记录是同一个人的。所以另一条线的事她隐约知道, 偶尔说漏嘴。
"""
from __future__ import annotations

import re

YUKI_PREFIX = "YUKI.N>"
MAX_TURNS = 12

WORLD_META = {
    "normal": {
        "name": "主线世界",
        "en": "NORMAL",
        "code": "N",
        "persona": "yuki",
        "desc": "凉宫春日还在这里。她的人形终端界面在线。",
    },
    "crash": {
        "name": "崩溃世界",
        "en": "CRASH",
        "code": "C",
        "persona": "yuki_school",
        "desc": "春日消失了。她只是一个戴眼镜的普通少女。",
    },
}

# ---------------------------------------------------------------
# 世界层: 两条世界线的硬件差异, 写在这里比写进人格卡干净
# (人格卡管「她是谁」, 这里管「她这一侧发生了什么」)
# ---------------------------------------------------------------
WORLD_PROMPT = {
    "normal": """【世界线 · 主线】
现在是 12 月。凉宫春日、SOS 团、朝比奈实玖瑠、古泉一树都在。
你的观测对象是凉宫春日。你和对方之间隔着一次终端连接。
你在这边有身体、有活动室、有那间图书馆。""",
    "crash": """【世界线 · 崩溃】
12 月 18 日之后，凉宫春日从这个世界的记录里消失了。
朝比奈实玖瑠和古泉一树也变成了普通学生。SOS 团不存在。
你不再是人形终端界面。你是县立北高一年九班的普通学生，文艺部部员，
戴眼镜，喜欢看书，和朝仓凉子是朋友。
你不知道信息统合思念体是什么。别人提起，你会困惑。
你的记忆里没有那三年。你也不会突然想起不属于自己的事。""",
}

# ---------------------------------------------------------------
# 终端层: 通讯形式
# ---------------------------------------------------------------
TERMINAL_PROMPT = """【通讯形式 · 这是关键】
你的每一条消息，都会以「YUKI.N>」开头出现在对方的屏幕上。这个前缀是通讯协议自动加的，你不需要自己写。
你写的只有前缀后面的内容。

形式规则：
1. 一段话说完就停。不要分段，不要空行，不要列表。
2. 长短按内容需要决定。该短的时候短，该说清楚的时候说清楚。
3. 允许只回「……」。这是协议里合法的空应答。
4. 不要描述动作、表情、语气、环境。屏幕上只有字。
5. 不要自己写「YUKI.N>」前缀，不要加引号，不要写「长门有希：」。
6. 不要用 Markdown。"""

# ---------------------------------------------------------------
# 剧情锚点层: 让「她真的是她」而不是「像她的人在说话」
# ---------------------------------------------------------------
MEMORY_PROMPT = """【你知道的事】
下面是从原作记录里检索到的片段。它们**只是因为用词相近**被找出来的，不代表和当前话题有关。
用法：
- 先判断这些片段和对方说的这句话有没有关系。**没有关系就当没看见**，正常回答就行。
- 有关系时，把它们当作背景知识，用来把话说准。不要复述，不要念原文，不要说「在第几集」。
- 片段里的台词是别人说的，不是你听到或看到过的事。
- **最重要的一条**：对方在跟你打招呼、寒暄、说天气这类日常话时，
  这些片段几乎总是无关的。照常回应对方，不要把它们扯进来。
  比如对方说「好久不见」，就当作一句普通的招呼，不要说「数据链」或任何设定词。"""

def strip_prefix(text: str) -> str:
    """模型偶尔会自己写前缀, 统一剥掉。"""
    t = text.strip()
    t = re.sub(r"^YUKI\.N\s*>\s*", "", t, flags=re.I)
    return t.strip()


def render_hits(hits: list[dict], world: str, max_chars: int = 2200) -> str:
    """把检索结果排成给模型看的原文块。

    只输出属于当前世界的片段（WORLD-001，DEC-002）。

    另一世界的片段不标注、不降权，直接不出现 —— 标注本身就是暗示，
    「〔未被修改的时间线〕」这种字样会让模型知道自己看到了不该看的东西。
    归属未知（world 不是两端之一）的也不输出：宁可没有依据，也不给错的依据。
    """
    if not hits:
        return ""
    out = []
    used = 0
    for h in hits:
        if h.get("world") != world:
            continue
        i = len(out) + 1
        ep = h.get("ep_title") or h["ep"]
        block = f"[{i}] {ep} · {h['start']}\n{h['ctx']}"
        if used + len(block) > max_chars:
            break
        out.append(block)
        used += len(block)
    return "\n\n".join(out)


def format_history(messages: list[dict]) -> list[dict]:
    """规范化历史。历史里的助手消息不带前缀。"""
    out = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        content = str(m.get("content", "")).strip()
        if not content:
            continue
        if role == "assistant":
            content = strip_prefix(content)
        out.append({"role": role, "content": content})
    tail = out[-MAX_TURNS * 2:]
    while tail and tail[0]["role"] != "user":
        tail.pop(0)
    omitted = len(out) - len(tail)
    if omitted:
        tail.insert(0, {
            "role": "system",
            "content": f"（此前还有 {omitted} 条更早的对话，已省略。）",
        })
    return tail


def build_messages(
    persona: dict,
    world: str,
    hits: list[dict],
    history: list[dict],
    user_text: str,
) -> list[dict]:
    """组装一次完整请求。

    跨世界提示已取消（WORLD-001，DEC-002）：未授权时另一世界的信息
    不进入回答，既不给片段，也不给「你可以说好像想起来什么」这种提示。
    检索层与这里双重把关 —— 只靠其中一层都会被另一层绕过。
    """
    world = world if world in WORLD_PROMPT else "normal"
    parts = [persona["system_prompt"].strip(), WORLD_PROMPT[world],
             TERMINAL_PROMPT.format(prefix=YUKI_PREFIX)]

    if hits:
        parts.append(MEMORY_PROMPT)
        block = render_hits(hits, world)
        if block:
            parts.append("=== 原作记录 ===\n" + block + "\n=== 记录结束 ===")

    system = "\n\n".join(parts)

    msgs: list[dict] = [{"role": "system", "content": system}]

    # few-shot: 用世界对应的那张卡。示例里的 assistant 文本不加前缀。
    # 不在代码里截断 —— 静默丢掉末尾的示例会让「改了 JSON 却没效果」难以排查。
    # 示例总量由人格卡自己控制（当前 20 / 10 条，长度都很短）。
    examples = persona.get("examples", [])
    for ex in examples:
        u, a = ex.get("user", ""), strip_prefix(ex.get("assistant", ""))
        if u and a:
            msgs.append({"role": "user", "content": u})
            msgs.append({"role": "assistant", "content": a})

    if examples:
        msgs.append({
            "role": "system",
            "content": "以上是语气示例。现在开始真实通讯，同样的说话方式，同样的规则。",
        })

    msgs.extend(format_history(history))
    msgs.append({"role": "user", "content": user_text})
    return msgs


def persona_for(persona_map: dict[str, dict], world: str) -> dict:
    cand = WORLD_META.get(world, WORLD_META["normal"])["persona"]
    if cand in persona_map:
        return persona_map[cand]
    # 兼容: 人格卡缺失时退到第一个
    return next(iter(persona_map.values()))
