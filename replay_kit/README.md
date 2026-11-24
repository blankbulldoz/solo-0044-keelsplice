# 可公开重放的关系争议样本包（替代数据）

本包面向外部复核组，用于在**不接触工位内部库键值**的前提下，公开重放
Keelsplice 关系样本工作台的选样、追踪、构建与认证流程。所有数据均为合成值，
表结构与内部演练库逐字同构（同一 `schema_digest`），但不含任何内部根值、
标记、编号、标签或 BLOB。

## 1. 目录内容

| 路径 | 用途 |
| --- | --- |
| `microscope_surrogate.sql` | 合成替代源库的建库脚本（唯一事实来源，可审计每一个字面量） |
| `replay_surrogate.py` | 一键重放驱动：起真实 JSON HTTP 服务，跑全部正确/错误对照并独立复核交付字节 |
| `control_inputs/` | 每个配方与错误诱导各一份 JSON，含请求体与预期结论，可逐条对照 |
| `build/surrogate_handoff_A.sqlite` | 已认证、可独立打开的替代子库（单 NOCASE 根场景） |
| `build/surrogate_handoff_B.sqlite` | 已认证替代子库（双 NOCASE 根 + 部分 NULL 关系场景） |
| `build/surrogate_handoff_C.sqlite` | 已认证替代子库（NULL 关系行单独做根场景） |
| `build/run/` | `--keep` 重放的完整产物：替代源库、工作台状态库、含三子库 SHA-256 的证书清单 |
| `LIMITATIONS.md` | 哪些原始结论只凭替代材料仍不能确认 |

## 2. 重放方式

需要 Python 3.11+ 与 SQLite 3.37+，仅用标准库；在仓库根目录执行：

```sh
python3 -I replay_kit/replay_surrogate.py --keep replay_kit/build/run
```

脚本会重建替代源库、启动回环 HTTP 服务、登记三组配方、驱动五类错误诱导、
封存三个捕获、构建并认证三个子库，再用**独立的只读 SQLite 连接**核对
`integrity_check`、`foreign_key_check`、生成列、BLOB 与逐表行数。
不带 `--keep` 时产物落在临时目录并在结束时清理。

只想直接查看交付子库时，无需任何服务：

```sh
sqlite3 replay_kit/build/surrogate_handoff_B.sqlite '.tables'
sqlite3 replay_kit/build/surrogate_handoff_B.sqlite 'PRAGMA foreign_key_check;'
```

## 3. 替代数据保留的区分能力（结构角色 → 合成替身）

| 内部库中的结构角色 | 替代材料中的合成替身 | 保留的区分能力 |
| --- | --- | --- |
| 复合 NOCASE 主键父表（2 行） | `substrate`: `(North,Quartz)`、`(South,Granite)`，两列仍为 `COLLATE NOCASE`、`WITHOUT ROWID` | 大小写不同的输入拼写命中同一实存根 |
| 两条 NOCASE 复合关系（assay→substrate，edge 0） | `T-10 north/QUARTZ`、`T-11 NORTH/quartz` → `North/Quartz`；`T-13 south/granite` → `South/Granite` | 父键查找按 NOCASE 比较语义；封存与追踪只认实存拼写 |
| 部分 NULL 复合关系 | `T-12 (South, NULL)`：子键含 NULL 分量，无 substrate 父行弧；`frame 204` 的 `mark` 为 NULL | NULL 分量不产生必需父行、NULL 外键不扇出，与普通关系明确区分 |
| 盒循环对（自引用表） | `cassette 41→42→41`，另有 `prior IS NULL` 的尾盒 `43` | 遍历按行身份去重、循环不会无限展开；尾盒与循环成员的入选路径不同 |
| 生成列、BLOB、部分索引 | `doubled = length_um*2` STORED、`texture` BLOB、两个原索引原样保留 | 子库重建后生成值、字节与索引定义与封存一致 |

数值替身与内部值刻意不同（例如 `length_um` 取 4.5/7.25/1.75/6.0，
盒序号取 41/42/43，帧号取 201–204），因此**不能**用替代材料反推内部行值。

## 4. 对照输入与预期结论

### 4.1 驱动正确结论的输入（`control_inputs/correct_*.json`）

| 文件 | 配方要点 | 关键可观察结论 |
| --- | --- | --- |
| `correct_a_single_nocase_root.json` | 根 `substrate("north","QUARTZ")` + 反向跟随 assay edge 0 | 实存根规范化为 `("North","Quartz")`；闭包 `substrate=1, cassette=2, assay=2, frame=0`；尾盒 43 不入选；追踪该根有 2 条直接原因 |
| `correct_b_dual_nocase_roots.json` | 两个 NOCASE 根 + 显式 NULL 关系根 `T-12` + 跟随 frame edge 0 | 闭包 2/3/4/3；`T-12` 无法经 substrate 扇出到达（只能显式给根）且无 substrate 父弧；`frame 204`（NULL mark）不入选；尾盒 43 仅经 T-12/T-13 的盒父边到达 |
| `correct_c_null_relation_root.json` | 根直接取 `assay("T-12")`，不跟随任何反向边 | 闭包 0/1/1/0；substrate 父弧为空但 cassette 父边仍强制纳入盒 43；根身份由 `roots=[0]` 证明 |

### 4.2 驱动错误结论的输入（`control_inputs/wrong_*.json`）

| 文件 | 诱导的错误主张 | 工作台的实际判定 |
| --- | --- | --- |
| `wrong_1_trace_input_spelling.json` | 用登记时的输入拼写（而非实存键）即可追踪 | `404 row_not_captured`：封存后只认实存带类型主键，不伪造路径 |
| `wrong_2_partial_composite_key.json` | 复合根缺一个分量也能登记 | `422 invalid_key`，不产生配方版本 |
| `wrong_3_null_and_missing_roots.json` | NULL 主键分量可静默落到别的行；不存在的盒节点可当作根 | 前者登记期 `invalid_key`；后者登记形状合法但试算期 `422 missing_root` |
| `wrong_4_row_budget_truncation.json` | 预算不足时先截一部分样本封存 | `422 row_budget` 整体拒绝，无部分捕获（防止据此误判盒循环不存在） |
| `wrong_5_edge_and_schema_conflict.json` | 外键编号可随意引用；异模式捕获可直接对比 | `422 invalid_edge`；模式漂移后旧配方封存与跨模式对比均为 `409 schema_conflict` |

重放脚本对上述每一项都有断言；HTTP 状态码与错误码同时记录在对照 JSON 的
`expect` 字段中，复核组可离线比对。

## 5. 交付子库的独立校验

三个 `surrogate_handoff_*.sqlite` 均由工作台 `build` → `certify` 流程产出，
证书中的 SHA-256 与下载字节、ETag 三者一致；三个子库的摘要与计数见
`build/run/replay_manifest.json` 的 `handoff_files`（重新执行重放会确定性地
生成相同字节与相同 SHA-256）。任何一方都可用独立连接复核：

```sh
sqlite3 replay_kit/build/surrogate_handoff_A.sqlite 'PRAGMA integrity_check; PRAGMA foreign_key_check;'
sha256sum replay_kit/build/surrogate_handoff_A.sqlite
```

子库保留全部四张表（包括 0 行的 `frame`/`substrate`）、声明约束与两个显式
索引；盒循环在延迟外键装载后闭合，无悬挂引用。

## 6. 边界声明

替代材料只复现**结构与机制层面**的争议区分能力。原始库中具体行值、真实
业务根的身份与数量、真实时间线以及任何未纳入演练库结构的表，均无法从本包
确认，详见 `LIMITATIONS.md`。
