# 长期记忆参考方案核对

> 最近更新：2026-10-23
> 范围：一手文档与源码静态阅读。未安装这些框架、未移植代码、未运行其测试。

用户要求参考 GitHub 同类高 star 项目。star 只用于筛选参考对象，不能证明抽取准确或适合本项目。

## 来源与版本

2026-10-23 通过 GitHub API 查询，数字会变化：

| 项目 | star | 本轮查询 main 提交 | 可借鉴部分 |
|---|---:|---|---|
| [Mem0](https://github.com/mem0ai/mem0) | 67,702 | `2c81af3bbe27d9bccb09c772d583df9e9be72ee5` | 结合最近对话、相关旧记忆抽取，再去重、关联和记录历史 |
| [Letta](https://github.com/letta-ai/letta) | 25,239 | `8816d3a8516893bfdc0fa14bfb1235226012e295` | 人格与用户记忆分层；历史 V1 的只读 memory block 可作设计参考 |
| [Graphiti](https://github.com/getzep/graphiti) | 32,165 | `afea04fecd34671c6608fd2a877c5372ff55ee29` | 事实回溯到 episode，区分有效时间与修订历史 |

## 源码核对后的取舍

- **Mem0 为主要流程参考**。[`Memory._add_to_vector_store`](https://github.com/mem0ai/mem0/blob/2c81af3bbe27d9bccb09c772d583df9e9be72ee5/mem0/memory/main.py) 当前执行路径是 V3 分阶段流程：检索旧记忆，单次 LLM 提取，批量处理并记历史。[当前抽取提示](https://github.com/mem0ai/mem0/blob/2c81af3bbe27d9bccb09c772d583df9e9be72ee5/mem0/configs/prompts.py) 使用 ADD 与 `linked_memory_ids`。同文件仍有旧的 ADD/UPDATE/DELETE/NONE 提示，不能只读到旧常量就把它说成当前执行机制。
- **不照搬 Mem0 的抽取范围**：其提示允许从 assistant 建议、计划和用户隐含偏好中抽取。本项目需要区分「用户说过」「她建议过」「确实共同发生」，不能把她编出的原因或计划落成事实。模型只负责提出候选；来源存在与语义成立是两回事。
- **Letta 只借分层思想**。[只读块文档](https://docs.letta.com/v1-sdk/memory/memory-blocks) 明确属于 V1 SDK；仓库 README 已把活跃源码指向 `letta-ai/letta-code`，旧服务器在 `archive` 分支。不能把旧 V1 架构说成其当前完整实现，更不需要为本项目引入 agent 循环。
- **Graphiti 只借来源与时间语义**。[README](https://github.com/getzep/graphiti/blob/afea04fecd34671c6608fd2a877c5372ff55ee29/README.md) 说明事实具有有效期、可追溯 episode，并依赖图数据库、模型与 embedding。现阶段没有证据表明本项目需要整套图数据库。

建议保持现有标准库存储与纠正链，先补候选、证据、冲突处理与召回验证。具体迁移顺序在 [PLAN §15.2](../../PLAN.md)，此处只维护资料依据。上述设计尚未经过本项目真实抽取测试，不构成 A/B/C/D 中任何一项功能验收通过。
