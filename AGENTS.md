# AGENTS.md · 开发与协作约定

> 给接手这个项目的开发者或 AI。**动手之前读完这一页**。
> 最近更新：2026-10-21

---

## 一、项目是什么

一个和长门有希通过终端屏幕持续相处的文字聊天项目。不是通用助手，也不是酒馆式角色扮演。
两个世界（主线 / 崩溃）各有一个她，身份与经历严格隔离，聊的是日常而不是剧情。

代码是零依赖 Python 标准库 + 单文件 HTML，没有构建步骤。

---

## 二、阅读顺序

按这个顺序读，不要跳：

| 顺序 | 文件 | 读它是为了知道 |
|---|---|---|
| 1 | 本文件 | 怎么改、怎么验 |
| 2 | [docs/PRODUCT.md](docs/PRODUCT.md) | 哪些规则是**已确认**的产品方向 |
| 3 | [docs/STATUS.md](docs/STATUS.md) | 每条规则**实现到哪一步**、卡在哪 |
| 4 | [docs/DECISIONS.md](docs/DECISIONS.md) | 为什么是现在这样，哪些旧方案已作废 |
| 5 | [docs/VALIDATION.md](docs/VALIDATION.md) | 怎么证明改对了 |
| 6 | [README.md](README.md) | 项目简介与启动方式 |
| 7 | [data/README.md](data/README.md) | 语料分级与**两种不同的**准入要求 |

**最容易犯的错**：把 PRODUCT.md 里的规则当成已经实现。规则是方向，STATUS.md 才是事实。

---

## 三、文档各管什么

**每件事只有一个主要维护位置**，其他地方引用而不复制。

| 文件 | 负责 | 什么时候更新 |
|---|---|---|
| `README.md` | 简介、**当前可用行为**、启动方式、文档入口 | 使用方式或已实现功能变化时 |
| `PLAN.md` | **目标与迁移过程**：要迁到哪、怎么迁、有什么坑 | 阶段推进或计划调整时 |
| `AGENTS.md` | 协作约定、阅读顺序、验证要求 | 协作流程变化时 |
| `docs/PRODUCT.md` | 已确认的产品规则 | 产品方向被确认时 |
| `docs/STATUS.md` | 阶段、任务状态、阻塞、下一步 | 每完成一批实际工作时 |
| `docs/DECISIONS.md` | 重要决定及原因 | 出现影响后续开发的选择时 |
| `docs/VALIDATION.md` | 验收场景、测试方法、验证结果 | 行为规则或验证结果变化时 |
| `data/README.md` | 数据来源、分级、准入、复核要求 | 数据处理规则变化时 |

`README` 与 `PLAN` 是**分工关系，不是互相取代**：前者说「现在能跑什么」，后者说「要迁到哪、怎么迁」。
`docs/history/` 是归档，**不再改写**。

---

## 四、动手前的三条硬约束

### 1. 改角色规则要五处同步

见 [DEC-006](docs/DECISIONS.md)。规则散落在五个地方，漏一处回归必红：

1. `personas/*.json`（`system_prompt` / `examples` / `guard`）
2. `prompt_builder.py`（`TERMINAL_PROMPT` / `WORLD_PROMPT`）
3. `web/index.html::checkReply()`
4. `scripts/check_persona.py::simulate_check_reply()`
5. 锁住旧行为的测试断言

第 3、4 项是同一套规则的两次独立实现，`scripts/test_frontend.js` 会逐例比对。只改一边，测试红的原因会被误读成「新规则不对」。

### 2. 有些测试是故意锁住当前行为的

`scripts/test_backend.py::test_05_cross_world_preview` 现在断言 prompt **必须**出现跨世界标签。这是为了固定当时的泄漏机制，不是 bug。
改行为时要**显式重写**它，不能删掉了事。删掉的测试不会告诉你哪里退化。

### 3. 不要用工程测试评价人设

自动测试只证明代码正确，不证明她像不像。二者见 [VALIDATION.md](docs/VALIDATION.md) 的证据分级。
「37 项通过」不构成「她说话自然」的证据。

---

## 五、验证要求

每完成一批改动，跑完这四项再报完成：

```powershell
py build_corpus.py            # 语料与归属
py scripts/check_persona.py   # 人格卡自检
node scripts/test_frontend.js # 前端 + HTTP 全链路
py scripts/test_backend.py    # 后端与检索
```

全绿才算完成。红了先修再继续，不要为了通过而放宽断言。

**不调用付费模型**：所有自动测试都用本地假上游（`scripts/stream_fixture.py`）。真实模型试聊是单独一步，需要 API Key，并留下记录。

---

## 六、完成一个任务的检查清单

- [ ] 代码改完，四项回归全绿
- [ ] [docs/STATUS.md](docs/STATUS.md) 对应任务的状态已更新（`待开发` → `已实现`）
- [ ] [docs/PRODUCT.md](docs/PRODUCT.md) 规则是否需要改？改了就在 [docs/DECISIONS.md](docs/DECISIONS.md) 留原因
- [ ] [docs/VALIDATION.md](docs/VALIDATION.md) 是否需要新增验收场景或更新结果
- [ ] [data/README.md](data/README.md) 如果动了语料准入，同步更新
- [ ] [README.md](README.md) 如果改了使用方式或已实现功能，同步更新

**没变化的文档不用动。** 但没做完的事必须留在 STATUS.md 里标 `待开发`，不能为了结项写成「已完成」。

---

## 七、写文档的约定

- 中文正文、UTF-8、标题清晰
- 有状态的文档在开头写「最近更新：YYYY-MM-DD」
- 任务用稳定编号：`CHAT-###`（聊天与表达）、`WORLD-###`（世界边界）、`SCOPE-###`（范围）、`V-*`（验收场景）、`DEC-###`（决策）、`BLK-###`（阻塞）
- 产品状态只有 `已确认` / `已替代`；实现状态用 `已实现` / `部分实现` / `待开发` / `待复核`
- 写「验收」要写**具体行为**，不写「更自然」这种无法判断的话
- 被替代的决定不删除，标 `已替代` 并指向替代者
- **不写**密钥、API Key、私人聊天全文

---

## 八、目录速览

```
character-voice/
├── server.py          本地服务：静态文件 + 检索 + 组装 prompt + SSE 透传
├── build_corpus.py    建检索索引（data/parsed → data/index）
├── retriever.py       BM25 检索
├── prompt_builder.py  人格卡 + 世界线 + 检索结果 → messages
├── personas/          两张人格卡（主线 / 崩溃）
├── web/index.html     界面（单文件，无构建）
├── docs/              产品规则、状态、决策、验收
├── data/              语料、索引、分级规则（见 data/README.md）
├── scripts/           构建、自检、回归、标注工具
└── app/  config/      旧引擎，不参与运行，仅供参考
```

`docs/history/` 存放不再改写的记录：`MERGE_NOTES-2026-10-21.md`（v1/v2 合并与验证）。
根目录的 `PLAN.md` 是**持续维护**的迁移计划（要迁到哪、怎么迁），不是归档。

---

## 九、当前该做什么

看 [docs/STATUS.md](docs/STATUS.md) 的「下一步」。不要凭本文或 PRODUCT.md 自己推断优先级。
