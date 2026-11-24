# 台账核对报告

对 `ledger.md`（公开抄本）逐行核对的结果。核对以真实工作台状态为准：
`state.sqlite`（recipes 5 版 / captures 5 次 / 证书 1 份）与 `microscope.sqlite`（源库现状）。
本报告列出的页面路径与结论在生成时已逐条对运行中的工作台验证（见 `generate.py` 输出）。

## 1. 材料与启动

```sh
python3 -m keelsplice --repository handoff/state.sqlite \
  --source microscope=handoff/microscope.sqlite --port 8794
# 打开 http://127.0.0.1:8794/workbench ，下列页面路径均相对此地址
```

交接时序（generate.py 剧本）：登记 swatch v1（根拼写 `east/MICA`，反向 assay，上限 20）→
登记 swatch v2（上限 1）并即停用 → v1 封存得捕获 A 并构建、认证、交付 → 源库 S-01 改长
3.25→4.75，v1 再封存得捕获 B → 源库 frame 加列 caption，登记 swatch v3 封存得捕获 C →
登记 mica-watch v1（拼写 `["east","MICA"]`）、v2（拼写 `["East","Mica"]`）分别封存得 D、E。

## 2. 权威来源与核对方法

权威顺序：状态库 recipes / captures 表与证书 ＞ 源库与关系目录（NOCASE 排序、主键列顺序）＞ 台账抄录。
每个台账行独立核对：先用行内线索在状态库中确定候选集，再按 §7 的规则补全缺抄；
候选唯一且其余抄录一致才算「可唯一核对」。

## 3. 结论总览

| 行 | 结论 | 定案依据 |
| --- | --- | --- |
| A1 | 可唯一核对 | 完整证书在证书集合中唯一 → 捕获 `40d2607b`…（短编号亦唯一） |
| A2 | 可唯一核对 | 短编号 `7f804ea3` 全库唯一且与配方版本一致；缺抄的 code 按 §7 补全 |
| A3 | 多种解释 | 候选捕获 A / B / C 三个都符合行内线索 |
| A4 | 证据矛盾 | 短编号 `7f804ea3` 属 v1；swatch v2 已停用且没有任何捕获 |
| A5 | 可唯一核对 | 短编号 `0d57f974` 全库唯一且与 v3 一致 |
| A6 | 证据矛盾 | 短编号 `40d2607b` 所指捕获的实存根是 `["East","Mica"]`，与抄录的 `["West","Basalt"]` 不符 |
| B1 | 多种解释 | 捕获 D、E 命中相同看起来的键，行内没有任何区分线索 |
| B2 | 可唯一核对 | 短编号 `38e97f6c` 全库唯一；缺抄的 region 按 §7 补全 |
| B3 | 证据矛盾 | 短编号 `38e97f6c` 属 mica-watch v2，与抄录的 v1 不符 |

## 4. 可唯一核对的行（真实页面路径）

### A1 —— 同组中凭完整证书定位的一行

台账抄了完整证书 SHA-256 `a55b2cffc863cc6745184cad1a91e4e9b62617c49d3641f54537be404cc393a0`（32768 字节）。
状态库中只有捕获 A 持有证书，且其 `certificate.sha256` 与抄录一致 → 唯一定位。
交付文件 `downloads/keelsplice-40d2607bbfcf454896b48e65ac6e6525.sqlite` 的字节摘要与证书相同，文件归属随之确定。

- 捕获详情（含证书）：`/workbench/captures/40d2607bbfcf454896b48e65ac6e6525`
- 认证子库下载（ETag 即证书摘要）：`/workbench/captures/40d2607bbfcf454896b48e65ac6e6525/database`
- 源配方：`/workbench/recipes/swatch/1`

### A2 —— 短编号唯一，部分键可补全

短编号 `7f804ea3` 在 captures 表全库唯一，归属 swatch v1，与抄录一致。
缺抄的 code 按 §7 规则补全：`["East", …]` 是主键 (region, code) 的前缀，源库中
region='East'（NOCASE）只有一行 → 补全为 `["East","Mica"]`，与捕获 B 的实存根一致。

