# P0-4 实施报告：世界边界客观化 + 评测记录唯一标识

> 实施日期：2026-10-26
> 批准依据：用户批准 Q-1（方案 B 客观世界）、Q-2、Q-3
> 未启动：Q-4（排序未按加权分，保留为 P1）、Q-5（相关性复核表，用户人工填写）
> 前置：`docs/P0_FIX_REPORT.md`（P0-1 ~ P0-3）

---

## 一、本轮做了什么

| 编号 | 改动 | 结果 |
|---|---|---|
| **P0-4a** | 世界边界锚点 `2:33:26` → `2:33:01`，口径明确为**客观世界** | crash 1534 → **1528**，normal 11536 → **11542** |
| **P0-4b** | 新增 `transition` 字段，标记恢复点与确认点之间的 6 条 | 6 条，`restore_confirm_gap` |
| **P0-4c** | 评测记录引用改为稳定唯一 `record_id` | 60 题 67 条引用全部迁移，2 处歧义已定点 |

**没有改的**：`retriever.py`（排序 / BM25 参数 / 去重半径 / 提示词构建）、`memory.py`、
`prompt_builder.py`、`personas/`、`config/`。`git status` 对这些路径为空。

---

## 二、P0-4a：世界边界改为客观世界口径

### 2.1 语义定义

`world` 回答的是「**画面演的是哪个世界**」，不是「阿虚此刻是否知道世界恢复了」。
这两个时刻在《消失》里相差 25 秒，混为一谈会让 6 条「恢复后的现实」被当成崩溃世界经历。

### 2.2 边界实况

```
 2:33:00  [crash ] 第二天
 2:33:01  [normal] 我健康地出院了         <<< movie_restore  客观世界恢复点
 2:33:03  [normal] 今天是12月24日         <<< transition
 2:33:06  [normal] 结业式的早上           <<< transition
 2:33:13  [normal] 私立光阳园学院又变回了女校  <<< transition  「又」= 对照副词
 2:33:19  [normal] 谷口也没有感冒         <<< transition  「也」= 对照副词
 2:33:21  [normal] 朝比奈和鹤屋学姐也都认识我  <<< transition
 2:33:26  [normal] 世界变回了原来的样子   <<< movie_confirm  角色确认句，不参与判定
```

判定依据不是推测：「**又**变回了女校」「谷口**也**没有感冒」这两句的对照副词，
只有在「已回到原世界、正逐项比对」的语境下才成立。`2:33:26` 是阿虚逐项确认完的**总结句**，
标记的是角色终于承认了，不是世界在此刻才切换。

### 2.3 锚点唯一性

| 锚句 | 全库出现次数 |
|---|---|
| `我健康地出院了`（恢复点） | **1** |
| `世界变回了原来的样子`（确认点） | **1** |

两案锚点都无歧义。若片源字幕有出入导致找不到恢复点，`detect_world_restore()` 返回 `None`，
行为退回「切点之后全是 crash」，不比 P0-3 之前更差。

---

## 三、P0-4b：`transition` 独立字段

### 3.1 字段设计

```json
{"id": "消失:2068", "world": "normal", "transition": "restore_confirm_gap", ...}
```

- 取值 `None`（非过渡）或原因字符串（当前只有 `restore_confirm_gap`）。
- **存字符串而不是 bool**：下游要能区分「不是过渡段」与「是过渡段，因为恢复/确认之间」，
  未来还可能有别的过渡原因（如世界切点附近）。
- **独立于 `world` 与 `speaker`**：`transition` 不提供世界归属结论，也不提供说话者信息。
  索引里本来就没有 `speaker` 字段，本轮也没有加。

### 3.2 `meta.json` 新增字段

```json
{
  "movie_restore": "2:33:01",
  "movie_restore_sec": 9181.96,
  "movie_restore_basis": "objective_world",
  "movie_restore_anchor": "我健康地出院了",
  "movie_confirm": "2:33:26",
  "movie_confirm_sec": 9206.18,
  "movie_confirm_anchor": "世界变回了原来的样子",
  "transition": {
    "field": "transition",
    "independent_of": ["world", "speaker"],
    "total": 6,
    "by_reason": {"restore_confirm_gap": 6}
  }
}
```

`movie_restore_basis` 是为了防止下游把 `movie_restore` 误读成「角色认知时刻」而显式写入的。

### 3.3 下游约定（已写入 `docs/WORLD_BOUNDARY.md` §四）

- **事实检索不得一律排除** `transition` 记录，返回时带上该标记；
- **角色表达参考不得**因 `transition` 存在就认定说话者是长门；
- `retriever.py` **不读**该字段，本轮约定不改检索逻辑（`test_record_id.py::test_11` 守护）。

---

## 四、P0-4c：评测 `record_id`

### 4.1 问题

