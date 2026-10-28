#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""《消失》世界归属的回归：客观世界边界 + 过渡段标记。

背景（CORPUS_AUDIT §3.3–3.4，P0-3 / P0-4）：
  `build_corpus.py` 原先只切不还 —— 从 0:38:13 一路把 crash 标到片尾字幕。
  P0-3 先加了「恢复点」，锚在 2:33:26「世界变回了原来的样子」；
  P0-4 把锚点前移到 2:33:01「我健康地出院了」，并明确该字段的语义是
  **客观世界状态**，不是角色主观认知状态。两个时刻相差 25 秒，
  中间 6 条记录打了 `transition` 标记（独立于 world / speaker）。

钉的是可观察的数据结论，不是实现细节：
  - 客观恢复点之后不应再有 crash
  - 切点之前不应有 crash
  - 恢复点本身要能落在正确的时间码上
  - 过渡段 6 条必须同时满足：world=normal、transition 非空、时间码在
    [restore, confirm) 内
  - transition 必须是独立字段，不能靠 world 推出来

需要真实索引才能跑；缺索引时跳过。
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

INDEX_DIR = ROOT / "data" / "index"

# 2:33:01「我健康地出院了」——**客观世界**恢复点（P0-4 起）。
RESTORE_TS = "2:33:01"
# 2:33:26「世界变回了原来的样子」——角色**主观确认**时刻，只记录不判定。
CONFIRM_TS = "2:33:26"
# 0:38:13「这样的选手变更是谁安排的？」——切入崩溃世界。
CUT_TS = "0:38:13"
# 过渡段条数：2:33:01 / 2:33:03 / 2:33:06 / 2:33:13 / 2:33:19 / 2:33:21。
EXPECTED_TRANSITION = 6
TRANSITION_REASON = "restore_confirm_gap"


def ts_sec(t: str) -> int:
    parts = [int(x) for x in t.split(":")]
    return parts[0] * 3600 + parts[1] * 60 + parts[2] if len(parts) == 3 \
        else parts[0] * 60 + parts[1]


