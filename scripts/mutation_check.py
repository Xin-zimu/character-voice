#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""mutation_check.py — 验证「人格卡回归真能拦住回流」

为什么需要这个：`test_12` 第一版只查字符串在不在，删掉段落、加回旧规则都能过，
是人工审阅查出来的，不是测试。后来 `test_12b` 修的是同一类问题——只认具体文案。
所以这里把当时手做的变异固化成脚本：**改动人格卡 → 跑测试 → 必须变红 → 还原**。

用法（默认复制当前工作文件与测试语料，在临时副本中变异）：
    python3 scripts/mutation_check.py
    python3 scripts/mutation_check.py --only M7 M60 M61 M62 M63

主工作区不参与变异；不复制共同经历、out、凭据和 Git 数据。
正常退出或 Ctrl+C 清理副本；强杀进程可能留下临时目录，但不会改坏源文件。
每项完整测试输出保存在 out/mutation-<时间戳>/，不只保留第一条失败信息。

判据：每个变异必须让其指定的检查出现断言失败。仍然全绿记为未拦住；
变异产物损坏记为失效；运行时崩溃或无断言证据记为未确证，均以非 0 退出。

注意：这不是「测试通过」的证据，而是「测试会失败」的证据。两者都要有。
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PERSONAS = ROOT / 'personas'
REPORT_DIR = None


def test_env():
    # 子进程管道在 Windows 也固定 UTF-8；禁写字节码，避免连续变异读到旧缓存。
    return dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8',
                PYTHONDONTWRITEBYTECODE='1')


def copy_snapshot(source, target):
    """复制磁盘上的当前文件（包含未提交改动），不依赖 Git，也不使用硬链接。"""
    source, target = source.resolve(), target.resolve()
    if target == source or source in target.parents:
        raise ValueError('变异副本必须在源工作区之外')
    target.mkdir(parents=True, exist_ok=False)
    manifest = {}

    def copy_path(src):
        if src.is_symlink() or getattr(src, 'is_junction', lambda: False)():
            raise ValueError(f'拒绝复制链接：{src.relative_to(source)}')
        if src.name == '__pycache__' or src.name.startswith('.'):
            return
        rel = src.relative_to(source)
        if src.is_dir():
            for child in src.iterdir():
                copy_path(child)
        elif src.suffix in {'.py', '.js', '.mjs', '.json', '.jsonl', '.md', '.html', '.css', '.txt'}:
            dest = target / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            before = hashlib.sha256(src.read_bytes()).hexdigest()
            shutil.copy2(src, dest)
            if before != hashlib.sha256(dest.read_bytes()).hexdigest():
                raise RuntimeError(f'复制时源文件发生变化：{rel}')
            manifest[rel.as_posix()] = before

    for path in source.iterdir():
        if path.is_file() and path.suffix in {'.py', '.md', '.mjs'}:
            copy_path(path)
    for name in ('personas', 'scripts', 'web', 'docs', 'data/index',
                 'data/parsed', 'data/_deprecated'):
        path = source / name
        if path.exists():
            copy_path(path)
    data = source / 'data'
    if data.exists():
        for path in data.glob('*.md'):
            copy_path(path)
    # 拒绝混合快照。复制过程中有并发编辑就停止，之后的源文件修改不影响副本。
    for name, digest in manifest.items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError(f'复制时源文件发生变化：{name}')
    return manifest


def save_output(name, output):
    if REPORT_DIR is not None:
        (REPORT_DIR / f'{name}.log').write_text(output, encoding='utf-8')


def failure_evidence(kind, output):
    """非零退出只是必要条件；运行时崩溃／未完成测试不证明断言拦住了变异。"""
    if 'Fatal Python error:' in output:
        return None
    if kind in {'backend', 'scene', 'textart', 'artbehavior'}:
        if not re.search(r'^FAILED \(', output, re.M):
            return None
        pattern = r'^FAIL: .+'
    elif kind == 'frontend':
        if 'AssertionError' not in output:
            return None
        pattern = r'^FAIL: .+'
    elif kind == 'selfcheck':
        if not re.search(r'合计错误 [1-9]\d* 条', output):
            return None
        pattern = r'^\s*✗ .+'
    elif kind == 'sources':
        if not re.search(r'[1-9]\d* 条对不上', output):
            return None
        pattern = r'^\s*× .+'
    else:
        return None
    match = re.search(pattern, output, re.M)
    return match.group().strip() if match else None


def path_of(name):
    """变异对象的真实路径。

    早先这里只有「.py 在根目录、其余在 personas/」两种，加了 `web/index.html`
    之后那条规则就不够用了——`web/index.html` 不以 .py 结尾，会被拼到
    personas 下面去，读文件直接报错。集中成一个函数，别再散在四处各写一遍。

    M50 要改 `data/seed_nagato_lines.md`，又是同一个坑的第三次：它不在
    personas/ 下，也不以 .py 结尾。判据改成「**人格卡只有那两张 .json**，
    其余一律按仓库相对路径」，这样新增任何非卡文件都不用再动这里。
    """
    if name.endswith('.json') and '/' not in name:
        return PERSONAS / name
    return ROOT / name


def is_text_source(name):
    """可以被当成文本源码直接替换的文件（相对于人格卡 JSON）。

    和 `path_of()` 用同一份判断依据：**非根目录 .json 的都是源码**，
    其余是人格卡。两处判断必须一致，否则「按 A 取路径、按 B 选处理方式」，
    就会像 M33～M35 那样去 personas/web/ 找文件。
    """
    return not (name.endswith('.json') and '/' not in name)


def load(name):
    return json.loads((PERSONAS / name).read_text(encoding='utf-8'))


def save(name, data):
    (PERSONAS / name).write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


# ---------------------------------------------------------------
# 变异定义：每个都是「把已废止的行为加回去」或「干掉一条仍在起作用的规则」
# ---------------------------------------------------------------

def m_add_advice_without_ask(d):
    d['examples'].append({'user': '我明天要考试，好慌',
                          'assistant': '先看三章，再看剩下的。',
                          'source': 'handwritten'})
    return d


def m_advice_on_emotion(d):
    for e in d['examples']:
        if e['user'] == '我有点难过':
            e['assistant'] = '那你可以先去睡一觉。'
    return d


#: 与 `scripts/test_backend.py::test_12b` 里的 `ASK` 保持同一份判据。
#: 变异脚本要删掉的是「测试认为是求助」的那些示例，两边定义分叉就会出现
#: 「删了但删的不是求助」——上一版 M3 就是这么被误判成「测试没拦住」的。
ASK_RE = re.compile(r'怎么办|该不该|要不要|怎么才能|你说我|帮我|能不能帮|给我个建议')


def _is_asking(user_text):
    """求助必须落在最后一句（与 test_12b 同名函数同一语义）。"""
    tail = [c for c in re.split(r'[，。！？；\n]', user_text) if c.strip()]
    return bool(tail) and bool(ASK_RE.search(tail[-1]))


def m_drop_ask_example(d):
    """删掉**全部**「用户明确求助」的示例。

    第一版只删含「怎么办 / 该不该」的那一条，但 `帮我…` 也是求助，
    删完还剩示例 → 正确行为，测试全绿，脚本却报「未拦住」。
    那是变异本身写错了，不是测试的漏洞；把「变异没生效」读成「测试没拦住」
    会让人误以为补了一条根本不存在的缺口。
    所以这里直接复用测试的判据来挑要删的示例。
    """
    d['examples'] = [e for e in d['examples'] if not _is_asking(e['user'])]
    return d


def m_advice_chain_on_ask(d):
    for e in d['examples']:
        if '怎么办' in e['user']:
            e['assistant'] = '先看三章，再复习一遍，然后早点睡。'
    return d


def m_restore_unconditional_advice(d):
    old = '- 指出问题是陈述，不是派任务：不附带「你该怎么做」。要不要怎么办，看他有没有问。'
    # replace 匹配不到时不会报错，变异就变成空操作 —— 测试照样全绿，
    # 脚本却打印「已拦住」，等于伪造一条保护证据。所以必须显式断言命中。
    assert old in d['system_prompt'], '变异失效：找不到那条「指出问题是陈述」规则'
    d['system_prompt'] = d['system_prompt'].replace(
        old, '- 发现问题就直接给建议或直接说该做什么。')
    return d


def m_restore_assistant_framing(d):
    old = '你和对方之间是一条持续的通讯，不是一次性的问答。你不太有自信，但会认真去听。'
    assert old in d['system_prompt'], '变异失效：崩溃卡里找不到改写后的身份句'
    d['system_prompt'] = d['system_prompt'].replace(
        old, '你在帮用户处理他提出的问题。你不太有自信，但会认真去做。')
    return d


def m_remove_social_section(d):
    """整段删掉【相处方式】——「删段落」这类绕过必须被拦住。"""
    sp = d['system_prompt']
    a = sp.index('【相处方式】')
    b = sp.index('【能力 — 这是设定，不是工具】')
    d['system_prompt'] = sp[:a] + sp[b:]
    return d


def m_restore_no_action_rule(d):
    """把「不要描述动作」加回 prompt，与 CHAT-007 相反。"""
    old = '可以用括号「（）」写动作'
    assert old in d['system_prompt'], '变异失效：卡里找不到括号动作规则'
    d['system_prompt'] = d['system_prompt'].replace(
        old, '不要描述动作。可以用括号「（）」写动作')
    return d


def m_broken_bracket_example(d):
    d['examples'].append({'user': '你看完了吗',
                          'assistant': '（把书合上还没有。',
                          'source': 'handwritten'})
    return d


def m_split_system_after_examples(src):
    """把示例后的收尾语改回单独一条 system —— 结构变异，改的是 .py 不是 .json。

    改前的形状：…… assistant「能。但不会。」→ system「现在开始真实通讯」→ user。
    非首条 system 在各家兼容实现里处理不一致，可能被当成助手消息的续写。
    `test_09b` 就是为这个形状写的，这里把它改回去看是否变红。
    """
    marker = '        msgs[0]["content"] = (\n'
    assert marker in src, '变异失效：prompt_builder 里找不到收尾语拼接处'
    head = src.index(marker)
    tail = src.index('\n\n    msgs.extend(format_history(history))', head)
    replacement = (
        '        msgs.append({"role": "system", "content": "以上是语气示例。"\n'
        '                     "现在开始真实通讯，同样的说话方式，同样的规则。"})')
    return src[:head] + replacement + src[tail:]


