#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""art_behavior_probe.py 的回归：两个检测口径各自管一类，且不能互相污染。

## 这个测试在防什么

前三轮分别用错了三种「有没有画」的量：

| 轮次 | 用错的量 | 后果 |
|---|---|---|
| 第一轮 | 人工汇总 | 「文字+图」被读成 0 次 |
| 第二轮 | `is_art()`（整条是不是画） | 混排稳定输出 0 |
| 第三轮 | `art_blocks()` | 漏掉「行内」视觉元素 |

第三轮的漏法最隐蔽：E5-info-2 turn3 回了一行嵌在正文里的方块走势图
`▁▂▄▆█▆▂▁▃▅█`，`art_blocks()` 判不出来（它要求连续两行才成块）。

**因此这里锁两件事：**

1. 行内视觉元素**必须**被 `analyse()` 数出来（否则第四轮还会漏）；
2. 但**不能**把它做成放宽 `art_blocks()`——那会把 `│ ok │` 这类表格行
   卷进图案块（PEND-011 已取舍过）。所以另有一条断言：
   表格行仍然 `has_art_block=False` 且 `inline_visual=False`。

这两条一起才有意义：只锁第 1 条会把判据越改越宽，
只锁第 2 条就一直数不到行内元素。

本测试只用合成文本，不调用模型。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import textart as ta                                   # noqa: E402
from art_behavior_probe import (                       # noqa: E402
    analyse, longest_pixel_run, strip_art_block, e6_arms,
    E6_SEED_REPLY_DREW,
)

#: 真实输出（2026-10-26 第三轮 E5-info-2 turn3），逐字保留开头那行走势图。
INLINE_CHART = (
    "（把笔拿出来，在纸角上划了几道）\n"
    "▁▂▄▆█▆▂▁▃▅█ 大概就是这种感觉。每周占一列，数字大的那列就高。"
)

#: 真实输出（第二轮 B2 turn2 的茶杯），用来验图案块一侧不受影响。
TEACUP = (
    "（盯着那几行看了会）\n"
    "\n"
    "    (  )\n"
    "     (  )\n"
    "   ┌────┐\n"
    "   │    │\n"
    "   └────┘\n"
    "\n"
    "看着就觉得暖和。"
)


class InlineVisualTests(unittest.TestCase):
    def test_01_inline_chart_is_counted(self):
        """行内视觉元素要被数出来——这是第三轮漏掉的那一类。"""
        got = analyse(INLINE_CHART)
        self.assertTrue(got["inline_visual"], "行内走势图没有被认出来（第四轮还会漏）")
        self.assertGreaterEqual(got["inline_run"], 5)
        # 它**不是**图案块：这一条同时说明为什么需要第二个指标。
        self.assertFalse(got["has_art_block"])
        # 「两者之一」必须为真，否则汇总仍会把它算成「没画」。
        self.assertTrue(got["visual_any"])

    def test_02_table_row_is_neither(self):
        """表格行不能被算成图案块、也不能算行内元素。

        这是与 test_01 相对的一侧：**不允许**为了数到行内图而放宽分块判据。
        `│ ok │` 里 `│` 是孤立的，不算「连续像素字符」。
        """
        got = analyse("┌────┐\n│ ok │\n└────┘")
        self.assertFalse(got["has_art_block"], "表格被卷进图案块了（PEND-011 的取舍被改掉）")
        self.assertFalse(got["inline_visual"])
        self.assertFalse(got["visual_any"])

    def test_03_art_block_is_not_inline(self):
        """真正的多行图案只记图案块，不重复记成行内。"""
        got = analyse(TEACUP)
        self.assertTrue(got["has_art_block"])
        self.assertFalse(got["inline_visual"], "图案块被同时算了两次")
        self.assertTrue(got["visual_any"])

    def test_04_ordinary_prose_is_neither(self):
        """日常话两种都不是。判据不能宽到把普通句子数进来。"""
        for text in ("今天还行。\n明天再说吧。",
                     "下雨了。\n记得带伞。",
                     "嗯。",
                     "……"):
            with self.subTest(text=text):
                got = analyse(text)
                self.assertFalse(got["visual_any"], f"「{text!r}」被当成了视觉表达")

    def test_05_isolated_symbols_do_not_count(self):
        """孤立的符号不算：`│` 与单独的 `~` 都不构成图形。

        阈值是「连续 ≥ 5 个」。四个以内视为排版或语气，不视为图案。
        """
        for text in ("│ ok │", "~~~", "=====", "a | b | c"):
            with self.subTest(text=text):
                run, _line = longest_pixel_run(text)
                self.assertLess(run, 5, f"「{text}」被算成了连续像素字符")

    def test_05b_pure_rule_lines_do_not_count(self):
        """**纯框线的连续段不算行内图形。**

        这条是第一版判据的真实误报：`┌────┐` 是一行连续 6 个框线字符，
        只看「连续段长度」完全够门槛，于是整张表格会被记成
        「她用了行内视觉表达」。表格不是画。

        与 test_02 的区别：test_02 验整段 `analyse()` 的结果，
        这条直接盯 `longest_pixel_run()` 的判据本身。
        """
        for text in ("┌────┐", "└──────┘", "────────", "├──┼──┤"):
            with self.subTest(text=text):
                run, _line = longest_pixel_run(text)
                self.assertLess(run, 5, f"「{text}」这种纯分隔线被算成了图形")
        # 对照：方块元素/盲文的连续段**必须**照旧算。
        for text in ("▁▂▃▄▅", "⣿⣿⣿⣿⣿"):
            with self.subTest(text=text):
                run, _line = longest_pixel_run(text)
                self.assertGreaterEqual(run, 5, f"「{text}」是当像素用的，必须算")
        # 框内嵌像素也要算：那确实是在用像素画东西，不是纯分隔线。
        self.assertGreaterEqual(longest_pixel_run("│▁▂▃▄▅│")[0], 5)

    def test_06_run_is_longest_continuous(self):
        """连续段计数：断开的两段不能相加。"""
        run, line = longest_pixel_run("███文字██")
        self.assertEqual(run, 3, "断开的两段被加在一起了（那会把普通排版读成图形）")
        self.assertEqual(line, "███文字██")

    def test_07_threshold_boundary(self):
        """4 个不算、5 个算——阈值两边各钉一条，免得改判据时没人发现。"""
        self.assertFalse(analyse("四格 ▁▂▃▄ 结束")["inline_visual"])
        self.assertTrue(analyse("五格 ▁▂▃▄▅ 结束")["inline_visual"])


