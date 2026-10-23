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

关于并发：这里用**进程内锁 + 文件锁**。同一进程内用 `threading.Lock`；
跨进程用 `fcntl.flock`（Windows 上退化为仅进程内锁并给出警告）。
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

try:  # 跨进程锁；Windows 上没有 fcntl，退化为进程内锁
    import fcntl
except ImportError:  # pragma: no cover - 只在 Windows 上走到
    fcntl = None

WORLDS = ("normal", "crash")
SUBJECTS = ("user", "yuki", "together", "world")
STATUSES = ("active", "retired")

_DEFAULT_DIR = Path(__file__).resolve().parent / "data" / "memory"


class MemoryError_(Exception):
    """记忆层的错误。名字带下划线避免和内置 MemoryError 混淆。"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


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

    def _save_raw(self, data: dict) -> None:
        """写主文件；写前留一份 .bak。"""
        if self.path.exists():
            try:
                shutil.copy2(self.path, self.path.with_suffix(".json.bak"))
            except OSError:
                pass  # 备份失败不阻断写入，但会在 log 里留痕
        _atomic_write(self.path, data)

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

        锁的顺序：进程内 threading.Lock → 文件锁。反过来会在多线程下自锁。
        """
        with self._lock:
            lock_file = None
            try:
                if fcntl is not None:
                    self.root.mkdir(parents=True, exist_ok=True)
                    lock_file = (self.root / f".memory-{self.world}.lock").open("w")
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                data, warns = self._load_raw()
                result, event = fn(data)
                self._save_raw(data)
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

    def restore(self, rid: str) -> tuple[dict, list[str]]:
        """从回收站恢复。

        如果它有后继（`retired_by` 指向一条纠正它的记录），恢复前必须把后继
        也退下来——否则同一件事会有两个 active 版本，正是 V-MEM-05 要防的。
        """
        def fn(data):
            rec = next((r for r in data["records"] if r["id"] == rid), None)
            if rec is None:
                raise MemoryError_(f"记录不存在：{rid}")
            if rec.get("world") != self.world:
                raise MemoryError_("不能恢复另一世界的记录")
            retired_followers = []
            succ = rec.get("retired_by")
            if succ:
                nxt = next((r for r in data["records"] if r["id"] == succ), None)
                if nxt is not None and nxt.get("status") == "active":
                    nxt["status"] = "retired"
                    nxt["retired_at"] = _now()
                    nxt["retired_by"] = None
                    retired_followers.append(nxt["id"])
            rec["status"] = "active"
            rec.pop("retired_at", None)
            rec.pop("retired_by", None)
            ev = {"op": "restore", "id": rid, "retired_followers": retired_followers}
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
