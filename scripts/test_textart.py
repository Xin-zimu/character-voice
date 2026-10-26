#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""textart.py 的回归：什么算字符画、画得对齐没对齐、前缀剥离动不动空白。

这些用例只查**可观察结果**：给一段文本，is_art 是不是 True，check_art 报出
什么，strip_prefix 之后字符串长什么样。不查内部怎么算的。

CHAT-017 还没定生成方式（PEND-006），所以这里**不测**「她该不该画」——
那要看真实试聊（V-ART-01，PEND-001）。这里测的是画出来之后会不会被显示搞坏。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import textart as ta  # noqa: E402
import check_persona  # noqa: E402  分段豁免的两端对照（DEC-006）

CAT = "\n  /\\_/\\\n ( o.o )\n  > ^ <\n"
# 左边距一致的版本，用来把「对齐问题」和「左边距问题」分开测。
# 注意第二行前面是**两个**空格：`(` 是窄字符，故意让它和上下对齐。
CAT_ALIGNED = "  /\\_/\\\n  ( o.o )\n  > ^ <"
BRAILLE = "  ⣠⣾⣿\n  ⠿⠿⠿"
TABLE = "┌────┐\n│ ok │\n└────┘"
WAVE = "  ~~~~~~\n ~~~~~~~~\n  ~~~~~~"


class WidthTests(unittest.TestCase):
    def test_01_wide_and_narrow(self):
        """汉字占 2 格、ASCII 占 1 格。这是所有对齐判断的地基。"""
        self.assertEqual(ta.text_width("猫"), 2)
        self.assertEqual(ta.text_width("ab"), 2)
        self.assertEqual(ta.text_width("猫ab"), 4)
        self.assertEqual(ta.text_width(""), 0)

    def test_02_braille_and_block_are_one_cell(self):
        """盲文与块元素当像素用，按 1 格算。

        这是本项目**刻意**的口径：它们的 East Asian Width 是 N（窄），
        但东亚字体常把它们画成全角，见 `braille_font_risk()`。
        这里锁的是「按窄算」这个选择，不是「它们真的窄」。
        """
        for ch in "⣿⠿░▓█▀":
            self.assertEqual(ta.cell_width(ch), 1, f"{ch} 应算 1 格")

    def test_03_combining_is_zero(self):
        """组合字符本身不占格：`e` + U+0301 是 1 格，不是 2 格。"""
        self.assertEqual(ta.text_width("e\u0301"), 1)
        self.assertEqual(ta.cell_width("\u0301"), 0)

    def test_04_fullwidth_punct_is_two(self):
        self.assertEqual(ta.cell_width("，"), 2)
        self.assertEqual(ta.cell_width("。"[::-1]), 2)

    def test_05_braille_mixed_with_cjk_is_flagged(self):
        """盲文与汉字混排时宽度不可靠——这是**提示**，不是错误。"""
        self.assertTrue(ta.braille_font_risk("  ⣿猫"))
        self.assertFalse(ta.braille_font_risk("  ⣿⣿⣿"))
        self.assertFalse(ta.braille_font_risk("猫"))


