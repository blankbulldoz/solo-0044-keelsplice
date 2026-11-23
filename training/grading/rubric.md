# 盲审评分判据（评分方使用，盲判阶段不向学员开放）

## 总原则

1. **只给证据分，不给感觉分。** 每个得分点都必须能在学员答卷引用的可观察证据中核实：
   具体字段（如 `capture_digest`、`state`、`roots`）、只读 SQL 的结果，或
   `recheck_kits.py` 输出的具名检查项。写“看起来一样/不一样”“应该没问题”不计分。
2. **决定与证据分离计分。** 决定正确但给不出证据只得决定分；证据齐全但结论错误，
   证据分照给，决定分不给。鼓励学员先记录证据再下结论。
3. **不评分的内容：** 捕获 ID（每次构建由 `uuid4` 随机生成）、文件绝对路径、时间戳。
   这些不是稳定可观察事实，判据不引用它们。
4. **可重复性：** 运行 `python3 training/build/build_kits.py` 会重建全部素材，
   再运行 `recheck_kits.py` 即可复现本判据引用的每一个布尔结论与摘要关系。
   单次构建的精确哈希记录在 `evidence_reference.json`，仅作对照，不作硬性得分前提。

## 每个交接包的期望可观察结论

下表的检查项名称与 `recheck_kits.py` 输出一致；`T` 表示该检查应通过（true），
`F` 表示应失败（false），`—` 表示该包无此材料、检查不适用。

| 检查项 | alpha | beta | b1 | b2 | gamma |
| --- | --- | --- | --- | --- | --- |
| `certificate_file_present` | T | T | T | T | F |
| `deliverable_file_present` | T | T | T | T | F |
| `file_sha256_matches_certificate` | T | T | T | T | — |
| `sqlite_integrity_ok` | T | T | T | T | — |
| `foreign_key_check_clean` | T | T | T | T | — |
| `sealed_fingerprint_matches_certificate` | T | **F** | T | T | — |
| `stored_certificate_equals_handed_certificate` | T | T | T | T | — |
| `claimed_root_matches_sealed_root` | T | T* | T | T | T |
| `sealed_root_present_in_deliverable` | T | **F** | T | T | — |
| `sealed_only_no_artifact` | — | — | — | — | T |

\* beta 的声明根（P3）与其被篡改后的封存根（P3）彼此一致，所以该项为 T；
矛盾体现在另外两处：封存指纹不等于证书指纹、封存根 P3 不在交付物中（交付物实际只有 P1）。
这正是“证书真实但归属错误”难以被表面标签发现的原因。

## 成对对比的期望结论

| 对比项（`--pair` 输出字段） | A 对 alpha/beta | B 对 b1/b2 |
| --- | --- | --- |
| `file_bytes_identical` | T | F |
| `same_capture_digest` | T | T |
| `same_schema_digest` | T | T |
| `same_logical_fingerprint` | T | T |
| `counts_identical` | T | T |
| `page_sizes` 两侧不同 | 否（同为 4096） | 是（4096 vs 8192） |

关键区分：B 对字节不同但 `capture_digest`、`schema_digest`、逻辑指纹、计数全部相同，
且唯一可观察差异是物理页大小，故为**合法物理重排**，关系内容未变。

## 期望处置决定

| 包 | 正确决定 | 决定成立所必须引用的最小证据集 |
| --- | --- | --- |
| alpha | 可交接 | 文件 SHA=证书 SHA；封存指纹=`capture_digest`；声明根=封存根且在交付物中；完整性/外键通过 |
| beta | 暂缓 | 封存指纹 ≠ 证书 `capture_digest`；且封存根 P3 不在交付物（交付物仅含 P1）；文件 SHA 仍匹配（证明这是归属问题而非文件损坏） |
| b1 | 可交接 | 同 alpha 的四项自洽证据 |
| b2 | 可交接 | 同 b1 的自洽证据；并能说明与 b1 的字节差异仅来自页大小，`capture_digest` 与逻辑指纹相同 |
| gamma | 继续操作 | `state=sealed`、无 artifact、无 certificate；流程未完成而非数据缺陷；未认证不可交接 |

## 评分表（满分 100）

### 一、单包处置（每包 12 分，共 60 分）

每个包按四档：

- 决定正确（4 分）：与上表一致。
- 关键归属/状态证据（4 分）：
  - alpha/b1/b2：引用了“封存指纹 = 证书 `capture_digest`”这一归属核对。
  - beta：明确指出封存指纹 ≠ `capture_digest`，并指出根 P3 不在交付物。
  - gamma：明确引用 `state=sealed` 且无证书、无子库。
- 字节与完整性证据（2 分）：引用文件 SHA 比对与 `integrity_check`/`foreign_key_check`
  （gamma 无此材料，改为引用状态机阻止未认证交接的事实）。
- 反方证据（2 分）：能说出为什么不是另外两个决定，且依据可观察事实而非主观判断。

beta 包特别说明：若学员仅凭“文件 SHA 匹配、显示已认证”判为可交接，决定分 0；
若学员发现两项归属矛盾中至少一项并据此暂缓，证据分按实际引用项给分。

### 二、成对判断（每对 15 分，共 30 分）

- A 对（15 分）：
  - 指出两包字节相同（3 分）。
  - 指出不能同等交接，差异在封存归属而非文件（6 分）。
  - 用 `capture_digest` 与封存指纹、交付物实际根两处证据定位 beta（6 分）。
- B 对（15 分）：
  - 指出两包 SHA-256 不同（3 分）。
  - 用 `capture_digest`/`schema_digest`/逻辑指纹/计数相同论证关系内容未变（8 分）。
  - 用页大小差异正确归因为物理重排，结论仍为可交接（4 分）。

### 三、方法与可复核性（10 分）

- 盲判阶段写下了可证伪的预期，再用独立只读连接复核（4 分）。
- 区分了 `sha256`（认证字节）与 `capture_digest`（认证封存归属）两个概念（3 分）。
- 全程未依赖工作台进程或“已认证”标签本身，证据可由第三方重跑复现（3 分）。

## 评级建议

- 90–100：能独立识别归属错误与物理重排，证据链完整。
- 75–89：决定全对但个别证据引用不充分，或成对论证不完整。
- 60–74：能抓字节/完整性问题，但漏掉 beta 的归属矛盾。
- 60 以下：以标签或文件外观下结论，未建立证书—封存—交付物的三方核对。
