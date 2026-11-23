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
| `POST /sources/onboarding/check` | 接收 `{"reference":"别名","candidates":{"标签":"路径"},"probes":[...]}，输出可审核的源接入基线 |
| `POST /sources/onboarding/recheck` | 接收 `{"baseline":<check 的返回>}`，在源更新后逐项指出先前结论是否仍适用 |
| `GET /captures/{id}/database` | 返回已认证 SQLite 字节，`ETag` 为带引号的 SHA-256 |

根记录中的键按目录的列顺序填写。整数为有符号 64 位 JSON 数字。字节用 `{"blob":"AAD/"}`，实数用 `{"real":"0x1.a000000000000p+1"}`，文本用 JSON 字符串。配方可以用不同拼写定位同一个 NOCASE 根，封存与追踪使用实存键。结果中的行去重，输入根出现项保留顺序。工作台近期列表显示最后登记的 50 个配方版本和最后封存的 50 次捕获，历史详情地址依然可访问。

## 新增源文件接入自查

迁移或重新接入时，管理员常同时持有运行中 WAL 库的目录拷贝、跟随原文件的别名路径，以及同结构但根值不同的替代库。既有配方继续使用登记的别名；新配方在登记前先用接入自查明确「这份路径到底提供哪种保证」。

`POST /sources/onboarding/check` 以已登记别名指向的运行库为参照，对 `candidates` 中每个路径独立采证并给出判定：

| 判定 | 证据含义 | 接入建议 |
| --- | --- | --- |
| `same_object` | 设备号与 inode 与参照主文件相同，路径实际指向同一对象（如符号链接） | `register_alias` |
| `same_database_copy` | 不同文件，但只读快照能看到全部已提交的代表记录（目录拷贝保留了 WAL 帧） | `register_copy` |
| `main_file_only` | 主文件字节与参照一致，但代表记录不可见，只能证明主文件相同 | `do_not_register` |
| `divergent_database` | 主文件不同且代表记录缺失（同结构、不同已提交内容） | `do_not_register` |
| `unreadable` | 路径缺失、无法只读打开或模式不支持 | `do_not_register` |

证据同时给出主文件 SHA-256、`-wal`/`-shm` 边车是否随拷贝提供、模式摘要是否一致以及每个代表行的命中情况。只读探测在私有暂存区打开候选，不会在操作员的候选目录里生成或改动任何边车文件。

返回的基线应保存到实际登记之间。期间源若有更新，调用 `POST /sources/onboarding/recheck` 并原样回传基线：服务重新采证，对每个候选给出 `verdict_unchanged`、逐项 `evidence_changes` 和总的 `still_applies`，并在参照上标记 `changed_since_baseline`。类别未变但证据（如 checkpoint 后主文件摘要）漂移时，`still_applies` 为 `false`，提示先前的接入结论需要重新确认。

## 复跑

```sh
python3 -m compileall -q keelsplice examples
python3 -m examples.walkthrough all
python3 -m examples.browser_walkthrough
python3 -m examples.onboarding_walkthrough
```

两份演练使用临时库和临时端口，验证成功和拒绝路径，并用独立 SQLite 连接复核交付数据。接入演练构造「运行中 WAL 库、跟随原文件的别名、带 WAL 的完整目录拷贝、仅主文件拷贝、同结构替代库」，先验证基线判定，再在源提交并 checkpoint 后验证复核结论。独立自动化测试套件延后。
