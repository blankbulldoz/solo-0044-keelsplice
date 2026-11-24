# 显微镜交接批次（数值班 / 关系班）

本目录是从 `examples/microscope.sql` 构造的一小批**真实捕获**（经工作台 JSON 服务封存、构建、认证并下载的 SQLite 字节），用于两个专业班组的交接验收。

## 目录内容

| 文件 | 说明 |
| --- | --- |
| `batch.py` | `prepare` 用真实 JSON 服务重建整批；`verify` 用独立 SQLite 连接复核已交付字节并整批重建对照 |
| `control.json` | 对照依据：每次捕获的源状态变更、证书、产物摘要、逐列差异分类、可交付/不可交付结论、拒绝场景 |
| `artifacts/*.sqlite` | 6 个已认证子库字节，摘要见 `control.json` |

`prepare` 会先清掉 `artifacts/*.sqlite` 再写入。`control.json` 里的随机 `capture_id`、`generated_at` 与 `runtime` 每次运行可以不同；其余字段（产物摘要、证书、行事实、差异分类、拒绝结果）在复跑时必须一致，`verify` 会逐项比对。

## 复跑

```sh
python3 -m handoff.batch prepare    # 重建 artifacts/ 与 control.json
python3 -m handoff.batch verify     # 复核字节 + 重建整批并比对
```

`verify` 的两段检查：

1. 逐个产物：SHA-256 必须等于记录值，`PRAGMA integrity_check` 为 `ok`，`PRAGMA foreign_key_check` 为空，并且通过只读连接读到的行必须与 `control.json` 的 `facts` 完全一致。
2. 整批重建：在临时目录里重新 `prepare` 一次，除随机 `capture_id` 与时间戳外，所有摘要、逐列差异和分类必须与已提交的 `control.json` 一致。

## 捕获状态（每次从原始样例重新播种）

根为 `substrate ["east","MICA"]`（实存键 `["East","Mica"]`），反向边为 `substrate → assay`，`row_limit=20`，闭包固定为 5 行：`substrate 1 / assay 2 / cassette 2 / frame 0`。

| 标签 | 源变更 | 认证摘要（sha256 前 16 位） |
| --- | --- | --- |
| `baseline` | 无（原始样例） | `a55b2cffc863cc67` |
| `numeric-only` | `assay.length_um` 3.25→4.75（生成列 `doubled` 随之 6.5→9.5） | `e5a7f7e7bac17e41` |
| `parent-only` | `substrate.texture` `x'0000FF'`→`x'1020'` | `8452878b888469f0` |
| `both` | 上述两项同时发生 | `e3cc6890a2a667bf` |
| `frame-only` | `frame`（闭包外）`note` 改动 | `a55b2cffc863cc67`（与 baseline 逐字节相同） |
| `case-key` | 父键大小写改为 `EAST`/`MICA` | `17d5665e13140a45` |

完整摘要、每表计数、`capture_digest` 与逐列差异见 `control.json`。

## 正式分派（只给能隔离条件的最小一批）

### 数值班：`baseline` + `numeric-only`

- 比较 `baseline→numeric-only`：只有 `assay` 的 `["S-01"]` 被修改，改动列是 `length_um` 与生成列 `doubled`。
- 同一次比较中 `substrate`、`cassette`、`frame` 全部无新增、无移除、无修改；`assay` 的外键列 `region`/`code`/`cassette` 不变。
- 因此生成值的变化可以单独归因，父关系保持不变这一点是**同一份认证字节**上的事实，不靠口头说明。

### 关系班：`baseline` + `parent-only`

- 比较 `baseline→parent-only`：只有 `substrate` 的 `["East","Mica"]` 被修改，改动列是 `texture`。
- 同一次比较中 `assay` 两行（含 `length_um` 与 `doubled`）完全未修改，`cassette`、`frame` 也未修改。
- 因此父对象的变化可以单独归因，测量值与生成值保持不变是同一份认证字节上的事实。

两班共用同一个 `baseline.sqlite` 作为对照；每班只需 2 个文件，合计 3 个文件（`baseline` 共用）。

## 不能作为认证交付的项

以下状态**也生成了文件**放在 `artifacts/`，仅为让班组看到反例，**不得计入认证交付**：

| 项 | 原因 |
| --- | --- |
| `both.sqlite` | 两种变化同时发生。`baseline→both` 同时列出 `assay S-01` 与 `substrate` 的修改；它的 `assay` 修改与数值班那一对完全相同，因此单看行修改无法判断父对象是否也变了。`control.json` 用 `same_assay_row_modification_as_numeric_pair: true` 固定了这一事实。 |
| `frame-only.sqlite` | 改动在闭包之外，产物与 `baseline` 逐字节相同（SHA-256 一致）；不能证明任一条件。 |
| `case-key.sqlite` | 父键只改大小写，NOCASE 下仍连接同一父行，但带类型主键从 `["East","Mica"]` 变成 `["EAST","MICA"]`；比较报告的是新增/移除而不是父对象修改，语义不稳定。 |
| `numeric-only→parent-only` 这一对 | 两组变化同时出现（`both_changes: true`），与 `baseline→both` 同类，不能替代上面的两对。 |
| 仅封存未构建的捕获（`sealed_download`） | `GET /captures/{id}/database` 返回 409 `fixture_not_certified`，没有可交接字节。 |
| 父键改写为 `North/Slate` | 配方根无法解析，封存返回 422 `missing_root`，没有产物。 |
| 模式漂移后的捕获 | 老配方封存返回 409 `schema_conflict`；新模式只能用新版本配方（`new_version: 2`），与已认证材料的模式摘要不同，比较返回 409 `schema_conflict`。该模式漂移捕获**没有**写入 `artifacts/`。 |

另外，`frame` 表在本闭包中为 0 行，因此本批材料**不能**用于验收 `frame` 相关的关系证据或表内行携带。

## 对照依据

- 每份产物的真实值事实（`substrate`、`assay`、`cassette` 全表行、`integrity_check`、`foreign_key_check`、`joined_assay_rows`）记录在 `control.json` 的 `captures[*].facts`。
- 每一对的分类来自这些事实的**独立只读连接**读取，而不是重新调用工作台；同时附上 `compare_api` 字段保存工作台 `/compare` 的原始报告，两种口径可以互相对照。
- 认证摘要（`artifact.sha256`、`certificate.capture_digest`）与源变更（`captures[*].mutations`）一并记录，任何一方被替换都会在 `verify` 中报警。
- `handoff/artifacts/.gitignore` 里的 `!*.sqlite` 抵消了仓库根 `.gitignore` 的 `*.sqlite`，让这 6 个认证字节可以随对照依据一起入库；仓库其他位置的 `.sqlite` 仍不跟踪。

## 已知差异对交接的影响

- 工作台 `/compare` 只报告「哪些带类型主键的行变了」，不报告**哪些列**变了。`control.json` 的 `pairs[*].assay.modified[*].columns` / `substrate.modified[*].columns` 是独立连接补出的列级证据，班组应以它为准判断「测量值」与「父对象」。
- 生成列 `doubled` 只出现在 `assay_columns[*].hidden=3`，子库中由 SQLite 计算；事实读取同时记录存储值与计算值（`doubled` 在行内）。因此数值班的验收应同时核对 `length_um` 与 `doubled` 一起变化。
