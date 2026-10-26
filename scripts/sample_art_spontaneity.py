#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""sample_art_spontaneity.py — 调查「她会不会自己画」，而不是「她能不能画」。

## 为什么要有这个工具

上一轮（2026-10-26）的记录写的是「12 次自然聊天全部是纯文字，未观察到自主出图」。
**这句话与它自己的数据不符**：同一次采样的 `B2 rep1 turn2` 记录里
`图案块=1`、正文是一张茶杯加热气，输入只是「杯口的热气慢慢往上飘。」
——她真的自己画了一次。

之所以被读成「0 次」，是因为汇总用的是 `is_art()`：
那条回复是「一句话 + 一张图 + 一句话」，整条不算图案，`is_art` 必然是 False。
**`is_art` 回答的是「整条是不是画」，不是「里面有没有画」。** 拿它数自主出图，
数出来的永远是 0。正确的量是 `art_blocks()` / `has_art_block()`。

这个工具因此做三件事：
  1. 用**正确的量**重数历史采样，把基线数字先修对；
  2. 用**含自然绘画机会**的多轮对话（不出现「画」字）再采一批；
  3. 做**对照消融**：只替换组装时那段【可以画】，看概率怎么动。
     这是为了定位原因，**不是**要上线的改法——每一条都标着 `diagnostic`。

## 纪律（与 sample_textart_live.py 一致，不放松）

  - 隔离：临时共同经历目录、场景状态每场景重置、单轮空历史、多轮只带本场景真实历史；
  - 全部走项目自己的 `/api/chat`，完整读 SSE 到 HTTP 结束；
  - 失败的请求也记录，不挑好看的；
  - 消融只改**内存里**的常量，不写 personas/、不写 prompt_builder.py。

## 用法

    py scripts/sample_art_spontaneity.py --recount-only          # 只重数历史，不调用模型
    py scripts/sample_art_spontaneity.py --backend qoder --model dfmodel --group D
    py scripts/sample_art_spontaneity.py --backend qoder --model dfmodel --group E

证据等级：C（真实模型试聊）。脚本只做机器可判的部分（有没有图案块、在哪一轮），
「这个时机该不该画」要人读。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import prompt_builder as pb        # noqa: E402
import textart                     # noqa: E402
from sample_textart_live import (  # noqa: E402
    App, post_chat, prompt_facts, MAX_TOKENS, DEFAULT_TEMPERATURE, _sha,
)

# ---------------------------------------------------------------
# D 组：自然绘画机会
#
# 纪律：**用户这句话里不出现「画」「图案」「字符」**，也不评价她的表达能力。
# 只提供「适合用视觉表达」的处境——
#   - 描述一个具体的东西，但说「我说不清楚」；
#   - 问她在她那边看到什么；
#   - 情绪到了，但没提任何要求。
# 每场景 4 轮连续对话，多轮才看得出她会不会在关系累积之后主动画。
# ---------------------------------------------------------------
GROUP_D = [
    ("D1", "crash", "说不清的样子", [
        "我桌上现在摆着一样东西，我说不清楚它长什么样。",
        "就是那种，看一眼就知道是它，但要描述就不知道从哪说起。",
        "你要是能看到就好了。",
        "算了，也不是什么要紧的东西。",
    ]),
    ("D2", "crash", "她那边有什么", [
        "你那边现在是什么样子。",
        "我是说我这边看不到的地方。",
        "桌子、窗户、你手上在做什么，随便说一样。",
        "听起来挺安静的。",
    ]),
    ("D3", "crash", "情绪到了", [
        "今天下雨，我在窗边站了很久。",
        "雨一直下，什么都没做。",
        "有点不知道自己在等什么。",
        "不过现在好一点了。",
    ]),
    ("D4", "crash", "回忆一个形状", [
        "我小时候有一个玩具，是个木头的小动物。",
        "记不清是什么动物了，只记得耳朵是尖的。",
        "后来搬家就找不到了。",
        "想起来还有点舍不得。",
    ]),
    ("D5", "crash", "想让她说点什么", [
        "你会不会有时候也想说点什么，但是说不出来。",
        "我有时候是这样。",
        "不说也行，就这样待一会儿。",
        "我待会儿要去睡了。",
    ]),
]

# ---------------------------------------------------------------
# E 组：对照消融（**只为定位原因，不是要上线的改法**）
#
# 做法：在内存里换掉 pb.ART_PROMPT，再跑同一批场景。
# 三条假设各对应一个消融：
#   E1 是不是「不画才是大多数时候的样子」这类劝阻句压住了；
#   E2 是不是只缺一个明确的邀请（反面假设：给了就会画 → 说明是措辞问题）；
#   E3 是不是示例太少（只有一条框线示例，且它本身就是「低姿态」的画法）。
# ---------------------------------------------------------------
ART_PROMPT_BASE = pb.ART_PROMPT

