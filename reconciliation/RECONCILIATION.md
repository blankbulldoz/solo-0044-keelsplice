# 跨版子库对账表：A 组（v1 结构）× B 组（v2 结构）

生成时间（UTC）：2026-11-24 05:23:50；由 `python3 -m examples.reconcile_handoff` 重新生成可整体复算。

## 1. 交付材料清单

| 组别 | 文件 | schema_digest | SHA-256 | 字节 | 捕获 ID | 配方 |
| --- | --- | --- | --- | --- | --- | --- |
| A 组（v1 结构） | `fixtures/group_a_v1.sqlite` | `c44f76b50609a3d2…` | `8c845e3a76fa81bf…` | 32768 | `ae82d0f489ad45498089c25680e6455c` | swatch v1 |
| B 组（v2 结构） | `fixtures/group_b_v2.sqlite` | `65c11874a5bf4e5b…` | `72bc3511872fa164…` | 32768 | `bed3a7cef859417795e4c3a31e6d9175` | swatch v2 |

每份 `fixtures/*.certificate.json` 记录捕获 ID、配方版本与完整认证摘要。接收方先核对文件 SHA-256，再用独立 SQLite 连接只读打开复算（见第 7 节）。

## 2. 结构差异与原比较接口拒绝实录

两版子库存在两处结构差异（机读对照见 `evidence/structure_diff.json`，原始 DDL 见 Q-DDL）：

1. `frame` **仅新增列** `caption TEXT`（Q-FRAME-COLS：A 侧 ['frame_id', 'mark', 'note']，B 侧 ['frame_id', 'mark', 'note', 'caption']），旧列与行内容不变；
2. `substrate` 复合主键 `(region, code)` **排序规则更换**：`COLLATE NOCASE` → `COLLATE BINARY`，大小写折叠身份不再成立。

```sql
-- A 组（v1）substrate 定义
CREATE TABLE substrate (
    region TEXT COLLATE NOCASE NOT NULL,
    code TEXT COLLATE NOCASE NOT NULL,
    texture BLOB NOT NULL,
    PRIMARY KEY (region, code)
) WITHOUT ROWID;
-- B 组（v2）substrate 定义
CREATE TABLE substrate (
    region TEXT COLLATE BINARY NOT NULL,
    code TEXT COLLATE BINARY NOT NULL,
    texture BLOB NOT NULL,
    PRIMARY KEY (region, code)
) WITHOUT ROWID;
```

因此原比较接口 `POST /compare` 按同模式前提拒绝两库：HTTP 409 `schema_conflict`（请求与响应实录见 `evidence/compare_rejection.json`）。本对账表改为在两份交付文件上直接对账。

## 3. 可合并的统计（两边数值一致）

| 编号 | 统计 | 口径（查询依据） | 共同值 |
| --- | --- | --- | --- |
| M01 | assay 行数 | Q-ROWCOUNTS（tab='assay' 行） | `3` |
| M02 | cassette 行数 | Q-ROWCOUNTS（tab='cassette' 行） | `3` |
| M03 | frame 行数 | Q-ROWCOUNTS（tab='frame' 行） | `3` |
| M04 | assay.length_um 最小值 | Q-LEN-AGG（min_um 列） | `3.25` |
| M05 | assay.length_um 最大值 | Q-LEN-AGG（max_um 列） | `9.0` |
| M06 | cassette 11（opal）的 assay 数 | Q-CASSETTE（serial=11 行） | `1` |
| M07 | cassette 12（agate）的 assay 数 | Q-CASSETTE（serial=12 行） | `1` |
| M08 | cassette 13（garnet）的 assay 数 | Q-CASSETTE（serial=13 行） | `1` |
| M09 | assay S-01 的 frame 数 | Q-FRAME-PER-ASSAY（mark='S-01' 行） | `1` |
| M10 | assay S-02 的 frame 数 | Q-FRAME-PER-ASSAY（mark='S-02' 行） | `2` |
| M11 | assay S-04 的 frame 数 | Q-FRAME-PER-ASSAY（mark='S-04' 行） | `0` |
| M12 | substrate ('West','Basalt') 的 assay 数 | Q-PER-SUBSTRATE（region='West' 行） | `1` |
| M13 | substrate ('West','Basalt') 的 texture | Q-SUBSTRATE-ROWS（region='West' 行） | `x'FE'` |