旧格式用 `{"ep","start"}` 引用记录，评测脚本拼 `key = f"{ep}@{start}"` 比对。
但索引里 **(ep, start) 冲突 226 组、涉及 462 条记录**。冲突组的记录会算出同一个 key。

实测 67 条引用里 2 条踩坑：

| 题 | 旧引用 | 冲突组 | 实际需要 | 依据 |
|---|---|---|---|---|
| `B05` | `03@0:05:00` | `03:83 …的确`<br>`03:84 …终端界面并不止我一个` | `03:83` | 原 note：「seed 里记录的「…的确」」；基线实判 expected 的文本即「…的确」 |
| `A11` | `03@0:05:00` | 同上 | `03:84` | 原 note：「三条都是她对「终端界面」的定义性表述」；基线实判 expected 的文本即 03:84 |

旧格式下这两题「恰好」算对——命中任意一条都判 expected。那是巧合，不是正确。

### 4.2 迁移策略：机器能定的静默迁，不能定的停下来问

`migrate_eval_record_id.py` 的规则写死在代码里：

- (ep, start) 只对应 1 条 → 直接映射到它的 `id`（**65 条**）
- 对应多条 → **不自动判断**，标 `ambiguous` 并列出候选；未在 `--decisions` 指定时**报错退出，不写任何文件**

`--check` 模式的实测输出：

```
索引 13070 条 · (ep,start) 冲突组 226 组 （涉及 462 条）
评测 60 题 · 引用 67 条
  unique               65 条
  ambiguous            2 条

需要人工决策的引用（脚本不替你判断）：
  B05.expected[0]  03@0:05:00  status=ambiguous
      候选 03:83      …的确
      候选 03:84      信息统合思念体配置在地球的终端界面并不止我一个
  A11.expected[2]  03@0:05:00  status=ambiguous
      ...
```

两处决策依据写在 `data/retrieval_eval/record_id_decisions.json` 的 `_evidence` 里，
每条都含「原 note」与「基线实际命中」两项，可复核、可改判。**这属于记录级标注对齐，
不是相关性重判**——用户要求人工填写的复核表（Q-5）本轮未动。

### 4.3 新格式

```json
{"id": "B05", "expected": [{"record_id": "03:83"}], ...}
```

`run_retrieval_eval.py` 新增 `--eval v2`，用 `record_id` 做 key；
`--eval legacy` 保留旧的 `ep@start` 路径，只用于复现历史基线（运行时会打印不唯一警告）。

---

## 五、修改前后差异

### 5.1 索引逐字段 diff（13070 条全表比对）

| 变化字段 | 条数 | 说明 |
|---|---|---|
| `world` | **6** | `消失:2068`–`消失:2073`，crash → normal |
| `transition` | **6** | 同上 6 条，`None` → `restore_confirm_gap` |
| `ctx_worlds` | 16 | 边界窗口内的记录，上下文逐行归属随之更新 |
| `bm25` | 16 | 同上——检索文本按世界裁剪，归属变则裁剪范围变 |
| 其它（`id`/`ep`/`idx`/`start`/`end`/`t`/`text`/`ctx`/`anchor`） | **0** | 完全不变 |

**没有任何记录的 `id` 或 `text` 变化**，所以历史基线与记录引用的可追溯性不受影响。

```
docs.jsonl 旧 d48bac6ad18e2c2ee3cc7d9851506132
docs.jsonl 新 9a9e0c18ac163b7ba94d60acc58891a3
```

### 5.2 计数

| | 修改前（P0-3） | 修改后（P0-4） | 差 |
|---|---|---|---|
| normal | 11536 | **11542** | +6 |
| crash | 1534 | **1528** | −6 |
| 合计 | 13070 | 13070 | 0 |

### 5.3 检索指标：无变化

这是本轮最需要确认的一点——改世界边界会不会把指标「改好」或「改坏」。

```
[旧基线 vs 新索引 + legacy 口径]  逐题差异 0 处
```

**60 题逐题、全字段（recall / precision / MRR / n_hits / forbidden_hits /
wrong_world / near_dup / top 排序与文本）0 差异。** 原因是这 6 条记录没有被任何评测题覆盖。

v2 口径与旧基线的差异也仅有 3 处，全部是 `forbidden_hits` 的表示法：
`["04@0:07:46"]` → `["04:141"]`、`["06@0:12:20"]` → `["06:243"]`、
`["04@0:21:38"]` → `["04:410"]`（同一批记录的两种写法，指向同一条）。

| 口径 | Recall@5 | Precision@5 | MRR |
|---|---|---|---|
| 旧基线 all | 64.65% | 19.95% | 0.5985 |
| **v2 新口径 all** | 64.65% | 19.95% | 0.5985 |
| 旧基线 main | 73.02% | 25.00% | 0.7183 |
| **v2 新口径 main** | 73.02% | 25.00% | 0.7183 |
| 旧基线 low | 50.00% | 11.79% | 0.3889 |
| **v2 新口径 low** | 50.00% | 11.79% | 0.3889 |

