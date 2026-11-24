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

## 并行运行的原入口与 WSGI 入口

内建入口仍由标准库 `http.server` 提供，行为与之前一致。需要受支持的 WSGI 宿主时，可用同一份代码另起一个进程：

```sh
# 受支持的 WSGI 宿主：把 keelsplice.wsgi:application 交给宿主加载
KEELSPLICE_REPOSITORY=keelsplice-local/state.sqlite \
KEELSPLICE_SOURCES='{"microscope": "keelsplice-local/microscope.sqlite"}' \
python3 -m waitress --host 127.0.0.1 --port 8795 keelsplice.wsgi:application

# 或使用随代码附带、行为相同的线程化 WSGI 宿主
python3 -m keelsplice --wsgi --repository keelsplice-local/state.sqlite \
  --source microscope=keelsplice-local/microscope.sqlite --port 8795
```

两条入口必须用同一对 `--repository` 和源文件路径（WSGI 宿主使用同名环境变量），因此共用同一个状态库、同一批证书和同一条模式摘要。两个进程可以同时在线：SQLite 串行化写入，读取只看已提交记录，任一侧封存的新捕获在另一侧立即可见。切换期间只要保证同一时刻只有一个入口在接待写请求，就可以随时把旧入口流量切到 WSGI 入口，再按需要切回。

WSGI 应用需挂载在站点根（`SCRIPT_NAME` 为空），因为页面表单与重定向使用 `/workbench` 根相对地址；非空挂载会以 500 明确拒绝，而不是悄悄生成错误链接。`transport.py` 是两条入口唯一共享的协议层：路径、状态码、JSON 错误结构 `{"error", "message"}`、`ETag` 与下载头、浏览器同源检查和表单错误页面都由它产出，所以旧客户端和浏览器不会因入口不同而看到差异。仅支持 GET 与 POST；其他方法在两条入口都返回 501 和同样的 JSON 错误。

浏览器表单继续提交真实类型的键（例如 `roots` 中的 `[{"table":"substrate","key":["east","MICA"]}]`）和 `digest`，服务按目录摘要校验后用同一套编解码落库，因此 NOCASE 拼写、BLOB、实数与 64 位整数在两侧没有区别。认证后的下载在两条入口使用同一份存储字节与同一 `certificate.sha256`：`ETag` 与响应体摘要相同，接收方可用任一入口的结果复核，也可以拿它和原入口此前下载的文件比对。

对照清单（同一仓库、同一证书）：

| 关注点 | 原入口 | WSGI 入口 | 一致性来源 |
| --- | --- | --- | --- |
| 监听 | `python3 -m keelsplice` | `python3 -m waitress ... keelsplice.wsgi:application` 或 `--wsgi` | 同一 `Repository`、同一源路径 |
| JSON 路径与错误 | `/sources/...` 等 | 完全相同 | `transport.route_json` |
| 浏览器路径 | `/workbench/...` | 完全相同 | `web.http.route` |
| 同源检查 | `Origin` 等于 `Host` 且为主机回环 | 完全相同 | `transport.Request.form_values` |
| 类型化键 | JSON 文本经 `codec.decode` | 完全相同 | `recipe.validate` |
| 捕获/证书 | 存于状态库 | 完全相同 | `storage.Repository` |
| 下载 | `ETag` 为 SHA-256 | 完全相同 | `service.download` |

不需要清空或迁移状态库：停掉旧入口后直接以相同参数启动 WSGI 入口即可，反向切换同理。进行中的捕获若只到 `sealed` 或 `built`，在新入口继续构建或认证仍按原状态机推进。

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
python3 -m examples.deployment_walkthrough
python3 -m examples.process_switchover
```

前两份演练使用临时库和临时端口，验证成功和拒绝路径，并用独立 SQLite 连接复核交付数据。`deployment_walkthrough` 在同一进程内并行启动两条入口、共用一份状态库，覆盖目录与错误结构一致、跨入口封存/读取、WSGI 侧表单与同源边界、两侧同一证书摘要，以及切回原入口后继续操作 WSGI 侧新封存记录。`process_switchover` 用 `python -m keelsplice` 起两个真实进程做同样的切换检查。独立自动化测试套件延后。
