#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""retriever 分词的回归：空 token 退化 与 否定词语义反转。

对应 CORPUS_AUDIT §4.2 的两条发现：
  Q-01  常见短句（在吗 / 是的 / 那个 / 啊 / 是吗）被停用字吃空，检索静默返回 0 条
  Q-02  否定词被当停用字删掉，`不知道` -> `['知道']`、`不需要` -> `['需要']`，语义反转

**关键分工**（P0 修复引入）：
  `tokenize()`       建索引用，规则冻结 —— 改它会重算全库 IDF、漂移所有分数
  `tokenize_query()` 查询侧用，在 tokenize() 之上追加否定信号与短句兜底

所以本文件分两组：
  - 查询侧行为（Q-01/Q-02 的修复效果）—— 针对 tokenize_query
  - 索引侧冻结（防止有人"顺手"把 tokenize 也改了，那是会改排序的）

设计原则（与 test_scene.py 一致）：只钉可观察行为，不钉 2-gram 的具体切法。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import retriever  # noqa: E402
from retriever import tokenize, tokenize_query  # noqa: E402

INDEX_DIR = ROOT / "data" / "index"


class QueryTokenizeEmptyTests(unittest.TestCase):
    """Q-01：短句在**查询侧**不能被停用字吃空。"""

    def test_01_short_phrases_not_empty(self):
        """这些是真实用户会说的话，tokenize_query 不能返回 []。

        修复前实测：tokenize 全部返回 []，search() 因此直接 return []，
        用户说「在吗」时检索根本不会启动。
        """
        for text in ("在吗", "是的", "那个", "是吗", "在不在"):
            with self.subTest(text=text):
                toks = tokenize_query(text)
                self.assertTrue(
                    toks,
                    f"「{text}」查询侧切不出 token —— 检索会静默跳过，永远召回 0 条",
                )

    def test_02_bare_function_word_still_empty(self):
        """单个虚词不兜底。「的」本来就不该检索，放行只会让所有文档一起涨分。"""
        self.assertEqual(tokenize_query("的"), [])
        self.assertEqual(tokenize_query("了"), [])

    def test_03_ordinary_words_unaffected(self):
        """普通词的查询侧结果必须与索引侧一致（没有否定、不是纯虚词）。"""
        for text in ("睡觉", "看书", "你叫什么", "你的工作是什么"):
            with self.subTest(text=text):
                self.assertEqual(
                    tokenize_query(text), tokenize(text),
                    f"「{text}」查询侧与索引侧不一致 —— 会让它召不到自己的原文",
                )


class NegationPreservedTests(unittest.TestCase):
    """Q-02：否定词不能在查询侧丢失语义。"""

    def test_01_buzhidao_keeps_negation(self):
        """「不知道」不能与「知道」得到同一套 token。

        修复前实测：两者 token 完全相同，检索结果必然一模一样。
        """
        q = tokenize_query("不知道")
        self.assertNotEqual(q, tokenize_query("知道"),
                            "「不知道」与「知道」token 相同，检索无法区分")
        self.assertTrue(
            any("不" in t for t in q),
            f"「不知道」的 token 里找不到否定痕迹: {q}",
        )

    def test_02_buxuyao_keeps_negation(self):
        """「不需要」是她的标志性回答（消失@0:25:56），不能被反转。"""
        q = tokenize_query("不需要")
        self.assertNotEqual(q, tokenize_query("需要"),
                            "「不需要」与「需要」token 相同，检索无法区分")
        self.assertTrue(
            any("不" in t for t in q),
            f"「不需要」的 token 里找不到否定痕迹: {q}",
        )

    def test_03_index_side_unchanged(self):
        """索引侧的 tokenize 保持旧行为 —— 这是刻意的，不是漏改。

        为什么不能一起改：tokenize 决定词表 / df / IDF / 文档长度。
        实测把否定字保留进索引分词，会让首条查询的正确答案从 7.838 分
        掉到 6.011 分，main Recall 从 73.0% 崩到 4.0%。
        """
        self.assertEqual(tokenize("不知道"), ["知道"],
                         "索引侧分词被改了 —— 这会让全库 IDF 重算、排序漂移")
        self.assertEqual(tokenize("不需要"), ["需要"],
                         "索引侧分词被改了 —— 这会让全库 IDF 重算、排序漂移")
        self.assertEqual(tokenize("在吗"), [],
                         "索引侧分词被改了 —— 这会让全库 IDF 重算、排序漂移")


@unittest.skipUnless((INDEX_DIR / "docs.jsonl").exists(), "缺 data/index，跳过检索行为测试")
class SearchBehaviorTests(unittest.TestCase):
    """修复后的 search() 行为。需要真实索引。"""

    @classmethod
    def setUpClass(cls):
        cls.corpus = retriever.Corpus(INDEX_DIR)

    def test_10_short_query_reaches_retrieval(self):
        """「在吗」要真的进检索，而不是被分词挡在门外。"""
        r = self.corpus.search_detailed("normal", "在吗")
        self.assertNotEqual(
            r.status, retriever.STATUS_NO_TOKENS,
            "「在吗」仍是 no_tokens，说明查询侧分词仍被吃空",
        )

    def test_11_negation_query_differs_from_affirmative(self):
        """问「不需要」和问「需要」的 token 必须可区分（不要求结果不同）。"""
        self.assertNotEqual(tokenize_query("不需要"), tokenize_query("需要"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
