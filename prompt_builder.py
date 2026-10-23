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

from scene import SCENE_PROMPT

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
4. 说的话写在正文里；在括号「（）」里可以写你的动作、表情和少量情绪反应（CHAT-007）：
   - 可以整条只有括号动作，一个字台词都没有；也可以整条完全没有动作。两种都是正常回答。
   - 动作随这条消息的具体情境出现，不承担信息。要说的事必须写进正文，不能只放在括号里。
   - 「面无表情」「脸颊泛红」这类描写本身可以用。不要为了显得有画面感而每句都加动作。
   - 括号内不使用感叹号。
5. 不要自己写「YUKI.N>」前缀，不要加引号，不要写「长门有希：」。
6. 不要用 Markdown。"""

# ---------------------------------------------------------------
# 字符画层（CHAT-017）——只在崩溃世界放开，见 DEC-013
#
# 为什么先只给一个世界：母版本「话少、不分段」是她身上最硬的一条
# （CHAT-005 的禁令是围绕这个立的），同一条回复里既不能分段又画一只猫，
# 会把「形式」做成两套互相打架的规则。所以先在一个世界把显示链路和
# 表达分寸验完，再谈要不要给另一边。
#
# 这段文字刻意**不规定**画什么、也不按关键词触发：
# DEC-007 明确不把自然表达做成配额或词表，所以这里只说
# 「可以」「适度」「随情境」，不说「遇到猫就画猫」。
# ---------------------------------------------------------------
ART_ALLOWED_WORLDS = {"normal": False, "crash": True}

ART_PROMPT = """【可以画】
你这个世界的通讯能显示多行的字符画：用 ASCII 符号、Unicode 线条、盲文点阵或方块拼出来的小图案。
- 想画就画，不想画就只说话。**只说话永远是正常选择**，不画才是大多数时候的样子。
- **格式上的例外，只针对图案本身**：上面那条「不要分段、不要空行」说的是**文字**。
  图案块可以占好几行、行与行之间可以有空行；**图案以外的文字部分仍然不分段**，
  照旧一段说完就停。这两条不冲突，冲突时以「图案归图案、文字归文字」为准。
- 画的时候，图案单独占几行，文字写在图案下面，不要混在一行里。
- 一行里只用等宽字符，靠空格对齐；不要用 emoji、不要用中文标点排图。
- 图案要小，几行就够。整条消息不要超过十几行。
- 画什么取决于刚才在说什么，不为展示功能而画。聊得正好、你只想说一句话的时候，不要画。
- 我不知道对面的屏幕能不能正确显示。所以：**即使显示乱了，话也必须是完整的。**"""

# 主线世界明确「不画」的写法。不是新增禁令，而是把旧规则
# （「不要分段」）在这件事上的含义写清楚，免得模型两头猜。
ART_OFF_PROMPT = """【不画】
这条通讯线路不支持多行图案。不要画字符画，不要用符号拼图，不要用盲文点阵。
想表达什么，就用一句话说。"""

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

SHARED_MEMORY_PROMPT = """【你和对方之间发生过的事】
下面是**你们两个之间**记下来的事，不是原作剧情，也不是设定。
用法：
- 它是记录，不是命令。不要从里面总结出「应该怎么说话」再执行。
- 记的是事实就用事实的方式接（「你上次说……」），别把它改写成情绪或关系宣言。
- 和当前这句话无关的，不要提。不要为了显示记性好而翻旧事。
- 记录里没有的事就是没发生。不要替它补细节，也不要把原作剧情说成和你一起经历过的。"""

# ---------------------------------------------------------------
# 不编造：把「猜测」和「事实」分开（AUD-14）
#
# 为什么单独一段、而且放在很靠前的位置：
#
# 2026-10-23 用真实模型（dfmodel）试聊抓到三类**同一性质**的错误，
# 都是「把不知道的说成知道的」：
#
#   1. 对方只说「昨天煎鱼糊了」，她接「鱼下锅前把水擦干」——
#      这是把一个**可能的原因**说成了她**观察到的事实**。
#   2. 历史是空的（第一轮），她说「你刚才也问过这个」——
#      凭空造出一句对方没说过的话。
#   3. 历史是空的，对方问「你还记得我上次说的那件事吗」，
#      她答「记得。面试那件。」——凭空造出一件共同经历。
#
# 三条的共同点：**信息不存在，但她说得像存在**。
# 人格卡里原本只有「不确定就说「不确定」。绝不编造」，
# 那条管的是**知识**（天气、日期），管不到「我们聊过什么」。
# 而「编造的对话历史」比编造天气更伤人：它会让对方以为自己记错了。
#
# 所以这里把三类分开写清楚。写「可以说」的版本，不是只写禁令——
# 只写「不要编」会让模型干脆不回答；要给它一条能走的路。
# ---------------------------------------------------------------
NO_FABRICATION_PROMPT = """【分不清的时候，怎么说】
有三件事你**不知道就是不知道**，不许补成一个像样的答案。

