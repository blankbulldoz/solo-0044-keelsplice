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
6. 交付后如需二次提取自查：在捕获详情为指定实存根出具自查清单，随子库一并发给接收人。接收人离线生成自查材料并复跑判定，工位核验发回的材料，区分原文件、接收端合法追加与缺根文件。

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
| `POST /captures/{id}/recheck` | 接收实存根如 `{"table":"substrate","key":["East","Mica"]}`，返回只含摘要的自查清单 |
| `POST /captures/{id}/recheck/verdict` | 接收 `{"root":{...},"material":{...}}`，返回与接收人本机一致的判定 |

根记录中的键按目录的列顺序填写。整数为有符号 64 位 JSON 数字。字节用 `{"blob":"AAD/"}`，实数用 `{"real":"0x1.a000000000000p+1"}`，文本用 JSON 字符串。配方可以用不同拼写定位同一个 NOCASE 根，封存与追踪使用实存键。结果中的行去重，输入根出现项保留顺序。工作台近期列表显示最后登记的 50 个配方版本和最后封存的 50 次捕获，历史详情地址依然可访问。

## 二次提取自查

接收人离线且不能回传库文件时，自查清单与自查材料都只含类型化摘要、计数和自检标志，不含任何行值。工位对已认证捕获按指定实存根出具清单（`POST /captures/{id}/recheck` 或捕获详情页表单），保存为 `manifest.json` 随子库一并交付。接收人在本机生成材料并复跑同一项判定：

```sh
python3 -m keelsplice.recheck probe handoff.sqlite manifest.json -o material.json
python3 -m keelsplice.recheck judge manifest.json material.json
```

`probe` 在独立只读连接上检查完整性、外键、模式摘要和指定根（沿用 SQLite 比较语义，NOCASE 拼写差异不影响命中），并逐表记录行摘要；空表记为零行。`judge` 与工位的核验是同一个函数：同一清单与材料在两侧结果一致。把 `material.json` 发回工位后，工位经 `POST /captures/{id}/recheck/verdict` 或捕获详情页表单核验。判定类别：`original` 原文件（`bytes_match` 表示字节是否也与证书一致）、`appended` 接收端合法追加、`missing_root` 仍有表定义但缺指定根、`rejected` 无法确认（完整性或外键失败、模式漂移、根键被改写、原有行被改删）。

## 复跑

```sh
python3 -m compileall -q keelsplice examples
python3 -m examples.walkthrough all
python3 -m examples.browser_walkthrough
```

两份演练使用临时库和临时端口，验证成功和拒绝路径，并用独立 SQLite 连接复核交付数据。独立自动化测试套件延后。