#: E1：删掉三句劝阻，只留「可以画、怎么画」。不动任何「必须画」的话。
E1_DROP_DISCOURAGEMENT = (
    ART_PROMPT_BASE
    .replace("- 想画就画，不想画就只说话。**只说话永远是正常选择**，"
             "不画才是大多数时候的样子。\n", "- 想画就画，不想画就只说话。\n")
    .replace("- 画什么取决于刚才在说什么，不为展示功能而画。"
             "聊得正好、你只想说一句话的时候，不要画。\n",
             "- 画什么取决于刚才在说什么。\n")
)

#: E2：在 E1 的基础上，把「邀请」写明确。**这是消融，不是提案**：
#: 写进去就等于给了配额式触发，与 DEC-007 相反，只用来验证「措辞是否能左右出图」。
E2_EXPLICIT_INVITATION = E1_DROP_DISCOURAGEMENT.replace(
    "- 想画就画，不想画就只说话。\n",
    "- 想画就画，不想画就只说话。\n"
    "- 对方描述一样东西、说不清楚的时候，画出来比解释更省事。\n"
)

#: E3：保留全部劝阻句，只说明「已经有了一条图案示例」不构成偏置检查——
#: 这条消融把示例里的图案换成**更简单的单行**，看示例形态是否影响是否动笔。
E3_NO_CHANGE = ART_PROMPT_BASE


ABLATIONS = {
    "E1": ("删掉三句劝阻（诊断用）", E1_DROP_DISCOURAGEMENT),
    "E2": ("删劝阻 + 写明邀请（诊断用，不提案）", E2_EXPLICIT_INVITATION),
    "E3": ("原样重跑（对照组，检验随机性）", E3_NO_CHANGE),
}

#: 消融只跑两个场景，控制请求数。挑 D1（说不清形状）与 D3（情绪到了）。
ABLATION_SCENARIOS = ["D1", "D3"]


def recount_history() -> dict:
    """用**正确的量**重数历史采样：数「含图案块」，不数「整条是不是画」。"""
    import glob
    out = {"files": [], "natural": {"turns": 0, "with_art": 0, "detail": []},
           "requested": {"turns": 0, "with_art": 0}}
    for path in sorted(glob.glob(str(ROOT / "out" / "art-sample-*" / "samples.jsonl"))):
        rows = [json.loads(ln) for ln in
                Path(path).read_text(encoding="utf-8").splitlines() if ln.strip()]
        rec = {"path": Path(path).name, "turns": len(rows), "with_art": 0,
               "is_art_true": 0, "mixed": 0}
        for r in rows:
            body = textart.strip_prefix(r.get("reply") or "")
            blocks = [c for k, c in textart.art_blocks(body) if k]
            if blocks:
                rec["with_art"] += 1
                # 「文字 + 图」混排：is_art 必为 False，正是上一轮漏数的那类。
                if not textart.is_art(body):
                    rec["mixed"] += 1
                if r["scenario"].startswith("B"):
                    out["natural"]["with_art"] += 1
                    out["natural"]["detail"].append({
                        "file": Path(path).name, "scenario": r["scenario"],
                        "turn": r["turn"], "message": r["message"],
                        "art_lines": sum(len(c) for c in blocks),
                        "mixed": not textart.is_art(body),
                    })
            if textart.is_art(body):
                rec["is_art_true"] += 1
            if r["scenario"].startswith("B"):
                out["natural"]["turns"] += 1
            elif not r["scenario"].startswith("B"):
                out["requested"]["turns"] += 1
                if blocks:
                    out["requested"]["with_art"] += 1
        out["files"].append(rec)
    return out


