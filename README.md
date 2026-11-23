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

## 源模式关注列表

关注对象是外键边的真实列映射和表的主键定义；显式索引不进入关注指纹，所以同一晚新增无关索引不会告警。在「源模式关注」页或关系目录里登记关注时，工作台以当前目录建立基线，并把之后每次成功的目录读取和显式核对都存成不可变的目录核对记录。

每次核对对该关注给出三种互斥结论：

- **连续核对一致**：在连续核对窗口（`continuity_seconds`，默认 3600 秒）内，相邻成功核对的关注结构指纹相同。
- **观测到结构变化 / 关注对象缺失**：与上一条成功核对相比，列映射、外键目标或主键定义真的变了，或关注对象已不在目录中。
- **结构相同但存在未观测区间**：两端指纹相同，但中间出现过读不到源库的核对，或两次成功核对间隔超过窗口。这类结论不会被冒充成连续核对。

源库几小时读不到时，每次核对都会留下不可达记录；连续不可达只发一条通知，恢复读取后再按上面的规则判定。结构变化会通知**当时所有用过这条边（`follow_children`）或以该表为根的配方版本**，包括已停用版本。通知生成时就把当时各次目录结论和收件方清单固化下来，交班时重新打开旧通知仍能看到当时的完整结论，而不是用当前状态覆盖历史。

## JSON 接口

同一后端继续提供既有接口。JSON 请求使用 `Content-Type: application/json`。捕获 ID 是不透明字符串。

| 方法与路径 | 请求与结果 |
| --- | --- |
| `GET /sources/{alias}/catalog` | 读取表、列、关系、索引和模式摘要；该源存在关注时同时留下一条目录核对 |
| `POST /sources/{alias}/check` | 接收空对象，显式核对一次。成功记录目录快照，读不到源库时记录不可达区间并返回产生的通知 |
| `PUT /watches` | 登记关注：`kind=edge` 配 `child`/`id`，或 `kind=pk` 配 `table`，可带 `continuity_seconds`、`note` |
| `GET /watches`、`GET /watches/{id}` | 关注列表（最新结论、连续核对段）与含历次目录结论的详情 |
| `DELETE /watches/{id}` | 停用关注，历史核对与通知仍可查看 |
| `GET /notifications`、`GET /notifications/{id}` | 模式变化/源库不可达通知；详情固化当时各次目录结论与配方收件方 |
| `POST /notifications/{id}/acknowledge` | 接收空对象，确认一条通知 |
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