def m_history_system_back(src):
    """把截断说明改回插进对话流的 system 条目（第二条 system）。"""
    marker = "    tail = out[-MAX_TURNS * 2:]\n    while tail and tail[0][\"role\"] != \"user\":\n        tail.pop(0)\n    return tail"
    assert marker in src, '变异失效：format_history 结尾形状变了'
    repl = ("    tail = out[-MAX_TURNS * 2:]\n"
            "    while tail and tail[0][\"role\"] != \"user\":\n"
            "        tail.pop(0)\n"
            "    return tail + [{\"role\": \"system\", \"content\": \"（更早的对话已省略。）\"}]")
    return src.replace(marker, repl, 1)


# ---------------------------------------------------------------
# 记忆（CHAT-016 / CHAT-018）：改的是 memory.py 与 server.py
# ---------------------------------------------------------------

def m_memory_ignore_world(src):
    """检索时不再按世界过滤 —— WORLD-001 的直接破口。"""
    marker = '            if rec.get("status") != "active" or rec.get("world") != self.world:'
    assert marker in src, '变异失效：memory.search 的过滤条件形状变了'
    return src.replace(marker, '            if rec.get("status") != "active":', 1)


def m_memory_default_world_normal(src):
    """世界名缺失时静默落到 normal，而不是报错。"""
    marker = 'world = _world_arg(req.get("world") or "")'
    assert marker in src, '变异失效：server 里找不到记忆接口的世界校验'
    return src.replace(marker, 'world = _world_arg(req.get("world") or "") or "normal"', 1)


def m_memory_keep_both_versions(src):
    """纠正时旧记录仍留在 active —— 互斥版本同时生效（V-MEM-03）。"""
    marker = 'old["status"] = "retired"'
    assert marker in src, '变异失效：memory.add 里找不到退下旧记录的那一行'
    return src.replace(marker, 'old["status"] = "active"', 1)


def m_memory_nonatomic_write(src):
    """改成直接覆盖写：中途失败会留下半个文件（V-MEM-04）。"""
    marker = '        os.replace(tmp, path)'
    assert marker in src, '变异失效：_atomic_write 里找不到 os.replace'
    body = ('        with open(tmp, "r", encoding="utf-8") as f:\n'
            '            text = f.read()\n'
            '        with open(path, "w", encoding="utf-8") as f:\n'
            '            f.write(text[: len(text) // 2])')
    return src.replace(marker, body, 1)


def m_memory_broken_file_reads_empty(src):
    """文件坏了当作「没有记忆」，而不是报错 —— 记忆静默丢失。"""
    marker = '                raise MemoryError_(f"记忆文件损坏且没有备份：{e}") from e'
    assert marker in src, '变异失效：_load_raw 的损坏分支形状变了'
    return src.replace(marker, '                return self._empty(), warns', 1)


# ---------------------------------------------------------------
# 场景状态（CHAT-013 / CHAT-008）：改的是 scene.py 与 server.py
# ---------------------------------------------------------------

def m_scene_body_counts_as_hers(src):
    """把用户正文也当成她自己的动作 —— 主语弄反。

    CHAT-013-B 第一条：「我把眼镜摘了」说的是用户。变异之后用户随便说一句
    「我摘了眼镜」，她的状态就被改了，后面所有轮次都按错的设定说话。
    """
    marker = '    for action in _bracket_actions(text):'
    assert marker in src, '变异失效：parse_updates 的括号遍历形状变了'
    return src.replace(marker, '    for action in _bracket_actions(text) + [text]:', 1)


def m_scene_no_exemptions(src):
    """去掉否定 / 假设 / 疑问的豁免 —— 反问也会改状态。

    **第一版这个变异没被拦住，但那不是测试的漏洞，是变异写错了。**
    第一版用的断言句是「（别摘眼镜）」，而「摘」在这里既是否定词后面的动作，
    也是 `_GLASSES_OFF` 的模式本身；去掉豁免后它仍然匹配不上，
    所以状态没变、测试全绿。真正的漏洞要换成否定/疑问词与动作词**分开**的句子
    （「别把书合上」「有没有翻开书」）才暴露得出来——而这类句子当时确实会改错状态，
    是补这条变异时查出来的真 bug。见 `test_scene.py::test_03a` / `test_03b`。
    """
    marker = ('    return not (_QUESTION.search(clause) or _HYPOTHETICAL.search(clause)\n'
              '                or _A_NOT_A.search(clause) or _NEGATED_ONLY.search(clause))')
    assert marker in src, '变异失效：_safe_clause 的形状变了'
    return src.replace(marker, '    return True', 1)


def m_scene_shared_state_across_worlds(src):
    """两个世界共用一份状态 —— 切换世界会继承另一边的眼镜和场景。

    **变异本身要挑对形态。** 第一版是「两个世界都指向新建的 Scene('normal')」，
    测试全绿——因为它让崩溃世界的默认值也变成了主线的默认值，
    而用例里两边被改成的值恰好一样，看不出差别。那不是测试的漏洞。
    这一版改成「崩溃世界复用**主线那个已经存在的** Scene 对象」：
    主线改什么，崩溃世界立刻跟着变，这才是真实会出现的错法。
    """
    marker = '        return self._scenes[world]'
    assert marker in src, '变异失效：SceneRegistry.get 的形状变了'
    body = ('        return self._scenes["normal"] if world == "crash" '
            'else self._scenes[world]')
    return src.replace(marker, body, 1)


def m_scene_unknown_as_known(src):
    """未知项也写出来 —— 没有记录的地方开始编。

    变异后书的状态未记录时会输出「book：没有记录」这类占位，
    这正是 CHAT-013 末条禁止的「编造」。
    """
    marker = '            if value is None:\n                continue'
    assert marker in src, '变异失效：Scene.render 的未知项分支形状变了'
    return src.replace(
        marker,
        '            if value is None:\n'
        '                lines.append(f"- {key}：没有记录")\n                continue', 1)


def m_scene_together_on_any_coplay(src):
    """「一起……」就算同处一室 —— CHAT-008 明说不是。"""
    marker = ('        if not m or not _arrived(clause, m):\n'
              '            continue')
    assert marker in src, '变异失效：共同场景判据的形状变了'
    return src.replace(marker, '        if "一起" not in clause:\n            continue', 1)


def m_scene_thinking_counts_as_being_there(src):
    """「我在想活动室」被当成「我在活动室」——把「身处」和「正在想」混为一谈。

    初版的到达表述写作 `我(已经)?(到|在|进)了?`，`了?` 可以省略，
    于是「我在想活动室的事啊」直接把状态置成同处一室。
    这类误判很伤：之后她会一直按「面对面」说话。
    """
    marker = '        if not m or not _arrived(clause, m):'
    assert marker in src, '变异失效：共同场景判据的形状变了'
    body = ('        if not m or (not _ARRIVE_STRONG.search(clause) '
            'and "在" not in clause):')
    return src.replace(marker, body, 1)


def m_scene_skipped_in_compose(src):
    """组装 prompt 时不再传场景状态 —— 这一层等于没接上。"""
    marker = '                                     memories=memories, scene=scene_block)'
    assert marker in src, '变异失效：_compose 里找不到 build_messages 的调用'
    return src.replace(marker, '                                     memories=memories)', 1)


def m_history_dropped(src):
    """历史整段不再随请求发送 —— 指代无从接住（CHAT-010）。

    这不是「回答变差」而是「对话断成单轮」：每句话都成了第一句话。
    """
    marker = '    msgs.extend(format_history(history))'
    assert marker in src, '变异失效：build_messages 里找不到历史拼接'
    return src.replace(marker, '    pass  # 历史丢弃', 1)


def m_history_into_system(src):
    """历史被塞进第一条 system —— 每轮都会重复累积，且挤掉人格的位置。"""
    marker = '    msgs.extend(format_history(history))'
    assert marker in src, '变异失效：build_messages 里找不到历史拼接'
    body = ("    msgs[0]['content'] += '\\n' + '\\n'.join("
            "m['content'] for m in format_history(history))")
    return src.replace(marker, body, 1)


# (编号, 说明, 文件名, 变异函数, 期望拦住它的回归项)
# `'selfcheck'` = scripts/check_persona.py；`'backend'` = scripts/test_backend.py。
# 写错期望项等于记录一条假的保护证据，所以这里不写「任意一项红即可」。
# ---------------- 字符画（CHAT-017 / DEC-013） ----------------

def m_art_everywhere(src):
    """字符画对所有世界放开 —— 主线那句「不要分段」就和它打架了。"""
    marker = 'ART_ALLOWED_WORLDS = {"normal": False, "crash": True}'
    assert marker in src, '变异失效：找不到 ART_ALLOWED_WORLDS'
    return src.replace(marker, 'ART_ALLOWED_WORLDS = {"normal": True, "crash": True}', 1)


def m_art_off_prompt_dropped(src):
    """主线世界不再说明「不画」—— 模型只能自己猜，猜错就画。"""
    marker = '             ART_PROMPT if ART_ALLOWED_WORLDS.get(world) else ART_OFF_PROMPT]'
    assert marker in src, '变异失效：找不到字符画那段的组装'
    return src.replace(marker, '             ]', 1)


def m_art_quota(src):
    """把「可以画」改成按轮次配额 —— DEC-007 明令禁止的那种写法。"""
    marker = '- 想画就画，不想画就只说话。'
    assert marker in src, '变异失效：找不到 ART_PROMPT 的写法说明'
    return src.replace(marker, '- 每三条至少画一次，不想画也要画。', 1)


def m_art_any_line_is_art(src):
    """「多行就算画」—— 普通分段会被当成图案走保空白那条路。"""
    marker = '    return any(any(_signals(ln)) for ln in lines)'
    assert marker in src, '变异失效：找不到 is_art 的迹象判断'
    return src.replace(marker, '    return True', 1)


def m_art_no_width_limit(src):
    """去掉「每行都要窄」—— 一整段话被折行也算画。"""
    marker = '    if any(text_width(ln) > _ART_MAX_WIDTH for ln in lines):\n        return False\n'
    assert marker in src, '变异失效：找不到宽度上限那段'
    return src.replace(marker, '', 1)