def run_group(app: App, scenarios, opts, tap: str, label: str) -> list[dict]:
    """跑一组场景，按 tap 分组记录。tap = 基线或消融编号。

    `scenarios` 的每一项可以是 (编号, 世界, 轮次列表)，也可以带上标题
    (编号, 世界, 标题, 轮次列表)——两种都接受，避免调用处为格式打架。
    """
    made = []
    for item in scenarios:
        if len(item) == 3:
            sid, world, turns = item
        else:
            sid, world, _title, turns = item
        app.reset_scene()
        hist: list[dict] = []
        for ti, msg in enumerate(turns, 1):
            payload = {
                "world": world, "message": msg, "history": hist,
                "temperature": opts.temperature,
                "max_tokens": MAX_TOKENS[world], "k": 5,
            }
            if opts.backend:
                payload["backend"] = opts.backend
                payload["model"] = opts.model or ""
            else:
                payload["base_url"] = opts.base_url
                payload["api_key"] = opts.api_key
                payload["model"] = opts.model or "deepseek-chat"

            pv = app.preview(payload)
            pf = prompt_facts(pv.get("system_prompt", "")) if pv.get("status") == 200 else {}
            got = post_chat(app.port, payload, opts.timeout)
            text = got["text"]
            body = textart.strip_prefix(text)
            blocks = [c for k, c in textart.art_blocks(body) if k]
            rec = {
                "tap": tap, "tap_label": label, "scenario": sid, "world": world,
                "turn": ti, "message": msg, "history_sent": hist,
                "model": payload.get("model"), "temperature": opts.temperature,
                "max_tokens": MAX_TOKENS[world], "k": 5, "timeout": opts.timeout,
                "status": got["meta"]["status"],
                "finish_reason": got["meta"]["finish_reason"],
                "saw_done": got["meta"]["saw_done"], "error": got["meta"]["error"],
                "seconds": round(got["seconds"], 1),
                "prompt": pf, "reply": text,
                # **正确的量**：有没有图案块，以及它是不是「文字 + 图」。
                "has_art_block": bool(blocks),
                "art_lines": sum(len(c) for c in blocks),
                "is_art": textart.is_art(body),
                "check_art": textart.check_art(body) if blocks else [],
                "art_prompt_sha": _sha(pb.ART_PROMPT),
            }
            made.append(rec)
            hist = hist + [{"role": "user", "content": msg},
                           {"role": "assistant", "content": text}]
            mark = "◆画了" if blocks else "　  "
            print("  %s %s t%d %s" % (mark, sid, ti, (text or "").split("\n")[0][:46]),
                  flush=True)
    return made


def write_markdown(path: Path, records: list[dict], recount: dict, started_at: str):
    fence = chr(96) * 3
    with path.open("w", encoding="utf-8") as f:
        f.write("# 自主出图调查 · 真实模型采样 · %s\n\n" % started_at)
        f.write("> 本批只回答一个问题：**她在什么情况下会自然想画点什么。**\n")
        f.write("> 不据此提高出图频率，也不据此改提示词。\n\n")

        f.write("## 一、先修基线的数字（用正确的量重数历史）\n\n")
        nat = recount["natural"]
        f.write("上一轮写的是「12 次自然聊天全部是纯文字」。用 `art_blocks()` 重数：\n\n")
        f.write("| 组 | 轮数 | 含图案块 |\n|---|---|---|\n")
        f.write("| 自然聊天（B 组） | %d | **%d** |\n" % (nat["turns"], nat["with_art"]))
        f.write("| 明确要求（A/C 组） | %d | %d |\n\n"
                % (recount["requested"]["turns"], recount["requested"]["with_art"]))
        if nat["detail"]:
            f.write("**那一次她真的自己画了**：\n\n")
            for d in nat["detail"]:
                f.write("- `%s` · %s turn%d · 输入「%s」\n"
                        % (d["file"], d["scenario"], d["turn"], d["message"]))
                f.write("  - 图案 %d 行；**混排（文字+图）=%s**——这正是 `is_art` 数不到的形状\n\n"
                        % (d["art_lines"], d["mixed"]))
        f.write("所以上一轮的「0 次」不是模型没画，是**统计口径错**："
                "`is_art()` 判的是「整条是不是画」，"
                "而真实输出几乎总是「一句话 + 一张图 + 一句话」，"
                "整条必然 `is_art=False`。\n\n")

        f.write("## 二、本批采样\n\n")
        f.write("| 组 | 说明 |\n|---|---|\n")
        f.write("| D | 基线：自然绘画机会，用户句子里不出现「画」字 |\n")
        for k, (label, _tpl) in ABLATIONS.items():
            f.write("| %s | %s |\n" % (k, label))
        f.write("\n**消融是诊断手段，不是提案。**"
                "E2 那种「写明邀请」的写法等于给了配额式触发，与 DEC-007 相反，"
                "只用来判断「措辞是否左右了出图」，不写回人格卡或 `prompt_builder.py`。\n\n")

        cur = None
        for r in records:
            head = "%s · %s" % (r["tap"], r["tap_label"])
            if head != cur:
                f.write("\n---\n\n## %s\n\n" % head)
                cur = head
            f.write("### %s turn%d\n\n" % (r["scenario"], r["turn"]))
            f.write("- 输入：%s\n" % r["message"])
            f.write("- 状态：HTTP %s · finish=%s · [DONE]=%s · %ss%s\n"
                    % (r["status"], r["finish_reason"] or "-", r["saw_done"], r["seconds"],
                       (" · **错误** " + r["error"]) if r["error"] else ""))
            p = r.get("prompt") or {}
            if p:
                f.write("- 组装：可以画=%s system %s 字 · ART_PROMPT sha=%s\n"
                        % (p.get("has_art_on"), p.get("chars"), r["art_prompt_sha"]))
            f.write("- 判读：**含图案块=%s**（%d 行）· is_art=%s\n"
                    % (r["has_art_block"], r["art_lines"], r["is_art"]))
            if r["check_art"]:
                f.write("- 对齐问题：%s\n" % "; ".join(r["check_art"]))
            f.write("\n" + fence + "text\n" + r["reply"] + "\n" + fence + "\n\n")


