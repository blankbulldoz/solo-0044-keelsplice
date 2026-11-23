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
6. 交接后在捕获详情打开「签收交接」，登记接收方给出的 SHA-256；若能同时给出表计数也可一并填写。只有证据在登记后能唯一指向本捕获时才显示「已签收」。不同捕获可能重建出相同的 SQLite 字节，此时仅凭摘要（或摘要加相同计数）无法分辨具体交接对象，记录会停在「待确认」而不算签收。填错时从记录页点「登记更正」重新提交，更正会追加新记录，旧记录与当时的核对结论保留可复查。

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
| `POST /captures/{id}/receipts` | 登记签收证据：必填 `sha256`，可选 `recipient`、`note`、`counts`、`corrects`；返回核对结论 |
| `GET /captures/{id}/receipts` | 列出该捕获的全部签收记录、当前有效记录及签收汇总 |
| `GET /receipts/{receipt_id}` | 复查单条签收记录，包括被更正的旧记录 |

签收证据中的 `sha256` 为 64 位十六进制（大小写与首尾空白不计）。`counts` 是 `{"表名": 非负整数}` 的 JSON 对象，可只列部分表；列出的表必须存在于证书且计数完全一致。结论 `confirmed`（已签收）要求该摘要（及给出时的计数）在登记时只匹配本捕获；若同时匹配其他已认证捕获则为 `ambiguous`（待确认），与本捕获证书不符则为 `mismatch`（未签收）。记录保存登记时的证书摘要与结论，读取时会按当前已认证捕获重新判定，并保留 `registered_status`。`corrects` 指向同一捕获中尚未被更正的记录；每条记录最多被更正一次，更正追加新记录而不改写旧记录。捕获通过认证前拒绝登记签收。

根记录中的键按目录的列顺序填写。整数为有符号 64 位 JSON 数字。字节用 `{"blob":"AAD/"}`，实数用 `{"real":"0x1.a000000000000p+1"}`，文本用 JSON 字符串。配方可以用不同拼写定位同一个 NOCASE 根，封存与追踪使用实存键。结果中的行去重，输入根出现项保留顺序。工作台近期列表显示最后登记的 50 个配方版本和最后封存的 50 次捕获，历史详情地址依然可访问。

## 复跑

```sh
python3 -m compileall -q keelsplice examples
python3 -m examples.walkthrough all
python3 -m examples.browser_walkthrough
```

`python3 -m examples.walkthrough receipt` 可单独演练签收与更正路径。两份演练使用临时库和临时端口，验证成功和拒绝路径，并用独立 SQLite 连接复核交付数据。独立自动化测试套件延后。