## 4. 值有变化的统计（身份可对齐、数值不同）

| 编号 | 统计 | 口径（查询依据） | A 组（v1） | B 组（v2） | 差异原因与行级依据 |
| --- | --- | --- | --- | --- | --- |
| C01 | substrate 行数 | Q-ROWCOUNTS（tab='substrate' 行） | `2` | `4` | v2 身份拆分新增 ('east','MICA')、('EAST','mica') 两行；行清单见 Q-SUBSTRATE-ROWS，身份影响见第 5 节 |
| C02 | assay.length_um 合计 | Q-LEN-AGG（total_um 列） | `17.75` | `18.0` | S-02 长度 5.5 → 5.75；行级依据 Q-ASSAY-ROWS 中 mark='S-02' 行（生成列 doubled 11.0 → 11.5 同步） |
| C03 | assay.length_um 均值 | Q-LEN-AGG（avg_um 列） | `5.916666666666667` | `6.0` | 同 C02 |

## 5. 身份不足以对齐的统计

**X01　各 substrate 身份的 assay 数（East/Mica 族）**　口径：Q-PER-SUBSTRATE。

| 身份 (region, code) | A 组（v1，NOCASE 折叠） | B 组（v2，BINARY 拆分） |
| --- | --- | --- |
| `('East','Mica')` | **2**（折叠族合计） | 0 |
| `('east','MICA')` | —（折叠于上行） | 1 |
| `('EAST','mica')` | —（折叠于上行） | 1 |

v1 中 `('East','Mica')` 一个身份折叠了全部大小写拼写（S-01、S-02 均挂其下，见 Q-ASSAY-ROWS 的 region/code 列）；v2 改为 BINARY 后同一族拆成三个实存身份，旧身份与任一新身份之间不存在一一对应，**该族按身份的统计无法逐项对齐**。

独立核对依据：

- 排序规则探针 Q-PROBE-NOCASE（`region='EaSt' AND code='MiCa'` 命中数）：A 组 1，B 组 0 —— 同一查询在两版语义不同；
- 折叠旁证 Q-PROBE-FOLD（显式 `COLLATE NOCASE` 折叠后的族 assay 总数）：A 组 2，B 组 2 —— 族合计一致，仅证明数据未丢失，不能恢复逐项对应；
- 身份清单 Q-SUBSTRATE-ROWS：A 组 2 个身份，B 组 4 个身份；
- 外键拼写 Q-ASSAY-ROWS：S-01 `('east','MICA')`、S-02 `('EAST','mica')` 两侧行内容逐字节相同，但 v2 中改挂新身份；
- 结构证据 Q-DDL / `evidence/structure_diff.json`：排序规则 NOCASE → BINARY。

## 6. 口径备注

- `frame.caption` 为 v2 新增列，A 侧无此口径：Q-CAPTION-NONNULL 仅 B 侧可算（B 侧 2 行非空），A 侧执行报错 `no such column: caption`（见 stats 证据文件），不参与三桶分类；
- assay S-03（code 为 NULL）与 frame 104（mark 为 NULL）按闭包规则不入选，两版一致；本表为子库对子库，不对源库全量；
- assay S-04 外键拼写在 v2 规范化（`('west','basalt')` → `('West','Basalt')`），身份 ('West','Basalt') 两侧可对齐，相关统计见 M12、M13。

## 7. 复验指引

1. 完整性：`sha256sum fixtures/*.sqlite`，对照第 1 节与证书中的 SHA-256；
2. 可打开性：用独立 SQLite 连接只读打开两份 fixture，Q-INTEG 应返回 `ok`，Q-FKCHECK 应为空（两侧均已实测）；
3. 逐项复算：`evidence/queries.sql` 中每条查询带 Q- 编号，在对应 fixture 上执行后与 `evidence/stats_group_a_v1.json` / `evidence/stats_group_b_v2.json` 中同编号结果逐行比对，即可独立核对本表每一项；
4. 整体重算：在仓库根目录执行 `python3 -m examples.reconcile_handoff`，会重新走「配方 → 封存 → 构建 → 认证 → 下载」全流程并重建本目录。
