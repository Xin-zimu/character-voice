#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""scene.py 的回归：场景状态怎么更新、什么时候**不**更新。

这些用例只检查可观察行为：给定一句话，state 变成什么、render 输出什么。
不检查内部实现怎么切词、怎么匹配。

CHAT-013-B 的每一条（状态属于她不属于你、否定/假设/疑问不更新、
两世界互不继承、状态宁可缺失不可编造）在这里各有一组用例钉住。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scene  # noqa: E402
import prompt_builder as pb  # noqa: E402


class ParseTests(unittest.TestCase):
    """`parse_updates` —— 从一句话读状态变化。"""

    def test_01_bracket_action_is_hers(self):
        """括号动作默认是**她**做的。这是 CHAT-007 的约定。"""
        up, _ = scene.parse_updates("（把眼镜摘下来）")
        self.assertEqual(up, {"glasses": False})

        up, _ = scene.parse_updates("（摘下眼镜，放到桌上）")
        self.assertEqual(up, {"glasses": False})

    def test_02_body_subject_is_user(self):
        """正文里的「我摘了眼镜」说的是用户，**不改她的状态**。

        这是 CHAT-013-B 第一条。「我」和「你」在这件事上不能混。
        """
        for text in ("我摘了眼镜", "我把眼镜摘下来放在桌上", "眼镜摘了，有点糊"):
            up, _ = scene.parse_updates(text)
            self.assertNotIn("glasses", up, f"「{text}」不该改她的眼镜状态")

    def test_03_negation_hypothesis_question(self):
        """否定 / 假设 / 疑问都不更新。CHAT-013-B 明文要求。"""
        cases = [
            "（别摘眼镜）",
            "（没有摘眼镜）",
            "（如果不戴眼镜）",
            "（要不要把书合上）",
            "（眼镜摘了吗）",
            "（要是合上书呢）",
            "（书合上了？）",
        ]
        for text in cases:
            up, _ = scene.parse_updates(text)
            self.assertEqual(up, {}, f"「{text}」不该更新任何状态")

    def test_03a_negation_across_object(self):
        """否定词和动作词隔着一个宾语时也要挡住。

        「（别把书合上）」是**明令禁止**，不是「她把书合上了」。
        初版只认「别」和动作词相邻，漏掉了这一整类——被手工试样例发现的。
        """
        for text in ("（别把书合上）", "（不用把眼镜摘了）", "（没合上书）",
                     "（不要翻开那本书）"):
            up, _ = scene.parse_updates(text)
            self.assertEqual(up, {}, f"「{text}」是禁止/否定，不该改状态")

    def test_03b_a_not_a_question(self):
        """正反问（有没有 / 是不是 / 会不会）真值不确定，不能当陈述读。

        「（有没有翻开书）」含「没有」，看着像否定、实际是在问；
        而 `_NEGATED` 只拦「没有 + 动作词」，中间隔了「翻开」就漏掉，
        结果被读成「翻开了」。这是修 `（别把书合上）` 时顺带查出来的。
        """
        for text in ("（有没有翻开书）", "（是不是合上了）", "（会不会合上）",
                     "（合没合上）"):
            up, _ = scene.parse_updates(text)
            self.assertEqual(up, {}, f"「{text}」是正反问，不该改状态")

    def test_04_question_forms_outside_brackets(self):
        """正文疑问句不更新共同场景。"""
        for text in ("我到活动室了吗", "我是不是到活动室了", "我要是到了活动室"):
            up, _ = scene.parse_updates(text)
            self.assertNotIn("together", up, f"「{text}」是疑问/假设，不该进入共同场景")

    def test_05_multi_step_last_wins(self):
        """一段括号里两步动作，后发生的覆盖先发生的。

        「（把书合上，又翻开）」结束时书是打开的，不是合上的。
        """
        up, _ = scene.parse_updates("（把书合上，又翻开）")
        self.assertEqual(up.get("book"), True)

        up, _ = scene.parse_updates("（把书翻开，又合上）")
        self.assertEqual(up.get("book"), False)

    def test_05b_two_actions_same_clause_last_wins(self):
        """同一段里「摘下又戴上」要读成戴上了。

        初版按模式顺序 `if 摘下 elif 戴上` 取先命中的，于是「把眼镜摘下来又戴上」
        被判成摘下了——正好读反。这类句子在崩溃卡里是现成的写法
        （人格卡第 11 条就写着「（把书合上，又翻开）」），不是罕见输入。
        """
        self.assertIs(scene.parse_updates("（把眼镜摘下来又戴上）")[0]["glasses"], True)
        self.assertIs(scene.parse_updates("（把眼镜摘下来戴好）")[0]["glasses"], True)
        self.assertIs(scene.parse_updates("（戴上眼镜又摘下来）")[0]["glasses"], False)
        self.assertIs(scene.parse_updates("（合上书，又翻开）")[0]["book"], True)
        self.assertIs(scene.parse_updates("（翻开书，又合上）")[0]["book"], False)

    def test_05c_bracket_negation_does_not_leak_outward(self):
        """括号里的否定只管括号里。

        「（不用在意）戴上眼镜」：那个「不用」是对「在意」的否定，
        不是「不要戴」。而括号外的「戴上眼镜」是**用户**的陈述，按 CHAT-013-B
        本来也不该改她的状态。两头都不能误判成「她摘下了眼镜」。
        """
        self.assertEqual(scene.parse_updates("（不用在意）戴上眼镜")[0], {})
        # 括号里她自己的动作照常生效
        self.assertIs(scene.parse_updates("（戴上眼镜）不用在意")[0]["glasses"], True)

    def test_07a_together_requires_being_there(self):
        """「我在想活动室」不是「我在活动室」。

        `_ARRIVE` 初版写成 `我(已经)?(到|在|进)了?`，`了?` 可省，
        于是「我在想活动室的事」被判成同处一室。这种误判很伤：她会以为
        用户就在身边，之后每句都按「面对面」来说。
        """
        for text in ("我在想活动室的事啊", "我在说活动室", "我在关心图书馆的事",
                     "我还没决定要不要去活动室", "我在去活动室的路上"):
            self.assertNotIn("together", scene.parse_updates(text)[0],
                             f"「{text}」说的不是身处何地")

        for text in ("我在活动室了", "我在活动室", "我在部室门口", "我刚到活动室",
                     "我坐在活动室里", "我已经到图书馆了"):
            self.assertIs(scene.parse_updates(text)[0].get("together"), True,
                          f"「{text}」是在说自己到了")

    def test_06_repeat_action_needs_object(self):
        """省略宾语的重复动作只在同一段提过宾语时才认。

        「（又合上）」合上的可能是窗，不能算到书上。
        """
        up, _ = scene.parse_updates("（又合上）")
        self.assertNotIn("book", up)

        up, _ = scene.parse_updates("（合上书，过了一会又打开）")
        self.assertEqual(up.get("book"), True)

    def test_07_together_needs_place_and_arrival(self):
        """共同场景要具体地点 + 到达表述 + 主语「我」（CHAT-008）。"""
        yes = ["我在部室门口", "我到活动室了", "我已经到图书馆了", "我在校门口"]
        for text in yes:
            up, _ = scene.parse_updates(text)
            self.assertIs(up.get("together"), True, f"「{text}」应进入共同场景")

    def test_08_together_not_inferred(self):
        """含糊的「一起……吧」不算进入共同场景（CHAT-008 明文）。"""
        no = [
            "一起看书吧",
            "我们一起读吧",
            "我想去活动室",
            "我去过活动室",
            "活动室关门了",
            "你说活动室在哪",
        ]
        for text in no:
            up, _ = scene.parse_updates(text)
            self.assertNotIn("together", up, f"「{text}」不该被当成共同场景")

    def test_09_empty_and_junk(self):
        """空串、纯符号不炸也不更新。"""
        for text in ("", "   ", "……", "？？？", None):
            up, notes = scene.parse_updates(text or "")
            self.assertEqual(up, {})
            self.assertEqual(notes, [])


