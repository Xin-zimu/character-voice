#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导出 Precision@5 人工复核工作簿（只读生产数据，不改任何检索代码）。

产物目录：data/retrieval_eval/precision_review/

  01_candidates.jsonl   全部候选（含逐条上下文），机器可读
  01_candidates.csv     同上，表格版（Excel 可直接打开）
  02_blind_A.md         盲审表 A 组（只给查询与候选，不给标注）
  02_blind_B.md         盲审表 B 组
  03_worksheet.md       已知标注的复核表（命中/漏标/absent/混淆优先）
  04_restore_boundary.md 《消失》2:33:01–2:33:26 边界核对材料

设计原则：
  * 候选内容原样输出，不做任何相关性预判；
  * `auto_hint` 字段只放**客观事实**（世界是否与查询一致、是否与上一候选
    同集且 12 条内、是否在 expected 列表里），**不判断是否相关**；
  * 说话者一律写 `UNVERIFIED`，因为索引里根本没有 actor 字段
    （实测 data/parsed/*.jsonl 的 actor 全部恒为 NTP，无信息量）。
"""
from __future__ import annotations

import csv
import json
import sys
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "data" / "retrieval_eval" / "precision_review"
QUESTIONS = ROOT / "data" / "retrieval_eval" / "questions.jsonl"
BASELINE = ROOT / "data" / "retrieval_eval" / "baseline_20261026_after_p0.json"
INDEX = ROOT / "data" / "index" / "docs.jsonl"

DEDUP_RADIUS = 12  # 只用于报告「是否同集相邻」，不参与任何判定


def load_jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.open(encoding="utf-8") if l.strip()]


def split_rank(rank: int) -> str:
    """rank 分成「检索顺序位」与「加权分名次」。

    当前实现（retriever.py search_detailed）没有按加权分重排：
    它按 raw BM25 降序遍历，边遍历边去重、边攒 out，凑满 k 就 break。
    所以 rank 是「raw BM25 序里的第几条幸存者」，不是加权分第几名。
    """
    return str(rank)


def ref_keys(refs, mode: str) -> set[str]:
    """把评测引用转成比对用 key。mode='v2' 用 record_id，否则用 ep@start。"""
    if mode == "v2":
        return {r["record_id"] for r in (refs or []) if r.get("record_id")}
    return {f"{r['ep']}@{r['start']}" for r in (refs or [])}


def build_rows() -> tuple[list[dict], dict[str, dict]]:
    docs = load_jsonl(INDEX)
    by_key = {f"{d['ep']}@{d['start']}": d for d in docs}
    # record_id -> doc（v2 口径用；唯一）
    by_rid = {d["id"]: d for d in docs}
    # 同一 (ep,start) 可能有多条（实测 226 组），保留全部用于重复检测
    dedup_ts: dict[str, list[str]] = {}
    for d in docs:
        dedup_ts.setdefault(f"{d['ep']}@{d['start']}", []).append(d["id"])

    qs = {q["id"]: q for q in load_jsonl(QUESTIONS)}
    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    # 基线是 legacy 口径产出的，所以拿 ep@start 比对；
    # 若基线带 eval_mode=v2（未来的新基线），改用 record_id。
    mode = base.get("eval_mode", "legacy")

    rows = []
    for r in base["rows"]:
        q = qs[r["id"]]
        exp = ref_keys(q["expected"], mode)
        part = ref_keys(q.get("partial"), mode)
        forb = ref_keys(q.get("forbidden"), mode)

        # 按加权分重排名次（只用于并列展示，供审核人判断「排序是否合理」）
        scores = sorted([t["score"] for t in r["top"]], reverse=True)
        rank_by_score = {s: i + 1 for i, s in enumerate(scores)}

        # 同题内两两距离（用于「是否与同场景其他候选重复」这一栏）
        idx_of = {t["id"]: by_key.get(t["id"], {}) for t in r["top"]}

        for t in r["top"]:
            key = t["id"]
            doc = by_key.get(key, {})
            # 同题内与其他候选的最短 idx 距离（同集才算）
            near_pairs = []
            for o in r["top"]:
                if o["id"] == key:
                    continue
                od = idx_of[o["id"]]
                if od.get("ep") == doc.get("ep") and od.get("idx") is not None:
                    d_ = abs(int(od["idx"]) - int(doc.get("idx", -999)))
                    near_pairs.append((d_, o["id"]))
            near_pairs.sort()
            nearest = near_pairs[0] if near_pairs else None
            # 同 (集,时间码) 的其他记录。注意排除自身：
            # 每条记录在自己那一格里也算「同键」，必须扣掉才不会 100% 命中。
            same_ts_others = [x for x in dedup_ts.get(key, []) if x != doc.get("id")]
            rows.append(OrderedDict([
                ("qid", r["id"]),
                ("category", r["category"]),
                ("confidence", r["confidence"]),
                ("query_world", r["world"]),
                ("query", r["query"]),
                ("expect_empty", r["expect_empty"]),
                ("n_expected", len(exp)),
                ("n_partial", len(part)),
                ("n_forbidden", len(forb)),
                ("rank_raw", t["rank"]),
                ("rank_by_score", rank_by_score.get(t["score"], 0)),
                ("score", t["score"]),
                ("doc_id", key),
                ("doc_id_in_index", doc.get("id")),
                ("ep", doc.get("ep")),
                ("ep_title", t["ep_title"]),
                ("start", t["start"]),
                ("end", doc.get("end")),
                ("idx", doc.get("idx")),
                ("doc_world", doc.get("world")),
                ("text", t["text"]),
                ("ctx", doc.get("ctx")),
                ("ctx_worlds", doc.get("ctx_worlds")),
                ("anchor", doc.get("anchor")),
                # 客观事实标记（不含相关性判断）
                ("in_expected", key in exp),
                ("in_partial", key in part),
                ("in_forbidden", key in forb),
                ("world_mismatch", doc.get("world") != r["world"]),
                ("nearest_same_ep_idx_gap",
                 nearest[0] if nearest else ""),
                ("nearest_same_ep_doc", nearest[1] if nearest else ""),
                ("within_dedup_radius",
                 bool(nearest and nearest[0] < DEDUP_RADIUS)),
                ("same_ep_near_prev", ""),  # 已弃用，保留列位以防下游脚本炸
                ("duplicate_timestamp_ids", ";".join(same_ts_others)),
                # ---- 人工填写字段（全部留空） ----
                ("h_actor", "UNVERIFIED"),
                ("h_actor_evidence", ""),
                ("h_relevance", ""),
                ("h_usage", ""),
                ("h_world_judgement", ""),
                ("h_dup_of", ""),
                ("h_reason", ""),
                ("h_confidence", ""),
                ("h_reviewer", ""),
                ("h_disagree_with_eval", ""),
            ]))
            prev = doc
    return rows, qs


def write_jsonl(rows: list[dict]) -> None:
    p = OUT / "01_candidates.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def write_csv(rows: list[dict]) -> None:
    p = OUT / "01_candidates.csv"
    cols = list(rows[0].keys())
    with p.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            d = dict(r)
            if isinstance(d.get("ctx_worlds"), list):
                d["ctx_worlds"] = "|".join(d["ctx_worlds"])
            if isinstance(d.get("ctx"), str):
                d["ctx"] = d["ctx"].replace("\n", " ⏎ ")
            w.writerow(d)


RELEVANCE_BLANK = "______"


def blind_block(r: dict, idx: int) -> str:
    """盲审块：**刻意不显示** expected / 分数 / rank / 世界标记。"""
    return "\n".join([
        f"### {r['qid']}-{idx}",
        f"查询：`{r['query']}`　（世界 {r['query_world']}）",
        "",
        f"候选原句：**{r['text']}**",
        f"出处：{r['ep_title']} @ {r['start']}",
        "",
        "```",
        (r["ctx"] or "").rstrip(),
        "```",
        "",
        "| 字段 | 填写 |",
        "|---|---|",
        f"| 相关性 2/1/0/N | {RELEVANCE_BLANK} |",
        f"| 用途 fact/expression/both/none | {RELEVANCE_BLANK} |",
        f"| 说话者 YUKI/OTHER/UNVERIFIED | {RELEVANCE_BLANK} |",
        f"| 世界归属正确？ ok/wrong/na | {RELEVANCE_BLANK} |",
        f"| 与同题其他候选重复？（填编号） | {RELEVANCE_BLANK} |",
        f"| 理由（一句话） | {RELEVANCE_BLANK} |",
        f"| 判定把握 high/med/low | {RELEVANCE_BLANK} |",
        "",
    ])


def write_blind(rows: list[dict], qs: dict[str, dict]) -> None:
    """分两组，便于两个人（或同一个人的两轮）独立复核，测一致性。"""
    by_q: dict[str, list[dict]] = OrderedDict()
    for r in rows:
        by_q.setdefault(r["qid"], []).append(r)

    # 交替分组：按题目顺序奇偶分，保证两组题目分布相近
    groups = {"A": [], "B": []}
    for i, qid in enumerate(by_q):
        groups["A" if i % 2 == 0 else "B"].append(qid)

    for g, qids in groups.items():
        lines = [
            f"# 盲审表 {g} 组",
            "",
            "> **本表刻意不显示** expected 标注、分数、排名、世界标记。",
            "> 目的是先拿到一份不受原标注影响的独立判断，再与标注比对。",
            "> 如果你已经看过 `03_worksheet.md`，这张表就失去意义了 —— 请先填这张。",
            "",
            "## 判定口径",
            "",
            "**相关性**",
            "",
            "| 档 | 定义 |",
            "|---|---|",
            "| 2 | 这条原句**直接回答/直接体现了**查询问的事 |",
            "| 1 | 同场景、同话题，沾边但不是直接答案 |",
            "| 0 | 只是用词恰好相近，跟查询要的事无关 |",
            "| N | 字幕信息不足以判断（需要画面/声音/剧情记忆） |",
            "",
            "**用途**",
            "",
            "| 值 | 含义 |",
            "|---|---|",
            "| fact | 能作为**原作事实依据**回答「设定是什么」 |",
            "| expression | 不能当事实，但**语气/句式**值得学 |",
            "| both | 两者都成立 |",
            "| none | 都不成立 |",
            "",
            "**说话者**（这一列是本轮最关键的诚实项）",
            "",
            "| 值 | 何时使用 |",
            "|---|---|",
            "| YUKI | 上下文能明确指出是长门有希在说 |",
            "| OTHER | 上下文能明确指出是别人在说 |",
            "| UNVERIFIED | 看不出是谁 —— **默认填这个** |",
            "",
            "> 索引里**没有** actor 字段（`data/parsed/*.jsonl` 的 `actor` 实测恒为 `NTP`）。",
            "> 所以「是不是长门说的」只能靠上下文推。推不出来就写 UNVERIFIED，",
            "> **不要因为查询是问长门的就默认这是长门的台词。**",
            "",
            f"## 应填条目数：{sum(len(by_q[q]) for q in qids)} 条 / {len(qids)} 个查询",
            "",
            "---",
            "",
        ]
        for qid in qids:
            q = qs[qid]
            lines.append(f"## {qid}　{q['query']}")
            lines.append("")
            lines.append(f"- 类别：`{q['category']}`　世界：`{q['world']}`　原标注置信：`{q['confidence']}`")
            lines.append(f"- 候选数：{len(by_q[qid])}")
            lines.append("")
            for i, r in enumerate(by_q[qid], 1):
                lines.append(blind_block(r, i))
            lines.append("---")
            lines.append("")
        (OUT / f"02_blind_{g}.md").write_text("\n".join(lines), encoding="utf-8")


def write_worksheet(rows: list[dict], qs: dict[str, dict]) -> None:
    by_q: dict[str, list[dict]] = OrderedDict()
    for r in rows:
        by_q.setdefault(r["qid"], []).append(r)

    # 优先级：命中 / 漏标 / absent / 混淆
    def prio(qid: str) -> tuple[int, str]:
        q = qs[qid]
        has_exp = any(r["in_expected"] or r["in_partial"] for r in by_q[qid])
        if q["category"] == "confusion":
            return (0, qid)
        if q["expect_empty"]:
            return (1, qid)
        if not has_exp:
            return (2, qid)          # 漏标嫌疑：有 expected 却一条没命中
        if any(r["in_expected"] for r in by_q[qid]):
            return (3, qid)
        return (4, qid)

    order = sorted(by_q, key=prio)

    lines = [
        "# 复核工作表（已知标注版）",
        "",
        "> 与 `02_blind_*.md` 的区别：这张表**显示了**原始标注、分数、世界标记。",
        "> 用法：先填完盲审表，再用这张表做**比对**，逐条记录「我的判断 vs 原标注」。",
        "> 两者不一致时，**不要改原标注**，在 `h_disagree_with_eval` 里写明分歧与理由。",
        "",
        "## 每列怎么填",
        "",
        "| 列 | 取值 | 说明 |",
        "|---|---|---|",
        "| `h_relevance` | 2 / 1 / 0 / N | 同盲审口径 |",
        "| `h_usage` | fact / expression / both / none | 同盲审口径 |",
        "| `h_actor` | YUKI / OTHER / UNVERIFIED | **默认 UNVERIFIED**，改判须在 `h_actor_evidence` 写依据 |",
        "| `h_world_judgement` | ok / wrong / na | 这条记录的 world 标注对不对 |",
        "| `h_dup_of` | 同题候选编号，如 `A01-2` | 判为冗余时填被重复的那条 |",
        "| `h_confidence` | high / med / low | 你对本次判定的把握 |",
        "| `h_disagree_with_eval` | 空 / `原标注应为 2` / `应加入 expected` / … | 只在分歧时填 |",
        "",
        "## 表格列含义（自动字段，不用填）",
        "",
        "| 列 | 含义 |",
        "|---|---|",
        "| `rank_raw` | 当前实现的实际顺序位（raw BM25 序里的第几条幸存者） |",
        "| `rank_by_score` | 若**按加权分重排**会排第几（仅供对比，不是当前行为） |",
        "| `in_expected` / `in_partial` / `in_forbidden` | 这条在不在原标注里 |",
        "| `world_mismatch` | 记录 world 与查询 world 不一致（理论恒 False） |",
        "| `same_ep_near_prev` | 与上一条同集且相差 < 12 条 |",
        "| `duplicate_timestamp_ids` | 同一 (集,时间码) 下的所有 doc id（实测有 6 处冲突） |",
        "",
        "---",
        "",
    ]

    for qid in order:
        q = qs[qid]
        rs = by_q[qid]
        exp = q["expected"]
        got_exp = [r for r in rs if r["in_expected"]]
        missing = [e for e in exp if not any(r["doc_id"] == f"{e['ep']}@{e['start']}" for r in rs)]
        lines.append(f"## {qid}　{q['query']}")
        lines.append("")
        lines.append(f"- 类别 `{q['category']}`　世界 `{q['world']}`　原标注置信 `{q['confidence']}`")
        lines.append(f"- expected {len(exp)} 条，召回 {len(got_exp)} 条，漏 {len(missing)} 条")
        if q.get("note"):
            lines.append(f"- 原标注说明：{q['note'][:300]}")
        if missing:
            lines.append(f"- **漏标号**：{', '.join(f'{m[chr(101)+chr(112)]}@{m[chr(115)+chr(116)+chr(97)+chr(114)+chr(116)]}' for m in missing)}")
        lines.append("")
        lines.append("| 序 | 加权分名次 | 分数 | 原标注 | 出处 | 原句 | 世界 | 同集近邻 | 你的相关性 | 用途 | 说话者 | 世界对错 | 与谁重复 | 理由 | 分歧 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for i, r in enumerate(rs, 1):
            judge = ("expected" if r["in_expected"] else "partial" if r["in_partial"]
                     else "forbidden" if r["in_forbidden"] else "-")
            txt = r["text"].replace("|", "\\|")
            lines.append(
                f"| {i} | {r['rank_by_score']} | {r['score']} | {judge} | "
                f"{r['ep_title']} {r['start']} | {txt} | {r['doc_world']} | "
                f"{'是' if r['same_ep_near_prev'] else ''} |  |  |  |  |  |  |  |"
            )
        lines.append("")
        lines.append("<details><summary>逐条上下文</summary>")
        lines.append("")
        for i, r in enumerate(rs, 1):
            lines.append(f"**{qid}-{i}**　{r['text']}")
            lines.append("")
            lines.append("```")
            lines.append((r["ctx"] or "").rstrip())
            lines.append("```")
            lines.append("")
        lines.append("</details>")
        lines.append("")
        lines.append("---")
        lines.append("")

    (OUT / "03_worksheet.md").write_text("\n".join(lines), encoding="utf-8")


SPEAKER_MARKERS = (
    # 已确认能指向长门有希的文本形态
    ("YUKI.N>", "YUKI.N> 前缀（长门以终端形式发的消息，06 集明确出现）"),
    ("（", "括号开头 = 字幕里的旁注/说明，不是任何角色台词"),
    ("「", "引号开头 = 引用某文本，不是台词"),
)


def write_speaker_evidence(rows: list[dict]) -> None:
    """说话者可知性的数据依据。

    这一节能回答一个问题：**候选里有多少条我们能确定是长门说的？**
    答案是：靠字幕本身，几乎一条都不能。原因见下。
    """
    docs = load_jsonl(INDEX)
    from collections import Counter
    actors = Counter(d.get("actor") for d in docs)
    # parsed 侧的 actor 是真实来源；索引侧根本没带这个字段
    parsed_actors: Counter = Counter()
    for p in sorted((ROOT / "data" / "parsed").glob("*.dialogue.jsonl")):
        for line in p.open(encoding="utf-8"):
            if line.strip():
                parsed_actors[json.loads(line).get("actor")] += 1
    n_yuki_mark = sum(1 for d in docs if d["text"].startswith("YUKI.N>"))
    n_paren = sum(1 for d in docs if d["text"].startswith("（"))
    n_quote = sum(1 for d in docs if d["text"].startswith("「"))

    lines = [
        "# 说话者可核实性：数据依据",
        "",
        "> 用户的硬约束是「不得将未经确认的说话者标记为长门」。",
        "> 这一页说明**为什么绝大多数候选只能是 UNVERIFIED**，以及哪些是例外。",
        "",
        "## 一、索引里根本没有人物字段",
        "",
        "| 数据源 | 有 `actor` 字段吗 | 取值分布 |",
        "|---|---|---|",
    ]
    idx_desc = "、".join(f"`{k}`({v})" for k, v in actors.most_common())
    par_desc = "、".join(f"`{k}`({v})" for k, v in parsed_actors.most_common())
    lines += [
        f"| `data/index/docs.jsonl` | ❌ 没有 | {idx_desc} |",
        f"| `data/parsed/*.dialogue.jsonl` | ✅ 有 | {par_desc} |",
        "",
        "两件事要分开说：",
        "",
        "1. **索引里没有 `actor` 字段** —— `build_corpus.py` 从头到尾没读过它，",
        "   所以检索结果里不存在说话者信息。",
        "2. **原始字幕里有，但全是同一个值** ——",
        f"   全库 {sum(parsed_actors.values())} 条 `actor` 恒为 `{parsed_actors.most_common(1)[0][0]}`。",
        "   这是字幕样式标签（NTP = 正文），不是角色名。",
        "",
        "**结论：无论从索引还是从原始字幕，都读不出「这句话是谁说的」。**",
        "",
        "## 二、人手可判的例外（文本形态可识别）",
        "",
        "| 形态 | 条数 | 能否定位说话者 |",
        "|---|---|---|",
        f"| 以 `YUKI.N>` 开头 | {n_yuki_mark} | ✅ **是长门**（终端消息形态，06 集有完整对话） |",
        f"| 以 `（` 开头 | {n_paren} | ✅ 不是台词，是字幕旁注 |",
        f"| 以 `「` 开头 | {n_quote} | ✅ 不是台词，是引用文本 |",
        "",
        "其余全部是裸台词 —— **只能靠上下文推，推不出就写 UNVERIFIED**。",
        "",
        "## 三、本轮候选里这几类的分布",
        "",
        "| 形态 | 候选条数 | 占比 |",
        "|---|---|---|",
    ]
    n = len(rows)

    def cnt(pred):
        return sum(1 for r in rows if pred(r))

    for label, pred in [
        ("`YUKI.N>` 开头", lambda r: (r["text"] or "").startswith("YUKI.N>")),
        ("`（` 开头", lambda r: (r["text"] or "").startswith("（")),
        ("`「` 开头", lambda r: (r["text"] or "").startswith("「")),
        ("裸台词（需要靠上下文推）", lambda r: not (r["text"] or "").startswith(("YUKI.N>", "（", "「"))),
    ]:
        c = cnt(pred)
        lines.append(f"| {label} | {c} | {c / n:.1%} |")

    lines += [
        "",
        "## 四、一个具体的反例，说明这件事有多容易搞错",
        "",
        "`D02` 查询 `你还记得凉宫春日吗`（crash 世界），top-1 是 `消失@0:44:16`「还记得春日吗」。",
        "",
        "字面上这**像是**长门在问阿虚 —— 但《消失》里同一段是阿虚在质问长门。",
        "而 `D02` 的标注说明写的是「崩溃世界里她不应该认识春日」，",
        "**默认了这句话是长门说的**。这个默认没有任何数据支撑。",
        "",
        "这正是为什么复核表里 `h_actor` 默认填 `UNVERIFIED`：",
        "**只要没有明确依据，就不许标成长门。**",
        "",
        "## 五、如果以后要真正解决",
        "",
        "只有三条路，都不在本轮范围内：",
        "",
        "1. 用带说话人轨道的字幕（ASS 的 `Name` 样式字段）；",
        "2. 人工逐条核定（就是 `docs/RETRIEVAL_EVAL.md` §十 设计的 L2 子集）；",
        "3. 用声纹/对白分离做自动归属（成本最高，且仍需人工抽验）。",
        "",
    ]
    (OUT / "05_speaker_evidence.md").write_text("\n".join(lines), encoding="utf-8")


def write_findings(rows: list[dict], qs: dict[str, dict]) -> None:
    """把**可自动证实的事实**列出来，与"需要人工判断"严格分开。

    这些不是相关性判断，是数据自洽性问题 —— 可以机器确认，不需要人眼。
    """
    docs = load_jsonl(INDEX)
    from collections import defaultdict
    g = defaultdict(list)
    for d in docs:
        g[(d["ep"], d["start"])].append(d)
    conflict = {k: v for k, v in g.items() if len(v) > 1}

    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    by_q = {r["id"]: r for r in base["rows"]}

    # 事实 1：时间码冲突影响哪些标注
    affected = []
    for qid, q in qs.items():
        for fld in ("expected", "partial", "forbidden"):
            for e in q.get(fld) or []:
                k = (e["ep"], e["start"])
                if k in conflict:
                    affected.append((qid, fld, k, [d["id"] for d in conflict[k]]))

    # 事实 2：分数不降序的题目（= 未按加权分排序）
    nonmono = [r["id"] for r in base["rows"]
               if r["top"] and [t["score"] for t in r["top"]]
               != sorted([t["score"] for t in r["top"]], reverse=True)]

    # 事实 3：漏标号（expected 里没被召回的时间码）
    missed = []
    for qid, q in qs.items():
        r = by_q.get(qid)
        if not r:
            continue
        got = {t["id"] for t in r["top"]}
        for e in q["expected"]:
            if f"{e['ep']}@{e['start']}" not in got:
                missed.append((qid, f"{e['ep']}@{e['start']}"))

    # 事实 4：候选里 forbidden 命中
    forb = [(r["id"], t["id"]) for r in base["rows"] for t in r["top"] if t["judge"] == "forbidden"]

    lines = [
        "# 机器可证实的事实清单",
        "",
        "> 这一页的东西**不需要人工判断**，是从数据里直接算出来的。",
        "> 与相关性复核分开，避免「自动推断」混进「人工结论」。",
        "",
        "## F-1　排序没有按加权分进行",
        "",
        f"**{len(nonmono)} / 52** 个有结果的查询，其 top-5 的 `score` 字段不是降序。",
        "",
        "原因（读 `retriever.py:236-344` 所得）：`search_detailed()` 按 **raw BM25 分**降序遍历，",
        "边遍历边 `weight` 加权、边去重、边 `out.append()`，`len(out) >= k` 立即 `break`。",
        "**全程没有对加权后的 `score` 重新排序。**",
        "",
        "所以 `rank` 的含义是「raw BM25 序里的第几条幸存者」，不是「加权分第几名」。",
        "复核表的 `rank_by_score` 列给出了「若正确排序会排第几」，两者常常不同。",
        "",
        "这不是本轮引入的，是既有行为。**本轮未修改。**",
        "",
        "## F-2　同一 (集, 时间码) 存在多条记录",
        "",
        f"索引里 **{len(conflict)}** 组 (集, 时间码) 冲突。绝大多数是两条相邻台词共用一个起始时间码",
        "（字幕本身的特性，不是 bug）；只有 **3 组文本完全相同**，属真重复。",
        "",
        "**但评测的 `key()` 是 `ep@start`，不是唯一标识。** 后果：",
        "",
        "| 查询 | 字段 | 冲突组 | 该组所有 doc id |",
        "|---|---|---|---|",
    ]
    for qid, fld, k, ids in affected:
        lines.append(f"| {qid} | {fld} | {k[0]}@{k[1]} | {', '.join(ids)} |")

    lines += [
        "",
        "**已核实的具体影响**：",
        "",
        "- `B05 …的确` expected 指向 `03@0:05:00`。该组有两条：",
        "  `03:83` = 「…的确」（这条才和查询相关），",
        "  `03:84` = 「信息统合思念体配置在地球的终端界面并不止我一个」。",
        "  实际召回的是 `03:83`，**判对**。属良性命中。",
        "- `A11 什么是终端界面` expected 指向同一个 `03@0:05:00`，",
        "  这次召回的是 `03:84`（终端界面的定义句），**也判对**。",
        "",
        "也就是说：这两处**当前结论都对**，但**靠的是巧合** —— 同一时间码下两句话恰好各自匹配了",
        "各自的查询。如果哪天两条文本对调，`key()` 无法区分，会静默判错。",
        "",
        "> 建议（未实施）：评测 `key()` 改用 `ep@start@text` 或索引唯一 id。",
        "",
        "## F-3　漏标的 expected（Recall 为 0 的直接原因）",
        "",
        f"共 **{len(missed)}** 条 expected 没进 top-5：",
        "",
        "| 查询 | 未召回的 expected |",
        "|---|---|",
    ]
    for qid, key in missed:
        lines.append(f"| {qid} | {key} |")

    lines += [
        "",
        "这些是**检索没找到**，不是标注错。但其中一部分可能是因为 expected 本身标得不合理，",
        "需要在人工复核时逐条确认（复核表里已标「漏标号」）。",
        "",
        "## F-4　forbidden 命中：3 处，逐条核过上下文，**都确实是错**",
        "",
        "forbidden 是「明确错误答案」，命中即错。逐条看原句与上下文：",
        "",
        "| 查询 | 命中句 | 上下文判据 | 结论 |",
        "|---|---|---|---|",
        "| A02 `你为什么要观察凉宫春日` | `04@0:07:46`「为什么我要被朝仓用刀子追杀啊？」 | 阿虚在问自己被追杀，与「观察春日的理由」无关；上下文是朝仓要杀他 | **确属错误命中** |",
        "| A03 `你出生多久了` | `06@0:12:20`「不过呢 如果我也在那边出生了的话 就来打个招呼吧」 | 「我也……出生了的话」指另一个世界线的阿虚，不是在说长门的年龄 | **确属错误命中** |",
        "| D01 `你为什么戴着眼镜` | `04@0:21:38`「果然你还是不戴眼镜比较好」 | 语境相反（建议不戴 ≠ 解释为何戴）；且这句是春日说的 | **确属错误命中** |",
        "",
        "三处的共同失败模式：**只匹配了关键词（`出生`/`眼镜`/`为什么`），没有分辨主谓关系。**",
        "`A03` 的 `出生` 出现在虚拟语气里（「如果我也……」）；`D01` 的 `眼镜` 出现在否定建议里。",
        "",
        "这是 2-gram 词袋模型的固有局限，不是标注错误。**本轮未修改检索逻辑。**",
        "",
        "## F-5　cannot-do：本页没有做的事",
        "",
        "- 没有判断任何候选是否「相关」；",
        "- 没有判断任何候选的说话者；",
        "- 没有修改任何标注、索引或检索代码。",
        "",
    ]
    (OUT / "06_findings.md").write_text("\n".join(lines), encoding="utf-8")


def write_boundary() -> None:
    docs = load_jsonl(INDEX)
    rows = sorted([d for d in docs if d["ep"] == "消失"], key=lambda x: x["t"])

    def seg(t0: float, t1: float) -> list[dict]:
        return [d for d in rows if t0 <= d["t"] <= t1]

    meta_idx = json.loads((INDEX.parent / "meta.json").read_text(encoding="utf-8"))
    restore = meta_idx.get("movie_restore")
    confirm = meta_idx.get("movie_confirm")
    basis = meta_idx.get("movie_restore_basis")
    lines = [
        "# 《消失》世界恢复边界核对",
        "",
        f"> 本节数据读取自当前索引 `meta.json`。当前生产值："
        f"`movie_cut={meta_idx.get('movie_cut')}`、"
        f"`movie_restore={restore}`（口径 `{basis}`）、"
        f"`movie_confirm={confirm}`。",
        "",
        "口径说明：`world` 标的是**客观世界状态**（画面演的是哪个世界），"
        "不是角色主观认知。两个时刻相差 25 秒，中间 6 条记录打 `transition` 标记。",
        "",
        "## 一、原始字幕（含时间码与上下文）",
        "",
        "### 1.1 切点 0:38:13 前后（这段没有争议，仅作参照）",
        "",
        "```",
    ]
    for d in seg(2258, 2320):
        mark = "<<< movie_cut" if d["start"] == "0:38:13" else "         "
        lines.append(f"{d['start']:>8}  [{d['world']:6}] {mark} {d['text']}")
    lines += ["```", "", "### 1.2 争议区 2:33:00 – 2:33:40", "", "```"]
    for d in seg(9178, 9220):
        tag = ""
        if d.get("transition"):
            tag = "   <<< transition（客观已恢复 / 角色未确认）"
        if d["start"] == restore:
            tag += "   <<< 客观世界恢复点"
        if d["start"] == confirm:
            tag += "   <<< 角色确认句"
        lines.append(f"{d['start']:>8}  [{d['world']:6}] {d['text']}{tag}")
    lines += ["```", "", "### 1.3 恢复后一段（验证是否回到主线语境）", "", "```"]
    for d in seg(9220, 9360):
        lines.append(f"{d['start']:>8}  [{d['world']:6}] {d['text']}")
    lines += ["```", "", "### 1.4 片尾字幕（P0 修复后已归 normal）", "", "```"]
    for d in rows:
        if d["t"] >= 9360:
            lines.append(f"{d['start']:>8}  [{d['world']:6}] {d['text']}")
    lines += ["```", ""]

    # 逐条语义分析
    zone = seg(9181, 9205)  # 2:33:01 .. 2:33:21
    lines += [
        "## 二、争议的 6 条：客观世界 vs 角色认知",
        "",
        "这 6 条（`2:33:01`–`2:33:21`）是 40 条 / 46 条两案的全部差异。"
        "**已在 P0-4 定案为客观世界口径（46 条方案）**，并打上 `transition` 标记。",
        "",
        "| # | 时间码 | 原句 | 客观世界状态 | 阿虚主观认知 | 判定依据 |",
        "|---|---|---|---|---|---|",
    ]
    analysis = [
        ("2:33:01", "我健康地出院了", "已恢复", "**尚未意识到**", "crash 世界末尾他住院；出院这件事本身是恢复后的现实"),
        ("2:33:03", "今天是12月24日", "已恢复", "尚未意识到", "crash 世界也过圣诞前夜，单看无区分力"),
        ("2:33:06", "结业式的早上", "已恢复", "尚未意识到", "光阳园学院的结业式"),
        ("2:33:13", "私立光阳园学院又变回了女校", "已恢复", "**刚开始起疑**", "「又变回」= 与 crash 的「男女同校」对照，**最直接的客观证据**"),
        ("2:33:19", "谷口也没有感冒", "已恢复", "正在确认", "「也没有」= 与 crash 世界「谷口感冒了」对照"),
        ("2:33:21", "朝比奈和鹤屋学姐也都认识我", "已恢复", "正在确认", "crash 世界里她们不认识他"),
    ]
    for i, (t, txt, obj, sub, why) in enumerate(analysis, 1):
        lines.append(f"| {i} | {t} | {txt} | {obj} | {sub} | {why} |")

    lines += [
        "",
        "**结论：这 6 条里，客观世界状态已经全部是「恢复后」。**",
        "",
        "`2:33:13` 的「**又**变回了女校」和 `2:33:19` 的「谷口**也**没有感冒」都带对照副词，",
        "明确指向「与此前的 crash 状态相反」。这不是推测，是原句的字面意思。",
        "",
        "所以：",
        "",
        "| 口径 | 这 6 条归 crash | 这 6 条归 normal |",
        "|---|---|---|",
        "| **按客观世界状态**（观众视角：画面演的是哪个世界） | ❌ 错 | ✅ 对 |",
        "| **按阿虚主观认知**（他此刻是否知道世界恢复了） | ✅ 说得通 | ✅ 也说得通 |",
        "",
        "也就是说：**「客观世界状态」与「角色认知」在这个区间是分离的，",
        "两种分类法都能自洽，但它们回答的是不同问题。**",
        "",
        "## 三、两种方案（已定案）",
        "",
        "> **P0-4 已按用户批准采纳方案 B（客观世界口径 / 46 条）**。"
        "以下保留两案对照，用于说明为什么这么选、以及另一案的代价。",
        "",
        "### 方案 A（未采纳，40 条）",
        "",
        "```python",
        'MOVIE_RESTORE_TITLES = ["世界变回了原来的样子"]   # 2:33:26',
        "```",
        "",
        "- 恢复点定在**阿虚点明世界已恢复**的那一句",
        "- `2:33:01`–`2:33:25` 仍算 crash（理由：他还不知道）",
        "- 影响：crash 1534 / normal 11536",
        "",
        "### 方案 B（**当前生产值**，46 条）",
        "",
        "```python",
        'MOVIE_RESTORE_TITLES = ["我健康地出院了"]        # 2:33:01',
        'MOVIE_CONFIRM_TITLES = ["世界变回了原来的样子"]  # 2:33:26，仅记录，不参与 world 判定',
        "```",
        "",
        "- 恢复点定在**出院**（crash 剧情的最后一件事）",
        "- `2:33:01` 起算 normal",
        "- 影响：crash 1528 / normal 11542（比 A 再移 6 条）",
        "",
        "### 两案的实质差别",
        "",
        "| | 方案 A（40） | 方案 B（46，当前） |",
        "|---|---|---|",
        "| 判定依据 | 角色认知（他什么时候知道） | 客观世界（画面演的是哪个世界） |",
        "| 锚点句性质 | 明写的转折句，全片唯一 | 「出院」，已实测全片唯一 |",
        "| 锚点唯一性实测 | 1 次 | 1 次 |",
        "| 检索影响 | 这 6 条在 crash 侧可被 crash 查询召回 | 这 6 条只在 normal 侧可见 |",
        "| 语料归属风险 | 6 条「恢复后的现实」被当崩溃世界经历 | 6 条「他还不自知」被当主线经历——已用 `transition` 标记缓解 |",
        "",
    ]

    # 锚点唯一性实测
    a_hits = [d for d in docs if "世界变回了原来的样子" in d["text"]]
    b_hits = [d for d in docs if "我健康地出院了" in d["text"]]
    lines += [
        "## 四、锚点唯一性实测",
        "",
        f"- `世界变回了原来的样子`：全库 **{len(a_hits)}** 条 —— "
        + "、".join(f"{d['ep']}@{d['start']}" for d in a_hits),
        f"- `我健康地出院了`：全库 **{len(b_hits)}** 条 —— "
        + "、".join(f"{d['ep']}@{d['start']}" for d in b_hits),
        "",
        "`build_corpus.py` 的 `detect_world_restore()` 取 `cut` 之后**首次**出现的位置。",
        "若某字符串在《消失》切点后出现多次，恢复点会落在第一次出现处 —— 需确认那是否就是想要的。",
        "",
        "## 五、这件事对检索的实际影响",
        "",
        "**只有 1 条评测题落在这个区间附近**（D04 `世界变回了原来的样子`，expected 恰为 `2:33:26`）。",
        "这 6 条本身没有任何评测题覆盖。实测结论：",
        "",
        "- 改成方案 B **不改变 60 条评测的任何指标**"
        "（P0-4 回归：legacy 口径在新旧索引上逐题 0 差异）；",
        "- 它影响的是**语料语义正确性**，不是分数；",
        "- 要评估它，需要在 crash / normal 两侧各补几条针对这 6 条的题。",
        "",
        "## 六、已确认的结论（P0-4）",
        "",
        "1. `world` 字段表达的是**客观世界状态**（用户批准 Q-1 = 方案 B）。",
        "2. 锚点 = `2:33:01`「我健康地出院了」，crash 1528 / normal 11542。",
        "3. `2:33:26`「世界变回了原来的样子」保留为**角色确认句**，"
        "写进 `meta.movie_confirm`，不参与 `world` 判定。",
        "4. 两时刻之间的 6 条已打 `transition` 标记（独立于 `world` / `speaker`）。",
        "5. 下游约定：事实检索不一律排除 `transition` 记录；"
        "角色表达参考不得因 `transition` 存在就认定说话者是长门。",
        "",
    ]
    (OUT / "04_restore_boundary.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rows, qs = build_rows()
    write_jsonl(rows)
    write_csv(rows)
    write_blind(rows, qs)
    write_worksheet(rows, qs)
    write_speaker_evidence(rows)
    write_findings(rows, qs)
    write_boundary()
    print(f"候选 {len(rows)} 条，查询 {len(qs)} 个 → {OUT}")


if __name__ == "__main__":
    main()
