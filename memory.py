#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
memory.py — 共同经历长期记忆（CHAT-016 / CHAT-018）

零依赖，只用标准库。存两份 JSON：正式记录与操作日志；写入走「临时文件 + 替换」。

## 为什么不用前端历史

聊天记录现在由 `web/index.html::saveHist()` 写进浏览器 `localStorage`
（按世界分键，每世界 200 条）。换设备或清缓存即丢，服务端也检索不到。
长期记忆必须另起服务端存储——这一条是 [PLAN.md](PLAN.md) §15.1 的结论，
不是本模块的新选择。

## 数据模型

一条记忆（record）：

| 字段 | 含义 |
|---|---|
| `id` | 稳定标识，`m-<时间戳>-<序号>`，删除后不复用 |
| `world` | `normal` / `crash`。**所有读写都按世界过滤**，不是只在展示时过滤 |
| `text` | 记忆内容本身 |
| `subject` | 这条事实说的是谁：`user` / `yuki` / `together` / `world` |
| `source` | 来源：`{'kind': 'turn', 'user': ..., 'assistant': ...}` 或 `{'kind': 'user-stated'}` |
| `created` | ISO 时间 |
| `revision` | 当前版本号，从 1 开始 |
| `revises` | 被本条取代的旧记录 id（纠正链） |
| `status` | `active` / `retired`。`retired` = 被纠正或删除，进回收站 |
| `tags` | 可选标签，纯文本 |

**互斥版本不同时生效**：纠正时旧记录变 `retired`，新的 `revises` 指向它。
检索只返回 `active`，所以「旧事实」不会被当成确定事实再用一次。

## 世界隔离（WORLD-001）

`MemoryStore(world=...)` 把世界绑在**存储对象上**，而不是每个方法传参。
理由：传参容易漏一处，漏的那处就是泄漏。所有方法都只能看到自己世界的记录，
连 `list_all()` 也是。跨世界访问只能新建另一个 store。

## 与人格的关系（CHAT-015）

记忆**不会**改写人格卡，也不会变成 system 里的规则。它只作为「共同经历」
被检索、被注入为可引用的事实记录，和原作片段并列但来源分开。
重要度、标签都不能把记忆提升为人格指令——这里根本没有 importance 字段。

## 失败时怎么办（V-MEM-04）

- 写入前先写临时文件再 `os.replace`，中途失败不留半条记录。
- 每次写入前把当前文件复制成 `.bak`；主文件损坏时 `load()` 会报告并尝试 `.bak`。
- 操作日志追加写 JSONL，一行一个事件；日志写失败**不影响**主记录，
  但会把警告返回给调用方，不静默吞掉。

关于并发：这里用**两层锁**，两层都要有。
第一层是**按绝对路径共享**的进程内锁（`_lock_for`）——同一进程里多个
`MemoryStore` 操作同一个文件时靠它串行；服务端每个请求新建一个实例，
锁挂在实例上等于没有。
第二层是**跨进程文件锁**：POSIX 用 `fcntl.flock`，Windows 用 `msvcrt.locking`。
（这一段原来写的是「Windows 上退化为仅进程内锁并给出警告」——那是修复前的
状态，也是 R-04 在 Windows 上丢记录的直接原因，2026-10-23 已改掉。
文档比代码慢半拍时，读者会照着文档以为 Windows 没有保护。）
它不是事务，只保证「读—改—写」不交错到丢数据。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

try:  # 跨进程锁；Windows 上没有 fcntl，用 msvcrt 顶上
    import fcntl
except ImportError:  # pragma: no cover - 只在 Windows 上走到
    fcntl = None

try:  # Windows 的跨进程文件锁
    import msvcrt
except ImportError:  # pragma: no cover - 只在非 Windows 上走到
    msvcrt = None

WORLDS = ("normal", "crash")
SUBJECTS = ("user", "yuki", "together", "world")
STATUSES = ("active", "retired")

_DEFAULT_DIR = Path(__file__).resolve().parent / "data" / "memory"

