# 反向边争议交接：实物与判读约定

本目录用于数据组与验证组就「目标行是不是靠某条**反向边**纳入的」这一争议做交接。
所有结论只允许引用本目录的冻结契约与按本说明复跑得到的实物，不接受口头推断。

## 1. 实物清单（实际目录）

| 文件 | 性质 | 用途 |
| --- | --- | --- |
| `frozen-contract.json` | 入库冻结证据（文本，可 diff） | 复跑的唯一对照基准；含源库哈希、目录边清单、4 组场景 × 开关 2 变体的封存摘要、trace 见证、证书摘要、独立 SQL 核对、逐行裁决 |
| `replay_dispute.py` | 复跑脚本 | 从 `examples/microscope.sql` 重新播种，经真实 JSON HTTP 接口登记配方、封存、构建并认证，再与冻结契约逐项比对 |
| `../microscope.sql` | 源库 DDL+数据（仓库内） | 唯一种子来源；复跑产物 `microscope.sqlite` 的 SHA-256 必须等于契约中的 `source.seeded_database_sha256` |

复跑加 `--out <目录>` 时，该目录另外保留：播种后的 `microscope.sqlite`（源实物）、
`state.sqlite`（工作台状态库，含 8 条已认证封存捕获）、`evidence.json`（本次运行的完整证据）。
`*.sqlite` 按仓库 `.gitignore` 不入库，因此交接时以契约内哈希与摘要为准，实物随时可重建。

## 2. 术语与方向约定（以代码为准，不以说法为准）

- **边身份**：`[child, id]`，`id` 是该子表 `PRAGMA foreign_key_list` 的编号。本源四条边：
  `["assay",0] assay→substrate(region,code)`、`["assay",1] assay→cassette(cassette)`、
  `["cassette",0] cassette→cassette(prior→serial)`、`["frame",0] frame→assay(mark)`。
- **父向（direction=`parent`）**：从子行沿外键找必需父行。**所有外键永远自动走父向，与配方是否勾选反向无关**
  （`keelsplice/capture.py` 的 `towards_parent` 对每条边无条件登记；复合子键任一列为 NULL 时该边无父行）。
- **反向（direction=`child`）**：从父行反向找引用它的子行。**只有配方 `follow_children` 显式勾选的边才走**
  （`towards_child` 仅由 `selected` 集合登记）。
- **纳入（present）**：行出现在封存 `result.rows` 中，按带类型主键去重，每个身份只展开一次。
- **见证（witness/arc）**：一次实际遍历的记录。目标行已在集合中时，新遍历**仍追加见证**（行去重、见证不去重）。
  因此「存在一条指向目标的见证」与「目标靠这条见证才纳入」是两件事。
- **根（root）**：配方显式给定的行，`trace.roots` 给出从零开始的根序号；非根时为空数组。
- **trace 三要素**：`roots`（是否根及序号）、`causes`（以本行为 target 的全部见证）、
  `dependencies`（本行向外的父向依赖）。目标未纳入时 trace 返回 HTTP 404 `row_not_captured`，
  404 本身是「缺席」的冻结证据。

## 3. 判读约定（两组共同遵守）

1. **必要性只能由反事实开关证明。** 同一配方注册两个版本：勾选争议边（ON）与 `follow_children=[]`（OFF），
   其余完全相同。目标在 ON 纳入、OFF 返回 404，才允许断言「该反向选择对该目标行必要」。
2. **单看一次封存的见证不能证明必要性，也不能证明非必要性。**
   - 数据组句式「选了反向边、存在 `child` 见证，所以目标靠它纳入」——只在 OFF 缺席时成立；
     若目标同时是根或处在父向闭包/环内，OFF 仍在，该句式失效。
   - 验证组句式「出现了父依赖，所以关掉反向也无影响」——父向边永远自动走，父见证的存在
     不提供任何关于反向边的信息；若父行本身是反向遍历的产物，整条父链随反向开关一起消失。
3. **结论的粒度是「配方 × 目标行」，不是边的全局属性。** 同一条 `["assay",0]` 在场景 A 对 S-01/S-02 必要，
   在场景 B 对 S-01 不必要（它是第 2 个根）、对 S-02 仍必要。引用结论必须带场景与目标主键。
4. **能断言与只能留见证的边界。** 根行且 `causes=[]` 时，只能断言「当前捕获没有任何直接纳入见证」，
   不得反推历史原因；复合外键含 NULL 的边按约定不产生父行（场景 D 的 S-03 无 substrate 父行）。