class SceneStateTests(unittest.TestCase):
    """`Scene` / `SceneRegistry` —— 状态怎么存、怎么输出。"""

    def test_10_world_defaults_differ(self):
        """两个世界初始值不同：主线不戴眼镜，崩溃戴。"""
        self.assertIs(scene.Scene("normal").state["glasses"], False)
        self.assertIs(scene.Scene("crash").state["glasses"], True)

    def test_11_unknown_not_rendered(self):
        """未知项不输出。没有记录就不提，不猜（CHAT-013 末条）。"""
        s = scene.Scene("normal")
        # 书的状态开局是 None（不知道）
        self.assertIsNone(s.state["book"])
        # 逐行断言，而不是 assertNotIn("书", ...)：改成后者的话，
        # 变异出「book：没有记录」这种**用键名占位**的输出仍然能通过
        # ——它不含「书」字。要断言的是「只有已知的两行」。
        self.assertEqual(s.render().splitlines(),
                         ["- 没有戴眼镜", "- 你们不在同一个空间，隔着终端说话"])

        s.apply({"book": True})
        self.assertIn("- 手里拿着一本打开的书", s.render().splitlines())

    def test_12_worlds_do_not_inherit(self):
        """一个世界改了，另一个世界不受影响。"""
        reg = scene.SceneRegistry()
        reg.update_from("normal", "（把眼镜摘下来）")
        self.assertIs(reg.get("normal").state["glasses"], False)
        self.assertIs(reg.get("crash").state["glasses"], True)

        reg.update_from("crash", "（把眼镜摘下来）")
        self.assertIs(reg.get("crash").state["glasses"], False)
        # 主线那边还是刚才的值，没被崩溃世界覆盖
        self.assertIs(reg.get("normal").state["glasses"], False)

    def test_13_apply_reports_only_changes(self):
        """`apply` 只报**这次真变了**的项。重复设置同一个值不算变化。"""
        s = scene.Scene("normal")
        self.assertEqual(s.apply({"glasses": False}), [])
        self.assertEqual(s.apply({"glasses": True}), ["glasses"])

    def test_14_apply_ignores_unknown_key(self):
        """状态是枚举内的固定几项，不接受任意键。"""
        s = scene.Scene("normal")
        self.assertEqual(s.apply({"hair_color": "blue"}), [])
        self.assertNotIn("hair_color", s.state)

    def test_15_bad_world_rejected(self):
        """世界名不认识就报错，不静默回退（WORLD-001）。"""
        with self.assertRaises(ValueError):
            scene.Scene("school")
        with self.assertRaises(ValueError):
            scene.SceneRegistry().get("")

    def test_16_render_labels_cover_every_tracked(self):
        """每个跟踪项都要有标签，否则状态改了却说不出口。"""
        for key in scene.TRACKED:
            for value in (True, False):
                self.assertIn(key, scene.LABEL)
                self.assertIn(value, scene.LABEL[key], f"{key}={value} 没有标签")

    def test_17_reset_restores_defaults(self):
        """reset 回到初始值，且可以只重置一个世界。"""
        reg = scene.SceneRegistry()
        reg.update_from("normal", "（把眼镜摘下来）")
        reg.update_from("crash", "（把眼镜摘下来）")
        reg.reset("normal")
        self.assertIs(reg.get("normal").state["glasses"], False)  # 主线默认就是 False
        self.assertIs(reg.get("crash").state["glasses"], False)   # 崩溃世界没被重置


