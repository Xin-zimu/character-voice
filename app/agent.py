"""角色对话主逻辑。

三件事决定「像不像」，按重要性排序：
  1. 角色卡规则写得好不好（行为规则 > 形容词）
  2. 生成内容有没有原作事实做锚（检索，不是靠模型瞎编）
  3. 有没有把助手腔堵死（强模型的默认人格非常顽固）

另外让模型同时输出「情绪标签」，
这是后面语音层能做出语气语调的前提。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import llm
from .memory import Memory


@dataclass
class Reply:
    text: str
    emotion: str = "neutral"
    intensity: float = 0.4
    speed: float = 1.0
    memories: list[dict] = field(default_factory=list)
    retried: bool = False
    raw: str = ""


class CharacterAgent:
    def __init__(self, root: Path, config_path: Path | None = None, history_turns: int = 6):
        self.root = root
        self.cfg = yaml.safe_load(
            (config_path or root / "config" / "character.yaml").read_text(encoding="utf-8")
        )
        mem = self.cfg.get("memory", {})
        self.memory = Memory(
            root,
            mem.get("sources", ["data/raw_text/*.txt"]),
            top_k=mem.get("top_k", 4),
            min_score=mem.get("min_score", 0.08),
        )
        self.history: list[dict] = []
        self.history_turns = history_turns
        self._sys = self._build_system()

    # ------------------------------------------------------------------
    # 角色卡 → system prompt
    # ------------------------------------------------------------------
    def _build_system(self) -> str:
        c = self.cfg
        sp = c.get("speech", {})
        em = c.get("emotions", {})

        def bullets(items: list[str]) -> str:
            return "\n".join(f"- {x}" for x in items) if items else "- （无）"

        tics = sp.get("verbal_tics", [])
        tic_line = "、".join(f"「{t}」" for t in tics) if tics else "（无固定口癖）"

        em_lines = "\n".join(
            f"- {k}：{v.get('desc', '')}" for k, v in em.items()
        ) or "- neutral：平常"

        return f"""你就是「{c['name']}」，出自《{c['series']}》。不是扮演，不是模仿，你就是本人。

跟你说话的人是「{c.get('user_role', '对方')}」，你一直叫他「{c.get('address_user', '你')}」。

# 你怎么说话
{bullets(sp.get('rules', []))}

# 你的口癖
{tic_line}

# 绝对不能出现
{bullets(sp.get('forbidden', []))}

你可以不耐烦、可以不讲理、可以打断对方、可以拒绝回答。这些都正常。
但你不会跳出这个身份，不会解释设定，不会承认自己在「演」。

# 情绪标签
{em_lines}

# 输出格式
只输出一个 JSON 对象，不要任何多余文字、不要代码块标记：
{{"text": "台词内容", "emotion": "标签名"}}

台词要求：
- 纯口语，5 到 60 个字
- 不要带引号，不要加（动作描写）或 *动作*
- 不要解释自己为什么这么说
- emotion 只能取上面列出的标签之一"""

    # ------------------------------------------------------------------
    # 生成
    # ------------------------------------------------------------------
    def _memories_block(self, hits: list[dict]) -> str:
        if not hits:
            return ""
        lines = "\n".join(f"[{i+1}] {h['text']}" for i, h in enumerate(hits))
        return f"""

# 原作里的相关片段
下面这些是原作内容，只用它把握事实和语气，不要照抄原句，也不要复述：
{lines}"""

    def _parse(self, raw: str) -> Reply:
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw).strip()

        data = None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", raw, re.S)
            if m:
                try:
                    data = json.loads(m.group())
                except json.JSONDecodeError:
                    data = None

        if not isinstance(data, dict):
            # 模型没按格式走，退化成纯文本
            text = raw.strip().strip('"').strip("「」")
            return self._with_emotion(Reply(text=text, raw=raw))

        text = str(data.get("text", "")).strip().strip('"').strip("「」")
        return self._with_emotion(Reply(text=text, emotion=str(data.get("emotion", "neutral")), raw=raw))

    def _with_emotion(self, r: Reply) -> Reply:
        em = self.cfg.get("emotions", {})
        if r.emotion not in em:
            r.emotion = "neutral"
        spec = em.get(r.emotion, {})
        r.intensity = float(spec.get("intensity", 0.4))
        r.speed = float(spec.get("speed", 1.0))
        return r

    def _violates_guard(self, text: str) -> str | None:
        for pat in self.cfg.get("guard", {}).get("banned_patterns", []):
            if re.search(pat, text):
                return pat
        return None

    def reply(self, user_text: str) -> Reply:
        hits = self.memory.search(user_text)
        system = self._sys + self._memories_block(hits)

        messages = [{"role": "system", "content": system}]
        messages += self.history[-self.history_turns * 2 :]
        messages.append({"role": "user", "content": user_text})

        raw = llm.chat(messages)
        result = self._parse(raw)
        result.memories = hits

        # 助手腔检测，命中一次就带着纠正重生成
        max_retry = int(self.cfg.get("guard", {}).get("max_retry", 1))
        for _ in range(max_retry):
            bad = self._violates_guard(result.text) if result.text else "empty"
            if not bad:
                break
            messages.append({"role": "assistant", "content": raw})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "刚才那句出戏了，带了不像她会说的话。"
                        "重来一次，只输出 JSON，别解释。"
                    ),
                }
            )
            raw = llm.chat(messages, temperature=0.95)
            result = self._parse(raw)
            result.memories = hits
            result.retried = True

        # 写进短期与长期记忆
        self.history.append({"role": "user", "content": user_text})
        self.history.append({"role": "assistant", "content": result.text})
        if result.text:
            self.memory.remember(user_text, result.text)

        return result