@unittest.skipUnless((INDEX_DIR / "docs.jsonl").exists(), "缺 data/index，跳过")
class MovieWorldBoundaryTests(unittest.TestCase):
    """《消失》的世界边界。"""

    @classmethod
    def setUpClass(cls):
        cls.mv = []
        with (INDEX_DIR / "docs.jsonl").open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                d = json.loads(line)
                if d["ep"] == "消失":
                    cls.mv.append(d)
        cls.meta = json.loads((INDEX_DIR / "meta.json").read_text(encoding="utf-8"))

    def test_01_restore_point_detected(self):
        """meta.json 必须记下恢复点，否则说明锚点没命中。"""
        self.assertIsNotNone(
            self.meta.get("movie_restore"),
            "meta.json 没有 movie_restore —— 恢复锚点没命中，"
            "切点之后仍会全部标 crash",
        )
        self.assertEqual(
            self.meta["movie_restore"], RESTORE_TS,
            f"恢复点应是 {RESTORE_TS}，实际 {self.meta.get('movie_restore')}",
        )

    def test_01b_restore_basis_is_objective_world(self):
        """恢复点的口径必须是「客观世界」，不能被读成「角色认知」。

        2:33:26 是角色确认句，只写进 movie_confirm，不参与 world 判定。
        """
        self.assertEqual(self.meta.get("movie_restore_basis"), "objective_world")
        self.assertEqual(self.meta.get("movie_confirm"), CONFIRM_TS)
        self.assertNotEqual(self.meta.get("movie_restore"),
                            self.meta.get("movie_confirm"),
                            "恢复点与确认点被混成同一处了")
        self.assertEqual(self.meta.get("movie_confirm_anchor"),
                         "世界变回了原来的样子")
        self.assertEqual(self.meta.get("movie_restore_anchor"),
                         "我健康地出院了")

    def test_02_no_crash_after_restore(self):
        """恢复点之后不能再有 crash。

        修复前实测：2:33:26 之后仍有 46 条 crash（含片尾 5 条字幕）。
        """
        after = [
            d for d in self.mv
            if ts_sec(d["start"]) >= ts_sec(RESTORE_TS) and d["world"] == "crash"
        ]
        samples = [f'{d["start"]} {d["text"][:30]}' for d in after[:5]]
        self.assertEqual(
            after, [],
            f"恢复点之后仍有 {len(after)} 条 crash，例如：{samples}",
        )

    def test_03_crash_zone_intact(self):
        """切点与恢复点之间仍然必须是 crash —— 修复不能把这段也放了。"""
        between = [
            d for d in self.mv
            if ts_sec(CUT_TS) <= ts_sec(d["start"]) < ts_sec(RESTORE_TS)
        ]
        self.assertGreater(len(between), 1000,
                           f"切点到恢复点之间只有 {len(between)} 条，明显不对")
        wrong = [d for d in between if d["world"] != "crash"]
        self.assertEqual(
            wrong, [],
            f"切点到恢复点之间有 {len(wrong)} 条不是 crash",
        )

    def test_04_before_cut_still_normal(self):
        """切点之前必须是 normal。"""
        before = [d for d in self.mv if ts_sec(d["start"]) < ts_sec(CUT_TS)]
        wrong = [d for d in before if d["world"] != "normal"]
        self.assertEqual(wrong, [], f"切点之前有 {len(wrong)} 条不是 normal")

    def test_05_tv_episodes_all_normal(self):
        """TV 28 集全部是 normal —— 修复只应影响《消失》。"""
        with (INDEX_DIR / "docs.jsonl").open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                d = json.loads(line)
                if d["ep"] != "消失":
                    self.assertEqual(d["world"], "normal",
                                     f'第 {d["ep"]} 集 {d["start"]} 不是 normal')

    def test_06_tail_subtitles_are_normal(self):
        """片尾字幕（2:41:10 起 5 条）属于主线。

        它们不是剧情对白，但也不该被当成崩溃世界的经历。
        """
        tail = [d for d in self.mv if ts_sec(d["start"]) >= ts_sec("2:41:10")]
        self.assertEqual(len(tail), 5, f"片尾应有 5 条，实际 {len(tail)}")
        wrong = [d for d in tail if d["world"] != "normal"]
        self.assertEqual(wrong, [], f"片尾有 {len(wrong)} 条不是 normal")

    def test_07_ctx_worlds_consistent(self):
        """ctx_worlds 必须与 ctx 行数一致，且与本条 world 匹配。"""
        for d in self.mv:
            cw = d.get("ctx_worlds")
            self.assertIsNotNone(cw, f'{d["ep"]}@{d["start"]} 缺 ctx_worlds')
            self.assertEqual(
                len(cw), len(d["ctx"].split("\n")),
                f'{d["ep"]}@{d["start"]} ctx_worlds 与 ctx 行数不一致',
            )

    # ---- P0-4: transition 标记 -------------------------------------

    def test_08_transition_count_and_span(self):
        """过渡段恰好 6 条，且全部落在 [restore, confirm) 之间。"""
        tr = [d for d in self.mv if d.get("transition")]
        self.assertEqual(
            len(tr), EXPECTED_TRANSITION,
            "过渡记录应为 %d 条，实际 %d 条：%s"
            % (EXPECTED_TRANSITION, len(tr),
               [f'{d["start"]} {d["text"][:12]}' for d in tr]),
        )
        lo, hi = ts_sec(RESTORE_TS), ts_sec(CONFIRM_TS)
        for d in tr:
            self.assertTrue(
                lo <= ts_sec(d["start"]) < hi,
                f'{d["id"]} {d["start"]} 不在过渡时间窗内',
            )
        # 时间窗内不能有漏标的
        in_span = [d for d in self.mv if lo <= ts_sec(d["start"]) < hi]
        self.assertEqual(
            len(in_span), EXPECTED_TRANSITION,
            f"时间窗内应有 {EXPECTED_TRANSITION} 条，实际 {len(in_span)} 条",
        )
        unmarked = [d for d in in_span if not d.get("transition")]
        self.assertEqual(unmarked, [], f"时间窗内有 {len(unmarked)} 条漏标 transition")

    def test_09_transition_world_is_normal(self):
        """过渡段客观世界已恢复 —— world 必须是 normal，不能靠 transition 兜。"""
        tr = [d for d in self.mv if d.get("transition")]
        wrong = [f'{d["id"]} {d["world"]}' for d in tr if d["world"] != "normal"]
        self.assertEqual(
            wrong, [],
            "过渡段 world 不是 normal：%s（objective_world 口径下这一段已回到主线）"
            % wrong,
        )

    def test_10_transition_reason_recorded(self):
        """transition 存原因字符串而不是 bool，否则下游无法区分来源。"""
        tr = [d for d in self.mv if d.get("transition")]
        for d in tr:
            self.assertIsInstance(d["transition"], str)
            self.assertEqual(d["transition"], TRANSITION_REASON)
        # 非过渡记录显式写 None，而不是缺字段
        for d in self.mv:
            self.assertIn("transition", d, f'{d["id"]} 缺 transition 字段')
            if not d["transition"]:
                self.assertIsNone(d["transition"])

    def test_11_transition_is_independent_of_world(self):
        """transition 必须能被独立观察到：存在 normal 且非过渡的记录。

        若 transition 只是 world 的别名，就无法在不改 world 的前提下
        表达「归属有争议」。这里验证两个字段不是一一对应关系。
        """
        normal_no_tr = [d for d in self.mv
                        if d["world"] == "normal" and not d["transition"]]
        normal_tr = [d for d in self.mv
                     if d["world"] == "normal" and d["transition"]]
        # self.mv 只装《消失》2114 条，normal 段 = 切点前 528 + 恢复后 58 = 586。
        self.assertGreater(len(normal_no_tr), 500)
        self.assertEqual(len(normal_tr), EXPECTED_TRANSITION)
        # 每条 transition 记录都不携带 speaker 字段 —— 过渡标记不提供
        # 任何说话者信息，不能据此认定是长门台词。
        for d in normal_tr:
            self.assertNotIn("speaker", d,
                             f'{d["id"]} 带了 speaker，transition 不应携带说话者信息')

    def test_12_boundary_world_counts(self):
        """修正后的世界总量（客观口径 46 条方案）。"""
        w = self.meta["worlds"]
        self.assertEqual(w["normal"]["lines"], 11542)
        self.assertEqual(w["crash"]["lines"], 1528)
        self.assertEqual(w["normal"]["lines"] + w["crash"]["lines"],
                         self.meta["docs"])
        self.assertEqual(self.meta["transition"]["total"], EXPECTED_TRANSITION)


if __name__ == "__main__":
    unittest.main(verbosity=2)
