#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
parse_ass.py — 把 .ass 字幕解析成结构化对话流

用法:
    python scripts/parse_ass.py --src "D:/bdwpxiazai/字幕" --out data/parsed

产出 (每集两个文件):
    <ep>.dialogue.jsonl   全量对白, 按时间排序, 每条一行
    <ep>.stream.txt       紧凑对话流, 供 LLM 人工/自动标注说话人

设计要点:
  - 自动探测编码 (PopSub 生成的旧字幕常为 UTF-16 LE)
  - 只保留 "对白" 样式, 剔除 OP/ED/注释/警告/样式字体行
  - 剔除 ASS 覆盖标签 {\\...}, 转换 \\N 换行
  - 相邻同场景台词合并成 "对话块", 记录 gap, 便于后续切场景
"""
import argparse
import json
import re
import sys
from pathlib import Path

# ---- 编码探测 ----------------------------------------------------------------

def read_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16")
    # 无 BOM 但大量 \x00 交替 -> UTF-16 LE
    head = raw[:400]
    if head.count(b"\x00") > len(head) * 0.25:
        return raw.decode("utf-16-le", errors="replace")
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# ---- ASS 解析 ---------------------------------------------------------------

TAG_RE = re.compile(r"\{[^}]*\}")
# 用排除法而非白名单: 各字幕组 style 命名完全不同
# (华盟用 zhengwen, 另一组用 Chinese), 白名单会整集漏掉
DROP_STYLES = {
    "OP", "OP中文", "OP-JP", "OP-Ch", "op", "op-jp", "op-ch",
    "ed", "edzw", "ED-JP", "ED-Ch", "ED",
    "jinggao", "staffbiaoti", "Staff", "staff", "neta", "Note", "note",
    "title", "Title", "songs", "romaji",
}
DROP_STYLE_PREFIX = ("op", "ed", "staff", "info", "warn")

DIALOGUE_RE = re.compile(r"^Dialogue:\s*(.+)$")


def clean_text(s: str) -> str:
    s = TAG_RE.sub("", s)
    s = s.replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ")
    s = s.replace("\u3000", " ").strip()
    return s


def parse_ass(path: Path):
    text = read_text(path)
    fmt = None
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("Format:"):
            fmt = [x.strip() for x in line[len("Format:"):].split(",")]
            continue
        m = DIALOGUE_RE.match(line)
        if not m or fmt is None:
            continue
        # 按 Format 字段数切分, Text 是最后一个, 需 maxsplit
        parts = m.group(1).split(",", len(fmt) - 1)
        if len(parts) < len(fmt):
            continue
        rec = dict(zip(fmt, parts))
        style = rec.get("Style", "").strip()
        if style in DROP_STYLES or style.lower().startswith(DROP_STYLE_PREFIX):
            continue
        body = clean_text(rec.get("Text", ""))
        if not body:
            continue
        # 歌词行兜底过滤: 整行假名且无中文, 直接丢
        if re.fullmatch(r"[\u3040-\u30ff\s!?.,'A-Za-z:：…\-~〜]*", body) and re.search(r"[\u3040-\u30ff]", body):
            continue
        out.append({
            "start": rec.get("Start", "").strip(),
            "end": rec.get("End", "").strip(),
            "style": style,
            "actor": rec.get("Actor", "").strip(),
            "text": body,
        })
    out.sort(key=lambda r: ts_key(r["start"]))
    return out


def ts_key(t: str) -> float:
    """'0:01:23.45' -> 秒"""
    try:
        h, m, s = t.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except Exception:
        return 0.0


# ---- 组装对话块 -------------------------------------------------------------

SCENE_GAP = 2.5  # 秒; 间隔超过则视为场景切换


def build_blocks(lines):
    """把逐条台词按时间间隔聚成对话块"""
    blocks, cur = [], []
    for i, ln in enumerate(lines):
        if cur:
            gap = ts_key(ln["start"]) - ts_key(cur[-1]["end"])
            if gap > SCENE_GAP:
                blocks.append(cur)
                cur = []
        cur.append(ln)
    if cur:
        blocks.append(cur)
    return blocks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default=None, help="只处理指定文件名前缀, 逗号分隔")
    args = ap.parse_args()

    src = Path(args.src)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    files = sorted(src.glob("*.ass"))
    if args.only:
        keys = [k.strip() for k in args.only.split(",")]
        files = [f for f in files if f.stem in keys]

    total = 0
    for f in files:
        lines = parse_ass(f)
        if not lines:
            print(f"[skip] {f.name}: 无对白")
            continue
        ep = f.stem
        with (out / f"{ep}.dialogue.jsonl").open("w", encoding="utf-8") as fh:
            for i, ln in enumerate(lines):
                ln2 = {"ep": ep, "idx": i, **ln}
                fh.write(json.dumps(ln2, ensure_ascii=False) + "\n")

        blocks = build_blocks(lines)
        with (out / f"{ep}.stream.txt").open("w", encoding="utf-8") as fh:
            fh.write(f"# {ep} | 对白 {len(lines)} 条 | 对话块 {len(blocks)} 个\n")
            for bi, blk in enumerate(blocks):
                fh.write(f"\n## 块{bi:03d}  {blk[0]['start'][:7]}–{blk[-1]['end'][:7]}\n")
                for ln in blk:
                    fh.write(f"{ln['start'][:7]}  {ln['text']}\n")
        total += len(lines)
        print(f"[ok] {f.name}: 对白 {len(lines)} 条 / 对话块 {len(blocks)} 个")
    print(f"\n总计 {len(files)} 个文件, {total} 条对白 -> {out}")


if __name__ == "__main__":
    main()
