#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
annotate.py — 用 LLM 给无标注字幕补说话人

背景:
    华盟这套字幕的 Actor 字段全是 NTP, 13070 条对白没有任何说话人信息。
    启发式筛选实测精度只有约 15% (阿虚是第一人称叙述者, 他的旁白同样短而平淡,
    和长门的台词在字面特征上高度重叠), 不可用。
    唯一能规模化的办法是让 LLM 结合剧情语境逐条判断。

前置:
    先跑 extract_scenes.py 得到场景池, 只标锚点附近片段, 不必处理全量。

用法:
    # 1. 先看会发什么 (不花钱)
    python scripts/annotate.py --scenes data/scenes/01.scenes.txt --dry-run

    # 2. 用任意 OpenAI 兼容接口跑
    export OPENAI_API_KEY=sk-xxx
    python scripts/annotate.py --scenes data/scenes --out data/labeled \
        --base-url https://api.openai.com/v1 --model gpt-4o-mini

    # 3. 本地跑 (ollama / vllm 等)
    python scripts/annotate.py --scenes data/scenes --out data/labeled \
        --base-url http://localhost:11434/v1 --model qwen2.5:14b --api-key ollama

输出:
    <ep>.labeled.jsonl  每行 {ep, idx, start, text, speaker, conf}
    <ep>.nagato.txt     抽出的长门台词清单, 人工复核用
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

ROSTER = """角色名册与说话特征（《凉宫春日的忧郁》）：
- 阿虚(N)：男主角、第一人称叙述者。**大量台词是他的内心独白/旁白**，语气随意带吐槽，常用"我""这家伙""真是的"。
- 春日(H)：凉宫春日。语速快、爱用感叹号、命令式、自我中心，句尾常带"哦""呢""吧"。
- 长门(Y)：长门有希。三无少女。**极短**（多为 2-8 字），句末几乎只用"。"和"……"，不用"！"。
  常用固定应答："……是的""不是""不知道""可以""无所谓""请便""了解"。
  陈述超现实内容时语气完全不变。自称"我"很少。
- 朝比奈(M)：朝比奈实玖瑠。软糯、常带"～"、容易慌张、多用敬语。
- 古泉(K)：古泉一树。礼貌、笑眯眯、措辞文雅，常解释"机关"相关。
- 谷口(T)/国木田(D)/鹤屋(S)：同学。谷口轻浮，国木田温和，鹤屋豪爽。
- 其他(O)：上述之外的任何角色。"""

PROMPT = """下面是日本动画《凉宫春日的忧郁》某一集的中文字幕，按时间先后逐条排列，**没有标注说话人**。

{roster}

任务：判断每一条台词是谁说的。

关键规则：
1. 叙述性、描述性、内心吐槽的句子，是阿虚的旁白，标 N。这类在字幕里占比很高，不要误判成长门。
2. 长门的台词必定极短且语气平。如果一句超过 15 字、带情绪词或感叹号，几乎不可能是她。
3. 长门只在被直接对话或在解释超自然现象时开口。她不会主动闲聊。
4. 不确定的，标 U，不要猜。

片段时间范围：{span}
台词：
{lines}

只输出 JSON，不要任何解释文字。格式：
{{"labels": [{{"i": 行号, "spk": "Y"}}, ...]}}
必须为上面列出的**每一个行号**都给出判断。spk 取值：N/H/Y/M/K/T/D/S/O/U"""

CODE = {
    "N": "阿虚/旁白", "H": "春日", "Y": "长门", "M": "朝比奈",
    "K": "古泉", "T": "谷口", "D": "国木田", "S": "鹤屋", "O": "其他", "U": "不确定",
}