- 捕获详情：`/workbench/captures/7f804ea3c7f94ef1b7ffd496aaf3a0fb`

### A5 —— 只留短编号的一行

短编号 `0d57f974` 全库唯一，归属 swatch v3。捕获页「源库实存根」栏直接给出
`["East","Mica"]`，无需键抄录。备注「勿与旧捕获对比」与状态一致：v3 绑定的模式摘要
`659c81663ead`… 与 v1 的 `c44f76b50609`… 不同，对比 A/C 返回 409。

- 捕获详情：`/workbench/captures/0d57f9744a16401589abaf9126725fe2`
- 源配方：`/workbench/recipes/swatch/3`

### B2 —— 短编号唯一，非前缀片段可补全

短编号 `38e97f6c` 全库唯一，归属 mica-watch v2。`[…, "Mica"]` 不是主键前缀，
但源库中 code='Mica'（NOCASE）只有一行，且与所属配方登记的根互相印证 →
补全为 `["East","Mica"]`。

- 捕获详情：`/workbench/captures/38e97f6c66ca4d4b86180afa4fc2a368`
- 源配方：`/workbench/recipes/mica-watch/2`

## 5. 仍有多种解释的行

### A3 —— 三个候选（swatch，根 `["East","Mica"]`，5 行 / 8 见证）

| 候选 | 配方版本 | 状态 | 模式摘要 | 内容区分点 |
| --- | --- | --- | --- | --- |
| `40d2607b`… | swatch v1 | 已认证 | `c44f76b50609`… | S-01 封存时为 3.25，持有证书 |
| `7f804ea3`… | swatch v1 | 已封存 | `c44f76b50609`… | S-01 封存时为 4.75 |
| `0d57f974`… | swatch v3 | 已封存 | `659c81663ead`… | frame 已加 caption 列 |

行内没有编号、版本、证书或内容线索，三个候选全部符合。即使假定「版本 1」仍剩
A、B 两个。对比页能区分 A/B（assay 修改 `["S-01"]`），但台账没有抄内容，对比结果
无法回挂到这一行。要定案需要补抄：短编号、或证书摘要、或版本加内容线索。

### B1 —— 两个捕获命中相同看起来的键，仍没有唯一归属

| 候选 | 配方版本 | 登记根拼写 | 实存根 |
| --- | --- | --- | --- |
| `373b10c5`… | mica-watch v1 | `["east","MICA"]` | `["East","Mica"]` |
| `38e97f6c`… | mica-watch v2 | `["East","Mica"]` | `["East","Mica"]` |

substrate 主键是 NOCASE，两个版本的输入拼写命中同一实存行；两次封存的实存根、
逐表计数、行内容完全一致（对比页显示「这两个捕获的行内容一致」）。台账抄的键与
两个捕获的实存根看起来相同，键线索对两者同时成立。生成时已验证：C、D、E 的封存
内容相同，即使补做认证，重建子库字节也相同，证书文件摘要同样无法区分 D、E。
唯一能把这一行定下来的线索是捕获编号或配方版本，而 B1 两者都缺 → 没有唯一归属。

## 6. 证据矛盾的行

### A4 —— 「v2 停用前抢封的一次」不成立

短编号 `7f804ea3` 在状态库中属 swatch **v1**（捕获页 `/workbench/captures/7f804ea3c7f94ef1b7ffd496aaf3a0fb` 显示
「swatch · 版本 1」）；配方页 `/workbench/recipes/swatch/2` 显示「已停用」，且
captures 表中不存在 version=2 的行。编号归属与版本抄录、备注叙述三处互相矛盾。

### A6 —— 根抄录与编号所指的捕获不符

短编号 `40d2607b` 指向捕获 A，其实存根为 `["East","Mica"]`。在
`/workbench/captures/40d2607bbfcf454896b48e65ac6e6525` 的追踪表单提交 `["West","Basalt"]` 返回
「封存结果中没有这个实存主键」。源库确有 West/Basalt 行，但它不在此次捕获的闭包内。

### B3 —— 「第一版的封存」张冠李戴

