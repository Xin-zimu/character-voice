#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""字符画的**显示宽度**换算：判断一段文字算不算「字符画」，以及它对齐没对齐。

CHAT-017 还没定生成方式（PEND-006：模板 / 自由生成未定案），所以这里**不做**
生成、不做选型、也不决定她该不该画。只提供一层与选型无关的能力：

1. `cell_width()` —— 一个字符在等宽字体里占几格。这是所有对齐判断的基础，
   也是最容易做错的地方（见下）。
2. `is_art()` —— 这段回复是不是字符画。**窄，刻意的窄**：只要有一行看起来
   是话，就不算。
3. `check_art()` —— 画得对不对齐。返回的问题会被界面上标 △。

为什么要单独 `cell_width()`：等宽字体不是「一个字符一格」。
- 汉字、全角标点占 2 格（East Asian Width 的 W/F）；
- 盲文（U+2800–U+28FF）和块元素（U+2580–U+259F）虽然窄，但常常被当成
  图案的像素来用，而东亚字体里它们会被画成全角——**这正是 PEND-006 里
  「Braille / Block 点阵」这条路的风险所在**，详见 `braille_font_risk()`；
- emoji 与组合字符（`e` + U+0301）的宽度在不同字体里差得更多，
  这里只能给一个近似值，并在 `check_art()` 里把它们直接报出来。

标准库没有 `unicodedata.east_asian_width` 之外的宽度信息，也不该为此引依赖，
所以这里的口径是「按 East Asian Width 的 W/F 算 2 格，其余算 1 格」，
再对块元素单独放行（它们虽然不是 W，但当像素用时必须按 1 格算才有意义）。