class IsArtTests(unittest.TestCase):
    def test_06_ascii_art_is_art(self):
        self.assertTrue(ta.is_art(CAT))
        self.assertTrue(ta.is_art(TABLE))
        self.assertTrue(ta.is_art(WAVE))

    def test_07_braille_art_is_art(self):
        self.assertTrue(ta.is_art(BRAILLE))

    def test_08_prose_is_not_art(self):
        """普通多行话不能被当成画：误判会让正常回复走错显示分支。"""
        cases = [
            "第一句，\n第二句。",
            "看书。\n\n另外，温度在下降。",
            "还没有。\n读到一半。",
            "好。",
            "",
            "   ",
        ]
        for t in cases:
            self.assertFalse(ta.is_art(t), f"「{t}」不该算字符画")

    def test_09_single_line_is_not_art(self):
        """一行不算画。哪怕它长得像。"""
        self.assertFalse(ta.is_art("  /\\_/\\"))

    def test_10_poem_is_not_art(self):
        """四行短诗：每行都窄、都没句读，但**没有图案迹象**。

        这是刻意保留的漏判（宁可漏判不误判），写成用例免得以后被当 bug「修」掉。
        """
        poem = "床前明月光\n疑是地上霜\n举头望明月\n低头思故乡"
        self.assertFalse(ta.is_art(poem))

    def test_11_long_lines_are_not_art(self):
        """有很长的一行，就不是画——一整段话被折行了不该算。"""
        long = "这是一句很长的话" * 8
        self.assertFalse(ta.is_art(f"  /\\_/\\\n{long}"))

    def test_12_idle_state_kind(self):
        """参数可以是 None 之外任意字符串；不抛异常。"""
        self.assertFalse(ta.is_art("\n\n\n"))

    def test_13_art_prefix_detected_while_streaming(self):
        """流式到达时只看开头：第一行就有图案迹象就算「像画」。

        用处：打字机遇到它就整块显示，不要一个字一个字地吐散图案。
        """
        self.assertTrue(ta.looks_like_art_prefix("  /\\_/\\"))
        self.assertTrue(ta.looks_like_art_prefix("  ⣠⣾"))
        self.assertFalse(ta.looks_like_art_prefix("今天"))
        self.assertFalse(ta.looks_like_art_prefix("这是一句很长的话" * 8))
        self.assertFalse(ta.looks_like_art_prefix(""))


class CheckArtTests(unittest.TestCase):
    def test_14_aligned_art_has_no_problem(self):
        self.assertEqual(ta.check_art(CAT_ALIGNED), [])
        self.assertEqual(ta.check_art(BRAILLE), [])

    def test_15_uneven_indent_is_reported(self):
        """框线歪了必须报出来。

        **2026-10-26 显式重写**：原来这条用的样例是 `  /\\_/\\` / ` ( o.o )` /
        `   > ^ <`——那是一只画对了的猫，第二行少一格是画法，不是错。
        旧断言把「自由形状缩进不同」锁成了违例，等于把误报当成契约。
        24 次真实采样里 9 条正确的图案全被这条判据标了 △，
        所以现在只用**框线**当判据：`┌ │ └` 这些竖线上下不齐，才是真歪。
        自由形状的对齐问题交给右边缘检查与人工看（V-ART-02 的 D 类）。
        """
        bad = "┌────────┐\n  │        │\n└────────┘"
        probs = ta.check_art(bad)
        self.assertTrue(any("左边距" in p for p in probs), probs)

    def test_16_trailing_space_is_reported(self):
        probs = ta.check_art("  aa \n  bb")
        self.assertTrue(any("行尾" in p for p in probs), probs)

    def test_17_emoji_in_art_is_reported(self):
        """emoji 宽度不可靠，出现在图里要提示。"""
        probs = ta.check_art("  🌙\n  🌙")
        self.assertTrue(any("emoji" in p for p in probs), probs)

    def test_18_combining_char_is_reported(self):
        probs = ta.check_art("  e\u0301e\u0301\n  e\u0301e\u0301")
        self.assertTrue(any("组合字符" in p for p in probs), probs)

    def test_19_empty_art_is_reported(self):
        self.assertTrue(ta.check_art("   \n  "))

    def test_20_blank_lines_do_not_break_indent_check(self):
        """图案中间的空行不算「左边距不一致」——空行本来就没有左边距。"""
        self.assertEqual(ta.check_art("  aa\n\n  bb"), [])


