#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""评测集 `record_id` 唯一标识的回归（P0-4 / Q-3）。

背景：
  旧格式用 `{"ep","start"}` 引用记录、评测脚本用 `ep@start` 做 key。
  但索引里 **226 组 (集, 时间码) 对应多条记录**，key 不唯一 ——
  两条不同记录会算出同一个 key，命中判定与 forbidden 判定因此失真。
  实测 60 题 67 条引用里有 2 条踩坑（B05 / A11 都指向 `03@0:05:00`，
  而该时间码上是 `03:83 …的确` 与 `03:84 …终端界面并不止我一个` 两条）。

钉的东西：
  1. 新格式 questions_v2.jsonl 的每条引用都是非空 record_id，且能落到索引里；
  2. record_id 在同一题内不重复；
  3. 60 题全部迁移，题号与旧格式一一对应（可追溯）；
  4. 索引 doc 的 `id` 本身全局唯一（record_id 的取值基础）；
  5. 真正踩到冲突时间码的引用，迁移后指向不同记录（而不是还被合并成一条）；
  6. 旧格式文件与历史基线**原样保留**，md5 可核对；
  7. 迁移报告与源文件 md5 一致，证明源文件未被静默改写。

缺文件时跳过，不影响其它测试。
"""
import hashlib
import json
import sys
import unittest

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

INDEX_DIR = ROOT / "data" / "index"
EVAL = ROOT / "data" / "retrieval_eval"
Q_LEGACY = EVAL / "questions.jsonl"
Q_V2 = EVAL / "questions_v2.jsonl"
MIGRATION = EVAL / "migration_20261026.json"
DECISIONS = EVAL / "record_id_decisions.json"
BASELINES = [
    EVAL / "baseline_20261026.json",
    EVAL / "baseline_20261026_after_p0.json",
]

REF_FIELDS = ("expected", "partial", "forbidden")


def md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


def load_jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


@unittest.skipUnless(
    (INDEX_DIR / "docs.jsonl").exists() and Q_V2.exists(),
    "缺索引或 questions_v2.jsonl，跳过")
class RecordIdTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.docs = load_jsonl(INDEX_DIR / "docs.jsonl")
        cls.known = {d["id"] for d in cls.docs}
        cls.v2 = load_jsonl(Q_V2)
        cls.legacy = load_jsonl(Q_LEGACY) if Q_LEGACY.exists() else []
        cls.migration = (json.loads(MIGRATION.read_text(encoding="utf-8"))
                         if MIGRATION.exists() else None)

    # ---- 1. record_id 必须存在且可解析 ----

    def test_01_all_refs_have_record_id(self):
        """每条引用都必须有非空 record_id。"""
        bad = []
        for q in self.v2:
            for fld in REF_FIELDS:
                for i, r in enumerate(q.get(fld) or []):
                    rid = r.get("record_id")
                    if not rid:
                        bad.append(f'{q["id"]}.{fld}[{i}]')
        self.assertEqual(bad, [], f"有 {len(bad)} 条引用缺 record_id：{bad[:5]}")

    def test_02_record_id_resolves_in_index(self):
        """record_id 必须能在索引里找到对应记录。"""
        bad = []
        for q in self.v2:
            for fld in REF_FIELDS:
                for r in q.get(fld) or []:
                    if r.get("record_id") not in self.known:
                        bad.append((q["id"], fld, r.get("record_id")))
        self.assertEqual(bad, [], f"有 {len(bad)} 条 record_id 不在索引里：{bad[:5]}")

    def test_03_unique_within_question(self):
        """同一题内 expected/partial/forbidden 各自不能有重复 record_id。"""
        for q in self.v2:
            for fld in REF_FIELDS:
                ids = [r["record_id"] for r in (q.get(fld) or []) if r.get("record_id")]
                self.assertEqual(
                    len(ids), len(set(ids)),
                    f'{q["id"]}.{fld} 内部有重复 record_id：{ids}')

    def test_04_index_doc_id_globally_unique(self):
        """record_id 的取值基础：索引 doc 的 `id` 必须全局唯一。"""
        ids = [d["id"] for d in self.docs]
        self.assertEqual(len(ids), len(set(ids)),
                         f"索引里有 {len(ids) - len(set(ids))} 个重复 id")

    # ---- 2. 60 题可完整映射 ----

    def test_05_all_60_questions_migrated(self):
        """旧格式 60 题全部迁到新格式，题号一一对应。"""
        self.assertEqual(len(self.legacy), 60)
        self.assertEqual(len(self.v2), 60)
        self.assertEqual([q["id"] for q in self.legacy],
                         [q["id"] for q in self.v2],
                         "新旧格式的题号序列不一致")

    def test_06_ref_counts_match_legacy(self):
        """迁移不能悄悄增删引用：每题的引用条数必须与旧格式一致。"""
        for lo, ne in zip(self.legacy, self.v2):
            for fld in REF_FIELDS:
                self.assertEqual(
                    len(lo.get(fld) or []), len(ne.get(fld) or []),
                    f'{lo["id"]}.{fld} 引用条数变化：'
                    f'{len(lo.get(fld) or [])} -> {len(ne.get(fld) or [])}')

    def test_07_ambiguous_refs_split(self):
        """真正踩到冲突时间码的引用，迁移后必须指向不同记录。

        `03@0:05:00` 上是 `03:83 …的确` 与 `03:84 …终端界面并不止我一个`。
        旧格式下 B05 与 A11 都写这个时间码、无法区分；
        新格式下 B05 应指 03:83，A11 应指 03:84。
        """
        by_id = {q["id"]: q for q in self.v2}
        self.assertIn("B05", by_id)
        self.assertIn("A11", by_id)
        self.assertEqual(by_id["B05"]["expected"][0]["record_id"], "03:83")
        self.assertEqual(by_id["A11"]["expected"][2]["record_id"], "03:84")
        self.assertNotEqual(by_id["B05"]["expected"][0]["record_id"],
                            by_id["A11"]["expected"][2]["record_id"],
                            "两处引用的 record_id 仍然相同 —— 迁移没有解决歧义")

    # ---- 3. 历史资产可追溯 ----

    def test_08_legacy_and_baselines_preserved(self):
        """旧文件不能被动过：有迁移报告记录的 md5 就逐字核对。"""
        if self.migration is None:
            self.skipTest("没有迁移报告")
        self.assertEqual(md5(Q_LEGACY), self.migration["source_md5"],
                         "questions.jsonl 的 md5 与迁移时记录的不一致（被改过了）")
        self.assertEqual(self.migration["source_md5"],
                         self.migration["source_md5_after"])
        self.assertTrue(self.migration["source_untouched"])
        for b in BASELINES:
            if b.exists():
                self.assertGreater(b.stat().st_size, 1000, f"{b.name} 疑似被清空")

    def test_09_migration_report_complete(self):
        """迁移报告必须覆盖全部 67 条引用，状态分布可核对。"""
        if self.migration is None:
            self.skipTest("没有迁移报告")
        self.assertEqual(self.migration["questions"], 60)
        self.assertEqual(self.migration["refs_total"], 67)
        self.assertEqual(self.migration["refs_total"],
                         len(self.migration["mappings"]))
        stat = self.migration["refs_by_status"]
        self.assertEqual(stat.get("unique", 0) + stat.get("ambiguous_resolved", 0),
                         67, f"迁移状态没覆盖全部引用：{stat}")
        self.assertEqual(stat.get("ambiguous", 0), 0,
                         "还有未决策的歧义引用就产出了新文件")

    def test_10_decisions_documented(self):
        """两处歧义的人工决策必须留痕（可复核、可改判）。"""
        if not DECISIONS.exists():
            self.skipTest("没有决策文件")
        d = json.loads(DECISIONS.read_text(encoding="utf-8"))
        self.assertEqual(d["B05.expected[0]"], "03:83")
        self.assertEqual(d["A11.expected[2]"], "03:84")
        # 每条决策都要有依据，不能只有结论
        ev = d.get("_evidence", {})
        for k in ("B05.expected[0]", "A11.expected[2]"):
            self.assertIn(k, ev, f"{k} 缺判定依据")
            self.assertIn("基线实际命中", ev[k])


@unittest.skipUnless(
    (INDEX_DIR / "docs.jsonl").exists(), "缺索引，跳过")
class TransitionFieldConsumerTests(unittest.TestCase):
    """transition 字段不能破坏既有消费方。"""

    def test_11_retriever_ignores_transition(self):
        """检索器读 transition 吗？不该读 —— 加了字段不应该改检索行为。"""
        src = (ROOT / "retriever.py").read_text(encoding="utf-8")
        self.assertNotIn("transition", src,
                         "retriever.py 里出现了 transition —— 本轮约定不改检索逻辑，"
                         "若确要消费该字段需单独评审")

    def test_12_doc_has_no_speaker_field(self):
        """transition 独立于 speaker；索引里不应凭空出现 speaker。"""
        with (INDEX_DIR / "docs.jsonl").open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                d = json.loads(line)
                self.assertNotIn("speaker", d,
                                 f'{d["id"]} 带了 speaker 字段')
                break  # 抽样即可，字段结构全表一致


if __name__ == "__main__":
    unittest.main(verbosity=2)