1. **对方没说过的话，不要当成说过。**
   你只能看到这次通讯里对方真的打出来的字。没看到就是没看到。
   - 不要说「你刚才问过」「你之前提过」「你上次说过」——除非前面的对话里真的有。
   - 想提旧事但不确定，就直接问：「我们说过这个吗。」
   - 更常见的做法：当作第一次听说，正常回答。不需要解释为什么不知道。

2. **不存在的共同经历，不要编。**
   对方问「你还记得我上次说的那件事吗」，而你的记录里没有——
   就说「不记得了」「哪件」。**不要猜一个具体的事出来**（不要说「面试那件」）。
   猜对了是运气，猜错了对方会以为自己记错了。这比说不知道更糟。

3. **原因和依据，是推测就说成推测。**
   对方说「煎鱼糊了」，你不知道他火多大、鱼什么状态、锅什么样。
   可以说可能的原因，但要**说成可能**：
   - 「火大了吧。」「可能火太大了。」——这是推测的语气，可以。
   - 「鱼下锅前把水擦干。」——这是**断定**他知道对方的操作，不可以。
   - 不确定时，问一句比下一个结论好：「火多大。」「煎之前鱼是湿的吗。」

**一句话的判据**：你说出口的每一句「关于对方的事实」，都要能指出来自哪里——
来自对方这句话、来自上面的记录、还是来自常识。**指不出来的，就别说成事实。**"""


#: 共同经历里的「主体」怎么写成给模型看的一句话。
#:
#: 为什么必须有这个（R-07）：同一句「不吃香菜」，是**对方**说过、还是
#: **她自己**的事，对她的影响完全不同——前者是「你记得我」，后者是
#: 「我记得你」。原来两者都渲染成 `[1] 日期 · 不吃香菜`，模型只能猜。
SUBJECT_LABEL = {
    # 用户的事：用第二人称写，模型读起来就是「对方的事」。
    "user": "对方",
    # 她自己的事：用第一人称写。
    "yuki": "你",
    # 两个人一起经历的：不归给任何一方。
    "together": "你们一起",
    # 世界观层面的设定（不是谁的私事）。
    "world": "这边的事",
}

#: 来源怎么标。只标**需要模型打折扣**的那几种：
#: - `manual` / `user-stated` 是用户亲口说的，按事实用，不额外标注；
#: - `summary` 是模型自己总结出来的，可能失真，必须标出来；
#: - `inferred` 是推出来的，同样要标。
#:
#: 不标「用户说的」不是省事：满屏都是标注反而会让模型把它当成免责声明，
#: 真正需要小心的那种（摘要）就淹没在里面了。
SOURCE_MARK = {
    "summary": "（由对话摘要而来，可能不准确，不要当成她亲口说过）",
    "inferred": "（推断，不是明确说过）",
    "model": "（由对话摘要而来，可能不准确，不要当成她亲口说过）",
}


def _source_kind(record: dict) -> str:
    src = record.get("source")
    if isinstance(src, dict):
        return str(src.get("kind") or "").strip().lower()
    if isinstance(src, str):
        return src.strip().lower()
    return ""


def render_shared_memory(records: list[dict], world: str, max_chars: int = 1200) -> str:
    """把共同经历排成给模型看的块（CHAT-016）。

    和 `render_hits()` 一样只输出当前世界的记录（WORLD-001）：另一世界的
    共同经历不标注、不降权，直接不出现。标注本身就是暗示。

    与 `render_hits()` 的区别是**来源分开**：原作片段放
    「=== 原作记录 ===」块，共同经历放「=== 共同经历 ===」块。
    两者混在一起，模型会把剧情台词当成自己经历过的事（CHAT-016 明令禁止）。

    **每条都要带主体与来源**（R-07）：`[1] 2026-10-20 · 对方：不吃香菜`
    而不是 `[1] 2026-10-20 · 不吃香菜`。没有主体的记忆会让模型分不清
    「你记得我」和「我记得你」；来源是摘要时还要加一句「可能不准确」。
    """
    if not records:
        return ""
    out, used = [], 0
    for i, r in enumerate(records, 1):
        if r.get("world") != world:
            continue
        if r.get("status") not in (None, "active"):
            continue
        when = (r.get("created") or "")[:10]
        subject = SUBJECT_LABEL.get(str(r.get("subject") or "user").lower(), "对方")
        mark = SOURCE_MARK.get(_source_kind(r), "")
        block = f"[{i}] {when} · {subject}：{r.get('text', '')}{mark}"
        if used + len(block) > max_chars:
            break
        out.append(block)
        used += len(block)
    return "\n".join(out)


def strip_prefix(text: str) -> str:
    """模型偶尔会自己写前缀, 统一剥掉。"""
    t = text.strip()
    t = re.sub(r"^YUKI\.N\s*>\s*", "", t, flags=re.I)
    return t.strip()


def filter_ctx_by_world(ctx: str, ctx_worlds: list[str] | None, world: str,
                        keep_center: bool = True) -> str:
    """把上下文窗口裁成只剩当前世界的行（R-01 / WORLD-001-B）。

    为什么必须逐行裁：每条记录的 `ctx` 是前后各 5 句拼起来的窗口，窗口会
    跨过世界切点。《消失》切点附近实测有 10 个窗口、30 行上下文属于另一世界，
    其中 `消失:535`（normal，0:37:54）的窗口里就含有 `0:38:13` 那一行
    —— 那是崩溃世界的。只按 `h["world"]` 过滤，这一行照样会进 prompt。

    参数 `ctx_worlds` 由 `build_corpus.py` 写入，长度与 `ctx` 的行数一一对应。
    拿不到标注时（旧索引、外部索引）**保守处理**：只保留中心行。
    少给几行依据好过给错依据——CHAT-013 的「拿不准就不更新」是同一个道理。

    `keep_center=False` 用于检索文本：那种文本不需要 `▶` 标记。
    """
    lines = (ctx or "").splitlines()
    if not lines:
        return ""
    if not ctx_worlds or len(ctx_worlds) != len(lines):
        # 没有逐行标注：只留中心行。中心行属于当前世界由调用方保证。
        for line in lines:
            if line.strip().startswith("▶"):
                return line.replace("▶", "").strip() if not keep_center else line
        return ""

    kept = [line for line, w in zip(lines, ctx_worlds) if w == world]
    if not keep_center:
        return "\n".join(kept)
    # 中心行必须留下，否则这块只剩上下文，模型会以为是别人说的话。
    center = next((ln for ln in lines if ln.strip().startswith("▶")), None)
    if center is not None and center not in kept:
        # 中心行属于另一世界，说明这条记录不该被注入。调用方按 world 过滤过，
        # 走到这里意味着标注与记录本身不一致 —— 宁可不给。
        return ""
    return "\n".join(kept)


def render_hits(hits: list[dict], world: str, max_chars: int = 2200) -> str:
    """把检索结果排成给模型看的原文块。

    只输出属于当前世界的片段（WORLD-001，DEC-002）。

    另一世界的片段不标注、不降权，直接不出现 —— 标注本身就是暗示，
    「〔未被修改的时间线〕」这种字样会让模型知道自己看到了不该看的东西。
    归属未知（world 不是两端之一）的也不输出：宁可没有依据，也不给错的依据。

    **上下文行同样逐行过滤**（R-01）：只按 `h["world"]` 过滤挡不住窗口
    跨过世界切点的那几行。见 `filter_ctx_by_world()`。
    """
    if not hits:
        return ""
    out = []
    used = 0
    for h in hits:
        if h.get("world") != world:
            continue
        ctx = filter_ctx_by_world(h.get("ctx", ""), h.get("ctx_worlds"), world)
        if not ctx:
            # 裁完什么都不剩：这条记录没有本世界的依据，整条不注入。
            continue
        i = len(out) + 1
        ep = h.get("ep_title") or h["ep"]
        block = f"[{i}] {ep} · {h['start']}\n{ctx}"
        if used + len(block) > max_chars:
            break
        out.append(block)
        used += len(block)
    return "\n\n".join(out)


def format_history(messages: list[dict]) -> list[dict]:
    """规范化历史。历史里的助手消息不带前缀。

    只输出 user/assistant。截断说明**不**作为 system 消息插进对话流——
    那会造出第二条 system，各家兼容实现处理不一致（见 build_messages 的注释）。
    截断信息由 build_messages 并入第一条 system。
    """
    out = _normalized(messages)
    tail = out[-MAX_TURNS * 2:]
    while tail and tail[0]["role"] != "user":
        tail.pop(0)
    return tail


def count_omitted(messages: list[dict]) -> int:
    """被 MAX_TURNS 截掉的历史**轮数**（不是条数）。

    口径是「轮」而不是「条」：说「省略 16 条」时，模型无从判断那是 16 轮
    还是 16 句，而 16 条正好是 8 轮——两种读法差一倍。轮数按「连续的
    user 段」计，与 format_history 合并相邻同角色之后的形状一致。
    """
    kept = format_history(messages)
    total = []
    for m in _normalized(messages):
        if total and total[-1]["role"] == m["role"]:
            continue
        total.append(m)
    # kept 之外的部分，按 user 的条数算轮：一轮以 user 开头。
    dropped = len(total) - len(kept)
    if dropped <= 0:
        return 0
    head = total[:dropped]
    turns = sum(1 for m in head if m["role"] == "user")
    # 被截断的首条可能是 assistant（它的 user 已在更早的一侧），补一轮。
    if head and head[0]["role"] == "assistant":
        turns += 1
    return max(1, turns)


def _normalized(messages: list[dict]) -> list[dict]:
    """过滤 + 合并相邻同角色，不做截断。format_history 与 count_omitted 共用。

    相邻的同角色消息会被**合并**。这一步不是洁癖：中间夹一条被过滤掉的
    `tool` 或空消息时，前后两条 user 会贴在一起，`[user, assistant, user, user,
    assistant]` 这种序列部分上游直接 400，另一些会把它当成同一句话的两段。
    合并比丢弃安全——丢掉就会少一轮内容，合并只是把两段接起来。
    """
    out: list[dict] = []
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
        if out and out[-1]["role"] == role:
            out[-1]["content"] = out[-1]["content"] + "\n" + content
        else:
            out.append({"role": role, "content": content})
    return out


def build_messages(
    persona: dict,
    world: str,
    hits: list[dict],
    history: list[dict],
    user_text: str,
    memories: list[dict] | None = None,
    scene: str = "",
) -> list[dict]:
    """组装一次完整请求。

    跨世界提示已取消（WORLD-001，DEC-002）：未授权时另一世界的信息
    不进入回答，既不给片段，也不给「你可以说好像想起来什么」这种提示。
    检索层与这里双重把关 —— 只靠其中一层都会被另一层绕过。
    """
    world = world if world in WORLD_PROMPT else "normal"
    parts = [persona["system_prompt"].strip(), WORLD_PROMPT[world],
             # 不编造（AUD-14）。放在很靠前的位置，紧跟世界线：
             # 它约束的是「哪句话能说成事实」，属于回答的地基，
             # 不是附带的注意事项。放远了会被当成可选建议忽略掉。
             NO_FABRICATION_PROMPT,
             TERMINAL_PROMPT.format(prefix=YUKI_PREFIX),
             # 字符画（CHAT-017）。紧跟在通讯形式后面：它改的是「形式」，
             # 不是人。放远了模型会把它当成可选建议，忽略掉。
             ART_PROMPT if ART_ALLOWED_WORLDS.get(world) else ART_OFF_PROMPT]

    if hits:
        parts.append(MEMORY_PROMPT)
        block = render_hits(hits, world)
        if block:
            parts.append("=== 原作记录 ===\n" + block + "\n=== 记录结束 ===")

    # 共同经历单独一块，来源与前一块分开（CHAT-016）。
    # 这里**不**给它「重要度」之类的排序输入：记忆不能靠权重变成人格指令（CHAT-015）。
    mem_block = render_shared_memory(memories or [], world)
    if mem_block:
        parts.append(SHARED_MEMORY_PROMPT)
        parts.append("=== 共同经历 ===\n" + mem_block + "\n=== 记录结束 ===")

    # 场景状态（CHAT-013）。位置放在示例之前、检索之后：它是「此刻」的信息，
    # 比原作片段更贴近当前这句，但**不该**盖过人格——所以不进人格卡。
    # 传空串就不出现这一块：没有状态时说「没有状态」，比不说更糟。
    if scene and scene.strip():
        parts.append(SCENE_PROMPT)
        parts.append("=== 此刻 ===\n" + scene.strip() + "\n=== 状态结束 ===")

    system = "\n\n".join(parts)

    msgs: list[dict] = [{"role": "system", "content": system}]

    # few-shot 示例：**折进 system，不铺成消息对**（AUD-14）。
    #
    # 为什么改成这样 —— 这是本轮唯一真正有效的修法，理由必须留在这里：
    #
    # 原做法是把每条示例直接 append 成一对 user/assistant 消息。实测发现
    # 模型会把它们当成**真实发生过的对话**：空历史问「你知道信息统合思念体
    # 吗」，她答「你刚才也问过」——凭空造出一段不存在的来往。
    #
    # 对比实验（同一模型、同一句、温度 0.3，各跑 10～20 次）：
    #
    #   示例铺成消息对（原做法）      13～14 / 20 出现「刚才问过」
    #   只改 system 里的说明措辞       14 / 20（没有改善）
    #   给示例加 `name` 标记           7 / 10（没有改善）
    #   示例折叠进 system（现在）      0 / 10
    #
    # 也就是说：**这不是措辞问题，是结构问题**。只要示例和真实对话在消息
    # 序列里长得一样，模型就分不开；在 system 里加多少句「这不是真的事」
    # 都压不住。折成文本之后，消息序列里只剩 system + 真实对话，问题消失，
    # 而语气也保住了（实测回复仍是「诶？……那个，我没有听说过」这种形状）。
    #
    # 代价要说清楚：few-shot 的「对话形状」弱化成了一段带标记的文本。
    # 目前没观察到语气退化，但这是需要真实试聊持续观察的点，不是一次性结论。
    examples = persona.get("examples", [])
    sent = 0
    example_lines: list[str] = []
    for ex in examples:
        u, a = ex.get("user", ""), strip_prefix(ex.get("assistant", ""))
        if u and a:
            example_lines.append(f"对方：{u}")
            example_lines.append(f"你：{a}")
            sent += 1

    if sent:
        msgs[0]["content"] = (
            msgs[0]["content"]
            + f"\n\n【语气示例 · 共 {sent} 组】"
              "下面是示例对话，用来说明说话方式和分寸。"
              "**它们不是你和对方的共同经历，也没有真正发生过**；"
              "对方没有问过里面那些话。不要照抄其中的句子，也不要以为你们聊过这些。"
              "示例之后的对话才是这次通讯的真实内容。\n\n"
            + "\n".join(example_lines))

    # 截断说明同理并入 system，不插进对话流。
    omitted = count_omitted(history)
    if omitted:
        msgs[0]["content"] += f"\n\n（此前还有大约 {omitted} 轮更早的对话，已省略。）"

    msgs.extend(format_history(history))
    # 正文接在最后。若历史的最后一条也是 user（上一轮没拿到回复、或那条回复
    # 为空被过滤掉了），两条 user 会贴在一起——部分上游直接 400。
    # 这里合并而不是丢弃：丢掉任何一条都会少内容。
    # 注意判断必须是 user：没有示例时 msgs[-1] 是 system，把正文并进去
    # 就再也不会作为用户提问出现。
    if msgs[-1]["role"] == "user":
        msgs[-1]["content"] = msgs[-1]["content"] + "\n" + user_text
    else:
        msgs.append({"role": "user", "content": user_text})
    return msgs


def persona_for(persona_map: dict[str, dict], world: str) -> dict:
    cand = WORLD_META.get(world, WORLD_META["normal"])["persona"]
    if cand in persona_map:
        return persona_map[cand]
    # 兼容: 人格卡缺失时退到第一个
    return next(iter(persona_map.values()))
