#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""patch_personas.py — 给两张人格卡补上终端层需要的字段（幂等，可重复运行）

补的是:
  protocol  终端前缀 / 打字节奏 / 空应答
  world     这张卡属于哪条世界线（server 用它挑卡）
  theme     crash 世界加一点「信号衰减」色调

不覆盖已有的 system_prompt / examples / guard。
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent

PATCH = {
    "yuki.json": {
        "world": "normal",
        "protocol": {
            "prefix": "YUKI.N>",
            "type_delay_ms": 42,
            "empty_line": "……",
            "note": "第 06 集 13:24 的终端通讯形式，她在另一个世界用显示器的电源开关联系阿虚。",
        },
    },
    "yuki_school.json": {
        "world": "crash",
        "protocol": {
            "prefix": "YUKI.N>",
            "type_delay_ms": 68,
            "empty_line": "……",
            "note": "同一套协议，同一块屏幕。这边的她打字更慢，错字更多。",
        },
    },
}


def main():
    for name, patch in PATCH.items():
        p = ROOT / "personas" / name
        if not p.exists():
            print(f"[skip] {name} 不存在")
            continue
        card = json.loads(p.read_text(encoding="utf-8"))
        changed = []
        for k, v in patch.items():
            if card.get(k) != v:
                card[k] = v
                changed.append(k)
        if changed:
            p.write_text(
                json.dumps(card, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(f"[ok] {name}: 更新 {changed}")
        else:
            print(f"[ok] {name}: 已是最新")


if __name__ == "__main__":
    main()
