#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""台词库（data/nagato_lines.jsonl）的回归测试。

## 钉什么

这份库是「检索抓错」的根治手段：把语料缩到她真正说过的话，检索就再也
抓不到别人的台词。所以它的**纯度**比**数量**重要得多 —— 混进一句别人
的话，整条论证就塌了。

  test_01  seed 10 句必须全在（漏 = 覆盖不足）
  test_02  不许有 `（…）` 屏幕注解行混入（那根本不是台词）
  test_03  id 唯一，且能在 parsed 原文里逐条对上（防手滑改错字）
  test_04  conf 只能是 high / medium
  test_05  必要条件：平均长度 ≤ 15 字；不含感叹号
           —— **这只是必要条件，不是纯度证明**。她的语言指纹确实没有
           感叹号（seed 10 句 0 个），所以出现感叹号一定说明混入了别人；
           但「没有感叹号」不能反推「没有混入」——判定时本来就不会挑
           春日大喊的句子，这是筛选标准的产物，不是独立验证。
  test_06  报告与 jsonl 一致（防止只改一边）
  test_07  区间归并：`YUKI.N>` 这类前缀只出现在每段首行，续行必须靠区间
           覆盖；同时区间不得吞入别人的插话（06 集 260/261/265/266/280/281）
  test_08  区间表与逐条表无重叠

## 为什么 test_05 是「指纹」而不是「硬规则」

她在 04 集战斗段落确实说过 20 字以上的技术性长句，所以不能限制单条长度。
但**平均**长度必须低 —— seed 实测 5.8 字，全库人工统计的心智模型是「极短
应答为主」。平均长度一旦超过 15 字，说明混进了大量旁白。
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
LINES = DATA / "nagato_lines.jsonl"
REPORT = DATA / "nagato_lines_report.json"
PARSED = DATA / "parsed"
SEED = DATA / "seed_nagato_lines.md"


def norm(s: str) -> str:
    return "".join(ch for ch in s if ch not in "…~～ \u3000")


class NagatoLinesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = [json.loads(l) for l in
                    LINES.read_text(encoding="utf-8").splitlines() if l.strip()]
        cls.report = json.loads(REPORT.read_text(encoding="utf-8"))

    def test_01_seed_all_present(self):
        """seed 里 10 句人工核定的台词必须全部在库内。"""
        seeds = [
            # (ep, start 前缀, 文本片段)
            ("01", "0:12:46", "不知道"),
            ("01", "0:18:22", "可以"),
            ("01", "0:18:27", "无所谓"),
            ("01", "0:18:31", "请便"),
            ("03", "0:05:00.03", "的确"),
            ("03", "0:05:00.74", "信息统合思念体配置在地球的终端界面"),
            ("03", "0:05:05", "信息统合思念体意识的一部分"),
            ("03", "0:05:07", "准备积极地行动"),
            ("03", "0:05:12", "你是凉宫春日的"),
            ("03", "0:05:15", "一旦危机来临"),
        ]
        for ep, st, frag in seeds:
            ok = any(r["ep"] == ep and r["start"].startswith(st)
                     and norm(frag) in norm(r["text"]) for r in self.rows)
            self.assertTrue(ok, f"漏了 {ep}@{st} {frag}")

    def test_02_no_screen_annotation(self):
        """`（…）` 开头的是屏幕术语注解，不是台词。"""
        bad = [f"{r['id']} {r['text'][:20]}" for r in self.rows
               if r["text"].startswith(("（", "("))]
        self.assertEqual(bad, [], f"混入屏幕注解：{bad}")

    def test_03_ids_unique_and_verbatim(self):
        """id 唯一；文本必须与 parsed 原文逐字一致（防手滑改写）。"""
        ids = [r["id"] for r in self.rows]
        self.assertEqual(len(ids), len(set(ids)), "id 有重复")

        cache: dict[str, dict[int, str]] = {}
        for r in self.rows:
            ep = r["ep"]
            if ep not in cache:
                p = PARSED / f"{ep}.dialogue.jsonl"
                cache[ep] = {d["idx"]: d["text"]
                             for d in (json.loads(l) for l in
                                       p.read_text(encoding="utf-8").splitlines()
                                       if l.strip())}
            self.assertEqual(cache[ep].get(r["idx"]), r["text"],
                             f"{r['id']} 与原文不一致")

    def test_04_conf_values(self):
        self.assertTrue({r["conf"] for r in self.rows} <= {"high", "medium"})

    def test_05_language_fingerprint(self):
        """必要条件：平均长度 ≤ 15 字，且不含感叹号。

        **不构成纯度证明**。全库含感叹号的句子占 7.7%，她的台词占全库 1.3%，
        两者本来就不同量级；再加上判定时不会挑春日大喊的句子，所以
        「0 条感叹号」是筛选标准的必然结果，不是独立证据。
        它只能当保险丝：一旦变红，说明确实混入了带情绪的别人的话。
        """
        lens = [len(norm(r["text"])) for r in self.rows]
        avg = sum(lens) / len(lens)
        self.assertLessEqual(avg, 15.0, f"平均 {avg:.2f} 字，疑似混入旁白")
        bangs = [r["id"] for r in self.rows
                 if "！" in r["text"] or "!" in r["text"]]
        self.assertEqual(bangs, [], f"她有感叹号台词：{bangs}")

    def test_06_report_consistent(self):
        """报告里的条数/分布必须与 jsonl 实际内容一致。"""
        self.assertEqual(self.report["n_lines"], len(self.rows))
        by_ep: dict[str, int] = {}
        for r in self.rows:
            by_ep[r["ep"]] = by_ep.get(r["ep"], 0) + 1
        self.assertEqual(self.report["by_ep"], by_ep)


    def test_07_yuki_block_covers_continuations(self):
        """`YUKI.N>` 前缀只出现在每段首行，续行必须由区间覆盖。

        06 集实际情况：带前缀 10 行（含 3 行画面重播），续行 16 行。
        如果只按 `startswith("YUKI.N>")` 筛，会漏掉 62%。
        """
        rows06 = [r for r in self.rows if r["ep"] == "06"]
        prefixed = [r for r in rows06 if r["text"].startswith("YUKI.N>")]
        continuations = [r for r in rows06
                         if not r["text"].startswith("YUKI.N>")
                         and r["basis"] == "narration"]
        # 前缀行只占一小部分，续行才是多数 —— 若两者数量接近说明口径又写错了
        self.assertLess(len(prefixed), len(continuations),
                        f"前缀 {len(prefixed)} / 续行 {len(continuations)}，"
                        f"比例异常，检查 BLOCKS 是否只覆盖了首行")
        self.assertGreaterEqual(len(continuations), 12,
                                "续行太少，区间可能没展开")

    def test_08_blocks_do_not_swallow_others(self):
        """区间不能吞入别人的插话。这 6 行是已知的阿虚台词，必须留在外面。"""
        ids = {r["id"] for r in self.rows}
        for idx in (260, 261, 265, 266, 280, 281):
            self.assertNotIn(f"06:{idx}", ids, f"06:{idx} 是阿虚的话，被区间吞了")


if __name__ == "__main__":
    unittest.main(verbosity=2)
