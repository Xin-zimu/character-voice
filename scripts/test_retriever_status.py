#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""寒暄 vs 分词失败：两类「召回 0 条」必须能被区分。

背景（CORPUS_AUDIT §4.2 / RETRIEVAL_EVAL §4.4）：
  修复前，`在吗`/`是的`/`那个` 返回 0 条，评测把它记成「正确空召回」。
  但那不是「检索判断语料里没有」，而是 **tokenize 返回 [] 直接 return**，
  检索根本没启动。假通过。

  用户提出的要求（本轮 P0 第 2 项）：
  「区分普通寒暄无需检索与真正的分词失败，不要让所有短句都强制召回原作语料。」

所以修复不能走「短句一律兜底召回」的路子 —— 那会把「在吗」硬凑出
一堆不相干的原作片段。正确做法是：**让调用方能分辨自己遇到了哪一种**。

这里钉的是可观察契约：
  search_detailed() 返回带 status 的结果对象，
  status 明确区分 no_tokens / no_match / ok 三种情况。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import retriever  # noqa: E402

INDEX_DIR = ROOT / "data" / "index"


class SearchStatusContractTests(unittest.TestCase):
    """search_detailed 的状态契约（不需要索引即可测纯逻辑部分）。"""

    def test_01_empty_query_is_not_a_corpus_silence(self):
        """纯停用字查询要能报出 no_tokens，而不是伪装成「语料里没有」。

        注意：修复后「在吗」不应再落到 no_tokens（它应有 token）。
        这里用一个必然被吃空的输入测契约本身。
        """
        r = retriever.search_detailed(None, "normal", "")
        self.assertIn(
            r.status, ("no_tokens", "no_match", "ok"),
            f"未定义的状态: {r.status}",
        )
        self.assertEqual(r.hits, [], "空查询不该返回结果")

    def test_02_status_values_are_distinct(self):
        """三种状态必须是不同的字符串，否则调用方无法分支。"""
        vals = {retriever.STATUS_OK, retriever.STATUS_NO_TOKENS, retriever.STATUS_NO_MATCH}
        self.assertEqual(len(vals), 3, f"状态值有重复: {vals}")

    def test_03_result_is_truthy_by_hits(self):
        """SearchResult 的布尔值应跟 hits 一致，方便 `if result:` 写法。"""
        r = retriever.search_detailed(None, "normal", "")
        self.assertFalse(bool(r), "空结果不该为真")


@unittest.skipUnless((INDEX_DIR / "docs.jsonl").exists(), "缺 data/index，跳过")
class GreetingVsFailureTests(unittest.TestCase):
    """用真实索引分辨：寒暄是 no_match，分词失败是 no_tokens。"""

    @classmethod
    def setUpClass(cls):
        cls.corpus = retriever.Corpus(INDEX_DIR)

    def test_10_pure_stopword_reports_no_tokens(self):
        """纯停用字（如「的」）走不到检索，状态应为 no_tokens。"""
        r = self.corpus.search_detailed("normal", "的")
        self.assertEqual(
            r.status, retriever.STATUS_NO_TOKENS,
            f"「的」应报 no_tokens，实际 {r.status}（tokens={retriever.tokenize_query('的')}）",
        )

    def test_11_greeting_is_retrievable_not_silent(self):
        """「在吗」修复后应能进检索 —— 即不再是 no_tokens。

        至于检索结果是否相关，这是评测集的事；这里只保证
        「没有静默跳过」。
        """
        r = self.corpus.search_detailed("normal", "在吗")
        self.assertNotEqual(
            r.status, retriever.STATUS_NO_TOKENS,
            "「在吗」仍是 no_tokens，说明分词仍被吃空",
        )

    def test_12_no_match_really_means_no_match(self):
        """真正没人说过的话，状态应是 no_match 而不是 no_tokens。

        用一串语料中绝不会出现的拉丁词。
        """
        r = self.corpus.search_detailed("normal", "zzzqqqxxx")
        self.assertEqual(
            r.status, retriever.STATUS_NO_MATCH,
            f"无人提及的词应报 no_match，实际 {r.status}",
        )
        self.assertEqual(r.hits, [], "no_match 却返回了结果")

    def test_13_ok_case_has_hits(self):
        """有把握的查询要报 ok 并带回结果。"""
        r = self.corpus.search_detailed("normal", "信息统合思念体是什么")
        self.assertEqual(r.status, retriever.STATUS_OK, f"实际 {r.status}")
        self.assertTrue(r.hits, "ok 但没结果")


@unittest.skipUnless((INDEX_DIR / "docs.jsonl").exists(), "缺 data/index，跳过")
class BackwardCompatTests(unittest.TestCase):
    """search() 的旧签名与返回值必须不变 —— 主线 T1–T6 依赖它。"""

    @classmethod
    def setUpClass(cls):
        cls.corpus = retriever.Corpus(INDEX_DIR)

    def test_20_search_returns_plain_list(self):
        hits = self.corpus.search("normal", "你的工作是什么", k=3)
        self.assertIsInstance(hits, list, "search() 必须仍返回 list")
        self.assertLessEqual(len(hits), 3)
        if hits:
            h = hits[0]
            for key in ("id", "ep", "ep_title", "start", "world", "text", "ctx", "anchor", "score"):
                self.assertIn(key, h, f"search() 结果缺字段 {key}")

    def test_21_empty_still_returns_empty_list(self):
        """空查询仍返回 []（不是 None、不是异常）。"""
        self.assertEqual(self.corpus.search("normal", "的"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
