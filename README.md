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

1. 在「关系目录」选源和表。主键顺序用于填写根，外键映射用于判断哪些子行需要反向纳入。复合主键不必猜样例：点「浏览实存主键」即可按表查看已存在的键。
2. 登记配方，填写一个或多个根及封存行上限。样例可填 `[{"table":"substrate","key":["east","MICA"]}]`，再选 `substrate → assay`。
3. 试算查看逐表计数和实存根。点「封存当前样本」时服务重新读取源。配方绑定的模式有变化时，需要重新核对目录。
4. 在捕获详情按实存主键追踪。可从直接入选原因或必需父行继续查看相关记录。对比页面按实存带类型主键显示两个同模式捕获的新增、移除和修改。
5. 构建后再认证。认证后可下载文件并用其 SHA-256 或独立 SQLite 连接核对。停用配方后，既有捕获仍可完成这些操作。

## 浏览实存主键

「实存主键」页按表列出数据库中真实存在的主键元组，用于在登记配方前挑选根，而不是猜样例。

- 顺序来自该表主键索引，而不是列声明：`PRIMARY KEY(a COLLATE BINARY)` 覆盖 NOCASE 列时按 BINARY 排序，`INTEGER PRIMARY KEY DESC` 不是 rowid 别名。分页按这一顺序做键集游标，页深不影响开销。
- 元组保留 SQLite 类型：整数为有符号 64 位，字节为 `{"blob": ...}`，实数为 `{"real": ...}`，文本为 JSON 字符串。「选取此根」会把实存元组连同当时的浏览证据记录为一条选取，可据此直接带入配方登记。
- 翻页期间源可能被写入。页面把每一页读到的主键集摘要（模式、键序、有界计数、两端键）与上一页比较：变化时显示「来源在翻页期间发生了变化」并重置覆盖判断。只有从表头连续翻到表尾、全程来源一致、且能精确计数时，页面才主张覆盖了全部实存主键；否则明确标注「尚未覆盖全表」。NOCASE 列命中另一拼写、或行数超过精确核对上限时同样不会冒充完整。
- 选取记录在 `GET /selections/{id}` 可回看，并会重新读取当前源：报告该实存主键是否仍存在，以及模式、键序、键集摘要是否仍与选取时一致。试算与封存始终按当前源判定，选取证据只说明当时看到的主键，不对未来数据作承诺。

## JSON 接口

同一后端继续提供既有接口。JSON 请求使用 `Content-Type: application/json`。捕获 ID 是不透明字符串。

| 方法与路径 | 请求与结果 |
| --- | --- |
| `GET /sources/{alias}/catalog` | 读取表、列、关系、索引和模式摘要 |
| `GET /sources/{alias}/tables/{table}/keys` | 读取该表第一页实存主键、键序、来源见证和分页游标 |
| `POST /sources/{alias}/tables/{table}/keys` | 接收可选的 `after`/`before`（带类型键数组）、`limit`、`session`，返回该方向的下一页 |
| `POST /sources/{alias}/tables/{table}/select` | 接收 `key`、可选的 `session` 和 `note`，记录一条选取证据 |
| `GET /selections/{id}` | 读取选取证据，并报告当前源是否仍能按该实存主键找到该行 |
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
```

两份演练使用临时库和临时端口，验证成功和拒绝路径，并用独立 SQLite 连接复核交付数据。独立自动化测试套件延后。
