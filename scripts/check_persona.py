#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
check_persona.py — 人格卡自检

改完 personas/*.json 后跑一遍，检查规则有没有被自己写坏。
比肉眼看可靠：人格卡最容易出的问题不是写错字，是**规则互相矛盾**。

用法:
    python scripts/check_persona.py
    python scripts/check_persona.py personas/yuki.json
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PERSONAS = ROOT / "personas"
# 已废弃的候选池：里面的句子做过交叉污染检测，绝不该出现在示例里
DEPRECATED_CAND = ROOT / "data/_deprecated/candidates_heuristic/all.cand.jsonl"
VALID_SOURCE = re.compile(
    r"^(handwritten(:[a-z0-9-]+)?|wiki:[a-z0-9-]+|seed:\S+@\S+|verified:\S+@\S+)$")

# 出现即判定为「助手腔」的通病
SERVICE_TALK = [
    "作为一个", "很高兴", "希望对你有帮助", "还有什么可以帮", "还有什么需要",
    "当然可以", "乐意为您", "感谢您的", "请问还有", "我可以帮你",
]
# 语气助词（长门不该有）
PARTICLES = ["呢", "哦", "吧", "啊", "哟", "啦", "嘛"]


def strip_len(s: str) -> int:
    # JavaScript String.length counts UTF-16 code units.
    return len(re.sub(r"\s", "", s).encode("utf-16-le")) // 2


def norm(s: str) -> str:
    """去掉所有标点、空白、省略号，只留汉字和字母数字，用于污染比对。"""
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", s)


def simulate_check_reply(reply: str, user_text: str, guard: dict) -> str:
    """精确复刻 web/index.html 里 checkReply() 的行为。

    存在的意义：人格卡里的示例必须自己通过自己的检测。
    否则界面上她的示范答案会亮 △ —— 那是自相矛盾的，
    而且只有真的跑起来才发现。这里提前测出来。

    注意：不再有字数判定。回复长短不是违例。
    user_text 保留在签名里，便于前端与自检逐例比对时传同一组参数。
    """
    g = guard or {}
    probs = []
    for p in g.get("banned_patterns") or []:
        try:
            m = re.search(p, reply, re.I)
        except re.error:
            continue
        if m:
            word = re.sub(r"[|()?]", "", m.group(0))[:8]
            tag = f"「{word}」"
            if tag not in probs:
                probs.append(tag)
    if re.search(r"\n\s*\n", reply):
        probs.append("分段")
    return " ".join(probs[:2])


def load_deprecated_pool():
    """加载已废弃候选池的句子集合。缺失则返回空集（不阻塞检查）。"""
    if not DEPRECATED_CAND.exists():
        return set()
    pool = set()
    try:
        for line in DEPRECATED_CAND.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            t = json.loads(line).get("text", "")
            n = norm(t)
            if n:
                pool.add(n)
    except Exception:
        return set()
    return pool


def check(path: Path):
    errs, warns, oks = [], [], []
    pool = load_deprecated_pool()

    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return [f"JSON 解析失败: {e}"], [], []

    for k in ("id", "display_name", "system_prompt", "examples", "guard"):
        if k not in d:
            errs.append(f"缺字段 {k}")

    sp = d.get("system_prompt", "")
    g = d.get("guard", {})
    name = d.get("id", path.stem)

    # 0. 防回流：字数配额已被产品取消（DEC-001），字段重新出现即为回归
    stale = [k for k in g if k.startswith("max_chars")]
    if stale:
        errs.append(f"guard 含已废止的字数配额字段 {stale} —— "
                    f"字数上限已由 DEC-001 取消，判定逻辑也已删除")
    if "tech_keywords" in g:
        errs.append("guard 含 tech_keywords —— 该字段原用于选择字数档位，"
                    "档位取消后已无用途，应删除以免误导")

    # 1. prompt 必含段落
    for seg in ("【身份】", "【说话方式", "【称呼"):
        if seg not in sp:
            warns.append(f"prompt 缺少段落 {seg}")

    # 2. prompt 自身不该出现服务话术
    for w in SERVICE_TALK:
        if w in sp and "不使用" not in sp[max(0, sp.find(w) - 20):sp.find(w) + 2]:
            warns.append(f"prompt 正文出现服务话术「{w}」（若在禁止列表里可忽略）")

    # 3. 情感表述矛盾检测（易错点）
    if "没有该功能" in sp:
        errs.append("prompt 含「没有该功能」——与原作不符，她会给弱化回答而非否认")

    # 4. examples 逐条体检
    for i, e in enumerate(d.get("examples", [])):
        if "user" not in e or "assistant" not in e:
            errs.append(f"示例 {i} 缺 user/assistant")
            continue
        a = e["assistant"]
        n = strip_len(a)
        tag = f"示例 {i}「{e['user'][:12]}」"
        # 服务话术（内容层面，与界面检测无关，故单独查）
        for w in SERVICE_TALK:
            if w in a:
                errs.append(f"{tag} 含服务话术「{w}」")
        # 语气助词（只对主线版严格；女高中生版允许「啊」等）
        if name == "yuki":
            for p in PARTICLES:
                if p in a:
                    warns.append(f"{tag} 含语气助词「{p}」")

        # 违例：直接复刻界面判定，保证「示例会不会被标 △」可复现。
        # 注意这里不再有长度判定 —— 长短不是违例（DEC-001）。
        sim = simulate_check_reply(a, e["user"], g)
        if sim:
            errs.append(f"{tag} 会在界面上被标 △（{sim}）—— 示例自己违例，自相矛盾")
        else:
            oks.append(f"{tag} {n} 字 · 不触发 △")

        # 溯源：必须写明依据，禁止凭印象编
        src = e.get("source")
        if not src:
            errs.append(f"{tag} 缺少 source 字段（示例必须可追溯）")
        elif not VALID_SOURCE.match(src):
            errs.append(f"{tag} source 格式非法「{src}」"
                        f"（应为 handwritten / wiki:<要点> / seed:<集>@<时间码>）")

        # 污染检测：与已废弃候选池重合的句子，必须人工核实过说话人
        a_norm = norm(a)
        if a_norm and a_norm in pool:
            if src and src.startswith("verified:"):
                oks.append(f"{tag} 候选池重合但已核实说话人（{src}）")
            else:
                errs.append(
                    f"{tag} 与废弃候选池重合「{a}」，且未核实说话人。"
                    f"该池精度仅 15%（多为阿虚/春日/朝比奈）。"
                    f"若确为长门原句，把 source 改为 verified:<集>@<时间码> 并注明出处")

    # 5. 示例字数分布（仅参考，不是判据）
    #    DEC-001：字数配额已取消；DEC-007：不设示例平均字数指标。
    #    这里只报告分布，供人工判断长短是否不齐，不报错也不建议压缩。
    if d.get("examples"):
        lens = sorted(strip_len(e.get("assistant", "")) for e in d["examples"])
        n_ex = len(lens)
        oks.append(f"示例长度分布 {lens[0]}–{lens[-1]} 字，中位 {lens[n_ex // 2]} 字（仅参考，非判据）")
        if lens[-1] - lens[0] < 10 and n_ex >= 6:
            warns.append("示例长度差异过小，可能缺少「说完整」的样本（提示，非错误）")

    # 6. 溯源分布：让"凭印象编"的示例无处藏身
    if d.get("examples"):
        dist = {}
        for e in d["examples"]:
            k = (e.get("source") or "MISSING").split(":")[0]
            dist[k] = dist.get(k, 0) + 1
        oks.append("示例溯源  " + "  ".join(f"{k}×{v}" for k, v in sorted(dist.items())))
        if "MISSING" in dist:
            errs.append(f"有 {dist['MISSING']} 条示例没有 source")
        if dist.get("handwritten", 0) == len(d["examples"]):
            warns.append("全部示例均为手写、无原文/设定依据——注意可能偏离原作语言习惯")

    return errs, warns, oks


def main():
    targets = ([Path(a) for a in sys.argv[1:]] if len(sys.argv) > 1
               else sorted(PERSONAS.glob("*.json")))
    total_err = 0
    for f in targets:
        errs, warns, oks = check(f)
        total_err += len(errs)
        print(f"\n{'='*56}\n{f.name}")
        print("="*56)
        for o in oks:
            print(f"  · {o}")
        for w in warns:
            print(f"  △ {w}")
        for e in errs:
            print(f"  ✗ {e}")
        if not errs:
            print("  → 通过")
    print(f"\n合计错误 {total_err} 条")
    sys.exit(1 if total_err else 0)


if __name__ == "__main__":
    main()