这一层的验证**只能靠人工实看字体**（V-ART-02 的 D 类是必要条件）：
本模块只能说「按这套口径算，它是对齐的」，不能说「它在你的屏幕上好看」。
"""
import re
import unicodedata

# 盲文与块元素：当像素用。
BRAILLE_LO, BRAILLE_HI = 0x2800, 0x28FF
BLOCK_LO, BLOCK_HI = 0x2580, 0x259F


def cell_width(ch: str) -> int:
    """一个字符在等宽字体里占几格（近似值，口径见模块文档）。

    三类例外，都不是「查表能得出来」的，是**选择**：
    - 盲文/块元素：当像素用，按 1 格（虽然东亚字体常把它们画成全角）；
    - emoji：`east_asian_width` 对多数 emoji 返回 W（2 格），但浏览器按 1 个
      字形排，两端据此得出的「宽不宽」会分叉。按 1 格算，并把「宽度不可靠」
      交给 `check_art()` 报——判据只该判「是不是画」，宽度靠不靠得住是另一件事；
    - 组合字符：本身不占格。
    """
    o = ord(ch)
    # 盲文/块元素：即使是「宽」字体也常被当窄图元用，按 1 格算。
    if BRAILLE_LO <= o <= BRAILLE_HI or BLOCK_LO <= o <= BLOCK_HI:
        return 1
    if unicodedata.combining(ch):
        return 0
    if _is_emoji(ch):
        return 1
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def text_width(line: str) -> int:
    return sum(cell_width(c) for c in line)


def braille_font_risk(line: str) -> str:
    """盲文/块元素落在**有汉字**的行里时，宽度最不可靠。

    东亚等宽字体常把这些码位画成全角，于是「按 1 格排好的图」在那边会错位。
    纯盲文行没有汉字可参照，错位只影响整块图案的位置，还能看；
    混排就真的会散。这是提示，不是错误——V-ART-02 要求人工实看。
    """
    has_braille = any(BRAILLE_LO <= ord(c) <= BRAILLE_HI or BLOCK_LO <= ord(c) <= BLOCK_HI
                      for c in line)
    has_cjk = any(_is_wide(c) for c in line)
    if has_braille and has_cjk:
        return "盲文/块元素与汉字混排，等宽字体下的宽度不可靠"
    return ""


def _is_wide(ch: str) -> bool:
    o = ord(ch)
    if BRAILLE_LO <= o <= BRAILLE_HI or BLOCK_LO <= o <= BLOCK_HI:
        return False
    return unicodedata.east_asian_width(ch) in ("W", "F")


# ---------------------------------------------------------------
# 什么算字符画
#
# 判据刻意窄：她平时说话会被误判成画，比漏判难看得多——
# 误判会让一条正常回复被标 △，还会让显示走不对齐的那条路。
# 所以要求**同时**满足：
#   1. 至少 2 行非空行；
#   2. 每一行都窄（宽字符不超过 40 格）——一整段话不算；
#   3. 至少一半的行**没有句读**；
#   4. 有图案的迹象：连续 3 个字都是符号/盲文/块元素/空格，
#      或者某行有两处以上的连续 2 个空格（缩进对齐）。
#
# 边界（写在这里，免得以后有人以为漏判是 bug）：
#   - 四行诗：每行都窄、多半没有句读，但没有连续的符号串、也没有双空格，
#     所以**不算**字符画。宁可漏判。
#   - 表格：有竖线「│」和横线，会被算成字符画（本来就该按图案对齐看）。
# ---------------------------------------------------------------
_SENTENCE_PUNCT = set("，。！？；：、…,.!?;:")
_ART_MAX_WIDTH = 40
_ART_RUN = 3
# 右边缘差多少格才算「不齐」。留 1 格余量：很多图案本就有一两行的收尾短一点。
_ART_EDGE_GAP = 3
# 框线字符：这些字符出现在行首时，各行必须上下对齐，否则框是断的。
# 自由形状（猫、雪花、盲文点阵）的缩进不参与左边距判定，见 check_art 的说明。
_FRAME_CHARS = set("│┃┌┏└┗├┣┤┫┬┳┴┻┼╋╭╮╰╯╔╗╚╝║╠╣╦╩╬")
# 「短图案行」的最大宽度（格）。超过这个宽度就不再凭「符号够密」认它，
# 因为长行里符号密的多半是排版（分隔线、表格），不是画。
_SHORT_ART_WIDTH = 16


def _density(line: str) -> tuple:
    """(图案字符数, 非空格字符数)。

    抽出来是因为 `_signals()` 与「短行」判据都要这两个数，
    各算一遍迟早会算出两个不同的值——那正是前后端分叉的老路。
    """
    symbols = 0
    nonspace = 0
    for c in line:
        if c == " ":
            continue
        nonspace += 1
        if not c.isalnum() and not _is_wide(c) and not unicodedata.combining(c) \
                and not _is_emoji(c):
            symbols += 1
    return symbols, nonspace


def _signals(line: str) -> tuple:
    """(有连续符号串, 有两处以上双空格)

    什么算「符号」：非字母数字、非宽字符、非组合字符。**emoji 不算**——
    `str.isalnum()` 认为它是字母数字，这条口径直接决定「🌙」被算成图案元素
    还是内容。前端逐字对齐了这条判断（`artSignals`），改这里就要同步改那边，
    `test_frontend.js` 的逐例比对会报。
    """
    run = longest_run = 0
    # 连续串里没有图案字符时不算数：`    你好` 前面那四个空格也不是图案。
    run_has_symbol = longest_has_symbol = False
    for c in line:
        # emoji 单独拎出来判「不是符号」：`str.isalnum()` 对它们是 True，
        # 但前端用的是 `\p{L}`/`\p{N}`，emoji 不属于这两类。两端必须用**同一条**
        # 口径，否则「🌙🌙」在自检里不是画、在界面上是画。这里以 emoji 归类为准。
        if c == " " or (not c.isalnum() and not _is_wide(c)
                        and not unicodedata.combining(c) and not _is_emoji(c)):
            run += 1
            run_has_symbol = run_has_symbol or c != " "
            if run > longest_run:
                longest_run, longest_has_symbol = run, run_has_symbol
        else:
            run = 0
            run_has_symbol = False
    # 「符号」只算图案字符，**空格不算**。两个信号各自独立：
    #   1. 有连续 3 个图案字符（`───`、`/\\_/\\`、`( o.o )`）；
    #   2. 有两处以上双空格，且至少有一个图案字符（靠空格对齐的 ASCII 画）。
    # 早先第 1 条把空格也算进「符号」，于是**缩进两行的普通文字**被判成画：
    # 「    你好呀 /    今天很好」四格缩进正好凑够连续 3 个，整段被换成保空白排版。
    # 这不是理论问题——模型排版、引用、列表都可能缩进。
    symbols, nonspace = _density(line)
    # **符号要占多数**才算图案行（2026-10-26 实测误判）。
    # 原来只要求「至少 1 个图案字符」，于是缩进代码被整段当成画：
    # 「    return x + 1」= 两处双空格 + 一个加号，条件正好成立，
    # 代码被换成保空白排版，还多一条「左边距不一致」。
    # 真实图案行的绝大多数非空格字符本身就是符号（~~~~、┌──┐、> ^ <），
    # 而代码行里符号只是零星点缀（return x + 1 是 1/10）。
    # 取三分之一做阈值：( o.o ) 是 3/5 能过，return x + 1 是 1/10 过不去。
    dense = bool(nonspace) and symbols * 3 >= nonspace
    return (longest_run >= _ART_RUN and longest_has_symbol and dense,
            line.count("  ") >= 2 and dense)


def _art_line_kind(line: str) -> str:
    """这一行在**分块**里算什么：`"hard"`（明确是图案行）/ `"soft"`（可能是）/ `""`。

    为什么要多一个 `soft`（2026-10-26 复采查出的漏判）：
    真实输出里 A1 rep2 是这样一张猫——

        /\\_/\\
        ( o.o )
         > ^ <

    中间那行括号是**顶格**的。`_signals()` 要求「连续 3 个图案字符」，
    `( o.o )` 只有 `( o . o )` 这样被字母隔开的散符号，最长连续串只有 1，
    于是整只猫**一块都拼不出来**——界面按普通文字排版，行首空格被折叠，
    脸和嘴对不上猫耳朵。同一只猫只要脸那行缩进 2 格（A1 rep1）就能成块，
    这说明漏判的是判据，不是模型画得不对。

    `soft` 刻意收得很窄，两条都满足才算：
      - 行宽不超过 `_SHORT_ART_WIDTH` 格（长行里符号密的多半是分隔线、表格）；
      - 符号占非空格字符的三分之一以上，**且至少 2 个图案字符**（挡掉单独一个 `~`）；
      - **不以框线字符开头**：`│ ok │` 这种框内写字的一行是表格行，不是图案行，
        仍按原来的取舍折行（`test_34` / PEND-011 锁住的就是这条）。
    另外在 `art_blocks()` 里还要求整块**至少含一个 hard 行**——
    `soft` 只能把已有的图案补齐，**不能自己起块**。所以「两行普通文字恰好
    符号偏多」不会突然变成图案块。
    """
    if not line.strip() or text_width(line) > _ART_MAX_WIDTH:
        return ""
    if any(_signals(line)):
        return "hard"
    left = line.lstrip(" ")
    if left[:1] in _FRAME_CHARS:
        return ""          # 框内写字的行：维持「宁可少判」的既有取舍
    symbols, nonspace = _density(line)
    if (text_width(line) <= _SHORT_ART_WIDTH and nonspace
            and symbols >= 2 and symbols * 3 >= nonspace):
        return "soft"
    return ""


def is_art(text: str) -> bool:
    """这段回复是不是字符画。判据见上；返回 True 时才走「保空白」的显示。"""
    lines = [ln for ln in str(text).split("\n") if ln.strip()]
    if len(lines) < 2:
        return False
    if any(text_width(ln) > _ART_MAX_WIDTH for ln in lines):
        return False
    plain = sum(1 for ln in lines if not (set(ln) & _SENTENCE_PUNCT))
    if plain * 2 < len(lines):
        return False
    return any(any(_signals(ln)) for ln in lines)


def art_blocks(text: str) -> list:
    """把这段回复拆成 [(是不是图案块, [行...])]，相邻同类的行合成一块。

    **为什么要拆块**：一条回复常常是「一句话 + 一张小图」或者「图 + 一句话」。
    整条按图案排版（保留空白、不折行）会把文字行也压成一行——实测汉字与
    ASCII 的宽度比是 1.66，一行十几字就顶到边界，句号被挤到下一行单独出现。
    所以「图案」这个待遇只能给图案那几行。

    判定逐行用 `_art_line_kind()`，但**要求连续两行都像图案**才起块：
    单独一行像图案（比如一整行 `=====` 分隔线）多半还是排版，不是画。
    `soft` 行必须挨在一个含 `hard` 行的块里才算数，不能自己起块。
    前端 `artBlocks()` 逐条对应，`test_frontend.js` 会逐例比对。
    """
    lines = str(text).split("\n")
    kinds = [_art_line_kind(ln) for ln in lines]
    n = len(lines)
    blocks = []
    i = 0
    while i < n:
        if not kinds[i]:
            blocks.append([False, [lines[i]]])
            i += 1
            continue
        j = i
        while True:
            while j + 1 < n and kinds[j + 1]:
                j += 1
            # **图案内部的空行不拆块**（2026-10-26 实测）。
            # 拆开的后果很具体：后半段变成普通文字块，white-space 一恢复
            # normal，行首空格被折叠，下边框就跳到左边与上边框错开，图案当场断掉。
            # CHAT-017 / R-08 本来就允许图案内部有空行。
            # 只跨**后面紧跟着图案行**的空行；空行之后是普通文字，照旧断块。
            k = j
            while k + 1 < n and not lines[k + 1].strip():
                k += 1
            if k > j and k + 1 < n and kinds[k + 1]:
                j = k
                continue
            break
        # `soft` 不能单独成块：整段里没有任何 hard 行，就按普通文字处理。
        has_hard = any(kinds[x] == "hard" for x in range(i, j + 1))
        if j > i and has_hard:
            blocks.append([True, lines[i:j + 1]])
            i = j + 1
        else:
            blocks.append([False, [lines[i]]])
            i += 1
    # 相邻同类合并，少画几个节点。
    merged = []
    for kind, chunk in blocks:
        if merged and merged[-1][0] == kind:
            merged[-1][1].extend(chunk)
        else:
            merged.append([kind, list(chunk)])
    return [(kind, chunk) for kind, chunk in merged]


def has_art_block(text: str) -> bool:
    """这条回复里有没有图案块。

    分段豁免用它，比 `is_art()` 更贴近规则：CHAT-017 / R-08 允许的是
    **图案块**占多行、与文字之间留空行，不是「整条必须是图案」。
    2026-10-26 实测反例：四句文字 + 空行 + 一张小图，整条不算图案，
    那个用来引出图案的空行就被报成了「分段」。
    """
    return any(kind for kind, _chunk in art_blocks(text))


def _art_runs(text: str) -> list:
    """把每个图案块再按空行切成连续段。

    右边缘只能在连续段内比：一个方框和下面隔一个空行的盲文点阵是两张画，
    宽度本来就不同。跨段比会得出「图案缺一块」的假警报。
    """
    runs = []
    for kind, chunk in art_blocks(text):
        if not kind:
            continue
        cur = []
        for ln in chunk:
            if ln.strip():
                cur.append(ln)
            elif cur:
                runs.append(cur)
                cur = []
        if cur:
            runs.append(cur)
    return runs


def check_art(text: str) -> list:
    """画得对不对齐。返回问题列表，界面按 △ 显示；空列表 = 没发现问题。

    这里只查**能自动判**的三件事：左边距是否一致、行尾多余空白、
    emoji / 组合字符（宽度不可靠）。「好不好看」「像不像那个东西」只能人看。
    """
    problems = []
    lines = str(text).split("\n")
    # 只拿非空行比左边距：图案的空白行本来就没有左边距可比。
    body = [ln for ln in lines if ln.strip()]
    if not body:
        return ["字符画里没有任何内容"]

    # 左边距：**只查有框线的图案**（2026-10-26 修的误报）。
    #
    # 原来把所有非空行的缩进放一起比，要求完全相同。实测证明这条判据是错的：
    # 自由形状的缩进本身就是形状的一部分——
    #     `   /\\_/\\` / `  ( o.o )` / `   > ^ <`
    # 第二行少一格是画法，不是错；盲文点阵、茶杯、雪花同理。
    # 24 次真实采样里 9 条画对的图案全被标了「图案会歪」，
    # 结果是用户学会忽略 △ —— 比漏报更糟。
    #
    # 真正能自动判的是**框线**：`┌ │ └` 这些竖线必须上下对齐，
    # 它们歪了才是真歪，而且歪了很显眼。自由形状交给右边缘检查和人工看。
    indents = []
    for ln in body:
        n = 0
        while n < len(ln) and ln[n] == " ":
            n += 1
        if len(ln) > n and ln[n] in _FRAME_CHARS:
            indents.append(n)
    if len(set(indents)) > 1:
        # 文案要和前端逐字一致（DEC-006 的镜像）：同一句话在两边显示得不一样，
        # 排查时会被当成两个问题。所以不用 list 的默认格式，手拼。
        problems.append("字符画各行左边距不一致（" + "/".join(str(i) for i in sorted(set(indents)))
                        + "），图案会歪")

    trailing = [i + 1 for i, ln in enumerate(lines) if ln != ln.rstrip()]
    if trailing:
        problems.append(f"字符画第 {trailing[0]} 行行尾有多余空格，某些字体会把后续行推歪")

    # 右边距：左边缘齐了、右边缘差很多，说明行宽不一致，图案会「缺一块」。
    #
    # **只在图案块内比**，不拿文字行当参照：一条回复通常是「图案 + 一句话」，
    # 那句话的宽度当然和图案不同，拿它当基准等于每张图都报错。
    # 也只在**没有**行尾空格时查——有行尾空格时上面那条更准确，
    # 两条一起报会把真正的原因埋在后面（提示只显示两条）。
    if not trailing:
        # 逐**连续段**比：一块图案里被空行分开的两段是两张画，
        # 拿它们的宽度互相比，等于每张上下叠放的图都报错。
        for run in _art_runs(text):
            rights = [text_width(ln) for ln in run]
            if len(rights) >= 2 and max(rights) - min(rights) >= _ART_EDGE_GAP:
                problems.append(
                    f"字符画各行右边缘不齐（最宽 {max(rights)} 格、最窄 {min(rights)} 格），看着会缺一块")
                break

    for i, ln in enumerate(lines):
        for c in ln:
            if unicodedata.combining(c):
                problems.append(f"字符画第 {i + 1} 行含 组合字符，宽度在不同字体里不一致")
                break
            if _is_emoji(c):
                problems.append(f"字符画第 {i + 1} 行含 emoji，宽度在不同字体里不一致")
                break
        else:
            continue
        break

    for i, ln in enumerate(lines):
        risk = braille_font_risk(ln)
        if risk:
            problems.append(f"字符画第 {i + 1} 行{risk}")
            break
    return problems


def _is_emoji(ch: str) -> bool:
    o = ord(ch)
    return (0x1F300 <= o <= 0x1FAFF) or (0x2600 <= o <= 0x27BF) or o in (0xFE0F, 0x200D)


# ---------------------------------------------------------------
# 单行文字的安全处理
# ---------------------------------------------------------------
def looks_like_art_prefix(text: str) -> bool:
    """这段**还没收完**的回复，开头看起来像字符画吗？用来避免打字机把图案打散。

    只看头两行：图案通常在开头，而且一旦开始就该整块出来，
    不像正文那样一个字一个字地吐。判据与 `is_art()` 同源，但放宽「至少 2 行」——
    流式到达时可能只到第一行。
    """
    lines = str(text).split("\n")
    head = [ln for ln in lines[:3] if ln.strip()]
    if not head:
        return False
    if any(text_width(ln) > _ART_MAX_WIDTH for ln in head):
        return False
    # 这一行就有图案迹象（连续符号串或双空格缩进）就算。
    # 不额外要求「已有两行」：流式第一块就可能只到半行，等第二行才判断，
    # 图案的前半截已经一个字一个字地吐出去了。
    return any(any(_signals(ln)) for ln in head)


def strip_prefix(text: str) -> str:
    """去掉模型误写的「YUKI.N>」前缀。

    **不动空白**：和前端同一处逻辑（`web/index.html::stripPrefix`）的区别就在
    右边的 `.trim()`——多行图案最右边那点空白是排版的一部分，整段 strip 会把
    最后一行缩短，图案跟着歪。

    只有开头**全是空白**时才认为那是前缀（真实前缀前面不可能有图案），
    这样不会误吃掉第一行的缩进。首尾的空**行**去掉（模型习惯性留的空行，
    不是图案的一部分），行内与行尾空格一律保留。
    """
    s = str(text)
    m = re.match(r"^[ \t\n]*YUKI\.N\s*>", s, flags=re.I)
    if m:
        s = s[m.end():]
        # 前缀后面紧跟的那一个空格是分隔符，去掉；再往后的空白属于正文/图案。
        if s[:1] == " ":
            s = s[1:]
    return s.rstrip("\n").lstrip("\n")
