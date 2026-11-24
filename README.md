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
| `POST /captures/{id}/reverify` | 接收空对象，仅重算已认证字节摘要并与既有证书核对，不改变状态；备用实例唯一的写路径操作 |
| `GET /instance` | 返回实例角色、是否权威、内容摘要、配方与捕获索引；备用实例附带快照来源 |
| `POST /compare` | 接收 `{"left":"id-a","right":"id-b"}`，按表报告带类型主键的差异 |
| `GET /captures/{id}/database` | 返回已认证 SQLite 字节，`ETag` 为带引号的 SHA-256 |

根记录中的键按目录的列顺序填写。整数为有符号 64 位 JSON 数字。字节用 `{"blob":"AAD/"}`，实数用 `{"real":"0x1.a000000000000p+1"}`，文本用 JSON 字符串。配方可以用不同拼写定位同一个 NOCASE 根，封存与追踪使用实存键。结果中的行去重，输入根出现项保留顺序。工作台近期列表显示最后登记的 50 个配方版本和最后封存的 50 次捕获，历史详情地址依然可访问。

## 复跑

```sh
python3 -m compileall -q keelsplice examples
python3 -m examples.walkthrough all
python3 -m examples.browser_walkthrough
python3 -m examples.switchover_drill
```

三份演练使用临时库和临时端口，验证成功和拒绝路径，并用独立 SQLite 连接复核交付数据。独立自动化测试套件延后。

## 两进程切换演练

`examples/switchover_drill.py` 在临时目录启动真实的两组 `python3 -m keelsplice` 进程，演示有明确回退窗口的重启切换：

- **备用实例**（`--role standby`）只读打开一份带来源戳的状态库快照。工作台与 JSON 都标出「备用实例视图」及快照时间，配方投用状态显示为「快照时投用」，不会把旧配方状态当作主实例当前状态。它只允许 `reverify` 复核此前已认证的子库，其余写入（登记、试算、封存、停用、构建、认证）返回 `standby_read_only`。
- **主实例**在备用运行时仍可继续封存新的关系捕获；旧成果在切换前后分别用 `SHA-256` 与独立 SQLite 连接核对。
- **切换清单**由 `keelsplice.switchover` 生成，记录持久状态库路径、设备号/inode、内容摘要、快照捕获索引与回退窗口。主实例以 `--switchover MANIFEST` 重启，先核对持久记录位置与摘要，再允许接续写入；摘要变化（`handover_content`）或窗口关闭（`handover_window`）都会拒绝启动。
- **回退归因**：`python3 -m keelsplice.switchover report --manifest M --instance URL ...` 按快照比对两个实例，指出哪个实例新增了捕获。演练本身会走这条真实命令。

本机手工复跑等价流程：

```sh
# 1) 主实例启动并形成已认证子库，然后停掉
python3 -m keelsplice --repository primary/state.sqlite \
  --source microscope=primary/microscope.sqlite &
# 2) 生成 standby/state.sqlite 与 switchover.json（见 examples/switchover_drill.py）
# 3) 同时启动主、备
python3 -m keelsplice --repository primary/state.sqlite \
  --source microscope=primary/microscope.sqlite --port 8794 &
python3 -m keelsplice --role standby --repository standby/state.sqlite \
  --source microscope=primary/microscope.sqlite --port 8795 &
# 4) 需要恢复写入时，主实例带清单重启
python3 -m keelsplice --repository primary/state.sqlite \
  --source microscope=primary/microscope.sqlite --switchover switchover.json &
```

`--port 0` 时服务把实际端口写入环境变量 `KEELSPLICE_PORT_FILE` 指定的文件，便于脚本驱动。`keelsplice.switchover` 的 `export_replica`/`plan`/`write_manifest`/`rollback_report` 也可直接导入复用。
