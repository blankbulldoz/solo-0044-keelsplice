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

## WAL 时间点取样归档

数据保留组需要在异地复跑一组边界明确的取样证据时，可以生成一份自校验归档：

```sh
python3 -m examples.retention_archive /path/to/empty-directory
python3 /path/to/empty-directory/verify.py /path/to/empty-directory
```

归档包含一个未提交写入、两个已提交更新，以及每个边界上工作台独立封存的捕获：

- `P0`：基线，WAL 尚无有效帧。
- `P1`：后台写入方保持未提交事务，工作台另开读事务封存；WAL 有合法帧但没有提交帧，
  读到的仍是最后一个已提交镜像。`points/P1/writer.sqlite` 是写入方自己的快照，
  只用于说明未提交值，不能当作读者镜像。
- `P2`、`P3`：两次后台提交之后各自封存。

每个点共享同一份主库字节 `source/main.sqlite`，提交只存在于各自的 WAL 段中。`verify.py`
只依赖标准库，不导入 Keelsplice，按字节和带类型的 SQLite 值定案：

- WAL 头与每帧的 salt、累计校验和决定有效帧，解析在第一个无效帧停止；
- 读者镜像止于最后一个提交帧，其后的合法帧属于写入方，永远不进入捕获；
- 捕获中的每个入选行都与重放镜像在实存主键上逐列相等，包括存储生成列
  （核对 `doubled = length_um * 2`）；
- 父键目标按声明外键和 SQLite 比较语义重新查找，包括 NOCASE 复合键；
- 同模式捕获按封存内容摘要比较，`P0`/`P1` 相同而 `P1`/`P2`、`P2`/`P3` 不同。

时间戳不参与判定；相同模式摘要只确认结构，行内容另由可见值和封存内容摘要判定。

## 复跑

```sh
python3 -m compileall -q keelsplice examples
python3 -m examples.walkthrough all
python3 -m examples.browser_walkthrough
python3 -m examples.retention_walkthrough
```

前两份排查演练使用临时库和临时端口，验证成功和拒绝路径，并用独立 SQLite 连接复核交付数据。
第三份构建 WAL 取样归档，复制到别处用独立进程运行随附的 `verify.py`，并确认被篡改的副本会被拒绝。
独立自动化测试套件延后。