class StripPrefixTests(unittest.TestCase):
    def test_21_prefix_removed(self):
        self.assertEqual(ta.strip_prefix("YUKI.N>写字"), "写字")
        self.assertEqual(ta.strip_prefix("yuki.n>写字"), "写字")
        # 前缀后面那个空格是分隔符，去掉；再往后是正文自己的缩进，保留。
        self.assertEqual(ta.strip_prefix("YUKI.N> 写字"), "写字")
        self.assertEqual(ta.strip_prefix("  YUKI.N >  写字"), " 写字")

    def test_22_prefix_line_alone(self):
        """模型把前缀单独写了一行时，图案不能被吃掉。"""
        self.assertEqual(ta.strip_prefix("YUKI.N>\n  /\\_/\\"), "  /\\_/\\")

    def test_23_indent_is_kept(self):
        """**首行缩进必须保留**。前端那版 `.trim()` 会把它吃掉，图案就歪了。"""
        self.assertEqual(ta.strip_prefix("   /\\_/\\"), "   /\\_/\\")

    def test_24_trailing_spaces_kept(self):
        """行尾空格也保留：右对齐的图案靠它。"""
        self.assertEqual(ta.strip_prefix("aa  \nbb"), "aa  \nbb")

    def test_25_outer_blank_lines_removed(self):
        self.assertEqual(ta.strip_prefix("\n\n  aa\n"), "  aa")

    def test_26_no_prefix_unchanged(self):
        for t in ("写字", "", "  aa", "YUKI.N 没有尖括号"):
            self.assertEqual(ta.strip_prefix(t), t)


class IndentedProseTests(unittest.TestCase):
    """缩进的普通文字不是画。

    独立复审查出来的：早先「连续 3 个符号」把空格也算进符号，
    于是「    你好呀 /    今天很好」这种两行缩进的文字被判成字符画，
    整段被换成保空白排版。缩进在模型的回复里太常见（列表、引用、排版），
    这不是边角情况。
    """

    def test_35_two_indented_lines_are_prose(self):
        self.assertFalse(ta.is_art("    你好呀\n    今天很好"))

    def test_36_indented_poem_is_prose(self):
        self.assertFalse(ta.is_art("    床前明月光\n    疑是地上霜\n    举头望明月\n    低头思故乡"))

    def test_37_indented_prose_gets_no_art_block(self):
        self.assertFalse(any(k for k, _ in ta.art_blocks("    你好呀\n    今天很好")))

    def test_38_real_art_still_art(self):
        """收紧之后，真图案不能被一起误伤。三张图都要还认得出来。"""
        for art in (CAT_ALIGNED, BRAILLE, "┌──────┐\n│      │\n└──────┘"):
            with self.subTest(art=art):
                self.assertTrue(ta.is_art(art))


class RightEdgeTests(unittest.TestCase):
    """右边距：左边缘齐了、右边差很多，图案会「缺一块」。"""

    def test_39_ragged_right_edge_is_reported(self):
        probs = ta.check_art("  ────\n      ────")
        self.assertTrue(any("右边缘" in p for p in probs), probs)

    def test_40_one_cell_short_is_tolerated(self):
        """差 1 格不报：很多图案本来就有短一点的收尾行。"""
        self.assertEqual(ta.check_art("  ┌──┐\n  │  │\n  └─┘"), [])

    def test_41_text_beside_art_is_not_a_reference(self):
        """**文字行不能当右边缘的参照**：图案 + 一句话是常态，拿那句话比宽度等于每张图都报错。"""
        self.assertEqual(ta.check_art("┌──────┐\n└──────┘\n雨声一直没停。"), [])

    def test_42_example_art_passes_new_check(self):
        """人格卡里那条示例必须仍然干净——新检查不能把自己的示例判歪。"""
        import json
        card = json.loads((ROOT / "personas" / "yuki_school.json").read_text(encoding="utf-8"))
        arts = [e["assistant"] for e in card["examples"] if ta.is_art(e["assistant"])]
        self.assertTrue(arts, "崩溃卡里应该至少有一条图案示例")
        for a in arts:
            with self.subTest(art=a):
                self.assertEqual(ta.check_art(a), [])