def m_art_strip_trim(src):
    """前缀剥离退回到 trim —— 图案第一行的缩进会被吃掉。"""
    marker = '    return s.rstrip("\\n").lstrip("\\n")'
    assert marker in src, '变异失效：找不到 strip_prefix 的收尾'
    return src.replace(marker, '    return s.strip()', 1)


def m_art_python_indent_unchecked(src):
    """不再报「左边距不一致」—— 歪掉的图案安静通过。"""
    marker = '    if len(set(indents)) > 1:'
    assert marker in src, '变异失效：找不到左边距判断'
    return src.replace(marker, '    if False:', 1)


def m_art_frontend_indent_unchecked(src):
    """前端不再报左边距 —— 契约的另一半没了（DEC-006 的五处同步）。"""
    marker = '    if(indents.length > 1) probs.push('
    assert marker in src, '变异失效：前端找不到左边距判断'
    return src.replace(marker, '    if(false) probs.push(', 1)


def m_art_frontend_typewriter(src):
    """图案也走逐字 —— 中途是一堆散字符。"""
    marker = '        if(!artMode && (looksLikeArtPrefix(text) || isArt(text))){ artMode = true; shown = target.length; }'
    assert marker in src, '变异失效：找不到打字机的图案分支'
    return src.replace(marker, '', 1)


def m_art_frontend_strip_trim(src):
    """前端前缀剥离退回到 trim —— 与 Python 那一侧不一致。"""
    marker = '    return s.replace(/\\n+$/, "").replace(/^\\n+/, "");'
    assert marker in src, '变异失效：前端找不到 stripPrefix 的收尾'
    return src.replace(marker, '    return s.trim();', 1)


def m_art_kind_reversed(src):
    """kind 反了：主线说可以画、崩溃说不行 —— 前端据此决定保空白。"""
    marker = '"kind": "chat" if art_allowed else "plain",'
    assert marker in src, '变异失效：找不到 kind 字段'
    return src.replace(marker, '"kind": "plain" if art_allowed else "chat",', 1)


def m_art_no_blocks(src):
    """分块退回到「整条一个块」——文字行会被压成一行、句号被挤下去。"""
    marker = '    blocks = []\n    i = 0\n    while i < n:'
    assert marker in src, '变异失效：找不到 art_blocks 的主循环'
    return src.replace(marker, '    return [(False, lines)]\n' + marker, 1)


def m_art_block_single_line(src):
    """单行像图案也起块 —— 一整行 ==== 会换一套排版。"""
    marker = '        if j > i and has_hard:\n            blocks.append([True, lines[i:j + 1]])'
    assert marker in src, '变异失效：找不到起块判定'
    return src.replace(marker, '        if True:\n            blocks.append([True, lines[i:j + 1]])', 1)


def m_art_frontend_no_blocks(src):
    """前端分块退回到整条一块 —— 两端分块不一致，文字行被压成一行。"""
    marker = '    for(const [kind, chunk] of artBlocks(text)){'
    assert marker in src, '变异失效：找不到 renderBlocks 的分块循环'
    return src.replace(marker, '    for(const [kind, chunk] of [[false, String(text).split("\\n")]]){', 1)


def m_art_frontend_multi_keeps_pre(src):
    """`.txt.multi` 忘了把空白恢复成 normal —— 文字块仍被压成一行。"""
    marker = '  .line .txt.multi{white-space:normal}'
    assert marker in src, '变异失效：找不到 .txt.multi 的样式'
    return src.replace(marker, '  .line .txt.multi{white-space:pre-wrap}', 1)


def m_art_selfcheck_no_art_exemption(src):
    """自检模拟器漏掉「字符画不算分段」——两端 checkReply 不再等价。

    这是独立复审查出来的真问题：前端改了、模拟器没改，自检报「分段」
    而界面不报。当时**没有任何用例比对这一条**，所以全绿也不能说明它对。
    """
    # 用原始字符串写锚点：源码里的字面量就是 r"\n\s*\n"，
    # 写成普通字符串要多套一层转义，很容易写成匹配不上的样子。
    marker = r'    if re.search(r"\n\s*\n", reply) and not textart.has_art_block(reply):'
    assert marker in src, '变异失效：找不到 simulate_check_reply 的分段判定'
    return src.replace(marker, r'    if re.search(r"\n\s*\n", reply):', 1)


def m_art_space_counts_as_symbol(src):
    """空格又被算进「符号」——缩进两行的普通文字会被判成画。"""
    marker = r'    return (longest_run >= _ART_RUN and longest_has_symbol and dense,'
    assert marker in src, '变异失效：找不到 _signals 的返回语句'
    # 去掉「连续串必须含图案字符」这个条件，效果等于回到「空格也算符号」。
    return src.replace(marker, r'    return (longest_run >= _ART_RUN,', 1)


def m_art_frontend_space_counts_as_symbol(src):
    """前端那一侧同样把空格算成符号 —— 两端判据漂移。"""
    marker = '    return [max >= 3 && maxSym && dense, (line.match(/  /g) || []).length >= 2 && dense];'
    assert marker in src, '变异失效：找不到 artSignals 的返回语句'
    return src.replace(marker, '    return [max >= 3, (line.match(/  /g) || []).length >= 2];', 1)


def m_art_no_right_edge_check(src):
    """不再查右边缘不齐 —— 图案缺一块也没人说话。"""
    marker = '            if len(rights) >= 2 and max(rights) - min(rights) >= _ART_EDGE_GAP:'
    assert marker in src, '变异失效：找不到右边缘判定'
    return src.replace(marker, '            if False:', 1)


# ---------------------------------------------------------------
# B1～B4 修复的反向变异（2026-10-23）
#
# 每修一条缺陷，都要让它**能被改回去**：只加修复不加变异，
# 等于假设「以后没人会改坏它」，而那从来不是真的。
# ---------------------------------------------------------------
def m_ctx_not_filtered_by_world(src):
    """渲染时把整段 ctx 原样输出 —— 上下文行又跨世界（R-01 反向）。"""
    marker = '        ctx = filter_ctx_by_world(h.get("ctx", ""), h.get("ctx_worlds"), world)'
    assert marker in src, '变异失效：render_hits 里找不到逐行过滤那一行'
    return src.replace(marker, '        ctx = h.get("ctx", "")', 1)


def m_scene_bitwise_lock_only(src):
    """进程内锁退回实例字段 —— 多实例并发又静默丢记录（R-04 反向）。

    改的是按路径共享的那把锁：让它每次返回新锁，等价于原来
    「每个实例自己一把锁」，两个 store 之间就不再互斥。
    """
    marker = '        lock = _LOCKS.get(key)'
    assert marker in src, '变异失效：_lock_for 里找不到查表那一行'
    return src.replace(marker, '        lock = None', 1)


def m_restore_only_direct_successor(src):
    """恢复只看直接后继 —— 三代链会留下互斥版本（R-03 反向）。"""
    marker = '            chain = self._chain_of(data["records"], rid)'
    assert marker in src, '变异失效：restore 里找不到取整条链那一行'
    return src.replace(marker, '            chain = [rid, rec.get("retired_by") or rid]', 1)


def m_backup_failure_swallowed(src):
    """备份失败又吞掉 —— 用户以为有备份，其实没有（R-06 反向）。"""
    marker = '                    warn = f"备份失败（主记录已保存，但没有新的 .bak）：{e}"'
    assert marker in src, '变异失效：_save_raw 里找不到备份失败警告那一行'
    return src.replace(marker, '                    warn = None', 1)


def m_memory_subject_dropped(src):
    """共同经历渲染时不再标主体 —— 分不清「你记得我」和「我记得你」（R-07 反向）。

    锚点跟着 T3 的改动移到了 `head` 那一行（原来拼 `block` 的那行已经不存在）。
    2026-10-26 全量变异时正是这里报「变异失效」——**这是好事**：
    变异失效 ≠ 被拦住，脚本把它单独计数，没有混进「已拦住」。
    """
    marker = '        head = f"[{len(out) + 1}] {when} · {subject}："'
    assert marker in src, '变异失效：render_shared_memory 里找不到带主体的 head'
    return src.replace(marker, '        head = f"[{len(out) + 1}] {when} · "', 1)


def m_scene_reply_not_consumed(src):
    """她的回复不再更新状态 —— 说了摘下眼镜，下一轮还戴着（R-05 反向）。"""
    marker = '            self._commit_reply_scene(composed["world"], "".join(reply_parts))'
    assert marker in src, '变异失效：api_chat 里找不到收尾那一行'
    return src.replace(marker, '            pass  # 变异：不再消费她的动作', 1)


def m_art_exception_removed(src):
    """把「格式例外」那段从 ART_PROMPT 删掉 —— 与「不要分段」又打架（R-08 反向）。"""
    start_marker = '- **格式上的例外，只针对图案本身**'
    assert start_marker in src, '变异失效：ART_PROMPT 里找不到例外说明的开头'
    start = src.index(start_marker)
    # 这一段占三行（到「为准。」结束），按行删干净，不要留下半句。
    end = src.index('- 画的时候，图案单独占几行', start)
    return src[:start] + src[end:]


def m_empty_memory_rewrites_parent(src):
    """空状态说明改回 `innerHTML +=` —— 空库第一条又存不进去（R-02 反向）。"""
    marker = ('      const empty = document.createElement("p");\n'
              '      empty.className = "note";\n'
              '      empty.textContent = "还没有记下任何事。她说的话不会凭空多出记忆。";\n'
              '      body.appendChild(empty);')
    assert marker in src, '变异失效：paintMemory 里找不到空状态那段'
    repl = ("      body.innerHTML += '<p class=\"note\">还没有记下任何事。"
            "她说的话不会凭空多出记忆。</p>';")
    return src.replace(marker, repl, 1)


def m_example_invents_weather(card):
    """示例又编造天气 —— 规则说「绝不编造」，第一条示例却在编。"""
    key = '今天天气怎么样'
    hit = [e for e in card['examples'] if e['user'] == key]
    assert hit, '变异失效：人格卡里找不到问天气的示例'
    hit[0]['assistant'] = '晴。气温二十三度。'
    return card


def m_examples_share_filler(card):
    """示例共用一个犹豫前缀 —— 学到的不是语气，是口癖。"""
    ex = card['examples']
    assert len(ex) >= 5, '变异失效：示例太少，凑不出口癖'
    for e in ex[2:7]:
        e['assistant'] = '那个……' + e['assistant'].lstrip('那个……')
    return card


