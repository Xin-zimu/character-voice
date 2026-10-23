#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""scene.py — 当前场景的必要状态（CHAT-013 / CHAT-008）

零依赖，只用标准库与进程内状态。

## 为什么要单独一层

CHAT-013 要求「记住当前场景中的必要状态，例如眼镜是否摘下、书是否合上、
是否正在共同场景中」。这些状态有三个特点，直接塞进检索或历史都不合适：

1. **是状态，不是事实。** 「眼镜摘了」之后又戴上，前者就不成立了。
   历史里两句话都还在，谁也不知道最后是哪样。所以要有覆盖语义。
2. **属于她，不属于用户。** 「我把眼镜摘了」说的是用户，不能改她的状态。
3. **两个世界各一份。** 崩溃世界的她本来就戴着眼镜；主线的她不戴。
   切换世界不能继承。

## 怎么更新

只认**明确的主语 + 明确的动作**：

- 「（把眼镜摘下来）」—— 括号动作，默认是**她**做的。这是 CHAT-007 的约定：
  括号里是她自己的动作。所以这一条算数。
- 「我摘了眼镜」「我把眼镜摘了」—— 主语是「我」，说的是用户，**不算**。
- 「你别摘眼镜」「如果你摘下眼镜」「你戴着眼镜吗」—— 否定、假设、疑问，
  **都不算**。SET_RE 要求的是陈述句形状，这三类会被 EXCLUDE 挡掉。
- 「眼镜呢」「书合上了吗」—— 疑问，不算。

**拿不准就不更新。** 状态宁可缺失，不可编造（CHAT-013 原文）。
所以这里的匹配刻意写得窄：宁可漏掉一句真的「（摘下眼镜）」，
也不要因为一句反问把状态改错——改错之后她会在后面所有轮次里都戴着错的设定。

## 共同场景（CHAT-008）