class BlockTests(unittest.TestCase):
    """art_blocks：一条回复里只有图案那几行该享受「保空白」的排版。

    起因是实测：汉字与 ASCII 的宽度比是 1.66（不是 2），整条按 `pre` 排版时
    文字行会被压成一行，句号被挤到下一行单独出现。所以判定必须细到「块」。
    """

    def test_27_text_art_text_split_into_three(self):
        got = ta.art_blocks("雨声一直没停。\n┌──────┐\n│      │\n└──────┘\n窗外没开灯。")
        self.assertEqual([k for k, _ in got], [False, True, False])
        self.assertEqual(got[1][1], ["┌──────┐", "│      │", "└──────┘"])
        self.assertEqual(got[0][1], ["雨声一直没停。"])

    def test_28_pure_art_single_block(self):
        got = ta.art_blocks(CAT_ALIGNED)
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0][0])

    def test_29_prose_has_no_art_block(self):
        got = ta.art_blocks("看书。\n\n另外，温度在下降。")
        self.assertFalse(any(k for k, _ in got))

    def test_30_single_looks_like_line_is_not_art(self):
        """单单一整行 `=====` 是排版分隔线，不是画。要求连续两行才起块。"""
        got = ta.art_blocks("上式成立\n=====\n下面继续")
        self.assertFalse(any(k for k, _ in got))

    def test_31_lines_are_preserved(self):
        """分块不能吃掉行：把每块拼回去必须还是原来的行序列。"""
        src = "雨声一直没停。\n┌──────┐\n│      │\n└──────┘\n窗外没开灯。"
        got = ta.art_blocks(src)
        flat = [ln for _, chunk in got for ln in chunk]
        self.assertEqual(flat, src.split("\n"))

    def test_32_adjacent_same_kind_merged(self):
        """相邻同类块要合并，避免每行都造一个节点。

        用中间行也是符号的图（`│    │` 里没有字）——`│ ok │` 中间是内容，
        那一行本来就该算普通行，见 test_34。
        """
        got = ta.art_blocks("一句话。\n又一句话。\n┌────┐\n│    │\n└────┘")
        self.assertEqual(len(got), 2)
        self.assertEqual(len(got[0][1]), 2)
        self.assertEqual(len(got[1][1]), 3)

    def test_33_blank_line_ends_block(self):
        """图案中间的空行**不断块**（2026-10-26 显式重写）。

        旧断言要求空行把图案断成两块，理由是「它本意就是分开两段」。
        实测证明这个理由站不住：断块之后，后半段变成普通文字块，
        `white-space` 一恢复 normal，行首空格被折叠——
        `   └────┘` 跳到左边，与上边框错开，一张图当场断掉。
        而 CHAT-017 / R-08 本来就允许图案内部有空行。

        所以现在空行只在**后面不再跟图案行**时才收尾。
        两张独立的画仍然各自保空白（合成一块也不影响观感，
        空行原样保留）；右边缘检查改为在连续段内比，不会跨空行误报。
        """
        got = ta.art_blocks("┌────┐\n│    │\n\n┌────┐\n│    │")
        self.assertEqual(len([1 for k, _ in got if k]), 1)
        self.assertIn("", got[0][1], "空行要原样留在块里")
        # 空行之后是普通文字时，照旧收尾。
        tail = ta.art_blocks("┌────┐\n│    │\n\n一句话。")
        self.assertEqual([k for k, _ in tail], [True, False])

    def test_34_line_with_words_inside_frame_is_not_art(self):
        """表格里写字的行不是图案行。

        这是**已知的取舍**：`│ ok │` 这一行不享受保空白排版，折行时可能歪。
        宁可少判，也不要把文字行当图案压成一行——后者实测更难看（句号被挤下去）。
        """
        got = ta.art_blocks(TABLE)
        self.assertFalse(any(k for k, _ in got))