def m_examples_back_to_message_pairs(src):
    """把示例铺回 user/assistant 消息对 —— AUD-14 的缺陷原样回来。

    这是本轮最值得防的回退：从「能跑通」的角度看，把示例铺成消息对**更自然**
    （few-shot 的标准写法），而且工程测试全绿。但它会让模型把示例当成真实
    对话历史，实测 13～14 / 20 次答「你刚才也问过」——只有真实模型试聊
    才看得见，回归测试完全测不到结构后果。

    所以必须有一条测试在**结构层**挡住它：`test_09d_examples_are_not_message_pairs`。
    """
    marker = '            example_lines.append(f"对方：{u}")'
    assert marker in src, '变异失效：找不到示例折进 system 的那一行'
    old = ('    for ex in examples:\n'
           '        u, a = ex.get("user", ""), strip_prefix(ex.get("assistant", ""))\n'
           '        if u and a:\n'
           '            example_lines.append(f"对方：{u}")\n'
           '            example_lines.append(f"你：{a}")\n'
           '            sent += 1')
    new = ('    for ex in examples:\n'
           '        u, a = ex.get("user", ""), strip_prefix(ex.get("assistant", ""))\n'
           '        if u and a:\n'
           '            msgs.append({"role": "user", "content": u})\n'
           '            msgs.append({"role": "assistant", "content": a})\n'
           '            sent += 1')
    assert old in src, '变异失效：找不到示例处理循环的完整形状'
    return src.replace(old, new, 1)


def m_drop_self_history_rule(src):
    """CHAT-019 反向：删掉「关于你自己的经历」整条规则。

    这条是 36 次采样里唯一指向原因的那一条：卡里已经没有巧克力与做饭，
    她仍然答得出来，说明缺的是规则。删掉它，`test_12f` 必须红。
    """
    marker = '4. **关于你自己的经历，只有记录里有的才能说。**'
    assert marker in src, '变异失效：找不到关于自己的经历那条规则'
    start = src.index(marker)
    end = src.index('**一句话的判据**', start)
    return src[:start] + src[end:]


def m_drop_hits_are_not_evidence(src):
    """CHAT-019 反向：删掉「检索没有直接讲到就等于没有依据」。

    采样里巧克力、做饭两组的 k=5 命中全是噪声（BM25 把「什么时候」匹配到
    「他们是什么时候接近的？」），她照样给出具体细节。只留「按字面找出来的」
    不够，必须写明「没讲到这件事就等于没有依据」。
    """
    marker = '- 检索只是按字面相仿找出来的'
    assert marker in src, '变异失效：找不到检索片段不算依据那一句'
    start = src.index(marker)
    end = src.index('- 有关系时', start)
    return src[:start] + src[end:]


def m_example_for_disagreement_removed(card):
    """删掉「意见分歧」示例 —— 规则还在，但没人示范它（AUD-08 反向）。"""
    before = len(card['examples'])
    card['examples'] = [e for e in card['examples']
                        if 'disagrees' not in (e.get('source') or '')]
    assert len(card['examples']) < before, '变异失效：找不到意见分歧示例'
    return card


def m_verified_label_wrong_text(card):
    """把 verified 示例的台词改掉，标签还留着 —— 标签变成假的（AUD-10 反向）。

    `verified:<集>@<时间码>` 是对「逐字原文」的承诺。只保留标签、改掉台词，
    在格式检查里完全合法，只有真去索引里比对才能发现。
    """
    hit = 0
    for e in card['examples']:
        if str(e.get('source') or '').startswith('verified:'):
            e['assistant'] = '不需要在意我。'
            hit += 1
    assert hit, '变异失效：找不到 verified 示例'
    return card


def m_seed_dead_rule_back(text):
    """把作废的「可执行硬约束」原样写回种子文件正文（AUD-11 反向）。

    变异的是**纯文本**，走的是源码替换那条分支（`mutate(str) -> str`）。

    **AUD-11 证据链的断点（2026-10-26 审计）**：这个函数只让「有作废标记的行
    不许出现」那半条判据变红，对「作废标记本身丢了」这半条毫无作用——
    因为它是**插在**已有标记旁边，不是**去掉**标记。判据①②③里，
    真正被它碰到的只有「没标作废的限制性句子」这一条。

    所以锚点不只求"能替换成功"，还要**落在被保护的那一节里**：
    上面`## 从这批种子直接读出的角色规则`是那句硬约束原来所在的位置。
    把句子写回它自己那一节，才等于「规则回到了原来的地方」；
    写进别的章节只是又出现了一遍同样的字，测试拦住的可能是别的原因。
    """
    section = text.find('## 从这批种子直接读出的角色规则')
    assert section != -1, '变异失效：找不到「从这批种子直接读出的角色规则」这一节'
    nxt = text.find('\n## ', section + 1)
    assert nxt != -1, '变异失效：找不到下一节，无法确定这一节的边界'

    # 锚点取**它原本所在的位置**：统计表最后一行之后、作废说明之前。
    # 为什么不用「这一节的开头/结尾」：写在结尾等于写在「这条已作废」的
    # 解释之后，读者先看到作废说明——那不是这条规则当初的处境，变异也就
    # 弱化了。它当初就是在统计表下面，以可执行的口吻直接下达的。
    table_end = text.find('| 情绪词 | **0 个** |')
    assert table_end != -1, '变异失效：找不到统计表的最后一行'
    assert section < table_end < nxt, '变异失效：统计表不在「角色规则」这一节里'
    line_end = text.find('\n', table_end)
    assert line_end != -1, '变异失效：统计表最后一行没有换行'
    dead = ('\n**可执行的硬约束**：日常对话单句 ≤ 15 字；'
            '只有解释超自然现象时才允许长句，且必须零情绪词、零感叹号。\n')
    after = text[:line_end + 1] + dead + text[line_end + 1:]

    # 变异必须真的落在这一节里，而且**排在同一条规则的作废说明之前**。
    # 只查「这句话在文件里」不够：同一句在别处也有引用（「这份文件不再用于
    # 什么」表格里就引了一次），那样 `in` 判据会通过，规则其实没回到正文。
    pos = after.find(dead)
    assert section < pos < nxt, '变异失效：写回的位置不在原来那一节里'
    voided = after.find('~~**可执行的硬约束**')
    assert voided != -1, '变异失效：找不到那条被划掉的旧硬约束（判据的对象没了）'
    assert pos < voided, '变异失效：写回的位置在作废说明之后，等于没回到规则的位置'
    assert ('~~' not in dead) and ('作废' not in dead), '变异失效：写回时必须去掉作废标记'
    return after


def m_seed_dead_rule_unvoided(text):
    """把那条旧硬约束的**作废标记**抹掉，让它重新生效（AUD-11 反向）。

    M50 反的是「规则被写回正文」，它碰的是「没标作废的限制性句子」那半条。
    另一半——**作废标记本身丢了**——原来没有任何变异覆盖：把
    `~~**可执行的硬约束**…~~` 的删除线去掉、再把下面那句「这条硬约束已作废」
    改成中性描述，文件里就只剩一条以可执行口吻写着「单句 ≤ 15 字」的规则，
    而 M50 一个字都不用动。

    这正是审计指出的证据链断点：只证明「把规则抄回来会被拦住」，
    没证明「把标记抹掉会被拦住」，而后者才是 AUD-11 真正落在文件上的形式。
    """
    start = text.find('~~**可执行的硬约束**')
    assert start != -1, '变异失效：找不到被划掉的旧硬约束'
    line_end = text.find('\n', start)
    assert line_end != -1, '变异失效：那行没有结束'
    line = text[start:line_end]
    bare = line.replace('~~', '')
    assert bare != line, '变异失效：那一行本来就没有删除线'
    after = text[:start] + bare + text[line_end:]
    # 下面那段「这条硬约束已作废，且与现有规则直接冲突」也要一起中性化，
    # 否则读者仍能看出它作废了——变异就只改了半件事。
    voided_note = '**这条硬约束已作废，且与现有规则直接冲突**'
    assert voided_note in after, '变异失效：找不到作废说明那一行'
    after = after.replace(voided_note, '**补充说明**', 1)
    # 这一节里其余的「作废」字样一并抹掉，否则判据的死标记仍会放行整行。
    # 只在这一节内替换，避免动到文件末尾那张「不再用于什么」的表（那是
    # 另一条判据的对象，改了会让变异拦住的原因变得说不清）。
    section = after.find('## 从这批种子直接读出的角色规则')
    nxt = after.find('\n## ', section + 1)
    assert section != -1 and nxt != -1, '变异失效：定位不到那一节的边界'
    body = after[section:nxt]
    cleaned = body.replace('整节降级为历史记录 · 不是规则', '这一节的结论')
    cleaned = cleaned.replace('一条都不作为运行规则', '')
    cleaned = cleaned.replace('已作废', '已确认')
    cleaned = cleaned.replace('作废', '确定')
    assert cleaned != body, '变异失效：这一节里没有可被抹掉的作废字样'
    after = after[:section] + cleaned + after[nxt:]
    assert after != text, '变异失效：变异没有改变文件内容'
    return after


def m_memory_render_breaks_on_overflow(src):
    """退回「超预算就整块停」—— 超长记录又吞掉后面所有记忆（T3 反向）。

    这是修复前的**原样实现**：没有单条封顶，遇到第一条装不下的直接 `break`。
    它造成的不是「少了一条」，而是**这条之后的所有记忆都不进上下文**，
    而且没有任何警告。三个事实——「检索到了」「放进上下文了」「模型用上了」
    ——这一条管的是中间那个。
    """
    start_marker = '    out: list[str] = []\n    used = 0\n    for r in records:'
    assert start_marker in src, '变异失效：找不到候选循环的开头'
    body_start = src.index(start_marker)
    body_end = src.index('    body = "\\n".join(out)', body_start)
    legacy = (
        '    out: list[str] = []\n'
        '    used = 0\n'
        '    for i, r in enumerate(records, 1):\n'
        '        if r.get("world") != world:\n'
        '            continue\n'
        '        if r.get("status") not in (None, "active"):\n'
        '            continue\n'
        '        when = (r.get("created") or "")[:10]\n'
        '        subject = SUBJECT_LABEL.get(str(r.get("subject") or "user").lower(), "对方")\n'
        '        mark = SOURCE_MARK.get(_source_kind(r), "")\n'
        '        block = f"[{i}] {when} · {subject}：{r.get(\'text\', \'\')}{mark}"\n'
        '        if used + len(block) > max_chars:\n'
        '            break\n'
        '        out.append(block)\n'
        '        used += len(block)\n'
    )
    return src[:body_start] + legacy + src[body_end:]