「一起看书吧」**不算**进入共同场景。「我在部室门口」「我到活动室了」才算。
判据同样是明确的空间表述：出现具体地点 + 到达/在场，且主语是用户。
"""
from __future__ import annotations

import re

WORLDS = ("normal", "crash")

#: 会被跟踪的状态项。新增一项要同时改：
#: `SCENE_PROMPT`（怎么用）、`DEFAULTS`（初始值）、`SET_RE` / `CLEAR_RE`（怎么改）。
TRACKED = ("glasses", "book", "together")

#: 每项初始值。`None` 表示**不知道**，不是 `False`。
#: 这一条很重要：主线世界的她不戴眼镜是设定，但「书是否合上」开局就是未知，
#: 未知时不该主动提，也不该默认成「合上了」。
DEFAULTS = {
    # 主线卡不戴眼镜；崩溃卡戴。按世界给不同初始值，不改就一直是这个。
    "glasses": {"normal": False, "crash": True},
    "book": {"normal": None, "crash": None},
    # 默认隔着终端（CHAT-008），不是共同场景。
    "together": {"normal": False, "crash": False},
}

#: 状态 → 给模型看的那句话。`None` 不输出（不知道就不说）。
LABEL = {
    "glasses": {True: "戴着眼镜", False: "没有戴眼镜"},
    "book": {True: "手里拿着一本打开的书", False: "书是合上的"},
    "together": {True: "对方刚说过自己到了同一个地方，你们此刻在同一个空间",
                 False: "你们不在同一个空间，隔着终端说话"},
}

# ---------------------------------------------------------------
# 括号动作：默认主语是「她」（CHAT-007）
# ---------------------------------------------------------------
#: 例：（把眼镜摘下来）（摘下眼镜，放在桌上）（合上书）
_BRACKET = re.compile(r'[（(]([^）)]{0,80})[）)]')

# 「书」与动作之间的距离要给够：「又翻开」「后来又合上」「重新打开」都算。
# 但**不能**无限宽：一段里同时出现两个动作时靠前后顺序决定结果，
# 窗口太宽会让前一个动作把后一个也匹配进去（见下面的分段处理）。
# 不含右括号：「（不用在意）戴上眼镜」里那个「不用」不是对「戴」的否定。
_SPAN = r'[^，。！？；,）)]{0,8}'
_GLASSES_OFF = re.compile(rf'(摘下|取下|摘掉|摘了|拿掉|拿下|除去){_SPAN}眼镜|眼镜{_SPAN}(摘下|取下|摘掉|拿掉)')
_GLASSES_ON = re.compile(rf'(戴上|戴好|带上|戴回|戴回去|架回|推回|扶好){_SPAN}眼镜|眼镜{_SPAN}(戴上|戴好|戴回|架回|推回)')
_BOOK_OPEN = re.compile(rf'(翻开|打开|摊开|展开){_SPAN}书|书{_SPAN}(翻开|打开|摊开|展开)')
_BOOK_CLOSE = re.compile(rf'(合上|阖上|关上|收起|放下|扣上){_SPAN}书|书{_SPAN}(合上|阖上|关上|收起|扣上)')
# 「又戴回去」「重新打开」这类省略宾语的第二次动作：只在同一段括号里
# 已经出现过同类动作时才算，否则「（又合上）」（合上的是窗）会被误判成书。
_REPEAT_GLASSES_OFF = re.compile(r'(摘下来|摘下|取下|拿掉)')
_REPEAT_GLASSES_ON = re.compile(r'(戴回|戴上|戴上|架回|戴好)')
_REPEAT_BOOK_OPEN = re.compile(r'(翻开|打开|摊开|展开)')
_REPEAT_BOOK_CLOSE = re.compile(r'(合上|阖上|关上|收起|扣上)')

#: 疑问 / 否定 / 假设 / 条件。出现这些词就**不更新**。
#: 不区分「她说的」和「用户说的」—— 两侧都挡，因为疑问句无论谁问都不是陈述。
_QUESTION = re.compile(r'(？|\?|吗|呢|么|是否|要不要)')
# 「有没有」「是不是」这类正反问，真值是不确定的，不能当陈述读。
# 单列一条是因为它含「没有」，看着像否定，其实是在问。
_A_NOT_A = re.compile(r'(有没有|是不是|会不会|要不要|能不能|算不算|合没合)')
#: 否定。要允许否定词和动作词之间隔着宾语（「别**把书**合上」「不用**把眼镜**摘了」），
#: 所以中间那一段用 `[^，。！？；,]{0,6}` 而不是只允许「要/用/必」。
#: 只看「不」和动作词相邻会把「别把书合上」漏掉——那是明令禁止，不是陈述。
_NEGATED_ONLY = re.compile(
    r'(不|别|没|未|甭|勿)(要|用|必|把|将)?[^，。！？；,]{0,6}?(摘|戴|合上|阖上|关上|翻开|打开|收起|放下)')
_HYPOTHETICAL = re.compile(r'(如果|假如|要是|若是|万一|假使|就算|即使|哪怕)')

# ---------------------------------------------------------------
# 共同场景：要具体地点 + 到达/在场，主语是用户
# ---------------------------------------------------------------
_PLACE = re.compile(
    r'(活动室|部室|文艺部|教室|图书馆|图书室|天台|走廊|校门|门口|车站|'
    r'家里|楼下|公园|操场|体育馆|社团|书店)')
#: 到达 / 在场。**「在」必须紧跟地点，不能隔字**：
#: 「我在想活动室的事」里的「在」是「正在」，不是「身处」。
#: 所以拆成两条：强表述（到/进/来了/坐在/待在）可以带间隔，
#: 弱表述（「我在」）要求「我在」后面直接就是地点。
_ARRIVE_STRONG = re.compile(
    r'(我(已经|刚|才|正好)?(到|进)了?|我(刚|才|正好)?(坐|待|站)(在|到|进)|到(了|达)|'
    r'来了|进来了|正在|过来)')
_ARRIVE_AT = re.compile(r'我(就|已经|正)?在(?=' + _PLACE.pattern + r')')
#: 不是在说自己到了。
#: `_PLACE_NEG` 管句子层面的排除；单字 `回` 只管「回来路上」这种，
#: 不放单字 `想` —— 「想到图书馆去」和「我在想图书馆」要分开，
#: 前者由「去」之后的判断处理，后者由「我在」不紧跟地点挡住。
_PLACE_NEG = re.compile(r'(想去|去过|没去|不在|离开|走了|回来|回去|路上|想起|问|不是)')
#: 「想 / 关心 / 说」这类动词一旦跟在地点前面，就说明不是在说身处何地。
_PLACE_THINK = re.compile(r'(想|关心|猜|觉得|在意|惦记|担心)\S{0,4}$')

# ---------------------------------------------------------------
# 离场：只有进入没有退出，状态会一直卡在「同处一室」（R-05）
# ---------------------------------------------------------------
#: 明确离开的表述。只认**明确的**离开：回家、走了、先回去、道别。
#: 不认「走了吗」「该回去了吗」这类疑问——它们先被疑问检查挡掉。
#: 也不认单字「走」：「走神」「走开一下」不是离场。
#:
#: 「再见 / 晚安 / 下次再聊」也收进来：它们不是空间陈述，但在这套设定里
#: 等于宣告这次相处结束。**不收**「拜拜」以外的拟声词，也没有收「嗯」这类
#: 应答——分不清是同意还是道别的话，宁可不更新（CHAT-013 末条）。
_LEAVE = re.compile(
    r'(我(先|就|要|得|也)?(回家|回去|走了|离开|出去了|出门了)|'
    r'我(也)?不(在|留)了|下次再聊|改天聊|我先去忙|下了|晚安|'
    r'^\s*再见|^\s*拜拜|^\s*那我(先)?(走|回去|下线))')
#: 括号内写明主语是用户：括号动作默认是她，但「我…」写明了就不是。
#: 「我」后面允许少量字再接动作（「我把眼镜摘下来」「我合上书」）。
_USER_SUBJECT = re.compile(r'^\s*我')
#: 她说的话里出现「我」通常是她自己（「（我合上书）」是她合上）。
#: 但「我到活动室了」这类空间表述说的是**用户**在不在，不能算她到了。
#: 所以空间判断在处理她的回复时整段跳过，见 `update_from_reply`。


def _bracket_actions(text: str) -> list[str]:
    return [m.group(1) for m in _BRACKET.finditer(text or "")]


def _safe_clause(clause: str) -> bool:
    """这一段能不能拿来更新状态。疑问、否定、假设一律不算。"""
    clause = clause or ""
    return not (_QUESTION.search(clause) or _HYPOTHETICAL.search(clause)
                or _A_NOT_A.search(clause) or _NEGATED_ONLY.search(clause))


def _first(pattern: re.Pattern, text: str) -> str | None:
    m = pattern.search(text)
    return m.group(0) if m else None


def _last_action(part: str, off_pat: re.Pattern, on_pat: re.Pattern,
                 seen_object: bool, repeat_off: re.Pattern, repeat_on: re.Pattern):
    """一段里同一对反义动作谁最后出现。

    `off_pat` 命中 → False，`on_pat` 命中 → True，都没命中 → None。
    两个都命中时取**结束位置更靠后**的那个；位置相同（几乎不可能）时取 on。

    省略宾语的第二次动作（「又合上」「又戴上」）只在该段提过宾语时才认，
    否则「（又合上）」（合上的是窗）会被算到书上。
    **两个方向都要有**：只给「重复打开」不给「重复合上」，
    「（翻开书，又合上）」就会被读成翻开了——反过来正好。
    """
    cands = []
    for m in off_pat.finditer(part):
        cands.append((m.end(), False))
    for m in on_pat.finditer(part):
        cands.append((m.end(), True))
    if seen_object:
        for m in repeat_off.finditer(part):
            cands.append((m.end(), False))
        for m in repeat_on.finditer(part):
            cands.append((m.end(), True))
    if not cands:
        return None
    cands.sort(key=lambda c: (c[0], c[1]))
    return cands[-1][1]


def parse_updates(user_text: str, *, from_reply: bool = False
                  ) -> tuple[dict, list[str]]:
    """从这一轮的话里读出场景状态变化。

    返回 `(更新, 命中说明)`。更新里只包含**确定**要改的项。

    看三处：
      1. 括号动作 —— 按 CHAT-007，括号里默认是**她**的动作。
         但括号里写明主语是「我」时除外：那是用户在描述自己（R-05）。
      2. 明确的空间表述 —— 判断是否进入共同场景。**只在用户说的时候看**：
         她说「我到活动室了」不代表用户到了。
      3. 明确的离场表述 —— 把同处一室置回（R-05）。进出都要有，否则
         一旦置上就永远卡在「同一个空间」。

    用户正文里的「我摘了眼镜」**不**会改她的状态，因为不走 1 的路。
    这一条由 `test_scene.py::test_02` 钉住。

    `from_reply=True` 表示这句话是**她**说的（assistant 已完成的回复）：
    括号动作照样算她的，但空间/离场一律不算——那两件事说的是用户在哪。
    """
    updates: dict = {}
    notes: list[str] = []
    text = user_text or ""

    # 整条消息里提过什么宾语。**跨括号累积**：一条回复可能是
    # 「（把眼镜摘下来）……（又戴上了）」，宾语在第一个括号里提过，
    # 第二个括号只写「又戴上了」。若按单个括号算 `seen_object`，
    # 第二个括号就不会被认出来，最终状态停在「摘下」——正好是反的。
    seen_glasses = bool(_GLASSES_OFF.search(text) or _GLASSES_ON.search(text))
    seen_book = bool(_BOOK_CLOSE.search(text) or _BOOK_OPEN.search(text))

    for action in _bracket_actions(text):
        # 括号里写明「我」= 用户在说自己的动作，不是她的（R-05）。
        # CHAT-007 的「默认是她」不等于「永远是她」。
        if _USER_SUBJECT.match(action):
            continue
        # 只判断**括号里边**的内容：「（不用在意）戴上眼镜」里那个「不用」
        # 是对「在意」的否定，不是对「戴」的否定。整段一起判断会把它当成
        # 「不要戴」，于是这句本该更新状态的陈述被跳过。
        if not _safe_clause(action):
            continue
        # 一段括号里可能有两步动作（「把书合上，又翻开」）。按逗号切开逐段判断，
        # 取**最后一个**明确动作作为结果——动作有先后，后发生的才是当前状态。
        # 不切开就会两个模式都命中，按 elif 顺序取到先出现的那个，正好取反。
        parts = [p for p in re.split(r"[，,；;]", action) if p.strip()]
        for part in parts:
            if not _safe_clause(part):
                continue
            # 同一段里可能有两个动作（「把眼镜摘下来又戴上」「摘下来戴好」）。
            # 不能按模式顺序取第一个命中的——那样「摘下又戴上」会被读成摘下了。
            # 取**位置最后**的那个：后面发生的才是结果。
            g = _last_action(part, _GLASSES_OFF, _GLASSES_ON, seen_glasses,
                             _REPEAT_GLASSES_OFF, _REPEAT_GLASSES_ON)
            if g is not None:
                # **覆盖而不是跳过**：一条回复里可能有多个括号
                # （「（把眼镜摘下来）……（又戴上了）」），后一个才是最终状态。
                # 写成 setdefault 或只在没设过时赋值，就会停在第一个动作上。
                updates["glasses"] = g
                notes.append(f"括号动作「{part}」→ {'戴上' if g else '摘下'}眼镜")
            b = _last_action(part, _BOOK_CLOSE, _BOOK_OPEN, seen_book,
                             _REPEAT_BOOK_CLOSE, _REPEAT_BOOK_OPEN)
            if b is not None:
                updates["book"] = b
                notes.append(f"括号动作「{part}」→ {'翻开' if b else '合上'}书")

    # 空间相关的判断只在**用户**说话时做：她说的「我到活动室了」
    # 说的是故事里的人，不是屏幕前的用户。
    if from_reply:
        return updates, notes

    body = _BRACKET.sub(" ", text)
    # 疑问要**在切句之前**判断：先按问号切开，切出来的每段都不含问号，
    # 后面的疑问检查就再也看不到它了（R-05 的「我到活动室了？」就是这样漏的）。
    if _QUESTION.search(body):
        # 整条都是疑问句时不做空间更新；但括号动作已经在上面处理过，
        # 不受影响（「（摘下眼镜）你在吗」仍然算摘下）。
        return updates, notes

    for clause in re.split(r"[，。！？!?；;\n]", body):
        clause = clause.strip()
        if not clause or not _safe_clause(clause):
            continue

        # 离场优先于入场：同一句里既说到了某地又说要走时，以「离开」为准。
        # 注意这里**不**用 `_PLACE_NEG` 过滤——那个正则本身就把「走了」「回去」
        # 当成「不是到达」的标记，用它来判断「是不是离场」会互相打架。
        if _LEAVE.search(clause):
            if updates.get("together") is not False:
                updates["together"] = False
                notes.append(f"「{clause}」→ 离开共同场景")
            continue

        m = _PLACE.search(clause)
        if not m or not _arrived(clause, m):
            continue
        if _PLACE_NEG.search(clause):
            continue
        # 主语必须是「我」：她说「我到活动室了」不算用户到了。
        if not re.search(r"我", clause):
            continue
        updates["together"] = True
        notes.append(f"「{clause}」→ 进入共同场景")
        break

    return updates, notes


def _arrived(clause: str, place: re.Match) -> bool:
    """这句话是不是在说「我到了某地」，而不是「我在想某地」。

    两条路，任一成立即可：
      1. 强表述（到/进/坐在/待在…）出现在句子里；
      2. 「我在」后面**紧接**地点（允许「就在」「已经在」「正在」几个虚词）。
    第二条不能放宽成「句子里有『在』」：「我在想活动室的事」会立刻误判。
    """
    # 地点前面紧挨着「想 / 关心 / 说」这类动词时，说的不是身处何地。
    if _PLACE_THINK.search(clause[:place.start()]):
        return False
    if _ARRIVE_STRONG.search(clause):
        return True
    return bool(_ARRIVE_AT.search(clause))


class Scene:
    """一个世界的场景状态。

    故意做成很小的字典：状态项是枚举内的固定几项，不是任意键值对。
    这样「她会编造没记录的事」这条风险可以靠「没有这一项就是不知道」控住。
    """

    def __init__(self, world: str):
        if world not in WORLDS:
            raise ValueError(f"未知世界 {world!r}；只接受 {WORLDS}")
        self.world = world
        self.state: dict = {k: DEFAULTS[k][world] for k in TRACKED}

    def apply(self, updates: dict) -> list[str]:
        """把更新写进状态。返回值里列的是**这次真的变了的**项。"""
        changed = []
        for key, value in updates.items():
            if key not in TRACKED:
                continue
            if self.state.get(key) != value:
                self.state[key] = value
                changed.append(key)
        return changed

    def update_from(self, user_text: str) -> tuple[list[str], list[str]]:
        updates, notes = parse_updates(user_text)
        return self.apply(updates), notes

    def update_from_reply(self, reply_text: str) -> tuple[list[str], list[str]]:
        """她说完一条回复之后，把她自己的动作记进状态（R-05）。

        为什么要有这一条：状态原来只从**用户这句话**更新，于是她说
        「（把眼镜摘下来）」之后，下一轮 prompt 里仍然是「戴着眼镜」——
        V-CONT-04 要求的「她说摘下 → 下一轮记住」根本没接通。

        只处理括号动作：空间与离场说的是**用户**在哪，不能从她的回复里读。
        """
        updates, notes = parse_updates(reply_text, from_reply=True)
        return self.apply(updates), notes

    def known(self) -> dict:
        return {k: v for k, v in self.state.items() if v is not None}

    def render(self) -> str:
        """给模型看的状态块。**未知项不输出**——没有记录就不提，不猜。"""
        lines = []
        for key in TRACKED:
            value = self.state.get(key)
            if value is None:
                continue
            label = LABEL[key].get(value)
            if label:
                lines.append(f"- {label}")
        return "\n".join(lines)


class SceneRegistry:
    """两个世界各一份状态。切换世界不继承，也不清空另一边。"""

    def __init__(self):
        self._scenes = {w: Scene(w) for w in WORLDS}

    def get(self, world: str) -> Scene:
        if world not in self._scenes:
            raise ValueError(f"未知世界 {world!r}；只接受 {WORLDS}")
        return self._scenes[world]

    def update_from(self, world: str, user_text: str) -> tuple[list[str], list[str]]:
        return self.get(world).update_from(user_text)

    def update_from_reply(self, world: str, reply_text: str) -> tuple[list[str], list[str]]:
        return self.get(world).update_from_reply(reply_text)

    def reset(self, world: str | None = None):
        for w in ([world] if world else WORLDS):
            self._scenes[w] = Scene(w)


#: 进程内单例。场景状态是**会话态**，不落盘：
#: 重启之后「刚才摘下眼镜」这件事已经过去了，把陈旧状态读回来比丢掉更糟。
REGISTRY = SceneRegistry()


SCENE_PROMPT = """【此刻的状态】
下面是你这一侧当前的状态，只列了**有记录的**几项。
- 这是记录，不是要复述的内容。对方问起才说，没问就不提。
- 没有列出的项就是没有记录。不要替它编一个，也不要从常识推。
- 状态会随对话变化：对方说的和你这边的事都可能改变它。别拿上一轮的描述当现在的。"""