class PromptWiringTests(unittest.TestCase):
    """场景状态进 prompt 的形状。"""

    PERSONA = {
        "id": "t", "world": "normal",
        "system_prompt": "你是长门有希。",
        "examples": [],
    }

    def _system(self, **kw):
        msgs = pb.build_messages(self.PERSONA, "normal", [], [], "在吗", **kw)
        return msgs[0]["content"]

    def test_18_scene_block_present(self):
        """有状态时出现「=== 此刻 ===」块，且内容就是 render 的结果。"""
        s = scene.Scene("normal")
        s.apply({"book": True})
        text = self._system(scene=s.render())
        self.assertIn("=== 此刻 ===", text)
        self.assertIn("手里拿着一本打开的书", text)
        self.assertIn("状态结束", text)

    def test_19_no_scene_no_block(self):
        """没有状态就不出现这一块，也不说「当前没有状态」。"""
        for kw in ({}, {"scene": ""}, {"scene": "   "}):
            text = self._system(**kw)
            self.assertNotIn("=== 此刻 ===", text)
            self.assertNotIn("此刻的状态", text)

    def test_20_scene_and_memory_both(self):
        """场景块与共同经历块共存，且是两个独立的块（来源不同不能混）。"""
        mems = [{"id": "m1", "world": "normal", "text": "上次说在赶项目",
                 "subject": "user", "status": "active", "created": "2026-01-01"}]
        text = self._system(scene="- 戴着眼镜", memories=mems)
        self.assertIn("=== 共同经历 ===", text)
        self.assertIn("=== 此刻 ===", text)
        self.assertLess(text.index("=== 共同经历 ==="), text.index("=== 此刻 ==="))

    def test_21_scene_prompt_forbids_fabrication(self):
        """SCENE_PROMPT 要明确写「没列出就是没记录，不要编」。"""
        self.assertIn("不要替它编", scene.SCENE_PROMPT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
