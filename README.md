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

## 接收能力登记与实物试交付

工作台登记两类接收设备，它们对 64 位整数、实数和 BLOB 的承载方式不同：

- **类型化中继 `typed-relay`**：整数、实数（IEEE hex 保位）、文本、BLOB（base64）和 NULL 各自独立承载，重新导入后存储类型不变。
- **纯文本中继 `text-relay`**：只承载文本与 NULL。数字文本按目标 SQLite 列声明的亲和性重新解释（INTEGER/REAL/NUMERIC 列会把文本转成数值），没有 BLOB 载体，NULL 以空串作为哨兵。

在「接收设备」页登记或查看能力，在捕获详情页选择设备发起实物试交付。工作台生成该设备**实际可导入**的中继载荷，导入为一个真实 SQLite 库后，再用独立只读连接重开复核。结论从不以行数相同判定，而是分别检查：

1. **主键可区分性**：无亲和性主键里整数 `1` 与文本 `'1'`、实数、BLOB 重开后是否仍是不同身份；
2. **复合外键**：可空复合键的每个分量重开后能否定位到唯一原父行，分量被改写或子行被拒时逐边列出影响；
3. **负零与空值**：无亲和列保留负零符号位（REAL 亲和列按 SQLite 规则归一为正零），NULL 是否被哨兵改写、与真空串混淆；
4. **int64 与 BLOB 保真**：极值整数、超出双精度的整数和 BLOB 字节是否存活，并指出受影响的外键关系。

报告给出 `passed`/`degraded`/`failed`：主键或外键分量的载体缺失、关系断裂、NULL/int64/BLOB 失真判为不通过；仅普通数据列存在载体缺口但关系核心完整时判为降级。REAL/NUMERIC 亲和列对负零的固有归一单独标注为平台行为，不计为中继失真。任何表示无法承载时都明确列出被拒行、丢失的主键和受影响关系。试交付样例来自 `examples/handoff_lab.sql`（无亲和主键、可空复合外键、负零、空 BLOB、int64 极值、自引用边）。运行：

```sh
python3 -m examples.receiver_walkthrough
```

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
| `GET /receivers` | 列出内置与已登记接收设备及其能力声明 |
| `POST /receivers` | 登记设备：`name`、`relay`（`typed`/`text`）、`storage_classes` 及可选载体选项 |
| `POST /captures/{id}/trials` | 接收 `{"device":"name"}`，生成可导入载荷并独立复核，返回试交付报告 |
| `GET /trials/{id}` | 读取试交付报告（主键、复合外键、负零、空值、BLOB、int64 各项结论） |
| `GET /trials/{id}/payload` | 返回双方可导入的中继载荷 JSON，`ETag` 为载荷 SHA-256 |
| `GET /trials/{id}/database` | 返回该设备实际导入后的 SQLite 库，`ETag` 为库 SHA-256 |

根记录中的键按目录的列顺序填写。整数为有符号 64 位 JSON 数字。字节用 `{"blob":"AAD/"}`，实数用 `{"real":"0x1.a000000000000p+1"}`，文本用 JSON 字符串。配方可以用不同拼写定位同一个 NOCASE 根，封存与追踪使用实存键。结果中的行去重，输入根出现项保留顺序。工作台近期列表显示最后登记的 50 个配方版本和最后封存的 50 次捕获，历史详情地址依然可访问。

## 复跑

```sh
python3 -m compileall -q keelsplice examples
python3 -m examples.walkthrough all
python3 -m examples.browser_walkthrough
python3 -m examples.receiver_walkthrough
```

三份演练使用临时库和临时端口，验证成功和拒绝路径，并用独立 SQLite 连接复核交付数据。独立自动化测试套件延后。
