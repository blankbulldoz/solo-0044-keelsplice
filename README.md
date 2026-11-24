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
| `POST /proposals/evaluate` | 评估未声明外键的候选列，分开返回目录条件与行级条件 |
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

## 外键草案评估

接入组的源库常没有声明目标关系，但候选列已知。`POST /proposals/evaluate` 与浏览器「外键草案」页针对一组候选子列、父列给出可复查的接入建议，结论分两层且互不混淆：

- **目录即可判定的条件**：不读任何行。包括父键是否由主键或非部分、非表达式的唯一索引保证（列序可互换、列排序规则须一致）、子/父列排序规则与类型亲和性差异、子键是否可空。样本里恰好唯一但没有可用唯一约束时判为阻断，因为 SQLite 写入期会报 `foreign key mismatch`。
- **需读取实际行验证的条件**：在同一只源读事务里执行。报告重复父键、匹配多父的子键、缺父子键，以及「仅在父列排序规则（如 NOCASE）下匹配、按 BINARY 拼法并不相等」的子键，并附带数量与最多 5 条证据样本。
- **NULL 子键**：复合子键任一分量为 NULL 时按 SQLite 语义没有必需父行，单列统计为豁免行，不计入缺父。

请求形如 `{"source":"onboarding","child":"ticket","parent":"asset","child_cols":["asset_tag"],"parent_cols":["tag"]}`。响应给出 `verdict`（`ready`、`blocked_catalog`、`blocked_data`、`inconclusive`）、`recommendation`、`catalog_findings`、`row_findings` 与本次读快照的 `metrics`。行级结论只对该次读快照有效，不写入状态库，也不影响既有配方、采样与认证。

## 复跑

```sh
python3 -m compileall -q keelsplice examples
python3 -m examples.walkthrough all
python3 -m examples.proposal_walkthrough all
python3 -m examples.browser_walkthrough
```

`examples/onboarding.sql` 是公开构造的正反例库（刻意不声明外键）：含可直接接入的正例、样本唯一但无唯一约束的目录反例、NOCASE 才匹配的拼写差异、缺父子键、重复父键与 NULL 豁免行。两份演练使用临时库和临时端口，验证成功和拒绝路径，并用独立 SQLite 连接复核交付数据。独立自动化测试套件延后。
