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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import textart  # noqa: E402  字符画的宽度与对齐判据（CHAT-017）

# AUD-10：`wiki:` 这个前缀有误导性——读起来像「维基百科有出处」，实际含义是
# 「来自人物解读，语料里追不到原句」。2026-10-25 逐条核过 6 条带标签的示例，
# 只有 `verified:消失@0:25:56` 能逐字定位，其余 5 条全部追不到（见 docs/SOURCES.md）。
# 所以把它们的标签改成 `reading:`（本项目的人物解读），并同步放开格式校验。
# `wiki:` 仍然保留在正则里：老提交里出现过的写法不该突然变成"格式非法"，
# 只是新增的示例不该再用它（这条靠 AUD 审计与 SOURCES.md 约束，不靠正则）。
VALID_SOURCE = re.compile(
    r"^(handwritten(:[a-z0-9-]+)?|reading:[a-z0-9-]+|wiki:[a-z0-9-]+"
    r"|seed:\S+@\S+|verified:\S+@\S+)$")

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


# ---------------------------------------------------------------
# 括号动作检测（CHAT-007 / DEC-005）
#
# 和 simulate_check_reply() 的关系：
#   - simulate_check_reply() 复刻「界面上会不会标 △」，只覆盖 guard 与分段；
#   - check_action_brackets() 是新规则的一致性检查，**独立函数、独立返回值**，
#     方便 scripts/test_frontend.js 把它与前端检测器逐例比对（签名刻意与
#     simulate_check_reply 一致：只吃一个文本）。
#
# 判据边界（DEC-007：不设配额、不列禁用词表）：
#   - 错误：半角/全角括号不配对——这是格式问题，可以自动判。
#   - 错误：括号内出现主线设定术语——动作不能借描写泄露设定（WORLD-001）。
#   - 提示：括号里有消息、数字等「信息」，或整条只有括号——只报告，不判错。
#     「动作是否承载关键信息」没法自动判，这里只做一个粗筛，给人看。
#   - 不检查：「面无表情」「脸颊泛红」这类用词、动作出现次数。那些靠人工观察。
# 感叹号不在这里判：它由 guard.banned_patterns 统一处理（PEND-002 未决，
# 括号内外一律禁止），避免同一个问题被两个检测器各自报一遍。
# ---------------------------------------------------------------

# 主线设定术语：括号里出现即判错。取专门词，不取「观测」这种日常也用的词。
SETTING_TERMS = [
    "数据链", "终端界面", "人形终端", "信息统合思念体", "思念体", "统合思念体",
    "信息流", "界面", "数据", "纳米机器", "重写", "权限", "情报",
    "观测对象", "凉宫春日", "SOS团",
]
# 提示用的粗筛：括号里出现这些，像是把内容写进了动作
BRACKET_INFO_HINT = [
    "因为", "所以", "结果", "方法", "步骤", "答案", "意思是", "需要先",
    "分钟", "小时", "第三", "第二", "第一",
]


def _bracket_spans(text: str):
    """返回 (配对出的括号内容列表, 是否有嵌套/错位)。

    只认全角括号。按出现顺序扫描，遇「（」入栈、遇「）」出栈；出栈时没有
    对应开括号，或扫完栈非空，都算错位。正文里的半角括号由调用方单独报错。
    嵌套（（a（b）c））不展开——她不需要嵌套，出现即提示人工确认。
    """
    content, depth, start, bad = [], 0, -1, False
    for i, ch in enumerate(text):
        if ch == "（":
            if depth:
                bad = True
            depth += 1
            start = i
        elif ch == "）":
            if depth == 0:
                bad = True
                continue
            depth -= 1
            if depth == 0:
                content.append(text[start + 1:i])
    if depth:
        bad = True
    return content, bad


def check_action_brackets(reply: str) -> dict:
    """检查一条回复里的括号动作（CHAT-007）。不依赖 guard，只看文本本身。

    返回 {"errors": [...], "hints": [...]}：
      errors 进自检的 ✗，hints 进 △。

    **errors / hints 的用途不同，前端只镜像 errors**：
      - errors 是判定（半角括号、不配对、嵌套、括号里塞设定术语）。这些规则
        在 `web/index.html::checkBrackets()` 里有逐字镜像，`test_frontend.js`
        用样本逐例比对两端的 errors 集合，不同构就是 bug。
      - hints 是**观察记录**，只在自检报告里出现，不进界面。理由是界面只显示
        前两条违例，把提示混进去会把「用了感叹号」这类主规则挤掉。
      - hints 里也没有任何配额或频次判据（DEC-007）；「括号里有像信息的词」
        只是粗筛，人看了才知道该不该改。

    所以：`checkBrackets()` 不返回 hints 是**有意为之**，不是漏实现。
    若将来要在界面上显示提示，需要先解决「前两条被挤占」的问题，
    而不是把 hints 直接并进 checkReply 的返回值。
    """
    errors, hints = [], []
    opens, closes = reply.count("（"), reply.count("）")
    # 只把「像动作描写」的半角括号算错（前面是汉字、里面没有 ASCII 字母数字）。
    # 「从 O(n) 降到 O(1)」「(n)」这类代码、算式是正文，不能误伤。
    ascii_hits = [m for m in
                  re.findall(r"(?<![\u4e00-\u9fff])\(([^()]{1,30})\)?(?=[\s。，、…）]|$)", reply)
                  if re.search(r"[\u4e00-\u9fff]", m) and not re.search(r"[A-Za-z0-9]", m)]
    if ascii_hits:
        errors.append(f"括号动作使用了半角 ( )，应统一为全角「（）」：（{ascii_hits[0]}）")
    if opens != closes:
        errors.append(f"括号不配对：左括号 {opens} 个，右括号 {closes} 个")

    content, bad = _bracket_spans(reply)
    if bad and opens == closes:
        errors.append("括号嵌套或错位，检测器无法配对")

    for inner in content:
        for term in SETTING_TERMS:
            if term in inner:
                errors.append(f"括号内含设定术语「{term}」——动作不能泄露设定（WORLD-001）")
                break

    for inner in content:
        hit = next((h for h in BRACKET_INFO_HINT if h in inner), None)
        if hit is not None:
            hints.append(f"括号「（{inner}）」里有像信息的内容（「{hit}」）——"
                         f"动作不承载关键信息，重要内容应写在正文（CHAT-007，提示非错误）")
            break

    stripped = reply.strip()
    if stripped and not re.sub(r"（[^（）]*）", "", stripped).strip():
        hints.append("整条只有括号动作、没有台词——合法回答（CHAT-007），仅作观察记录")

    return {"errors": errors, "hints": hints}


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
    # 字符画本来就是多行，还常常要空行分隔文字，那不是「分段」这个违例（CHAT-017）。
    # 这条豁免必须跟前端 `checkReply()` 一致（DEC-006）——这里少写一次，
    # 自检报「分段」而界面不报，就会变成「两个检测器对同一句话结论不同」。
    if re.search(r"\n\s*\n", reply) and not textart.is_art(reply):
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