5. **冻结范围。** 契约冻结：源库 SHA-256、`schema_digest`、边清单、各变体计数与见证数、
   每个 probe 的 trace 结果（含 404）、证书 `capture_digest` 与子库 `sha256/bytes`、独立 SQL 核对值。
   捕获 ID 是每次运行新生成的不透明字符串，**不冻结、不参与比对**。

## 4. 四组对照场景与裁决（数字均已冻结于契约）

| 场景 | 配方根 | 争议边开关 | 目标行 | ON | OFF | 裁决 |
| --- | --- | --- | --- | --- | --- | --- |
| A | `substrate(East,Mica)` | `[assay,0]` | `assay S-01` | 纳入，causes 仅 1 条 `child` | **404** | `necessary` 反向必要 |
| A | 同上 | 同上 | `assay S-02` | 纳入，causes 仅 1 条 `child` | **404** | `necessary` 反向必要 |
| A | 同上 | 同上 | `cassette 11` | 经 assay 父依赖纳入 | **404** | `necessary_transitively` 传递必要：父见证存在，但父链随反向一起消失 |
| B | `substrate(East,Mica)` + `assay S-01` | `[assay,0]` | `assay S-01` | 纳入，`roots=[1]` 且有 `child` 见证 | 纳入，`roots=[1]`，`child` 见证消失 | `not_necessary_root_witness` 另一根改变结论 |
| B | 同上 | 同上 | `assay S-02` | 纳入，causes 仅 1 条 `child` | **404** | `necessary` 同边不同目标，结论相反 |
| C | `cassette 11` | `[cassette,0]`（自环） | `cassette 12` | 纳入，causes 含 `child`+`parent` 两条 | 纳入，causes 仅 `parent` 一条 | `not_necessary_cycle_parent_closure` 环的父向闭包已纳入，反向只多见证 |
| D | `assay S-03` | 无反向边 | `assay S-03` | 根行 `causes=[]`，仅 1 条到 `cassette 13` 的父依赖 | 同 ON（完全相同） | `root_only_witness_boundary` 只能断言当前无直接纳入见证 |

**两组说法外表都像正确的两条边：**

- 场景 B 的 `["assay",0]` 对 S-01：ON 里确实有一条 `substrate→S-01` 的 `child` 见证（数据组外表成立），
  S-01 也确实带父依赖（验证组外表成立）；但 S-01 是第二个根，OFF 仍在——数据组的因果句式被另一个根推翻。
- 场景 C 的 `["cassette",0]` 对 12：ON 里确实有一条 `11→12` 的 `child` 见证（数据组外表成立），
  12 也确实由 `11.prior=12` 的父向边纳入（验证组外表成立）；环使父向闭包在 OFF 下同样到达 12，
  差别仅是见证数 4 vs 2——验证组结论正确，但正确原因是环，不是「有父依赖」这个泛化句式。

## 5. 复跑步骤（接手者）

```sh
# 默认：临时目录复跑并与冻结契约逐项比对，全部一致退出码 0
python3 -I examples/dispute_handoff/replay_dispute.py

# 保留实物用于独立核对（源库、状态库、evidence.json）
python3 -I examples/dispute_handoff/replay_dispute.py --out /tmp/ks-handoff-out

# 仅在有意变更基线并完成评审后重写契约（正常交接不要使用）
python3 -I examples/dispute_handoff/replay_dispute.py --freeze
```

独立复核（不经工作台，模拟外部接收方）：

```sh
python3 -I - <<'PY'
import sqlite3, json, hashlib
from pathlib import Path
out = Path('/tmp/ks-handoff-out')
contract = json.load(open('examples/dispute_handoff/frozen-contract.json'))
assert hashlib.sha256((out/'microscope.sqlite').read_bytes()).hexdigest() \
       == contract['source']['seeded_database_sha256']
with sqlite3.connect((out/'state.sqlite').as_uri()+'?mode=ro', uri=True) as conn:
    print(conn.execute('SELECT name, state FROM captures ORDER BY name').fetchall())
with sqlite3.connect((out/'microscope.sqlite').as_uri()+'?mode=ro', uri=True) as conn:
    assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
print('independent check ok')
PY
```

预期：8 个捕获全部 `certified`，源库无外键违例，复跑退出码 0。
任一字段漂移时脚本以退出码 1 失败并打印形如
`值差异 @ scenarios.2.on.summary.witnesses: 99 != 4` 的定位行。
