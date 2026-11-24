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
| `POST /recipes/{name}/versions/{version}/readiness` | 接收空对象，报告现有索引能否支撑本配方实际走的读取，并在同一快照上复算闭包 |
| `POST /preview`、`POST /captures` | 接收 `{"name":"swatch","version":1}`，分别试算和封存 |
| `GET /captures/{id}` | 读取捕获状态、实存根、计数和证书 |
| `POST /captures/{id}/trace` | 接收如 `{"table":"substrate","key":["East","Mica"]}`，返回直接证据和从零开始的根序号 |
| `POST /captures/{id}/build`、`POST /captures/{id}/certify` | 接收空对象，构建或认证该捕获的子库 |
| `POST /compare` | 接收 `{"left":"id-a","right":"id-b"}`，按表报告带类型主键的差异 |
| `GET /captures/{id}/database` | 返回已认证 SQLite 字节，`ETag` 为带引号的 SHA-256 |

根记录中的键按目录的列顺序填写。整数为有符号 64 位 JSON 数字。字节用 `{"blob":"AAD/"}`，实数用 `{"real":"0x1.a000000000000p+1"}`，文本用 JSON 字符串。配方可以用不同拼写定位同一个 NOCASE 根，封存与追踪使用实存键。结果中的行去重，输入根出现项保留顺序。工作台近期列表显示最后登记的 50 个配方版本和最后封存的 50 次捕获，历史详情地址依然可访问。

## 索引就绪检查

配方详情页的「检查索引就绪」和 `POST /recipes/{name}/versions/{version}/readiness` 回答一个运维问题：现有哪些索引真能支撑本配方实际走的父子方向，哪些只是列名重合。检查在只读事务里只用 `EXPLAIN QUERY PLAN` 和真实取样键试跑，不在源库安装任何索引，也不改变源文件——操作员不需要管理员在源库上做任何改动。

报告把三件事分开：

- **计划**：配方只用两条读取形状。父键定位（根和必需父行）用等值查找；子行扇出用 `p.列 = +c.列` 的连接。这里的 `+` 是有意的，它去掉子列的类型亲和性，让比较跟随父键，因此**任何子索引都无法让这条腿获得键定位**，最多只能把全表扫描换成（覆盖）索引扫描，或借部分索引谓词跳过行。
- **实际工作量**：每条读取都会在无索引基线（`NOT INDEXED`）和强制使用每个索引两种情况下计量 VM 步数。结论分成 `seeks`（相对基线获得键定位）、`less_work`（形状不变但工作量确实下降）、`neutral`、`costs`（工作量上升，或强制后丢掉基线已有的键定位）和 `unusable`（部分索引谓词不被此读取蕴含，SQLite 直接拒绝）。
- **关系闭包**：由真实捕获引擎在同一快照上算出，单独报告。装不装索引都不改变捕获选中的行。

例：NOCASE 父键上再建一个 BINARY 同名索引会显示为 `costs`；子表上列名相同但排序规则或列顺序不符的索引同样显示为 `costs`；真正匹配的部分 NOCASE 子索引在子行大量为空键时显示为 `less_work`。两个可复跑的源形态见 `examples/index_ready_a.sql` 与 `examples/index_ready_b.sql`：前者是带 NOCASE 父键与部分子索引的高扇出形态，后者由多个根共享同一父键。

## 复跑

```sh
python3 -m compileall -q keelsplice examples
python3 -m examples.walkthrough all
python3 -m examples.browser_walkthrough
python3 -m examples.index_readiness all
```

两份演练使用临时库和临时端口，验证成功和拒绝路径，并用独立 SQLite 连接复核交付数据。索引演练另用两种可复跑的源形态核对计划变化、实际工作量与关系闭包，并断言源文件字节未被改动。独立自动化测试套件延后。
