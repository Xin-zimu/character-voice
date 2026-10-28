#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 60 条评测问题以可复现的方式写入 data/retrieval_eval/questions.jsonl。

为什么用脚本生成而不是手写 JSONL：
  1. 每条问题都要带 `expected`（预期相关的时间码），手写 60 条极易打错时间码，
     而打错的时间码在评测里会表现为「检索错了」，实际是标注错了；
  2. 生成时逐条断言 `expected` 的时间码在索引里真实存在——标注错误在生成阶段
     就暴露，不会污染基线数字；
  3. 问题集本身是**可复核的产物**，脚本读起来比 60 行 JSON 更能看出设计意图。

标注纪律（写进 RETRIEVAL_EVAL.md，这里再重复一次）：
  - `expected` 只放「这段话确实是回答这个问题的依据」，不放「提到过类似词」；
  - `forbidden` 只放「命中就算错」的条目，且必须能说清为什么错；
  - 拿不准的一律不写进 expected，标 `confidence: low` 并在注里说明；
  - 语料里确实没有依据的，`expected` 为空，判据写成 `expect_empty: true`。

用法：
    python scripts/build_retrieval_eval.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "data" / "index" / "docs.jsonl"
OUT = ROOT / "data" / "retrieval_eval" / "questions.jsonl"