def _ensure_root_on_path():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))


def world_allows_art(world) -> bool:
    """这个世界放不放字符画。规则在 prompt_builder，这里只查表，不另立一份。"""
    _ensure_root_on_path()
    import prompt_builder as _pb
    return bool(_pb.ART_ALLOWED_WORLDS.get(world or "normal"))


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

        # 括号动作（CHAT-007）：错误进 ✗，提示进 △，不改变能否上屏。
        act = check_action_brackets(a)
        for msg in act["errors"]:
            errs.append(f"{tag} {msg}")
        for msg in act["hints"]:
            warns.append(f"{tag} {msg}")
        if "（" in a and not act["errors"]:
            oks.append(f"{tag} 括号动作配对正常")

        # 字符画（CHAT-017）：示例里的图案必须自己对齐。
        # 自检只查能自动判的（左边距、行尾空白、宽度不可靠的字符）；
        # 「好不好看」只能人看，见 VALIDATION 的 V-ART-02。
        # 例外：主线卡的示例里明确写了「不画」，那里出现多行是为了说明
        # 「这条线路不支持」，不该被当成画去查对齐。
        if world_allows_art(d.get("world")) and textart.is_art(a):
            art = textart.check_art(a)
            for msg in art:
                warns.append(f"{tag} {msg}")
            if not art:
                oks.append(f"{tag} 字符画对齐正常")

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
    #    CHAT-007：纯动作回复（「（合上书）」）本来就短，把这类算进分布没有参考价值，
    #    会触发「差异过小」——那不是回归，是规则本身带来的。长度分布只统计带台词的示例。
    spoken = [strip_len(e.get("assistant", "")) for e in d.get("examples", [])
              if re.sub(r"（[^（）]*）", "", e.get("assistant", "")).strip()]
    if spoken:
        lens = sorted(spoken)
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

    # 7. 括号动作的规则是否真的开放（CHAT-007 / DEC-006 第 1、2 处同步）
    #    prompt_builder 仍在，而人格卡没提，模型只会看见「不许动作」；
    #    人格卡提了，而 prompt_builder 已删掉禁令。两边只要有一边是旧行为，就报错。
    try:
        sys.path.insert(0, str(ROOT))
        import prompt_builder as pb
        tp = pb.TERMINAL_PROMPT
        if "不允许" in tp and "（" not in tp or "只有字" in tp:
            errs.append("prompt_builder.TERMINAL_PROMPT 仍禁止动作描写（「屏幕上只有字」），"
                        "与 CHAT-007 相反")
        elif "（" not in tp:
            errs.append("prompt_builder.TERMINAL_PROMPT 未提到括号动作（CHAT-007）")
        else:
            oks.append("prompt_builder.TERMINAL_PROMPT 已开放括号动作")
    except Exception as e:  # 单独跑人格卡时不应因为 import 失败而中断
        warns.append(f"无法读取 prompt_builder.TERMINAL_PROMPT：{e}")

    if "（" not in sp:
        errs.append("system_prompt 未说明括号动作的用法（CHAT-007）——"
                    "示例里有动作、规则里没有，模型只能靠猜")
    elif "不使用感叹号" not in sp and "不写感叹号" not in sp and "不用感叹号" not in sp:
        warns.append("system_prompt 已开放括号动作，但没有说明括号内不用感叹号"
                     "（PEND-002 未决，当前默认保持禁止）")

    # 示例里是否两种情形都有（观察，不设配额）
    exs = d.get("examples") or []
    pure_action = [e for e in exs
                   if e.get("assistant", "").strip()
                   and not re.sub(r"（[^（）]*）", "", e["assistant"]).strip()]
    with_action = [e for e in exs if "（" in e.get("assistant", "")]
    if exs and not pure_action:
        warns.append("示例里没有「整条只有动作、没有台词」的样本（CHAT-007 允许纯动作回复）")
    if with_action:
        oks.append(f"示例含动作 {len(with_action)} 条，其中纯动作 {len(pure_action)} 条（观察，非配额）")

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