#: 每个「存储文件」一把进程内锁，**按绝对路径共享**。
#:
#: 为什么不能用实例字段：`server.py::memory_store()` 每个请求都新建一个
#: `MemoryStore`，实例锁只保护得住同一个实例。两个请求各自持锁互不相干，
#: 「读—改—写」照样交错，后写的把先写的整个覆盖掉——两边都返回成功，
#: 用户看不出丢了数据（R-04，实测 6 条并发写只剩 1 条）。
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    """取这条存储路径对应的进程内锁（同一路径永远同一把）。"""
    key = str(path.resolve()).lower()
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


class MemoryError_(Exception):
    """记忆层的错误。名字带下划线避免和内置 MemoryError 混淆。"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _is_readable_json(path: Path) -> bool:
    """这个文件能不能解析成 JSON。

    备份前用它判断「主文件本身是不是已经坏了」：坏文件复制过去只会
    毁掉唯一一份可用的备份（R-06）。只判断可读性，不校验内容结构——
    结构问题由 `_load_raw()` 负责，那是读的路径。
    """
    try:
        json.loads(path.read_text(encoding="utf-8"))
        return True
    except (OSError, ValueError):
        return False


def _atomic_write(path: Path, payload: dict) -> None:
    """写 JSON：临时文件 + fsync + replace。中途失败不会留下半个正式文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        # 走到这里说明没替换成功，正式文件仍是旧的；临时文件清掉。
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class MemoryStore:
    """一个世界的共同经历存储。

    >>> s = MemoryStore(Path('/tmp/mem'), world='normal')
    >>> r = s.add('你说过在学做饭', subject='user', source={'kind': 'user-stated'})
    >>> s.search('做饭')[0]['id'] == r['id']
    True
    """

    def __init__(self, root: Path | str = _DEFAULT_DIR, world: str = "normal"):
        if world not in WORLDS:
            raise MemoryError_(f"未知世界 {world!r}；只接受 {WORLDS}")
        self.world = world
        self.root = Path(root)
        self.path = self.root / f"memory-{world}.json"
        self.log_path = self.root / f"memory-{world}.log.jsonl"
        self._lock = threading.Lock()

    # ------------------------------------------------------------
    # 读写基础设施
    # ------------------------------------------------------------

    def _empty(self) -> dict:
        return {"world": self.world, "version": 1, "seq": 0, "records": []}

    def _load_raw(self) -> tuple[dict, list[str]]:
        """返回 (数据, 警告)。主文件坏了会试 .bak，都不行就抛错。"""
        warns: list[str] = []
        if not self.path.exists():
            return self._empty(), warns
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            warns.append(f"记忆文件损坏：{e}")
            bak = self.path.with_suffix(".json.bak")
            if bak.exists():
                try:
                    data = json.loads(bak.read_text(encoding="utf-8"))
                    warns.append("已从备份恢复，可能丢失最后一次修改")
                except (OSError, ValueError) as e2:
                    raise MemoryError_(f"记忆文件与备份都不可用：{e} / {e2}") from e2
            else:
                raise MemoryError_(f"记忆文件损坏且没有备份：{e}") from e
        if data.get("world") not in (None, self.world):
            # 文件被放错世界目录：宁可报错，也不能把另一世界的经历读进来。
            raise MemoryError_(
                f"记忆文件的 world={data.get('world')!r} 与存储的 {self.world!r} 不一致")
        data.setdefault("world", self.world)
        data.setdefault("seq", len(data.get("records", [])))
        data.setdefault("records", [])
        return data, warns

    def _save_raw(self, data: dict) -> str | None:
        """写主文件；写前留一份 `.bak`。**返回备份相关的警告，没有就是 None。**

        两条规则（R-06，见 reviews/2026-10-23-zip-merge-review.md）：

        1. **备份失败必须说出来。** 原实现 `except OSError: pass`，注释写
           「会在 log 里留痕」，实际什么都没留：用户以为有备份，其实没有。
           这里把原因返回给调用方，由它并进 `warnings` 与操作日志。
        2. **不用坏主文件覆盖好备份。** 如果当前主文件本身就是坏的
           （读的时候退到了 `.bak`），再照常复制就会把唯一可用的备份毁掉；
           之后 replace 一旦失败，就彻底没有可恢复的东西了。
           所以先确认主文件能解析，不能就跳过备份、保留旧 `.bak`。
        """
        warn = None
        if self.path.exists():
            if _is_readable_json(self.path):
                try:
                    shutil.copy2(self.path, self.path.with_suffix(".json.bak"))
                except OSError as e:
                    warn = f"备份失败（主记录已保存，但没有新的 .bak）：{e}"
            else:
                # 主文件损坏：跳过备份，宁可让 .bak 停在上一份好的。
                warn = "主文件损坏，本次未更新备份；.bak 保留为上一份可用版本"
        _atomic_write(self.path, data)
        return warn

    def _log(self, event: dict) -> str | None:
        """追加一条操作日志。返回警告文本（失败时），不抛异常。"""
        event = {"at": _now(), "world": self.world, **event}
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError as e:
            return f"操作日志写入失败（主记录已保存）：{e}"
        return None

    def _mutate(self, fn):
        """加锁执行 fn(data) -> (result, event)。

        三层锁，顺序固定：
          1. **按路径共享的进程内锁**（`_lock_for`）——同一进程里多个
             `MemoryStore` 实例操作同一个文件时靠它串行。
          2. **跨进程文件锁**——POSIX 用 `fcntl.flock`，Windows 用
             `msvcrt.locking`。多进程/多标签页同时写时靠它。
          3. 都不影响读：读走 `_load_raw()`，不加写锁。

        第 1 层是 R-04 的核心：原来锁是实例字段，而服务端每个请求新建一个
        实例，等于没有锁。第 2 层 Windows 上原来完全缺失（`fcntl` 导入失败
        就直接跳过），现在用 `msvcrt` 补上——否则重启一个进程就又能并发覆盖。
        """
        with _lock_for(self.path):
            lock_file = None
            lock_handle = None
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                lock_path = self.root / f".memory-{self.world}.lock"
                if fcntl is not None:
                    lock_file = lock_path.open("w")
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                elif msvcrt is not None:
                    # Windows：`msvcrt.locking` 需要一个至少有 1 字节的文件，
                    # 并且从当前位置锁起。`LK_LOCK` 会重试 10 秒后抛 OSError。
                    lock_handle = lock_path.open("a+b")
                    if lock_handle.tell() == 0 and lock_path.stat().st_size == 0:
                        lock_handle.write(b"\0")
                        lock_handle.flush()
                    lock_handle.seek(0)
                    msvcrt.locking(lock_handle.fileno(), msvcrt.LK_LOCK, 1)
                data, warns = self._load_raw()
                result, event = fn(data)
                save_warn = self._save_raw(data)
                if save_warn:
                    warns.append(save_warn)
                warn = self._log(event)
                if warn:
                    warns.append(warn)
                return result, warns
            finally:
                if lock_file is not None:
                    try:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                    finally:
                        lock_file.close()
                if lock_handle is not None:
                    try:
                        lock_handle.seek(0)
                        msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                    finally:
                        lock_handle.close()

    # ------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------

    def load(self) -> tuple[list[dict], list[str]]:
        """返回 (全部记录, 警告)。**包括 retired**，界面要看回收站。"""
        data, warns = self._load_raw()
        return list(data["records"]), warns

    def list_active(self) -> list[dict]:
        return [r for r in self.load()[0] if r.get("status") == "active"]

    def get(self, rid: str) -> dict | None:
        for r in self.load()[0]:
            if r["id"] == rid:
                return r
        return None

    def add(self, text: str, subject: str = "user", source: dict | None = None,
            tags: list[str] | None = None, revises: str | None = None
            ) -> tuple[dict, list[str]]:
        """新增一条。返回 (记录, 警告)。

        `revises` 给出时，被指向的旧记录会变成 `retired`——纠正不是「再记一条」，
        而是把旧的那条退下来，避免互斥版本同时生效（V-MEM-03）。
        """
        text = (text or "").strip()
        if not text:
            raise MemoryError_("记忆内容为空")
        if subject not in SUBJECTS:
            raise MemoryError_(f"未知主体 {subject!r}；只接受 {SUBJECTS}")

        def fn(data):
            if revises:
                old = next((r for r in data["records"] if r["id"] == revises), None)
                if old is None:
                    raise MemoryError_(f"要纠正的记录不存在：{revises}")
                if old.get("world") != self.world:
                    raise MemoryError_("不能纠正另一世界的记录")
                old["status"] = "retired"
                old["retired_at"] = _now()
                old["retired_by"] = None  # 下面填新记录的 id
            data["seq"] = int(data.get("seq", 0)) + 1
            rid = f"m-{int(time.time() * 1000)}-{data['seq']}"
            rec = {
                "id": rid,
                "world": self.world,
                "text": text,
                "subject": subject,
                "source": source or {"kind": "user-stated"},
                "created": _now(),
                "revision": (old["revision"] + 1) if revises else 1,
                "revises": revises,
                "status": "active",
                "tags": list(tags or []),
            }
            if revises:
                old["retired_by"] = rid
            data["records"].append(rec)
            return rec, {"op": "add", "id": rid, "revises": revises, "text": text}

        return self._mutate(fn)

    def update(self, rid: str, text: str) -> tuple[dict, list[str]]:
        """改一条的文本。**保留 id**，版本号 +1，并在日志里留旧文本。

        与 `add(revises=...)` 的区别：这里是同一条记录的修订（拼写、措辞），
        不是「新事实取代旧事实」。两种都要有日志，因为「改了什么」要查得到。
        """
        text = (text or "").strip()
        if not text:
            raise MemoryError_("记忆内容为空")

        def fn(data):
            rec = next((r for r in data["records"] if r["id"] == rid), None)
            if rec is None:
                raise MemoryError_(f"记录不存在：{rid}")
            if rec.get("world") != self.world:
                raise MemoryError_("不能修改另一世界的记录")
            old_text = rec["text"]
            rec["text"] = text
            rec["revision"] = int(rec.get("revision", 1)) + 1
            rec["updated"] = _now()
            return rec, {"op": "update", "id": rid, "from": old_text, "to": text}

        return self._mutate(fn)

    def delete(self, rid: str) -> tuple[dict, list[str]]:
        """删除 = 进回收站（status=retired）。不物理移除，除非 purge。"""

        def fn(data):
            rec = next((r for r in data["records"] if r["id"] == rid), None)
            if rec is None:
                raise MemoryError_(f"记录不存在：{rid}")
            if rec.get("world") != self.world:
                raise MemoryError_("不能删除另一世界的记录")
            rec["status"] = "retired"
            rec["retired_at"] = _now()
            rec["retired_by"] = None
            return rec, {"op": "delete", "id": rid, "text": rec["text"]}

        return self._mutate(fn)

    # ------------------------------------------------------------
    # 修订链
    # ------------------------------------------------------------
    @staticmethod
    def _chain_of(records: list[dict], rid: str) -> list[str]:
        """找出 `rid` 所在的整条修订链（按 `revises` 连起来的），含自己。

        **为什么用 `revises` 而不是 `retired_by`**：`retired_by` 只在「纠正」
        那一刻写进去，恢复时会被清掉，链条就断了；而且它只是单向的指针
        （旧 → 新），拿到新的一条无法反查旧的有哪些。
        `revises` 是**跟着记录本身**存下来的，一条记录永远知道自己是改自哪条，
        所以从任意一条出发都能把整条链走全。R-03 的两个反例都源于
        「只沿 `retired_by` 看一步」。

        返回顺序按 `revision` 升序（最早的在前），同 revision 时按 created。
        """
        def parent_of(rec):
            return rec.get("revises")

        # 先往上走到根，再往下收集整棵树（链是线性的，但仍按树处理，
        # 万一有分叉也能全收进来——总比漏掉好）。
        by_id = {r["id"]: r for r in records}
        root = rid
        seen = set()
        while True:
            rec = by_id.get(root)
            if rec is None or root in seen:
                break
            seen.add(root)
            parent = parent_of(rec)
            if not parent or parent not in by_id:
                break
            root = parent

        members = []
        stack = [root]
        while stack:
            cur = stack.pop()
            if cur in {m["id"] for m in members}:
                continue
            rec = by_id.get(cur)
            if rec is None:
                continue
            members.append(rec)
            stack.extend(r["id"] for r in records if r.get("revises") == cur)

        members.sort(key=lambda r: (int(r.get("revision", 1)), r.get("created", "")))
        return [r["id"] for r in members]

    def restore(self, rid: str) -> tuple[dict, list[str]]:
        """从回收站恢复。**恢复之后，整条修订链只能剩下这一条 active。**

        原实现只沿 `retired_by` 看一个直接后继，所以有两个真实反例
        （R-03，见 reviews/2026-10-23-zip-merge-review.md）：

          1. 三代链 A→B→C，恢复 A：只退下 B，C 仍是 active，
             于是 prompt 里同时出现 A 和 C —— 两个互斥版本都成了事实。
          2. A→B 往返恢复：恢复时 `pop("retired_by")` 把关联抹掉，
             再恢复 B 时已经看不到 A，最后 A、B 一起 active。

        现在改成：先把**整条链**按 `revises` 找出来，除了要恢复的这一条，
        其余 active 全部退下。链上其余记录的状态与 `retired_by` 一律重算，
        不留半截关联——半截关联正是第二个反例的成因。
        """
        def fn(data):
            rec = next((r for r in data["records"] if r["id"] == rid), None)
            if rec is None:
                raise MemoryError_(f"记录不存在：{rid}")
            if rec.get("world") != self.world:
                raise MemoryError_("不能恢复另一世界的记录")

            chain = self._chain_of(data["records"], rid)
            by_id = {r["id"]: r for r in data["records"]}
            retired_followers = []
            for cid in chain:
                if cid == rid:
                    continue
                other = by_id.get(cid)
                if other is None:
                    continue
                if other.get("status") == "active":
                    other["status"] = "retired"
                    other["retired_at"] = _now()
                    retired_followers.append(cid)
                # 链上其余记录的 retired_by 一律指向**这条**（当前唯一有效版本）。
                # 留成 None 会让下一次恢复又找不到关联，就是反例 2。
                if other.get("status") == "retired":
                    other["retired_by"] = rid

            rec["status"] = "active"
            rec.pop("retired_at", None)
            # 恢复的这一条自己不再是「被谁取代」的状态。
            rec["retired_by"] = None
            ev = {"op": "restore", "id": rid, "retired_followers": retired_followers,
                  "chain": chain}
            return rec, ev

        return self._mutate(fn)

    def search(self, query: str, k: int = 5) -> list[dict]:
        """按词命中检索，只返回本世界的 active 记录。

        刻意做得很笨（子串 + 字符覆盖），不用 BM25：
          - 共同经历通常几十条，全量比对足够快；
          - 原作检索已经是 BM25，这里再用会让「哪种排序更好」混在一起，
            出问题时说不清是哪一层。
        真实召回效果要按 V-MEM-06 用已知答案样本比，不是看这个函数写得多聪明。
        """
        query = (query or "").strip()
        if not query:
            return []
        chars = {c for c in query if not c.isspace()}
        scored = []
        for rec in self.load()[0]:
            if rec.get("status") != "active" or rec.get("world") != self.world:
                continue
            text = rec.get("text", "")
            hit = sum(1 for c in chars if c in text)
            if hit == 0:
                continue
            scored.append((hit, len(rec.get("tags") or []), rec, ))
        scored.sort(key=lambda x: (-x[0], -x[1], x[2].get("created", "")))
        return [rec for _, _, rec in scored[:k]]

    def read(self, query: str, k: int = 4) -> list[dict]:
        """对话时实际注入用的取数：先按词命中，不够再补最近几条。

        为什么要有后半段：共同经历通常只有几条到几十条，用户刚刚亲口说过的事
        往往一个字都不重合（「今天好累」对上「上周说过在赶项目」）。只做命中检索
        会让记忆在**最该被记住的时候**缺席。补最近几条是弱召回，但不越界——
        依然只在本世界、只取 active，而且数量有上限。

        两部分合并后按 id 去重，顺序是「命中在前、最近在后」。
        """
        hits = self.search(query, k=k)
        seen = {r["id"] for r in hits}
        recent = sorted(
            (r for r in self.load()[0]
             if r.get("status") == "active" and r.get("world") == self.world
             and r["id"] not in seen),
            key=lambda r: r.get("created", ""), reverse=True)[:k]
        return hits + recent

    def log_events(self, limit: int = 100) -> list[dict]:
        """读最近的日志。用于界面追溯「谁在什么时候改了什么」。"""
        if not self.log_path.exists():
            return []
        lines = self.log_path.read_text(encoding="utf-8").splitlines()
        out = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out


def store_for(world: str, root: Path | str = _DEFAULT_DIR) -> MemoryStore:
    return MemoryStore(root=root, world=world)