# ---------------------------------------------------------------------------
# 问题集。字段含义见 RETRIEVAL_EVAL.md 的「标注口径」一节。
#
# 每条：id / category / world / query / expected / partial / forbidden /
#       expect_empty / note / confidence
#   expected  : 必须命中的 (集, 时间码)，任一命中即算 recall 达标
#   partial   : 相关但不必需（命中加分，不命中不扣分）
#   forbidden : 命中即算错
#   expect_empty : True 表示「正确行为是一条都不召回」
# ---------------------------------------------------------------------------
QUESTIONS: list[dict] = [
    # =====================================================================
    # A · 原作事实（她的自述与世界观陈述）
    # 依据：03 集开头是她的连续独白，语境明确，是全片最可靠的「她本人陈述」
    # =====================================================================
    dict(id="A01", category="fact", world="normal",
         query="你的工作是什么",
         expected=[("03", "0:00:04"), ("17", "0:21:20")],
         forbidden=[],
         note="03@0:00:04「我的工作是观察凉宫春日」是她的原句自述。17@0:21:20 是同一句的重复表述。"),
    dict(id="A02", category="fact", world="normal",
         query="你为什么要观察凉宫春日",
         expected=[("03", "0:00:04"), ("03", "0:00:07"), ("03", "0:00:18")],
         forbidden=[("04", "0:07:46")],
         note="03@0:00:04「我的工作是观察凉宫春日」是被问的那件事本身；"
              "0:00:07「并将获得的信息反馈给统合思念体」与 0:00:18「出现了无法忽略的异常因子」是理由。"
              "04@0:07:46 是阿虚在问「为什么我要被朝仓用刀子追杀」，语义完全不同，命中即错。"
              "**这一条第一次标注漏了 0:00:04**——它明明是「观察凉宫春日」的出处，"
              "漏标的后果是「检索明明召回了正确句却算 0 分」。见 RETRIEVAL_EVAL.md 的标注审计一节。"),
    dict(id="A03", category="fact", world="normal",
         query="你出生多久了",
         expected=[("03", "0:00:11"), ("03", "0:00:15")],
         forbidden=[("06", "0:12:20")],
         note="「在我「出生」以来的这三年」。06@0:12:20 是阿虚在说「如果我也在那边出生了的话」，"
              "指另一个世界线的自己，不是她的年龄。"),
    dict(id="A04", category="fact", world="normal",
         query="信息统合思念体是什么",
         expected=[("03", "0:05:05"), ("03", "0:01:57"), ("02", "0:21:43")],
         forbidden=[],
         note="03 集 0:05:00 起是她集中解释思念体的段落。02@0:21:43 是这段解释的另一次出现。"),
    dict(id="A05", category="fact", world="normal",
         query="凉宫春日是什么人",
         expected=[("03", "0:04:02"), ("02", "0:04:53")],
         forbidden=[],
         confidence="low",
         note="语料里没有一段「她的身份是」式的完整定义；这两条是最接近的。"
              "标 low：结论只能是「召回到相关讨论」，不能说是标准答案。"),
    dict(id="A06", category="fact", world="normal",
         query="你能改变世界吗",
         expected=[("24", "0:08:27"), ("03", "0:20:42")],
         forbidden=[],
         note="24@0:08:27「我不觉得那是改变世界构造的能力」是否定式回答，正是这个问题的答案。"),
    dict(id="A07", category="fact", world="normal",
         query="凉宫春日有危险吗",
         expected=[("03", "0:00:18")],
         forbidden=[],
         confidence="low",
         note="「最近在凉宫春日的身边出现了无法忽略的异常因子」隐含风险。"
              "标 low：语料里没有「她有危险」的直陈句，这条考的是「能不能找到最接近的」。"),

    # =====================================================================
    # B · 长门的典型表达（短应答 / 冷陈述 / 术语只在被问时给）
    # =====================================================================
    dict(id="B01", category="style", world="normal",
         query="可以借用这里吗",
         expected=[("01", "0:18:17")],
         partial=[("01", "0:18:22"), ("01", "0:18:27"), ("01", "0:18:31")],
         forbidden=[],
         note="01 集 0:18:17 是阿虚提出的请求句「这家伙想借用这里」，"
              "0:18:22–0:18:31 是她的三连答复「可以／无所谓／请便」。"
              "**expected 只能放请求句**：她的答复单句（「可以」「请便」）没有主语也没有内容，"
              "把这种句子当「必须命中」的期望是不公平的标注——检索器没有任何理由知道"
              "「可以」是在回答什么。三条答复放 partial。"),
    dict(id="B02", category="style", world="normal",
         query="如果给你带来很多麻烦呢",
         expected=[("01", "0:18:23")],
         partial=[("01", "0:18:27"), ("01", "0:18:31")],
         forbidden=[],
         note="0:18:23「那个…不过我想可能会给你带来很多麻烦哦」是这句问法的完整来源。"
              "她的回应「无所谓」「请便」放 partial，理由同 B01。"),
    dict(id="B03", category="style", world="normal",
         query="怎么和凉宫同学聊天",
         expected=[("01", "0:12:43")],
         partial=[("01", "0:12:46")],
         forbidden=[],
         note="0:12:43「到底应该怎么和她聊天呢？有诀窍吗？」是阿虚的提问，"
              "0:12:46「不知道」是她的回答。同理：问句作 expected，答案作 partial。"),
    dict(id="B04", category="style", world="normal",
         query="你叫什么名字",
         expected=[],
         partial=[("01", "0:18:12")],
         forbidden=[],
         confidence="low",
         note="语料里她对「你叫什么名字」没有直接回答（自我介绍由旁人代做）。"
              "expected 留空 + 标 low：这条考的是「检索会不会硬凑一个自我介绍」。"
              "partial 里 01@0:18:12 是阿虚念出她的名字，属旁白，仅供参考。"),
    dict(id="B05", category="style", world="normal",
         query="…的确",
         expected=[("03", "0:05:00")],
         forbidden=[],
         note="seed 里记录的「…的确」→ 立刻切换长篇技术陈述。考的是省略号句首能不能被检索到。"),
    dict(id="B06", category="style", world="normal",
         query="你是外星人吗",
         expected=[("03", "0:00:02")],
         forbidden=[],
         confidence="low",
         note="03@0:00:02「外…星人？」是阿虚的惊问（旁人），不是她的回答。"
              "标 low：这条故意考「检索能不能区分问答对」，预期它召回对的语境即算达标。"),
    dict(id="B07", category="style", world="normal",
         query="SOS团是做什么的",
         expected=[("03", "0:10:45"), ("02", "0:09:29")],
         forbidden=[],
         note="两条都是「至今还没人知道这社团要做什么」的表述，与问题同指。"),
    dict(id="B08", category="style", world="normal",
         query="你有没有感情",
         expected=[("09", "0:22:59")],
         forbidden=[],
         confidence="low",
         note="09@0:22:59「长门 你果然也有这样的感情吧？」是阿虚在问她。"
              "标 low：语料里没有她本人谈感情的段落，这条只能考「找到被问的那一处」。"),

    # =====================================================================
    # C · 多角色混淆（问法里出现别人的名字，正确答案不是那个人）
    # =====================================================================
    dict(id="C01", category="confusion", world="normal",
         query="朝比奈学姐是什么人",
         expected=[("02", "0:21:43")],
         forbidden=[("01", "0:18:12")],
         confidence="low",
         note="02@0:21:43 是解释思念体与终端界面的段落，提到「制造出来」，与朝比奈无关但同属世界观陈述。"
              "标 low 且 expected 可能不准——列在这里正是为了暴露「问他人时检索会拉回什么」。"),
    dict(id="C02", category="confusion", world="normal",
         query="阿虚说过什么",
         expected=[],
         forbidden=[],
         expect_empty=False,
         confidence="low",
         note="这个问题本身按设计不该有「正确召回」——语料没有说话人标注，"
              "无法按说话人过滤。这条只做观察项，不进主指标。"),
    dict(id="C03", category="confusion", world="normal",
         query="古泉一树怎么看这件事",
         expected=[],
         forbidden=[],
         confidence="low",
         note="同上，观察项。看检索会不会因为「古泉」出现而召回古泉的台词。"),
    dict(id="C04", category="confusion", world="normal",
         query="凉宫春日说要改变世界",
         expected=[("03", "0:20:42")],
         forbidden=[],
         note="主语是春日，但依据段落是长门在解释春日的性质。考的是「主语错了也知道该找谁的解释」。"),

    # =====================================================================
    # D · NORMAL / CRASH 世界边界
    # =====================================================================
    dict(id="D01", category="world", world="normal",
         query="你为什么戴着眼镜",
         expected=[("消失", "0:35:57")],
         forbidden=[("消失", "2:11:54"), ("04", "0:21:38")],
         note="0:35:57「为什么戴着眼镜」在切点（0:38:13）之前，属 normal 侧。"
              "同一个词在 crash 侧 2:11:54 是「而且不戴眼镜比较好」，语境相反；"
              "04@0:21:38「果然你还是不戴眼镜比较好」也是相反语境。两条都列进 forbidden。"),
    dict(id="D02", category="world", world="crash",
         query="你还记得凉宫春日吗",
         expected=[("消失", "1:05:10")],
         forbidden=[],
         note="崩溃世界里她不应该认识春日。这条考「crash 侧召回到的是什么」——"
              "如果召回到她谈论春日的段落，说明归属区间有问题。"),
    dict(id="D03", category="world", world="crash",
         query="信息统合思念体",
         expected=[],
         forbidden=[],
         confidence="low",
         note="崩溃世界里她「不懂那些东西」（yuki_school.json 的自我认知）。"
              "但语料 crash 段确实有阿虚在问她思念体的事。这条用于暴露"
              "「语料归属 vs 人格设定」的冲突，本身不作为通过/不通过判据。"),
    dict(id="D04", category="world", world="normal",
         query="世界变回了原来的样子",
         expected=[("消失", "2:33:26")],
         forbidden=[],
         confidence="low",
         note="这句是《消失》片尾阿虚的旁白。它现在被标为 crash（见 CORPUS_AUDIT 的 C-04）。"
              "列在这里正是为了暴露「同一句话在错误世界里根本检索不到」。"),

    # =====================================================================
    # E · 语料中无依据（正确行为：不召回 / 召回也很低分）
    # =====================================================================
    dict(id="E01", category="absent", world="normal",
         query="你会写Python吗",
         expected=[], forbidden=[], expect_empty=True,
         note="原作里没有编程相关台词。正确行为：召回 0 条。"),
    dict(id="E02", category="absent", world="normal",
         query="帮我看看这段代码有什么bug",
         expected=[], forbidden=[], expect_empty=True,
         note="同上。「BUG」一词在原作里指剧情现象（24 集），不是编程 bug。"),
    dict(id="E03", category="absent", world="normal",
         query="今天股市怎么样",
         expected=[], forbidden=[], expect_empty=True,
         note="语料里有「股市」注释（Black Monday 的译者注），但那是注释文本不是对白。"),
    dict(id="E04", category="absent", world="normal",
         query="你用什么牌子的手机",
         expected=[], forbidden=[], expect_empty=True,
         note="原作提到手机但没有品牌。"),
    dict(id="E05", category="absent", world="normal",
         query="你怎么看量子纠缠",
         expected=[], forbidden=[], expect_empty=True,
         note="完全无依据。"),
    dict(id="E06", category="absent", world="normal",
         query="在吗",
         expected=[], forbidden=[], expect_empty=True,
         note="tokenize('在吗') == []，检索直接返回空。这条验证「纯虚词查询」不会硬凑。"),
    dict(id="E07", category="absent", world="normal",
         query="那个",
         expected=[], forbidden=[], expect_empty=True,
         note="tokenize('那个') == []。同上。"),
    dict(id="E08", category="absent", world="normal",
         query="是吗",
         expected=[], forbidden=[], expect_empty=True,
         note="tokenize('是吗') == []。同上。"),

    # =====================================================================
    # 第二批（A09–A14 / B09–B16 / C05–C07 / D05–D07 / E09–E16）
    # 扩充到 60 条。锚点全部来自实测存在的索引记录（生成脚本会断言）。
    # =====================================================================
    dict(id="A08", category="fact", world="normal",
         query="你平时做什么",
         expected=[("03", "0:00:04")],
         partial=[("03", "0:00:11")],
         forbidden=[],
         confidence="low",
         note="「我的工作是观察凉宫春日」。标 low：与 A01 同源，"
              "两条同时出题是为了看不同问法是否收敛到同一处依据。"),

    dict(id="A09", category="fact", world="normal",
         query="神人是什么",
         expected=[("05", "0:21:11"), ("05", "0:21:14")],
         partial=[("05", "0:21:14"), ("05", "0:21:20")],
         forbidden=[],
         note="05 集 0:21:11 起是她的连续解释：「我们称之为神之属族——「神人」」「神人和凉宫同学的精神活动相同步」。"),
    dict(id="A10", category="fact", world="normal",
         query="封闭空间变大会怎样",
         expected=[("05", "0:21:46")],
         partial=[("05", "0:21:20")],
         forbidden=[],
         note="「否则随着神人破坏范围的增大 封闭空间也会增大」。"),
    dict(id="A11", category="fact", world="normal",
         query="什么是终端界面",
         expected=[("03", "0:03:21"), ("02", "0:21:49"), ("03", "0:05:00")],
         forbidden=[],
         note="三条都是她对「终端界面」的定义性表述。"),
    dict(id="A12", category="fact", world="normal",
         query="时间移动是怎么做到的",
         expected=[("03", "0:13:29"), ("04", "0:21:13")],
         forbidden=[],
         note="「…时间移动就是一个时间平面上的三次元移动」。"),
    dict(id="A13", category="fact", world="normal",
         query="我们的力量是不是要消失了",
         expected=[("06", "0:10:20"), ("06", "0:11:08")],
         forbidden=[],
         note="06 集是「她把自己的力量托付给你」那一集，两条都是相关陈述。"),
    dict(id="A14", category="fact", world="normal",
         query="你为什么加入文艺部",
         expected=[("01", "0:22:22")],
         partial=[("01", "0:19:29")],
         forbidden=[],
         confidence="low",
         note="01@0:22:22「我会退出书法社 加入你们的社团」是被春日拉人时的应答。"
              "标 low：语料里没有她解释「为什么」的句子。"),

    dict(id="B09", category="style", world="normal",
         query="你真喜欢看书呢",
         expected=[("01", "0:19:29")],
         forbidden=[],
         note="她最典型的一个标签。这句是阿虚说的，但她的回应紧邻其后——"
              "考的是「能不能召回到这个语境」。"),
    dict(id="B10", category="style", world="normal",
         query="谢谢你借我手绢",
         expected=[("消失", "0:25:53")],
         partial=[("消失", "0:25:56")],
         forbidden=[],
         note="这正是 persona 里唯一一条 verified 示例的来源："
              "「谢谢你」→「不需要。」。考的是示例依据能不能被检索到。"),
    dict(id="B11", category="style", world="normal",
         query="不用了",
         expected=[],
         forbidden=[],
         confidence="low",
         note="观察项：看短应答「不用了」会被谁的话命中——语料没有说话人标注，无法保证是她。"),
    dict(id="B12", category="style", world="normal",
         query="我知道了",
         expected=[],
         forbidden=[],
         confidence="low",
         note="同上，观察项。"),
    dict(id="B13", category="style", world="normal",
         query="而且还有可能把你赶出去哦",
         expected=[("01", "0:18:28")],
         partial=[("01", "0:18:31")],
         forbidden=[],
         note="01 集 0:18:27–0:18:31 是「无所谓的威胁」那段：她的回应是「无所谓」「请便」。"),
    dict(id="B14", category="style", world="normal",
         query="你会拒绝吗",
         expected=[],
         forbidden=[],
         confidence="low",
         note="观察项：抽象提问，看检索能否找到「拒绝」类表述，不设正确答案。"),
    dict(id="B15", category="style", world="normal",
         query="嗯",
         expected=[],
         forbidden=[],
         confidence="low",
         note="观察项：单字应答「嗯」在原作里出现 54 次，几乎全不是她。"
              "这条用于量化「高频短句检索的必然噪声」。"),
    dict(id="B16", category="style", world="normal",
         query="凉宫同学",
         expected=[],
         forbidden=[],
         confidence="low",
         note="观察项：称呼类查询，与 B15 同理。"),

    dict(id="C05", category="confusion", world="normal",
         query="朝仓凉子是什么人",
         expected=[("04", "0:07:46")],
         forbidden=[],
         confidence="low",
         note="04@0:07:46「为什么我要被朝仓用刀子追杀啊？」是阿虚的话，但它是「朝仓」最集中出现的段落。"
              "标 low：这条精确地暴露「问别人时召回的是问句本身，不是答案」。"),
    dict(id="C06", category="confusion", world="normal",
         query="古泉说闭锁空间是怎么回事",
         expected=[("消失", "0:11:19")],
         forbidden=[],
         confidence="low",
         note="「闭锁空间和神人是吧」——全片只有这一处提到「闭锁空间」。"
              "标 low：语境是对话中提起，不是解释。"),
    dict(id="C07", category="confusion", world="normal",
         query="鹤屋学姐知道这件事吗",
         expected=[],
         forbidden=[],
         confidence="low",
         note="观察项：语料里鹤屋在场但几乎不说话。看检索会不会拉无关片段硬凑。"),

    dict(id="D05", category="world", world="normal",
         query="另一个世界的长门",
         expected=[("消失", "0:38:16")],
         forbidden=[],
         confidence="low",
         note="0:38:16「是你的本家情报统合思念体吗」在 crash 段。normal 侧检索不该返回它——"
              "这条同时考「normal 侧召回到什么」和「切点是不是切对了」。"),
    dict(id="D06", category="world", world="crash",
         query="你和凉宫春日是什么关系",
         expected=[],
         forbidden=[],
         confidence="low",
         note="观察项：崩溃世界里她不该认识春日（yuki_school 自我认知），"
              "但语料 crash 段有大量「春日」讨论（阿虚在说）。用于暴露设定与语料的冲突。"),
    dict(id="D07", category="world", world="crash",
         query="你为什么戴眼镜",
         expected=[("消失", "2:11:54")],
         forbidden=[("消失", "0:35:57")],
         confidence="low",
         note="crash 侧「不戴眼镜比较好」（2:11:54）与 normal 侧「为什么戴着眼镜」（0:35:57）"
              "是镜像语境。标 low：2:11:54 实际是阿虚的话。"),

    dict(id="E09", category="absent", world="normal",
         query="你多大了",
         expected=[], forbidden=[], expect_empty=True,
         confidence="low",
         note="观察项：她只说过「出生以来的这三年」（A03），没有直接回答年龄。"
              "expect_empty 但标 low——因为 A03 那条可能被召回，属合理。"),
    dict(id="E10", category="absent", world="normal",
         query="你有手机吗",
         expected=[], forbidden=[], expect_empty=True,
         note="原作提到手机但没有「她有手机」的陈述。"),
    dict(id="E11", category="absent", world="normal",
         query="帮我写一首诗",
         expected=[], forbidden=[], expect_empty=True,
         note="完全无依据。"),
    dict(id="E12", category="absent", world="normal",
         query="今天吃什么好",
         expected=[], forbidden=[], expect_empty=True,
         note="日常寒暄，语料没有对应内容。"),
    dict(id="E13", category="absent", world="normal",
         query="你怎么看人工智能",
         expected=[], forbidden=[], expect_empty=True,
         note="现代话题，2006 年的作品里不存在。"),
    dict(id="E14", category="absent", world="normal",
         query="你叫什么",
         expected=[], forbidden=[], expect_empty=True,
         confidence="low",
         note="观察项：和 B04 同义但更短，看两者召回是否一致。"),
    dict(id="E15", category="absent", world="normal",
         query="你在吗",
         expected=[], forbidden=[], expect_empty=True,
         confidence="low",
         note="观察项：「在吗」tokenize 为空，但「你在吗」含「你在」→ 应能切出 token。"
              "用于暴露「加一个字就从不召回变成召回」的脆弱性。"),
    dict(id="E16", category="absent", world="crash",
         query="信息统合思念体是什么",
         expected=[], forbidden=[], expect_empty=True,
         confidence="low",
         note="观察项：崩溃世界里她不懂这些（yuki_school）。但语料 crash 段有阿虚问她思念体。"
              "这条专门用于暴露「人格设定说不知道 / 语料里有这段」的冲突，不作通过判据。"),
]