def m_memory_render_ignores_budget(src):
    """预算不再生效 —— 超长记录整条塞进提示词（T3 反向，另一个方向）。

    修复「跳过超限项」时最容易顺手做过头：把预算判断去掉，什么都不丢，
    但 system 会被一条记录撑爆。这一条证明预算仍是真的。
    """
    marker = 'def _fit_memory_entry('
    assert marker in src, '变异失效：找不到 _fit_memory_entry'
    idx = src.index(marker)
    end = src.index('def render_shared_memory', idx)
    body = src[idx:end]
    assert 'budget' in body or 'max' in body, '变异失效：_fit_memory_entry 里没有预算参数'
    return (src[:idx]
            + 'def _fit_memory_entry(text, budget):\n'
              '    # 变异：不再压缩、不再截断，原样返回（预算等于不存在）\n'
              '    return text, False\n\n\n'
            + src[end:])


def m_cross_process_lock_removed(src):
    """把跨进程文件锁整层去掉 —— 多进程并发写又静默丢记录（R-04 反向）。

    与 M55 分工明确：M55 拆的是第 1 层（进程内、按路径共享），只证明
    **线程**用例有效；这一条拆第 2 层，只有**真的起进程**的用例看得见。
    Linux 上 `fcntl` 一直在，所以 Windows 那半条分支写错时，
    原来的回归在任何平台都不会红——这就是审计说的「Linux 的 fcntl 掩盖了
    Windows 锁失效」。

    **两个分支都要关掉**。第一版只把 `if fcntl is not None:` 改成
    `if False:`，而下面那条是 `elif msvcrt is not None:`——在 Windows 上
    它照样执行，变异根本没生效，于是「未拦住」被读成了「测试有漏洞」。
    实际是**变异写错了**：这正是本项目反复踩的那类坑（变异没生效 ≠ 没被拦住），
    所以这里显式断言两个分支都不再进入。
    """
    posix = '                if fcntl is not None:'
    win = '                elif msvcrt is not None:'
    assert posix in src, '变异失效：_mutate 里找不到 POSIX 文件锁分支'
    assert win in src, '变异失效：_mutate 里找不到 Windows 文件锁分支'
    src = src.replace(posix, '                if False:  # 变异：POSIX 文件锁去掉', 1)
    src = src.replace(win, '                elif False:  # 变异：Windows 文件锁去掉', 1)
    assert 'if fcntl is not None:' not in src, '变异失效：POSIX 分支还在'
    assert 'elif msvcrt is not None:' not in src, '变异失效：Windows 分支还在'
    return src


def m_windows_lock_never_unlocks(src):
    """Windows 分支锁了不解 —— 下一次写入卡到超时（R-04 反向的另一种写法）。

    这种错在 POSIX 机器上完全看不出来（那半根本不执行），在 Windows 上
    表现为「偶尔要卡十秒然后失败」，很容易被当成磁盘慢。
    """
    marker = ('                        msvcrt.locking(lock_handle.fileno(), '
              'msvcrt.LK_UNLCK, 1)')
    assert marker in src, '变异失效：找不到 Windows 解锁那一行'
    return src.replace(marker, '                        pass  # 变异：不解锁', 1)


def m_windows_lock_no_seed_byte(src):
    """Windows 分支不先写那 1 个字节 —— 空文件上锁不到任何东西。

    `msvcrt.locking` 锁的是文件里的**字节**。空文件从 0 字节处锁 1 字节
    会直接抛 OSError（permission denied），表现为「第一次写入就失败」。
    """
    marker = '                    if lock_handle.tell() == 0 and lock_path.stat().st_size == 0:'
    assert marker in src, '变异失效：找不到写种子字节那一行'
    return src.replace(marker, '                    if False:  # 变异：不写种子字节', 1)


def m_frontend_combining_narrow(text):
    """把前端 `checkArt` 的组合字符判据退回旧写法（只剩 0300-036f）。

    独立复核发现的真实不一致：排版用三块范围，`checkArt` 只用一块，
    于是「带希伯来元音点的行」在宽度上算 0 格、却查不出含组合字符。
    修法是两处共用一个 COMBINING 常量；这个变异证明那条共用被测试锁住了。
    """
    marker = 'if(isCombining(ch)){ hit = "组合字符"; break; }'
    assert marker in text, '变异失效：找不到 checkArt 的组合字符判断'
    return text.replace(marker, 'if(/[\\u0300-\\u036f]/.test(ch)){ hit = "组合字符"; break; }', 1)


def m_restore_unsupported_normal_claims(card):
    """PEND-012 反向：把无来源的性格结论和固定反应写回卡。"""
    card['system_prompt'] += ('\n- 对人的动机和情绪的推断常出错。'
                              '\n- 自我评价偏低。被夸奖时不接受也不反驳，直接跳过。')
    return card


def m_restore_unsupported_food_history(card):
    """PEND-013 反向：恢复未经确认的偏好、固定话题与往事。"""
    card['system_prompt'] += '\n喜欢甜食。会忍不住提到吃的。你做过巧克力，虽然不太成功。'
    return card


def m_restore_skip_praise_example(card):
    """只回退示例，不回退规则，证明示例自身的防回流断言有效。"""
    example = next(e for e in card['examples'] if e['user'] == '你好厉害')
    example.update(assistant='……', source='reading:self-deprecating-skip')
    return card


def m_art_density_dropped(src):
    """符号密度条件被删掉 —— 缩进代码又被当成字符画（2026-10-26 的误判）。"""
    marker = "    dense = bool(nonspace) and symbols * 3 >= nonspace"
    assert marker in src, '变异失效：找不到符号密度判定'
    return src.replace(marker, "    dense = True", 1)


def m_art_frontend_density_dropped(src):
    """前端删掉密度条件 —— 两端对「缩进代码是不是画」分叉。"""
    marker = "    const dense = nonspace > 0 && symbols * 3 >= nonspace;"
    assert marker in src, '变异失效：前端找不到密度判定'
    return src.replace(marker, "    const dense = true;", 1)


def m_art_blank_line_splits_again(src):
    """图案内部空行又拆块 —— 下边框重新跳到左边（2026-10-26 修掉的显示 bug）。"""
    marker = "            if k > j and k + 1 < n and kinds[k + 1]:"
    assert marker in src, '变异失效：找不到空行跨块的判定'
    return src.replace(marker, "            if False and k > j and k + 1 < n and kinds[k + 1]:", 1)


def m_art_frontend_blank_line_splits_again(src):
    """前端图案内部空行又拆块 —— 与 Python 分叉，下边框错位。"""
    marker = "        if(k > j && k + 1 < lines.length && kinds[k + 1]){ j = k; continue; }"
    assert marker in src, '变异失效：前端找不到空行跨块的判定'
    return src.replace(marker, "        if(false){ j = k; continue; }", 1)


def m_art_freeform_indent_reported_again(src):
    """左边距退回「所有行都要一样」—— 自由形状又全被标歪（旧的误报）。"""
    marker = "        if len(ln) > n and ln[n] in _FRAME_CHARS:"
    assert marker in src, '变异失效：找不到框线字符判定'
    return src.replace(marker, "        if len(ln) > n:", 1)


def m_art_frontend_freeform_indent_reported_again(src):
    """前端左边距退回「所有行都要一样」—— 两端一起误报。"""
    marker = "      .filter(x => FRAME_CHARS.has(x.rest))"
    assert marker in src, '变异失效：前端找不到框线字符判定'
    return src.replace(marker, "      .filter(() => true)", 1)


def m_art_segment_needs_whole_art(src):
    """分段豁免退回「整条都得是图案」—— 文字 + 空行 + 小图又被报分段。"""
    marker = "if(/\\n\\s*\\n/.test(text) && !hasArtBlock(text)) probs.push(\"分段\");"
    assert marker in src, '变异失效：前端找不到分段判定'
    return src.replace(marker, "if(/\\n\\s*\\n/.test(text) && !isArt(text)) probs.push(\"分段\");", 1)


def m_art_segment_needs_whole_art_py(src):
    """自检那边同样退回整条判定 —— 两端对同一条回复给出不同的违例。"""
    marker = r'    if re.search(r"\n\s*\n", reply) and not textart.has_art_block(reply):'
    assert marker in src, '变异失效：找不到自检的分段判定'
    return src.replace(marker, r'    if re.search(r"\n\s*\n", reply) and not textart.is_art(reply):', 1)


def m_art_frontend_text_block_collapses(src):
    """文字块退回 white-space:normal —— 文字与图案之间的空行被折叠（2026-10-26）。"""
    marker = "  .line .txt .blk{display:block;margin:0;white-space:pre-wrap}"
    assert marker in src, '变异失效：找不到文字块的样式'
    return src.replace(marker, "  .line .txt .blk{display:block;margin:0}", 1)


def m_art_face_line_requires_long_run(src):
    """`_art_line_kind` 退回「只认 hard」—— 顶格的脸又把整只猫拆散（2026-10-26 复采）。

    这一条锁的是软判据本身：把 `soft` 分支删掉，`test_49` 的顶格猫必须红。
    不能只删 `soft` 的「至少 2 个图案字符」半句——那样 `( o.o )` 依然能进，
    变异就是空操作。
    """
    marker = '    if (text_width(line) <= _SHORT_ART_WIDTH and nonspace\n' \
             '            and symbols >= 2 and symbols * 3 >= nonspace):\n' \
             '        return "soft"\n'
    assert marker in src, '变异失效：找不到 soft 行的判据'
    return src.replace(marker, '    return ""\n', 1)


def m_art_frontend_face_line_requires_long_run(src):
    """前端同样退回只认 hard —— 两端对「顶格的脸算不算图案」分叉。"""
    marker = "    if(widthOk && nonspace > 0 && symbols >= 2 && symbols * 3 >= nonspace) return 'soft';"
    assert marker in src, '变异失效：前端找不到 soft 行的判据'
    return src.replace(marker, "    return '';", 1)


