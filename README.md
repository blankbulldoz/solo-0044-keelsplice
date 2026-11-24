# Keelsplice 关系样本工作台

从已注册的 SQLite 源库提取可独立打开的关系子库。数据库工程人员在浏览器里查看主键和外键、登记配方、确认入选行与直接证据，然后构建、认证并下载子库。

## 本地启动

需要 Python 3.11+ 和 SQLite 3.37+。Python 后端使用标准库，HTML 由服务端渲染。

```sh
mkdir keelsplice-local
python3 -c 'from examples.walkthrough import seed; seed("keelsplice-local/microscope.sqlite")'
python3 -m keelsplice --repository keelsplice-local/state.sqlite \
  --source microscope=keelsplice-local/microscope.sqlite --port 8794
```

浏览器打开 <http://127.0.0.1:8794/workbench>。同一进程可用多个 `--source 别名=文件` 注册不同源库。工作台仅用于受信任的本机操作人员，服务监听回环地址。源文件必须与状态库分开，状态库所在目录须可写。

## 从配方到交付

1. 在「关系目录」选源和表。主键顺序用于填写根，外键映射用于判断哪些子行需要反向纳入。
2. 登记配方，填写一个或多个根及封存行上限。样例可填 `[{"table":"substrate","key":["east","MICA"]}]`，再选 `substrate → assay`。
3. 试算查看逐表计数和实存根。点「封存当前样本」时服务重新读取源。配方绑定的模式有变化时，需要重新核对目录。
4. 在捕获详情按实存主键追踪。可从直接入选原因或必需父行继续查看相关记录。对比页面按实存带类型主键显示两个同模式捕获的新增、移除和修改。
5. 构建后再认证。认证后可下载文件并用其 SHA-256 或独立 SQLite 连接核对。停用配方后，既有捕获仍可完成这些操作。

## JSON 接口

同一后端继续提供既有接口。JSON 请求使用 `Content-Type: application/json`。捕获 ID 是不透明字符串。

| 方法与路径 | 请求与结果 |
| --- | --- |
| `GET /sources/{alias}/catalog` | 读取表、列、关系、索引和模式摘要 |
| `POST /recipes/{name}/versions` | 登记 `source`、`roots`，以及可选的 `follow_children`、`row_limit` |
| `GET /recipes/{name}/versions/{version}` | 读取版本及其投用状态 |
| `POST /recipes/{name}/versions/{version}/retire` | 接收空对象并停用版本 |
| `POST /preview`、`POST /captures` | 接收 `{"name":"swatch","version":1}`，分别试算和封存 |
| `GET /captures/{id}` | 读取捕获状态、实存根、计数和证书 |
| `POST /captures/{id}/trace` | 接收如 `{"table":"substrate","key":["East","Mica"]}`，返回直接证据和从零开始的根序号 |
| `POST /captures/{id}/build`、`POST /captures/{id}/certify` | 接收空对象，构建或认证该捕获的子库 |
| `POST /compare` | 接收 `{"left":"id-a","right":"id-b"}`，按表报告带类型主键的差异 |
| `GET /captures/{id}/database` | 返回已认证 SQLite 字节，`ETag` 为带引号的 SHA-256 |

根记录中的键按目录的列顺序填写。整数为有符号 64 位 JSON 数字。字节用 `{"blob":"AAD/"}`，实数用 `{"real":"0x1.a000000000000p+1"}`，文本用 JSON 字符串。配方可以用不同拼写定位同一个 NOCASE 根，封存与追踪使用实存键。结果中的行去重，输入根出现项保留顺序。工作台近期列表显示最后登记的 50 个配方版本和最后封存的 50 次捕获，历史详情地址依然可访问。

## 复跑

```sh
python3 -m compileall -q keelsplice examples
python3 -m examples.walkthrough all
python3 -m examples.browser_walkthrough
python3 -m examples.handoff_drill --out /tmp/keelsplice-drill
```

前两份演练使用临时库和临时端口，验证成功和拒绝路径，并用独立 SQLite 连接复核交付数据。独立自动化测试套件延后。

## 双接收方交接演练

`examples/handoff_drill.py` 从一个经真实服务认证的子库生成两套可重放材料，分别给只能读标准库 CSV 的接收方和能直接查 SQLite 的接收方：

- `material-a-csv/`：每列一个带类型标记的 JSON 单元格，附 `roster.json` 说明复合边、父键排序规则和生成列；不含任何 SQLite 文件。
- `material-b-sqlite/`：认证子库字节、证书，以及当场生成的对照库 `control.sqlite`。

认证路径会拒绝复合边指向不存在父行的捕获（`invalid_reference`），也不允许 `foreign_key_check` 不通过的子库，因此认证子库只能体现“子键含 NULL 故无需父行”这一半。另一半来自对照库：它沿用同一模式，加入真正缺父的行、合法空串、排序规则陷阱和一份手抄列。`ledger.json` 按结论列出两个接收方的答案、所需表示（类型标记、排序规则元数据、DDL 声明、原始字节）以及交换后丢掉的依据，并要求双方一致，否则中止。

```sh
python3 examples/receivers/receiver_csv.py /tmp/keelsplice-drill/material-a-csv --dataset control
python3 examples/receivers/receiver_sqlite.py /tmp/keelsplice-drill/material-b-sqlite --export-material /tmp/keelsplice-drill/material-a-csv
```

接收方 CSV 脚本不导入 `sqlite3`；对照库上以退出码 2 表示“按设计发现了问题”。

结论依赖的表示各不相同：NULL 判定需要逐单元格的类型标记，缺父判定需要一份含真实悬空行的对照件，排序规则判定需要父键列的 `COLLATE` 声明，生成列判定则同时需要 DDL 中的 `GENERATED` 声明和精确的 REAL 位，字节一致性只有原始文件能回答。普通 CSV 导出会把 NULL 与空串写成同一个空字段、丢掉存储类别和 REAL 精度，因此这些依据在只读 CSV 的一方只能靠 `roster.json` 补回，无法从数据本身恢复。
