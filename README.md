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

## 接收组验收会话

「验收会话」页把一组接收组的条款、配方版本和模式摘要冻结在一个会话里。普通条款（`single`）只要任一已认证子库单独覆盖即可，可由不同快照分别举证；同一文件条款（`joint`）要求两项要求出现在同一个认证文件里。会话页显示每个条款的并用判定、缺口及需要补的样本：

- 两项分别由不同认证文件举证时判为「不能并用」，并列出两份分项证书，不把它们的摘要合并。
- 已有同一文件覆盖两项但尚未引用时提示引用；只有已封存捕获时提示先认证；只有同名配方其他版本的证书时明确不能使用。
- 每条要求可写成行（`{"kind":"row","table":…,"key":[…]}`）或外键关联（`{"kind":"link","edge":["子表",外键编号],"origin":{…},"target":{…}}`），方向由目录决定。创建会话时按实存主键冻结，因此输入拼法不会改变判定。
- 引用证书会保存该次认证记录（捕获、文件 SHA-256、捕获摘要）；引用后若文件字节或封存内容与证书不符，该证书被判为不一致而不是继续满足条款。

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
| `POST /acceptance` | 接收 `name`、`version`、`groups` 和 `clauses`，冻结验收会话 |
| `GET /acceptance/{id}` | 读取会话的条款、已引用证书、并用判定、缺口和所需样本 |
| `POST /acceptance/{id}/cite` | 接收 `{"clause":…,"capture":…,"group":…}`，引用一份已认证证书 |
| `POST /acceptance/{id}/supplement` | 接收 `{"clause":…}`，按缺口认证或封存所需样本 |
| `GET /captures/{id}/database` | 返回已认证 SQLite 字节，`ETag` 为带引号的 SHA-256 |

根记录中的键按目录的列顺序填写。整数为有符号 64 位 JSON 数字。字节用 `{"blob":"AAD/"}`，实数用 `{"real":"0x1.a000000000000p+1"}`，文本用 JSON 字符串。配方可以用不同拼写定位同一个 NOCASE 根，封存与追踪使用实存键。结果中的行去重，输入根出现项保留顺序。工作台近期列表显示最后登记的 50 个配方版本和最后封存的 50 次捕获，历史详情地址依然可访问。

验收会话的条款在创建时按实存主键冻结。`single` 条款恰好 1 项要求，`joint` 条款恰好 2 项不同要求；每项为行或外键关联。引用只接受已认证、同配方版本、同模式摘要且证书仍与文件字节及封存内容一致的捕获。回复中的 `combination.reason` 取 `same_file`、`separate_files`、`awaiting_citation`、`needs_certification`、`version_mismatch`、`schema_mismatch`、`evidence_integrity` 或 `incomplete`，`needed_sample` 给出补证动作与可复用的封存根。

## 复跑

```sh
python3 -m compileall -q keelsplice examples
python3 -m examples.walkthrough all
python3 -m examples.browser_walkthrough
```

两份演练使用临时库和临时端口，验证成功和拒绝路径，并用独立 SQLite 连接复核交付数据。独立自动化测试套件延后。