def m_art_soft_lines_can_start_block(src):
    """`soft` 行可以自己起块 —— 两行普通话被换成保空白排版（收紧过头的反向）。

    这一条锁「soft 不能独自成块」：把 `has_hard` 去掉，`test_50`
    里那两行「带点符号的日常话」必须变成图案块。
    """
    marker = "        has_hard = any(kinds[x] == \"hard\" for x in range(i, j + 1))"
    assert marker in src, '变异失效：找不到 soft 必须有 hard 的判定'
    return src.replace(marker, "        has_hard = True", 1)


def m_art_soft_swallows_table_rows(src):
    """`soft` 不再排除框线开头的行 —— 表格行被卷进图案块（PEND-011 的取舍被改掉）。"""
    marker = '    if left[:1] in _FRAME_CHARS:'
    assert marker in src, '变异失效：找不到框线开头的排除条件'
    return src.replace(marker, '    if False:', 1)


def m_corpus_load_without_lock(src):
    """`load_corpus()` 退回无锁的「先查后建」—— 并发时构造多份索引。

    这是 2026-10-26 查出来的一次真实崩溃的根因：全量变异连跑两遍各崩一次，
    崩的位置还是随机的。去掉锁之后，`test_03b` 会看到 6 个线程构造 6 份。
    """
    marker = ('    if CORPUS is not None:          # 快路径：已加载时不进锁\n'
              '        return CORPUS\n'
              '    with _CORPUS_LOCK:\n'
              '        if CORPUS is None:          # 双重检查：等锁期间可能已被别人建好\n'
              '            CORPUS = Corpus(INDEX)\n'
              '        return CORPUS')
    assert marker in src, '变异失效：找不到 load_corpus 的加锁实现'
    return src.replace(marker, '    if CORPUS is None:\n        CORPUS = Corpus(INDEX)\n'
                               '    return CORPUS', 1)



def m_inline_visual_removed(src):
    """行内视觉元素判据被删掉 —— 数不到「一行嵌在正文里的方块走势图」。

    这是 2026-10-26 第三轮真实发生过的漏数：E5-info-2 turn3 回了
    `▁▂▄▆█▆▂▁▃▅█` 接在正文里，`art_blocks()` 判不出来。
    把阈值抬到 99 就等于「永远不认」，`test_01_inline_chart_is_counted` 会红。
    """
    marker = '"inline_visual": inline_run >= 5 and not blocks,'
    assert marker in src, '变异失效：找不到 inline_visual 的判据'
    return src.replace(marker, '"inline_visual": inline_run >= 99 and not blocks,', 1)


def m_inline_counts_pure_rule(src):
    """纯框线的连续段也算行内 —— 表格边框被当成「她在用图形表达」。

    这是新判据第一版的真实误报：`┌────┐` 是一行连续 6 个框线字符，
    只看长度完全够门槛。去掉 `_is_pure_rule` 的排除之后，
    `test_02_table_row_is_neither` / `test_05b` 会红。
    """
    marker = '                if cur > best and not _is_pure_rule(run_text):'
    assert marker in src, '变异失效：找不到纯框线的排除条件'
    return src.replace(marker, '                if cur > best:', 1)


def m_e6_plain_arm_keeps_art(src):
    """「没画过」那一臂忘了删图案块 —— 两臂的自变量不再是「画没画」。

    E6 的全部意义在两臂只差一张图。如果 `strip_art_block()` 直接返回原文，
    两臂的 seed 就一模一样，`test_10` / `test_11` 会红。
    """
    marker = ('    kept = []\n'
              '    for kind, chunk in textart.art_blocks(reply):\n'
              '        if kind:\n'
              '            continue\n'
              '        kept.extend(chunk)')
    assert marker in src, '变异失效：找不到 strip_art_block 的删块循环'
    return src.replace(marker, '    return reply\n' + marker, 1)


def m_recount_trusts_stale_stats(src):
    """重算沿用存档里的旧统计字段 —— 换了判据也不会重判。

    2026-10-26 第三轮的真实情形：66 条采样跑完时 `stats.json` 还没有
    `inline_visual`（判据是事后补的）。若重算不按 `reply` 重判，
    存档里的数字会永远停在旧判据上，而「原始回复」这个证据就白存了。
    `test_13` 会红。
    """
    marker = ('    for r in rows:\n'
              '        # 原始回复是唯一真源：统计字段全部按当前判据重算，不信任存档里的旧值。\n'
              '        r.update(analyse(r.get("reply") or ""))')
    assert marker in src, '变异失效：找不到 recount 的重判循环'
    return src.replace(marker, '    for r in rows:\n        pass', 1)