def parse_scene_file(path: Path):
    """把 scenes.txt 解析成 [{span, lines:[{i,start,text}]}]"""
    scenes, cur, span = [], None, ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.startswith("====="):
            m = re.search(r"(\d+:\d+:\d+(?:\.\d+)?)\s*[–-]\s*(\d+:\d+:\d+(?:\.\d+)?)", raw)
            span = f"{m.group(1)} – {m.group(2)}" if m else raw
            cur = {"span": span, "lines": []}
            scenes.append(cur)
            continue
        m = re.match(r"^\s*\*?\s*(\d+)\s+(\d+:\d+:\d+(?:\.\d+)?)\s+(.*)$", raw)
        if m and cur is not None:
            cur["lines"].append({"i": int(m.group(1)), "start": m.group(2), "text": m.group(3).strip()})
    return [s for s in scenes if s["lines"]]


def call_llm(base_url, api_key, model, prompt, timeout=180):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }).encode()
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"]


def extract_json(s: str):
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```\w*\n?", "", s)
        s = re.sub(r"\n?```$", "", s)
    try:
        return json.loads(s)
    except Exception:
        m = re.search(r"\{.*\}", s, re.S)
        if m:
            return json.loads(m.group(0))
        raise


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenes", required=True, help="scenes.txt 文件或目录")
    ap.add_argument("--out", required=True)
    ap.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    ap.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY", ""))
    ap.add_argument("--model", default=os.environ.get("ANNOTATE_MODEL", "gpt-4o-mini"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", default=None)
    args = ap.parse_args()

    src = Path(args.scenes)
    files = sorted(src.glob("*.scenes.txt")) if src.is_dir() else [src]
    if args.only:
        files = [f for f in files if f.name.split(".")[0] in args.only.split(",")]

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    total_y = 0
    for f in files:
        ep = f.name.replace(".scenes.txt", "")
        scenes = parse_scene_file(f)
        labeled, nagato = [], []

        for si, sc in enumerate(scenes):
            lines_txt = "\n".join(f'{ln["i"]}  {ln["text"]}' for ln in sc["lines"])
            prompt = PROMPT.format(roster=ROSTER, span=sc["span"], lines=lines_txt)

            if args.dry_run:
                if si == 0:
                    print(f"--- {ep} 片段 {si} prompt 预览 ---")
                    print(prompt[:1200])
                    print("...\n")
                continue

            if not args.api_key:
                sys.exit("缺少 API key: 设置 OPENAI_API_KEY 环境变量或传 --api-key")

            try:
                raw = call_llm(args.base_url, args.api_key, args.model, prompt)
                labels = extract_json(raw).get("labels", [])
            except Exception as e:
                print(f"  [!] {ep} 片段{si} 失败: {e}")
                time.sleep(2)
                continue

            by_i = {int(x["i"]): x.get("spk", "U") for x in labels if "i" in x}
            for ln in sc["lines"]:
                spk = by_i.get(ln["i"], "U")
                rec = {"ep": ep, "idx": ln["i"], "start": ln["start"],
                       "text": ln["text"], "speaker": spk, "spk_name": CODE.get(spk, spk),
                       "conf": "model"}
                labeled.append(rec)
                if spk == "Y":
                    nagato.append(rec)
            print(f"  {ep} 片段{si:>3}: {len(sc['lines'])} 条 -> 长门 {sum(1 for l in sc['lines'] if by_i.get(l['i'])=='Y')}")
            time.sleep(0.3)

        if args.dry_run:
            continue

        with (out / f"{ep}.labeled.jsonl").open("w", encoding="utf-8") as fh:
            for r in labeled:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        with (out / f"{ep}.nagato.txt").open("w", encoding="utf-8") as fh:
            fh.write(f"# {ep} | 长门台词 {len(nagato)} 条 (待人工复核)\n")
            for r in nagato:
                fh.write(f"{r['start'][:7]}  {r['text']}\n")
        total_y += len(nagato)
        print(f"[ok] {ep}: 标注 {len(labeled)} 条, 长门 {len(nagato)} 条")

    if not args.dry_run:
        print(f"\n合计长门台词 {total_y} 条 -> {out}")


if __name__ == "__main__":
    main()