class ArtFalsePositiveTests(unittest.TestCase):
    """2026-10-26 复现的误报（V-ART-02 显示部分）。

    24 次真实采样里有 9 条**画对了的**图案被标 △「左边距不一致」——
    猫、茶杯、雪花、盲文点阵全中。原因是判据假定「图案各行缩进必须相同」，
    而自由形状的缩进本身就是形状的一部分（`  /\\_/\\` / ` ( o.o )` / `  > ^ <`
    第二行少一格是画法，不是错）。把这类判成错，等于训练用户忽略 △。

    所以左边距只对**有框线的图案**查：`┌ │ └` 这些竖线必须上下对齐，
    那是真错了才歪。自由形状交给右边缘检查和人工看。
    """

    def test_43_freeform_art_is_not_reported(self):
        """自由形状的图画对了就不该报——这是本轮实测暴露的误报。"""
        for art in (
            "   /\\_/\\\n  ( o.o )\n   > ^ <",          # 真实采样 A1 rep1
            "  ⣠⣾⣿⣦⡀\n  ⠙⠿⠿⠿⠋",                    # 盲文点阵
        ):
            with self.subTest(art=art):
                self.assertTrue(ta.is_art(art), "这条本身要算图案，否则测的是别的")
                self.assertEqual(ta.check_art(art), [],
                                 "画对的自由形状被当成歪了")

    def test_44_indented_code_is_not_art(self):
        """缩进的代码不是字符画。

        判据是「两处以上双空格 + 至少一个图案字符」，而 `    return x + 1`
        正好凑够：四个空格算两处双空格，`+` 算图案字符。于是普通代码被整段
        换成保空白排版，还多一条「左边距不一致」。真实采样里她就是会写代码。
        """
        for code in ("def f(x):\n    return x + 1", "    return x + 1\n    return y - 2"):
            with self.subTest(code=code):
                self.assertFalse(ta.is_art(code), "缩进代码被当成字符画")
                self.assertFalse(any(k for k, _ in ta.art_blocks(code)), "代码不该起图案块")
                self.assertEqual(ta.check_art(code), [], "代码不该被报「图案会歪」")

    def test_45_broken_frame_is_still_reported(self):
        """收紧之后，**真的歪了**的框线仍然要报——不能一删了之。"""
        broken = "┌────┐\n  │    │\n└────┘"
        probs = ta.check_art(broken)
        self.assertTrue(any("左边距" in p for p in probs), probs)

    def test_46_blank_line_inside_pattern_keeps_one_block(self):
        """图案内部的空行不该把图案拆成两块。

        拆开的后果是实测出来的：后半段变成「普通文字块」，`white-space` 一恢复
        normal，行首那三个空格被折叠，`└────┘` 就跳到左边与上边框错开——
        图案当场断掉。CHAT-017 / R-08 明确允许图案内部有空行。
        """
        art = "   ┌────┐\n   │    │\n\n   └────┘\n\n下面还有一句。"
        blocks = ta.art_blocks(art)
        art_chunks = [ch for k, ch in blocks if k]
        self.assertEqual(len(art_chunks), 1, "图案被空行拆成了多块")
        self.assertIn("   └────┘", art_chunks[0], "下边框没有留在图案块里")
        self.assertEqual(ta.check_art(art), [], "这条图案是画对的")

    def test_47_indented_text_beside_art_is_not_a_reference(self):
        """文字行的缩进不是图案的左边距参照。

        形态：图案前一句缩进两格的说明。原来把所有非空行的缩进放一起比，
        于是「0/2」被判成图案歪了——那句话根本不是图案的一部分。
        """
        art = "  说明一句。\n┌────┐\n└────┘"
        self.assertEqual(ta.check_art(art), [])

    def test_48_text_beside_art_may_use_a_blank_line(self):
        """文字 + 空行 + 图案**不是**「分段」。

        规则（CHAT-017 / R-08）允许的是图案块与文字之间留空行，
        不是「只有整条都是图案时才允许空行」。原来用 `is_art()` 判整条，
        于是四句文字 + 空行 + 一张小图被报「分段」——实测出来的误报。
        """
        mixed = "第一句话。\n第二句话。\n第三句话。\n\n┌────┐\n└────┘"
        self.assertFalse(ta.is_art(mixed), "整条不算图案——所以 is_art 这条判据不适用")
        self.assertTrue(ta.has_art_block(mixed), "但它确实含图案块")
        self.assertEqual(check_persona.simulate_check_reply(mixed, "x", {}), "")
        # 对照：没有图案块的普通空行仍然要报，不能被上面那条豁免掉。
        prose = "看书。\n\n另外，温度在下降。"
        self.assertFalse(ta.has_art_block(prose))
        self.assertEqual(check_persona.simulate_check_reply(prose, "", {}), "分段")

    def test_49_face_line_without_indent_joins_the_pattern(self):
        """**顶格的脸**也要算进图案（2026-10-26 复采查出的漏判）。

        真实采样 A1 rep2 的原样输出是：

             /\\_/\\
            ( o.o )
             > ^ <

        脸那行顶格，`_signals()` 要求「连续 3 个图案字符」，而
        `( o.o )` 被两个字母隔成散符号，最长连续串只有 1，于是**整只猫**
        一块都拼不出来：按普通文字排版，行首空格被折叠，脸和嘴对不上耳朵。
        同一只猫只要脸缩进 2 格就能成块——漏的是判据，不是画。
        """
        flat = " /\\_/\\\n( o.o )\n > ^ <"
        blocks = ta.art_blocks(flat)
        self.assertEqual([k for k, _ in blocks], [True], "顶格的脸让整只猫没成块")
        self.assertEqual(len(blocks[0][1]), 3, "三行都是这只猫的一部分")
        # 对照：缩进版本本来就成块（改前也对），两条一起锁住「形状不是判据」。
        indented = "   /\\_/\\\n  ( o.o )\n   > ^ <"
        self.assertEqual([k for k, _ in ta.art_blocks(indented)], [True])

    def test_50_soft_lines_cannot_start_an_art_block(self):
        """收宽松之后，**普通文字不能自己变成图案块**。

        `soft` 行的门槛（短、符号占三分之一、至少 2 个图案字符）单看很低，
        所以加了一条硬约束：整块里必须有一个 `hard` 行。这里锁住这条约束——
        否则「带点符号的两行话」都会被换成保空白排版，那比漏判更难看。

        用例必须挑**真的能过 soft 门槛**的句子，否则测的是「它本来就不是 soft」，
        把 has_hard 删掉也照样绿——第一版就是这样漏掉 M77 的。
        下面两行的符号占比都是 2/5，过 soft 的 1/3 门槛，但最长连续串只有 1。
        """
        # 先自证这两行确实会被判成 soft，否则整条用例没有意义。
        for line in ("x=1;y=2", "k=3;j=4"):
            self.assertEqual(ta._art_line_kind(line), "soft",
                             f"「{line}」不再过 soft 门槛，这条用例失去意义")
        # 但整段里没有 hard 行，所以**一块都不能起**。
        # 相邻的 False 块会被合并成一块，所以这里期望的是恰好一个 False 块；
        # 变异成「soft 可以自己起块」时会变成 [True]，这条断言就会红。
        prose = "x=1;y=2\nk=3;j=4"
        self.assertEqual([k for k, _ in ta.art_blocks(prose)], [False],
                         "两行 soft 自己起了图案块——普通文字被换成保空白排版")
        # 对照：没有任何符号的日常话，仍然不是画。
        self.assertEqual([k for k, _ in ta.art_blocks("今天还行。\n明天再说吧。")], [False])

    def test_51_table_row_inside_frame_still_not_art(self):
        """`soft` 不能把表格行卷进图案块——PEND-011 的取舍要原样保留。

        `│ ok │` 顶格、短、符号占多数，单看完全符合 `soft`。所以 `soft`
        额外排除「以框线字符开头的行」。这条与 test_34 是同一条取舍：
        test_34 锁整段，这条锁「收紧之后也没有被顺手改掉」。
        """
        got = ta.art_blocks(TABLE)
        self.assertFalse(any(k for k, _ in got), "表格行被 soft 卷进图案块了")
        # 但真正的图案块里，框线行当然还留在块内。
        box = "┌────┐\n│    │\n└────┘"
        self.assertEqual([k for k, _ in ta.art_blocks(box)], [True])



if __name__ == "__main__":
    unittest.main(verbosity=2)