MUTATIONS = [
    ('M1', '用户只表达情绪，回答却给方案（旧「好慌→看三章」）',
     'yuki.json', m_add_advice_without_ask, 'backend'),
    ('M2', '把「难过」示例改成给方案', 'yuki.json', m_advice_on_emotion, 'backend'),
    ('M3', '删掉全部「明确求助」示例', 'yuki.json', m_drop_ask_example, 'backend'),
    ('M4', '求助示例给一串建议', 'yuki.json', m_advice_chain_on_ask, 'backend'),
    ('M5', '把「发现问题直接指出」改回无条件给建议', 'yuki.json',
     m_restore_unconditional_advice, 'backend'),
    ('M6', '整段删掉【相处方式】', 'yuki.json', m_remove_social_section, 'backend'),
    ('M7', '崩溃卡恢复「帮用户处理他提出的问题」', 'yuki_school.json',
     m_restore_assistant_framing, 'backend'),
    ('M8', '把「不要描述动作」加回 prompt（CHAT-007 反向）', 'yuki.json',
     m_restore_no_action_rule, 'backend'),
    ('M9', '新增括号不配对的示例', 'yuki.json', m_broken_bracket_example, 'selfcheck'),
    ('M10', '示例收尾语改回单独一条 system', 'prompt_builder.py',
     m_split_system_after_examples, 'backend'),
    ('M11', '历史截断说明改回插进对话流', 'prompt_builder.py',
     m_history_system_back, 'backend'),
    ('M12', '记忆检索不再按世界过滤', 'memory.py', m_memory_ignore_world, 'backend'),
    ('M13', '记忆接口在世界名缺失时静默落到 normal', 'server.py',
     m_memory_default_world_normal, 'backend'),
    ('M14', '纠正后旧记录仍留在 active（互斥版本同时生效）', 'memory.py',
     m_memory_keep_both_versions, 'backend'),
    ('M15', '记忆写入改成直接覆盖（非原子）', 'memory.py',
     m_memory_nonatomic_write, 'backend'),
    ('M16', '记忆文件损坏时当作「没有记忆」', 'memory.py',
     m_memory_broken_file_reads_empty, 'backend'),
    ('M17', '用户正文被当成她自己的动作（主语弄反）', 'scene.py',
     m_scene_body_counts_as_hers, 'scene'),
    ('M18', '去掉否定/假设/疑问豁免', 'scene.py', m_scene_no_exemptions, 'scene'),
    ('M19', '两个世界共用一份场景状态', 'scene.py',
     m_scene_shared_state_across_worlds, 'scene'),
    ('M20', '未记录的状态项也写出来（开始编）', 'scene.py',
     m_scene_unknown_as_known, 'backend'),
    ('M21', '「一起……」就算同处一室', 'scene.py',
     m_scene_together_on_any_coplay, 'backend'),
    ('M22', '组装 prompt 时不传场景状态', 'server.py',
     m_scene_skipped_in_compose, 'backend'),
    ('M23', '历史整段不再随请求发送（指代无从接住）', 'prompt_builder.py',
     m_history_dropped, 'backend'),
    ('M24', '历史被塞进 system（每轮重复累积）', 'prompt_builder.py',
     m_history_into_system, 'backend'),
    ('M25', '「我在想活动室」被当成「我在活动室」', 'scene.py',
     m_scene_thinking_counts_as_being_there, 'scene'),
    ('M26', '字符画对所有世界放开（主线「不要分段」与之打架）', 'prompt_builder.py',
     m_art_everywhere, 'backend'),
    ('M27', '主线世界不再说明「不画」', 'prompt_builder.py',
     m_art_off_prompt_dropped, 'backend'),
    ('M28', '「可以画」改成按轮次配额（DEC-007 反向）', 'prompt_builder.py',
     m_art_quota, 'backend'),
    ('M29', '多行就算字符画（普通分段被当成图案）', 'textart.py',
     m_art_any_line_is_art, 'textart'),
    ('M30', '字符画不再限制每行宽度（整段话也算画）', 'textart.py',
     m_art_no_width_limit, 'textart'),
    ('M31', '前缀剥离退回 trim（吃掉图案首行缩进）', 'textart.py',
     m_art_strip_trim, 'textart'),
    ('M32', '后面不再报图案左边距不一致', 'textart.py',
     m_art_python_indent_unchecked, 'textart'),
    ('M33', '前端不再报图案左边距不一致（契约少一半）', 'web/index.html',
     m_art_frontend_indent_unchecked, 'frontend'),
    ('M34', '图案也走打字机逐字显示', 'web/index.html',
     m_art_frontend_typewriter, 'frontend'),
    ('M35', '前端前缀剥离退回 trim', 'web/index.html',
     m_art_frontend_strip_trim, 'frontend'),
    ('M36', 'kind 字段反了（按世界给错格式约定）', 'server.py',
     m_art_kind_reversed, 'backend'),
    ('M37', '图案分块退回到「整条一个块」（文字行被压成一行）', 'textart.py',
     m_art_no_blocks, 'textart'),
    ('M38', '单行像图案也起块（一整行 ==== 被当画）', 'textart.py',
     m_art_block_single_line, 'textart'),
    ('M39', '前端分块退回到整条一块（两端判据漂移）', 'web/index.html',
     m_art_frontend_no_blocks, 'frontend'),
    ('M40', '`.txt.multi` 仍保空白（文字块压成一行）', 'web/index.html',
     m_art_frontend_multi_keeps_pre, 'frontend'),
    ('M41', '自检模拟器漏掉字符画的分段豁免（两端判据漂移）', 'scripts/check_persona.py',
     m_art_selfcheck_no_art_exemption, 'frontend'),
    ('M42', '空格又被算成图案符号（缩进的普通文字被判成画）', 'textart.py',
     m_art_space_counts_as_symbol, 'textart'),
    ('M43', '前端把空格算成符号（两端判据漂移）', 'web/index.html',
     m_art_frontend_space_counts_as_symbol, 'frontend'),
    ('M44', '不再查右边缘不齐', 'textart.py',
     m_art_no_right_edge_check, 'textart'),
    ('M45', '示例又编造天气（AUD-09 反向）', 'yuki.json',
     m_example_invents_weather, 'backend'),
    ('M46', '崩溃卡示例又编造天气', 'yuki_school.json',
     m_example_invents_weather, 'backend'),
    ('M47', '示例共用一个犹豫前缀（AUD-12 反向）', 'yuki_school.json',
     m_examples_share_filler, 'backend'),
    ('M48', '删掉意见分歧示例（规则还在，示例没了）', 'yuki.json',
     m_example_for_disagreement_removed, 'backend'),
    ('M49', 'verified 示例台词改了，标签还在（标签变假）', 'yuki.json',
     m_verified_label_wrong_text, 'sources'),
    ('M50', '把「单句 ≤ 15 字」的旧硬约束写回种子文件正文', 'data/seed_nagato_lines.md',
     m_seed_dead_rule_back, 'backend'),
    ('M51', '前端 checkArt 的组合字符退回只查 0300-036f', 'web/index.html',
     m_frontend_combining_narrow, 'frontend'),
    # B1～B4 修复的反向变异（2026-10-23）：每修一条都要证明它能被改回去
    ('M52', 'R-01 反向：上下文整段输出，不再逐行按世界过滤', 'prompt_builder.py',
     m_ctx_not_filtered_by_world, 'backend'),
    ('M53', 'R-02 反向：空状态说明改回 innerHTML +=（冲掉已绑定的处理器）', 'web/index.html',
     m_empty_memory_rewrites_parent, 'frontend'),
    ('M54', 'R-03 反向：恢复只看直接后继（三代链留下互斥版本）', 'memory.py',
     m_restore_only_direct_successor, 'backend'),
    ('M55', 'R-04 反向：进程内锁退回每个实例一把', 'memory.py',
     m_scene_bitwise_lock_only, 'backend'),
    ('M56', 'R-06 反向：备份失败又被吞掉', 'memory.py',
     m_backup_failure_swallowed, 'backend'),
    ('M57', 'R-05 反向：她的回复不再更新场景状态', 'server.py',
     m_scene_reply_not_consumed, 'backend'),
    ('M58', 'R-07 反向：共同经历不再标主体与来源', 'prompt_builder.py',
     m_memory_subject_dropped, 'backend'),
    ('M59', 'R-08 反向：删掉字符画的格式例外说明', 'prompt_builder.py',
     m_art_exception_removed, 'backend'),
    ('M60', 'AUD-14 反向：示例又铺成 user/assistant 消息对', 'prompt_builder.py',
     m_examples_back_to_message_pairs, 'backend'),
    ('M61', 'PEND-012 反向：恢复低自评、推断常错和夸奖固定回避', 'yuki.json',
     m_restore_unsupported_normal_claims, 'backend'),
    ('M62', 'PEND-013 反向：恢复无来源的甜食偏好与巧克力往事', 'yuki_school.json',
     m_restore_unsupported_food_history, 'backend'),
    ('M63', 'PEND-012 反向：仅恢复被夸直接跳过的示例', 'yuki.json',
     m_restore_skip_praise_example, 'backend'),
    ('M64', 'CHAT-019 反向：删掉「关于你自己的经历」整条规则', 'prompt_builder.py',
     m_drop_self_history_rule, 'backend'),
    ('M65', 'CHAT-019 反向：删掉「检索没讲到就等于没有依据」', 'prompt_builder.py',
     m_drop_hits_are_not_evidence, 'backend'),
    ('M66', '2026-10-26 反向：删掉符号密度，缩进代码又被当成字符画', 'textart.py',
     m_art_density_dropped, 'textart'),
    ('M67', '2026-10-26 反向：前端删掉符号密度，两端对代码分叉', 'web/index.html',
     m_art_frontend_density_dropped, 'frontend'),
    ('M68', '2026-10-26 反向：图案内部空行又拆块，下边框错位', 'textart.py',
     m_art_blank_line_splits_again, 'textart'),
    ('M69', '2026-10-26 反向：前端图案内部空行又拆块', 'web/index.html',
     m_art_frontend_blank_line_splits_again, 'frontend'),
    ('M70', '2026-10-26 反向：左边距退回全行比较，自由形状又误报', 'textart.py',
     m_art_freeform_indent_reported_again, 'textart'),
    ('M71', '2026-10-26 反向：前端左边距退回全行比较', 'web/index.html',
     m_art_frontend_freeform_indent_reported_again, 'frontend'),
    ('M72', '2026-10-26 反向：分段豁免退回「整条都得是图案」（前端）', 'web/index.html',
     m_art_segment_needs_whole_art, 'frontend'),
    ('M73', '2026-10-26 反向：自检的分段豁免退回整条判定', 'scripts/check_persona.py',
     m_art_segment_needs_whole_art_py, 'textart'),
    ('M74', '2026-10-26 反向：文字块退回 normal，块间空行被折叠', 'web/index.html',
     m_art_frontend_text_block_collapses, 'frontend'),
    # 2026-10-26 复采查出的漏判：「顶格的脸」让整只猫拼不成块（A1 rep2 原样输出）。
    # 四条一起锁：软判据在不在、前端的镜像、soft 不能自己起块、表格行的取舍没被顺手改掉。
    ('M75', '2026-10-26 反向：删掉 soft 行判据，顶格的脸又把整只猫拆散', 'textart.py',
     m_art_face_line_requires_long_run, 'textart'),
    ('M76', '2026-10-26 反向：前端删掉 soft 行判据，两端对顶格的猫分叉', 'web/index.html',
     m_art_frontend_face_line_requires_long_run, 'frontend'),
    ('M77', '2026-10-26 反向：soft 行可以自己起块，两行普通话被压成图案', 'textart.py',
     m_art_soft_lines_can_start_block, 'textart'),
    ('M78', '2026-10-26 反向：soft 不再排除框线行，表格行被卷进图案块', 'textart.py',
     m_art_soft_swallows_table_rows, 'textart'),
    # 2026-10-26 查实的一次解释器崩溃，根因是索引惰性加载的竞态。
    ('M79', '2026-10-26 反向：load_corpus 退回无锁，并发构造多份索引', 'server.py',
     m_corpus_load_without_lock, 'backend'),
    # 2026-10-26 第三轮：采样统计口径（与显示层的 textart 分开，见 run_artbehavior）。
    ('M80', '2026-10-26 反向：删掉行内视觉元素判据，数不到嵌在正文里的走势图',
     'scripts/art_behavior_probe.py', m_inline_visual_removed, 'artbehavior'),
    ('M81', '2026-10-26 反向：纯框线也算行内视觉，表格边框被当成图',
     'scripts/art_behavior_probe.py', m_inline_counts_pure_rule, 'artbehavior'),
    ('M82', '2026-10-26 反向：E6 的「没画过」臂忘了删图案，两臂不再只差一张图',
     'scripts/art_behavior_probe.py', m_e6_plain_arm_keeps_art, 'artbehavior'),
    ('M83', '2026-10-26 反向：重算沿用存档旧统计，换判据也不重判',
     'scripts/art_behavior_probe.py', m_recount_trusts_stale_stats, 'artbehavior'),
    # 2026-10-26 审计：T1 的证据链（AUD-11 有三半判据，原来只锁住了一半）
    # 与 T3 的记忆渲染预算。
    ('M84', 'AUD-11 反向：抹掉旧硬约束的删除线与「作废」字样，让它重新生效',
     'data/seed_nagato_lines.md', m_seed_dead_rule_unvoided, 'backend'),
    ('M85', 'T3 反向：超预算时退回直接 break，超长记录又吞掉后面所有记忆',
     'prompt_builder.py', m_memory_render_breaks_on_overflow, 'backend'),
    ('M86', 'T3 反向：超限项强行整条塞进去，字符预算不再生效', 'prompt_builder.py',
     m_memory_render_ignores_budget, 'backend'),
    ('M87', 'T2 反向：去掉第 2 层跨进程文件锁，只有真起进程的用例看得见',
     'memory.py', m_cross_process_lock_removed, 'backend'),
    ('M88', 'T2 反向：Windows 分支锁了不解锁（POSIX 上完全看不出来）',
     'memory.py', m_windows_lock_never_unlocks, 'backend'),
    ('M89', 'T2 反向：Windows 分支不写锁文件种子字节（空文件锁不住）',
     'memory.py', m_windows_lock_no_seed_byte, 'backend'),
]



def run_check_persona():
    """人格自检（check_persona.py）也必须会红。

    括号规则、示例是否自相矛盾属于脚本层检查，`test_backend.py` 看不到；
    只盯后端测试会让「拦住」变成巧合——M9 就曾被 test_08（查 source 字段）
    误拦，而不是因为括号不配对。
    """
    proc = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'check_persona.py')],
                          cwd=ROOT, env=test_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding='utf-8', errors='replace')
    return proc.returncode == 0, (proc.stdout or '')


def run_backend():
    """返回 (是否通过, 输出)。

    判据用 unittest 的**退出码**，不用输出里的 OK/FAIL 字样：
    子进程的 stdout 走管道时，任何 grep 式二次判断都可能把退出码吞掉，
    让「测试失败了」被读成「测试通过了」——变异脚本本身必须避免这种误判。
    """
    proc = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'test_backend.py')],
                          cwd=ROOT, env=test_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding='utf-8', errors='replace')
    out = proc.stdout or ''
    return proc.returncode == 0, out


def run_scene():
    """场景模块自己的回归。

    为什么单独跑：场景状态有两条路径——「解析一句话」和「进 prompt」。
    `test_backend.py` 只走 HTTP，覆盖的是后一条；
    `test_scene.py` 还覆盖 parse_updates 的判据（正反问、否定跨宾语）。
    期望项写成 `scene` 时只跑这一份，是为了让「哪一层在保护这条规则」
    在报告里是明确的，而不是「反正有人拦住了」。
    """
    proc = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'test_scene.py')],
                          cwd=ROOT, env=test_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding='utf-8', errors='replace')
    return proc.returncode == 0, (proc.stdout or '')


def run_textart():
    """字符画判据自己的回归（宽度换算、is_art、对齐、前缀剥离）。

    为什么不并进 backend：这一层是纯函数，没有 HTTP，跑一次不到 10 毫秒，
    单独跑能让报告写清楚「这条规则由哪一层保护」。
    """
    proc = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'test_textart.py')],
                          cwd=ROOT, env=test_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding='utf-8', errors='replace')
    return proc.returncode == 0, (proc.stdout or '')