短编号 `38e97f6c` 属 mica-watch **v2**（捕获页 `/workbench/captures/38e97f6c66ca4d4b86180afa4fc2a368` 显示
「mica-watch · 版本 2」）；v1 的封存是 `373b10c5`…（`/workbench/captures/373b10c5c46a4e49a9479684c0b45fbe`）。

## 7. 允许用于补全的跨行线索

允许（全部落回权威状态，不落在台账他行）：

1. **短编号前缀唯一性**：短编号在 captures 表全库前缀匹配唯一，且行内其余抄录
   与该捕获一致时，可补全为完整编号（本库 5 个 8 位短编号已验证互不相同）。
2. **捕获 → 配方归属**：captures.(name, version) 外键到 recipes，可核对
   「配方 + 版本 + 编号」三元组，并取出配方登记的根拼写与行上限。
3. **目录的排序规则与主键顺序**：substrate 主键 (region, code) 为 NOCASE，
   输入拼写 `east/MICA`、`East/Mica` 等都归一到实存键 `["East","Mica"]`；
   部分键只在「按主键顺序的前缀」（如 `["East", …]`）或「片段在源库中唯一匹配」
   （如 code='Mica' 只有一行）且与所属配方登记根互相印证时才允许补全。
4. **证书链**：完整证书 SHA-256 ↔ 证书字节数 ↔ 下载文件字节 ↔ capture_digest。
   证书在证书集合中唯一时才可用来定位（本库已验证唯一）；注意证书绑定的是内容，
   内容相同的捕获（C、D、E）若认证会共享同一文件摘要。
5. **封存模式摘要分组**：schema_digest 相同才可对比、才可谈「同内容」；不同则
   排除「可对比」，但不排除「归属」（A3 的候选 C 模式不同，仍是合法候选）。
6. **对比结果**：同模式捕获对的行级差异（A/B 修改 S-01）或「行内容一致」（D/E）
   用来说明候选之间的可区分性，不直接决定归属。

不允许：

- 用台账他行的抄录补全本行归属。台账整体是被核对对象；他行即使已确认，也只能
  把结论落回状态库再核，不能行间「借」证据（例如不得因 A1 已确认 A 有证书，
  就把 A3 归给 A）。
- 把输入拼写当实存键直接追踪。追踪只认实存键：`["east","MICA"]` 在捕获 A 的
  追踪表单返回「封存结果中没有这个实存主键」；拼写归一必须经目录规则。
- 凭行数、见证数或行内容相同合并或拆分捕获（A、B 计数相同仍是两次捕获；
  D、E 内容相同仍是两次捕获）。
- 凭备注叙述、交接批次或时间先后定归属。
- 短编号不唯一时凭其他线索「就近」认领（本库未出现，出现时该行只能降级为待补）。

## 8. 复核命令

```sh
# 状态库直查（只读）
python3 - <<'EOF'
import sqlite3
conn = sqlite3.connect('file:handoff/state.sqlite?mode=ro', uri=True)
print(conn.execute('SELECT substr(id,1,8), name, version, state FROM captures ORDER BY rowid').fetchall())
print(conn.execute('SELECT name, version, active, substr(schema_digest,1,12) FROM recipes ORDER BY name, version').fetchall())
print(conn.execute("SELECT substr(id,1,8), json_extract(certificate,'$.sha256') FROM captures WHERE certificate IS NOT NULL").fetchall())
EOF

# 交付文件对证书（应等于 A1 抄录的 SHA-256）
python3 -c "import hashlib,pathlib;print(hashlib.sha256(pathlib.Path('handoff/downloads/keelsplice-40d2607bbfcf454896b48e65ac6e6525.sqlite').read_bytes()).hexdigest())"
```

页面清单（生成时已验证返回 200）：`/workbench`、`/workbench/recipes`、
`/workbench/captures`、`/workbench/compare`、`/workbench/recipes/swatch/1..3`、
`/workbench/recipes/mica-watch/1..2`，以及 §4 各捕获详情页。
追踪与对比是 POST 表单：追踪在捕获详情页提交表名与 JSON 主键元组（如
`substrate` + `["East","Mica"]`）；对比在 `/workbench/compare` 选择两个完整捕获编号。