`radius_sweep.py` 等价性校验通过：半径 12 = 73.0%，与基线一致。

---

## 六、测试结果

### 6.1 全量

```
pytest scripts/  →  203 passed, 164 subtests passed
```

基线 185 → 203，新增 18 项（`test_world_boundary.py` +6、`test_record_id.py` +12）。

### 6.2 新增：`scripts/test_world_boundary.py`（13 项，原 7 项）

新增 6 项：

| 测试 | 钉什么 |
|---|---|
| `test_01b_restore_basis_is_objective_world` | 口径是 `objective_world`；`movie_confirm` 独立存在且 ≠ 恢复点；两个锚句字符串正确 |
| `test_08_transition_count_and_span` | 过渡段恰 6 条，全在 `[restore, confirm)` 内，**窗口内无漏标** |
| `test_09_transition_world_is_normal` | 过渡段 `world` 必须是 normal（不能靠 `transition` 兜底） |
| `test_10_transition_reason_recorded` | `transition` 存原因字符串而非 bool；非过渡记录显式 `None` 而**不是缺字段** |
| `test_11_transition_is_independent_of_world` | 字段独立性：存在 normal+非过渡记录；过渡记录不带 `speaker` |
| `test_12_boundary_world_counts` | 世界总量 11542 / 1528 与 `meta.transition.total` 自洽 |

修改 2 项：`test_01` 断言 `"2:33:26"` → `"2:33:01"`；`test_02` 恢复点常量同步。

**失败先于修复已验证**：把 P0-3 的旧索引喂给新测试：

```
FAILED test_01_restore_point_detected
FAILED test_01b_restore_basis_is_objective_world
FAILED test_02_no_crash_after_restore
FAILED test_08_transition_count_and_span
FAILED test_10_transition_reason_recorded
FAILED test_11_transition_is_independent_of_world
FAILED test_12_boundary_world_counts
7 failed, 6 passed
```

### 6.3 新增：`scripts/test_record_id.py`（12 项）

| 测试 | 钉什么 |
|---|---|
| `test_01` | 每条引用都有非空 `record_id` |
| `test_02` | `record_id` 都能在索引里解析到 |
| `test_03` | 同一题内 expected/partial/forbidden 各自无重复 `record_id` |
| `test_04` | 索引 doc 的 `id` 全局唯一（`record_id` 的取值基础） |
| `test_05` | 60 题全部迁移，新旧题号序列一一对应 |
| `test_06` | 迁移不增删引用：每题引用条数必须与旧格式一致 |
| `test_07` | **真正踩到冲突时间码的引用，迁移后指向不同记录**（B05→03:83、A11→03:84） |
| `test_08` | 旧格式文件与历史基线的 md5 与迁移时记录一致，未被改写 |
| `test_09` | 迁移报告覆盖全部 67 条引用，无未决策的歧义 |
| `test_10` | 两处歧义的人工决策留痕且带判定依据 |
| `test_11` | `retriever.py` 里不出现 `transition`（本轮不改检索逻辑） |
| `test_12` | 索引里没有凭空出现 `speaker` |

**失败先于修复已验证**：把旧的 `questions.jsonl` 当作 v2 格式喂进去：

```
FAILED test_01_all_refs_have_record_id
FAILED test_02_record_id_resolves_in_index
FAILED test_07_ambiguous_refs_split
3 failed, 6 passed, 3 skipped
```

### 6.4 用户要求的 5 项回归

| # | 要求 | 结果 |
|---|---|---|
| 1 | NORMAL / CRASH 的记录数与边界 | ✅ 11542 / 1528；切点后首条 normal = `消失:2068 2:33:01 我健康地出院了`；crash 最后一条 = `消失:2067 2:33:00 第二天` |
| 2 | 6 条过渡记录均有正确标记 | ✅ `消失:2068`–`2073`，全部 `transition="restore_confirm_gap"` 且 `world="normal"`，窗口内无漏标 |
| 3 | 同集同时间码的多条记录可唯一引用 | ✅ `record_id`= 索引 `id`，全局唯一（`test_04`）；歧义组已拆（`test_07`） |
| 4 | 原有评测问题及历史基线仍可追溯 | ✅ 60 题题号一一对应（`test_05`）；`questions.jsonl` md5 `7a3ae1e2...` 未变；两份基线 md5 未变（`test_08`） |
| 5 | `seed_nagato_lines.md` 未被覆盖 | ✅ md5 `60672b3f78451a2e0def5d3412540e03`，`git status` 对该文件为空 |

### 6.5 历史资产完整性