class E6DesignTests(unittest.TestCase):
    def test_10_two_arms_differ_only_by_art(self):
        """E6 两臂必须只差「有没有一张图」。

        `plain` 臂是删掉图案块之后的文本：不能再含图案块，
        否则两臂的自变量就不是「画没画」了。
        """
        arms = e6_arms()
        self.assertEqual(len(arms), 2)
        drew = [a for a in arms if a[0] == "E6-drew"][0]
        plain = [a for a in arms if a[0] == "E6-plain"][0]
        self.assertTrue(drew[3], "drew 臂应标记为「上一轮画了」")
        self.assertFalse(plain[3], "plain 臂应标记为「上一轮没画」")
        self.assertTrue(ta.has_art_block(drew[2]), "drew 臂的 seed 里没有图案块")
        self.assertFalse(ta.has_art_block(plain[2]), "plain 臂的 seed 里仍有图案块")

    def test_11_plain_arm_keeps_the_words(self):
        """删图案不能把话也删掉：两臂的文字内容必须一致。

        这是控制变量的核心。如果 `plain` 臂连正文都少了，那两臂的差别
        就不止「有没有一张图」，结论会读错。
        """
        plain = strip_art_block(E6_SEED_REPLY_DREW)
        self.assertIn("看着就觉得暖和", plain)
        self.assertIn("手里有个热的东西挺好的", plain)
        # 图案里的框线字符一个都不该剩下。
        for ch in "┌┐└┘│":
            self.assertNotIn(ch, plain, f"删掉图案后仍残留框线字符 {ch}")

    def test_12_plain_arm_has_no_blank_lines(self):
        """`plain` 臂不能留空行。

        `TERMINAL_PROMPT` 明令文字部分不分段。留着图案被删后剩下的空行，
        等于让这一臂同时违反了通讯格式，两臂的差别又多一个。
        """
        plain = strip_art_block(E6_SEED_REPLY_DREW)
        self.assertNotIn("\n\n", plain)


class RecountTests(unittest.TestCase):
    def test_13_recount_uses_reply_not_stale_stats(self):
        """重算必须按**原始回复**判，不能沿用存档里旧的统计字段。

        2026-10-26 第三轮真实发生过：66 条采样跑完时 `stats.json` 还没有
        `inline_visual`（判据是事后补的），存档里的数字比判据旧。
        修法是「原始回复是唯一真源，统计随时可重算」——这条锁住它。

        做法：造一条 samples.jsonl，故意把统计字段写成错的（说没画），
        重算之后必须按 reply 得出「画了」。
        """
        import json
        import tempfile
        from art_behavior_probe import recount

        rows = [{
            "group": "E5", "arm": "E5", "scenario": "S", "turn": 1,
            "message": "x", "reply": INLINE_CHART,
            "status": 200, "error": "",
            # 故意写错：存档说没有行内图。
            "has_art_block": False, "inline_visual": False, "visual_any": False,
        }]
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "samples.jsonl").write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                encoding="utf-8")
            (d / "stats.json").write_text(json.dumps(
                {"stats": {"arms": {"E5/E5": {"turns": 1, "art": 0}}}},
                ensure_ascii=False), encoding="utf-8")
            self.assertEqual(recount(d), 0)
            got = json.loads((d / "stats.json").read_text(encoding="utf-8"))
            arm = got["stats"]["arms"]["E5/E5"]
            self.assertEqual(arm["inline"], 1,
                             "重算沿用了存档里的旧字段，没有按 reply 重新判")
            self.assertEqual(arm["visual"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