def run_frontend():
    """前端回归。慢（要起本地假上游），但显示层的规则只有它看得见。

    只有**前端独有的行为**才把期望项写成 `frontend`（整块显示、保空白、
    文档逃逸）。规则层能由 Python 覆盖的，就写 Python 那边，别在这里重跑。
    """
    proc = subprocess.run(['node', str(ROOT / 'scripts' / 'test_frontend.js')],
                          cwd=ROOT, env=test_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding='utf-8', errors='replace')
    return proc.returncode == 0, (proc.stdout or '')


def run_sources():
    """示例来源核对（AUD-10）。

    为什么单独跑：`check_persona.py` 只能看见标签的**格式**，
    `verified:消失@0:25:56` 与 `verified:消失@9:99:99` 在它眼里一样合法。
    只有真去索引里逐字比对，才知道标签有没有说真话。
    """
    proc = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'verify_sources.py')],
                          cwd=ROOT, env=test_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding='utf-8', errors='replace')
    return proc.returncode == 0, (proc.stdout or '')


#: 期望项 → (执行器, 打印用的文件名)
def run_artbehavior():
    """采样工具自己的判据回归（行内视觉元素 / E6 两臂的控制变量）。

    为什么不并进 textart：`textart.py` 是显示层判据，`art_behavior_probe.py`
    是**采样时的统计口径**，两者的取舍方向不同——显示层宁可少判，
    统计层宁可多记一个字段（因为数漏了会把 1 次读成 0 次，前三轮已经发生过三次）。
    混在一起会让「加宽判据」的压力传导到显示层。
    """
    proc = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'test_art_behavior.py')],
                          cwd=ROOT, env=test_env(), stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT,
                          text=True, encoding='utf-8', errors='replace')
    return proc.returncode == 0, (proc.stdout or '')


RUNNERS = {
    'backend': (run_backend, 'test_backend.py'),
    'selfcheck': (run_check_persona, 'check_persona.py'),
    'scene': (run_scene, 'test_scene.py'),
    'textart': (run_textart, 'test_textart.py'),
    'artbehavior': (run_artbehavior, 'test_art_behavior.py'),
    'frontend': (run_frontend, 'test_frontend.js'),
    'sources': (run_sources, 'verify_sources.py'),
}


def _restore(originals, fname, mid):
    """把文件还原成变异之前的内容。

    备份是**从清单派生的**，理论上不会缺项；但还原失败比变异失败严重得多
    （工作区被留在改坏的状态），所以这里也要给出明确报错，而不是 KeyError。
    """
    if fname not in originals:
        raise SystemExit(f'还原失败：{fname} 不在备份里（变异 {mid}）。'
                         '副本可能已被改动，请检查本次变异输出。')
    path_of(fname).write_bytes(originals[fname])


def run_mutations():
    # 只由 main 在隔离副本内调用；还原用于让各变异有相同起点。
    backup = Path(tempfile.mkdtemp(prefix='persona-backup-'))
    originals = {}
    # 备份范围**从变体清单派生**，不再手写。手写清单漏一项的后果很重：
    # 变异改的是那个文件，还原时 KeyError，文件留在被改坏的状态——
    # 而这恰恰发生在「变异脚本」里，最容易把工作区搞脏。
    targets = {'yuki.json', 'yuki_school.json'}
    targets.update(m[2] for m in MUTATIONS)
    for name in sorted(targets):
        src = path_of(name)
        originals[name] = src.read_bytes()
        # 备份目录是平的，`web/index.html` 这类带子目录的名字要先建目录，
        # 否则 copy2 直接 FileNotFoundError——而那时还没进 try，
        # finally 的还原逻辑也不会执行（好在还没开始改文件）。
        (backup / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, backup / name)

    passed, leaks, broken, unverified = 0, [], [], []
    try:
        for kind in dict.fromkeys(m[4] for m in MUTATIONS):
            runner, where = RUNNERS[kind]
            baseline, out = runner()
            save_output(f'baseline-{kind}', out)
            if not baseline:
                print(f'基线 {where} 就是红的，先修基线再谈变异：\n' + out[-2000:])
                return 2
        print(f'所需测试基线全绿（{len(MUTATIONS)} 个变异开始）\n', flush=True)

        for mid, desc, fname, mutate, expect in MUTATIONS:
            # 人格卡走 JSON 读写；prompt_builder.py 这类源码变异走文本替换。
            # 两者共用同一套备份/还原，避免多写一条容易出错的路径。
            target = path_of(fname)
            try:
                # 判「是不是文本源码」不能只看扩展名：`web/index.html` 也是
                # 要被替换的文本，早先按 `.py` 判断，它掉进了人格卡那条分支，
                # 于是 `load()` 去 personas/ 下找文件——报的是路径错误，
                # 很容易被读成「文件不存在」，而其实是分支判断写窄了。
                #
                # 备份的字节必须与即将被改写的文件一致。`originals` 是**循环
                # 开始时**读的，如果某个变异没有把文件还原干净，下一条变异
                # 就是在「上一条留下的内容」上改——变异和证据都会错位。
                assert target.read_bytes() == originals[fname], (
                    f'变异 {mid} 开始前文件与备份不一致：上一条变异没有还原干净')
                if is_text_source(fname):
                    before = target.read_text(encoding='utf-8')
                    after = mutate(before)
                    # 变异必须真的改变了内容。`str.replace` 匹配不到时不报错，
                    # 变异成了空操作，测试自然全绿 —— 脚本却会打印「已拦住」。
                    # 那不是「测试有保护」，是「根本没改」。这一条断言比
                    # 每一个变异函数内部的 assert 都重要，因为它兜住全部。
                    assert after != before, f'变异 {mid} 没有改变文件内容'
                    target.write_text(after, encoding='utf-8', newline='\n')
                else:
                    raw_before = target.read_bytes()
                    changed = mutate(load(fname))
                    assert isinstance(changed, dict), f'变异 {mid} 未返回人格卡对象'
                    assert isinstance(changed.get('system_prompt'), str), f'变异 {mid} 损坏 system_prompt'
                    assert isinstance(changed.get('examples'), list), f'变异 {mid} 损坏 examples'
                    save(fname, changed)
                    assert target.read_bytes() != raw_before, \
                        f'变异 {mid} 没有改变文件内容'
            except Exception as e:
                # 不是 AssertionError 也要按「变异失效」处理并还原。
                # `path_of()` 写错时抛的是 FileNotFoundError，早先只捕
                # AssertionError，异常会一路穿出去——上面那些变异已经跑完、
                # 打印了「已拦住」，但 finally 之后进程以 2 退出，最后那句
                # 「拦住 N/M」永远不会出现。看着像「跑完了」，实际没结论。
                if not isinstance(e, AssertionError):
                    e = f'{type(e).__name__}: {e}'
                # 变异失效 ≠ 变异被拦住。分开计数，最后都算失败。
                broken.append((mid, desc, str(e)))
                _restore(originals, fname, mid)
                print(f'  ! {mid} 变异失效：{desc}\n      → {e}')
                continue
            runner, where = RUNNERS[expect]
            ok, out = runner()
            save_output(mid, out)
            _restore(originals, fname, mid)
            if ok:
                leaks.append((mid, desc))
                print(f'  ✗ {mid} 未拦住：{desc}（期望 {where} 变红，实际全绿）')
            else:
                first = failure_evidence(expect, out)
                if first is None:
                    unverified.append((mid, desc))
                    print(f'  ! {mid} 未确证：异常退出或没有断言失败证据；请查完整日志')
                else:
                    print(f'  ✓ {mid} 已拦住：{desc}\n      → {first}')
                    passed += 1
    finally:
        for name, raw in originals.items():
            path_of(name).write_bytes(raw)
        shutil.rmtree(backup, ignore_errors=True)

    print(f'\n拦住 {passed}/{len(MUTATIONS)} 个变异')
    if broken:
        print('以下变异**没有生效**（不是「被拦住」，是根本没改成）：')
        for mid, desc, why in broken:
            print(f'  - {mid} {desc}：{why}')
    if leaks:
        print('以下变异没有被任何断言发现（说明对应规则没有真正的测试保护）：')
        for mid, desc in leaks:
            print(f'  - {mid} {desc}')
    if unverified:
        print('以下变异的测试未提供有效断言证据，不能算已拦住：')
        for mid, desc in unverified:
            print(f'  - {mid} {desc}')
        return 2
    if broken or leaks:
        return 1
    print('所有变异都会让回归变红：规则确实被测试锁住。')
    return 0


def main():
    global ROOT, PERSONAS
    source, source_personas = ROOT, PERSONAS
    with tempfile.TemporaryDirectory(prefix='yuki-mutation-') as temp:
        snapshot = Path(temp) / 'workspace'
        manifest = copy_snapshot(source, snapshot)
        if REPORT_DIR is not None:
            (REPORT_DIR / 'snapshot-sha256.json').write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'隔离副本：{snapshot}（{len(manifest)} 个文件）', flush=True)
        try:
            ROOT, PERSONAS = snapshot, snapshot / 'personas'
            return run_mutations()
        finally:
            ROOT, PERSONAS = source, source_personas


def cli():
    global MUTATIONS, REPORT_DIR
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--only', nargs='+', metavar='M编号', help='只运行所列变异；省略则全量')
    args = parser.parse_args()
    if args.only:
        unknown = set(args.only) - {m[0] for m in MUTATIONS}
        if unknown:
            parser.error('未知变异编号：' + ', '.join(sorted(unknown)))
        MUTATIONS = [m for m in MUTATIONS if m[0] in args.only]
    REPORT_DIR = ROOT / 'out' / ('mutation-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    REPORT_DIR.mkdir(parents=True)
    print(f'完整输出：{REPORT_DIR}', flush=True)
    return main()


if __name__ == '__main__':
    # main() 抛异常时必须返回非 0。否则 CI 只看退出码，会把崩溃读成成功——
    # 复审实测过：让某个变异函数抛 RuntimeError，进程带 traceback 退出但
    # `echo $?` 是 0，后面的变异再没跑过。
    try:
        sys.exit(cli())
    except KeyboardInterrupt:
        print('已中断；源工作区未参与变异。', file=sys.stderr)
        sys.exit(130)
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(2)