def load_slots() -> set[tuple[str, str]]:
    slots = set()
    with INDEX.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                slots.add((r["ep"], r["start"]))
    return slots


def main() -> int:
    if not INDEX.exists():
        print(f"找不到索引 {INDEX}，先跑 python build_corpus.py")
        return 1
    slots = load_slots()

    # 生成阶段就抓住标注错误：时间码必须真实存在。
    problems = []
    for q in QUESTIONS:
        for ep, ts in list(q.get("expected", [])) + list(q.get("partial", [])) + \
                list(q.get("forbidden", [])):
            if (ep, ts) not in slots:
                problems.append(f'{q["id"]}: {ep}@{ts} 在索引里不存在')
    if problems:
        print("标注有误，未写出任何文件：")
        for p in problems:
            print("  ×", p)
        return 1

    ids = [q["id"] for q in QUESTIONS]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        print(f"问题 id 重复：{sorted(dup)}")
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        for q in QUESTIONS:
            row = {
                "id": q["id"],
                "category": q["category"],
                "world": q["world"],
                "query": q["query"],
                "expected": [{"ep": e, "start": t} for e, t in q.get("expected", [])],
                "partial": [{"ep": e, "start": t} for e, t in q.get("partial", [])],
                "forbidden": [{"ep": e, "start": t} for e, t in q.get("forbidden", [])],
                "expect_empty": q.get("expect_empty", False),
                "confidence": q.get("confidence", "high"),
                "note": q.get("note", ""),
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    by_cat = {}
    for q in QUESTIONS:
        by_cat[q["category"]] = by_cat.get(q["category"], 0) + 1
    print(f"写出 {len(QUESTIONS)} 条 -> {OUT.relative_to(ROOT)}")
    for c, n in sorted(by_cat.items()):
        print(f"  {c:>10}: {n}")
    low = sum(1 for q in QUESTIONS if q.get("confidence") == "low")
    print(f"  其中 confidence=low: {low} 条（不计入主指标，见 RETRIEVAL_EVAL.md）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