| 文件 | md5 | 状态 |
|---|---|---|
| `data/retrieval_eval/questions.jsonl` | `7a3ae1e2ddc83c49002b413eea4acfe0` | 未改动（迁移报告记录迁移前后一致） |
| `data/retrieval_eval/baseline_20261026.json` | `e4a10c0b3aa23afdef56851ea6c8be32` | 未改动 |
| `data/retrieval_eval/baseline_20261026_after_p0.json` | `815571d4a710cddf9c5386dbd8add80e` | 未改动 |
| `data/seed_nagato_lines.md` | `60672b3f78451a2e0def5d3412540e03` | 未改动 |

---

## 七、改动文件清单

**代码**

- `build_corpus.py` — 锚点改为 `我健康地出院了`；新增 `MOVIE_CONFIRM_TITLES`、
  `detect_world_confirm()`、`is_transition()`；doc 加 `transition`；meta 加 6 个字段
- `scripts/run_retrieval_eval.py` — 新增 `--eval {legacy,v2}`、`record_id()`、`ref_keys()`；
  `top[]` 增加 `record_id` 与 `transition`
- `scripts/migrate_eval_record_id.py` — **新建**，旧格式 → `record_id` 迁移 + 唯一性断言
- `scripts/export_precision_review.py` — 兼容 v2 口径；边界文档改为读 `meta.json` 反映真实状态

**测试**

- `scripts/test_world_boundary.py` — 7 项 → 13 项
- `scripts/test_record_id.py` — **新建**，12 项

**数据**

- `data/index/docs.jsonl` / `meta.json` — 重建
- `data/retrieval_eval/questions_v2.jsonl` — **新建**（60 题新格式）
- `data/retrieval_eval/migration_20261026.json` — **新建**（逐条映射 + md5 + 状态分布）
- `data/retrieval_eval/record_id_decisions.json` — **新建**（2 处歧义决策 + 依据）
- `data/retrieval_eval/precision_review/` — 3 个文件随新索引刷新（`ctx_worlds` 数据 + 边界文案）

**文档**

- `docs/WORLD_BOUNDARY.md` — **新建**，字段定义 + 下游约定 + 未验证项
- `docs/RETRIEVAL_EVAL.md` — 新增 record_id 章节；复现命令更新为 v2
- `PLAN.md` / `README.md` / `data/README.md` / `docs/VALIDATION.md` / `docs/SOURCES.md` — 计数与锚点同步

---

## 八、仍未验证的项目

| 项 | 状态 | 原因 / 下一步 |
|---|---|---|
| **Q-4 排序未按加权分** | 未修，保留 P1 | 用户明确保留。实测 29/52 题 top-5 分数非降序；「按加权分重排 0/52 对得上 / raw 序去重不重排 52/52 精确复现」 |
| **Q-5 相关性复核表** | 未填，等用户 | 用户明确要求人工填写。`precision_review/02_worksheet.csv` 等文件已就绪 |
| **Precision@5 的人工判定值** | **未计算** | 用户要求「不计算未经人工判定的 Precision@5」。现有 19.95% 全部来自机器标注，不是人工结论 |
| **6 条过渡记录作为角色表达参考的适切性** | 未验证 | 需人工判断「这 6 句像不像长门会说的话」，且说话者未知（无法确认是谁说的） |
| **该区间的评测覆盖** | 未补题 | 现有 60 题完全不覆盖 2:33:01–2:33:21；改动的指标影响为 0，补题属 P1 |
| **`transition` 在生成侧的用法** | 未接入 | 提示词构建 / 记忆写入是否读该字段属 P1 设计；本轮禁止改提示词 |
| **TV 28 集是否也有归属争议段** | 未排查 | 本轮只处理《消失》恢复点；TV 全 normal、无切点，但没做逐条语义排查 |
| **`03:83` / `03:84` 的取舍** | 已按记录级证据定点，非相关性判定 | 若用户复核后认为标注本身有误，改 `record_id_decisions.json` 后重跑迁移即可 |
| **去重半径 12 的实际作用** | 未改 | F-3 已查明：60 题里从未触发（最短距离恰为 12），`近邻重复 0.0%` 是空转 |

---

## 九、下一步

按用户要求，**到此停止，等待盲审**。P1 暂不启动。

盲审时建议按这个顺序看：

1. `docs/WORLD_BOUNDARY.md` §二 的两个时刻表 —— 确认「客观世界 / 角色认知」分离这个前提没有理解错；
2. `data/retrieval_eval/record_id_decisions.json` —— 确认 `B05` / `A11` 的定点与你的意图一致；
3. `data/retrieval_eval/precision_review/02_worksheet.csv` —— 逐条填相关性（Q-5）；
4. 若对 `transition` 的下游用法有别的想法，在 `docs/WORLD_BOUNDARY.md` §四 上加约束。