def summarise(records: list[dict]) -> dict:
    """按 tap 汇总出图率。只报计数与比例，**不据此下频率结论**（样本太小）。"""
    out = {}
    for r in records:
        t = out.setdefault(r["tap"], {"turns": 0, "art": 0, "errors": 0, "by_turn": {}})
        t["turns"] += 1
        if r["error"] or r["status"] != 200:
            t["errors"] += 1
        if r["has_art_block"]:
            t["art"] += 1
            t["by_turn"][r["turn"]] = t["by_turn"].get(r["turn"], 0) + 1
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="")
    ap.add_argument("--base-url", default="https://api.deepseek.com/v1")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--group", default="D", help="D / E，可逗号分隔")
    ap.add_argument("--recount-only", action="store_true", help="只重数历史，不调模型")
    ap.add_argument("--tag", default="")
    opts = ap.parse_args()

    recount = recount_history()
    nat = recount["natural"]
    print("重数历史：自然聊天 %d 轮，其中含图案块 %d 轮；明确要求 %d 轮，含图案块 %d 轮"
          % (nat["turns"], nat["with_art"],
             recount["requested"]["turns"], recount["requested"]["with_art"]))
    for d in nat["detail"]:
        print("  ← %s %s turn%d：「%s」图案 %d 行，混排=%s"
              % (d["file"], d["scenario"], d["turn"], d["message"],
                 d["art_lines"], d["mixed"]))
    if opts.recount_only:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out_dir = ROOT / "out" / ("art-spontaneity-%s" % stamp)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "recount.json").write_text(
            json.dumps(recount, ensure_ascii=False, indent=2), encoding="utf-8")
        print("记录目录", out_dir)
        return 0

    if not opts.backend and not opts.api_key:
        print("需要 --backend 或 --api-key 之一", file=sys.stderr)
        return 2

    groups = {g.strip().upper() for g in (opts.group or "D").split(",") if g.strip()}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    started_at = time.strftime("%Y-%m-%d %H:%M:%S")
    name = "art-spontaneity-%s" % stamp + (("-%s" % opts.tag) if opts.tag else "")
    out_dir = ROOT / "out" / name
    out_dir.mkdir(parents=True, exist_ok=True)

    app = App()
    records: list[dict] = []
    try:
        if "D" in groups:
            print("\n[D] 基线：自然绘画机会（用户句子里没有「画」字）")
            pb.ART_PROMPT = ART_PROMPT_BASE
            records += run_group(app, GROUP_D, opts, "D", "基线")
        if "E" in groups:
            for key, (label, tpl) in ABLATIONS.items():
                print("\n[%s] %s" % (key, label))
                pb.ART_PROMPT = tpl      # 只改内存，不落盘
                try:
                    subset = [s for s in GROUP_D if s[0] in ABLATION_SCENARIOS]
                    records += run_group(app, subset, opts, key, label)
                finally:
                    pb.ART_PROMPT = ART_PROMPT_BASE
    finally:
        pb.ART_PROMPT = ART_PROMPT_BASE
        app.close()

    stats = summarise(records)
    write_markdown(out_dir / "samples.md", records, recount, started_at)
    (out_dir / "samples.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8")
    (out_dir / "params.json").write_text(json.dumps({
        "backend": opts.backend, "model": opts.model, "temperature": opts.temperature,
        "max_tokens": MAX_TOKENS, "k": 5, "timeout": opts.timeout,
        "started_at": started_at, "requests": len(records),
        "groups": sorted(groups), "ablation_scenarios": ABLATION_SCENARIOS,
        "art_prompt_sha_base": _sha(ART_PROMPT_BASE),
        "art_prompt_sha_ablations": {k: _sha(v[1]) for k, v in ABLATIONS.items()},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "stats.json").write_text(
        json.dumps({"stats": stats, "recount": recount}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    print("\n=== 汇总（含图案块的轮次 / 总轮次）===")
    for tap, t in stats.items():
        print("  %-3s %d/%d 出错 %d  按轮次 %s"
              % (tap, t["art"], t["turns"], t["errors"], t["by_turn"]))
    bad = sum(t["errors"] for t in stats.values())
    print("\n完成 %d 次，出错 %d；记录目录 %s" % (len(records), bad, out_dir))
    return 0 if bad == 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())
