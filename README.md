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
5. 构建后再认证。认证后 A 工位在捕获详情审核证书：服务复读实存字节并记录带工位、证书和字节摘要的判断，给出可转交结论。B 工位随后下载，下载前服务重新核对字节与证书。实存字节损坏时可从封存数据重建子库，当前证书归档为历史，基于历史证书的判断标记为结果过期，重新认证并复审后才能交接。停用配方后，既有捕获仍可完成这些操作。

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
| `POST /captures/{id}/review` | 接收 `{"station":"A"}`，复核实存字节并记录审核判断 |
| `GET /captures/{id}/reviews` | 读取交接结论、审核判断和证书历史，不读取子库字节 |
| `POST /captures/{id}/rebuild` | 接收空对象，从封存数据重建子库并把当前证书归档为历史 |
| `POST /compare` | 接收 `{"left":"id-a","right":"id-b"}`，按表报告带类型主键的差异 |
| `GET /captures/{id}/database` | 返回已认证 SQLite 字节，`ETag` 为带引号的 SHA-256 |

根记录中的键按目录的列顺序填写。整数为有符号 64 位 JSON 数字。字节用 `{"blob":"AAD/"}`，实数用 `{"real":"0x1.a000000000000p+1"}`，文本用 JSON 字符串。配方可以用不同拼写定位同一个 NOCASE 根，封存与追踪使用实存键。结果中的行去重，输入根出现项保留顺序。工作台近期列表显示最后登记的 50 个配方版本和最后封存的 50 次捕获，历史详情地址依然可访问。

## 交接审核

交接分两岗：A 工位先审核证书，B 工位稍后取文件。审核时服务复读实存子库字节并与当前证书核对，把判断（工位、证书身份、实存字节摘要、结论）写入审核记录；浏览结论和记录只读元数据，不重复读取完整子库字节。交接结论覆盖三种情形：核验通过为可转交；判断基于已被取代的证书为结果过期，需重新审核；重建后尚未重新认证时只看到历史证书，不能交接。B 工位正式下载时服务仍重新计算字节摘要，与证书不一致就拒绝发送。已认证捕获可重建：子库从封存数据重新生成，当前证书归档为历史，基于历史证书的判断随之过期。值班人可在审核记录中定位哪次判断使用过哪些字节。

`python3 -m examples.handoff_rehearsal` 在备用副本上演练这一流程：A 审核成功后损坏副本中的字节，验证 B 取件被阻止、值班人复审记录损坏字节的判断、重建与重新认证后恢复交接，而生产库不受影响。

## 复跑

```sh
python3 -m compileall -q keelsplice examples
python3 -m examples.walkthrough all
python3 -m examples.browser_walkthrough
python3 -m examples.handoff_rehearsal
```

三份演练使用临时库和临时端口，验证成功和拒绝路径，并用独立 SQLite 连接复核交付数据。独立自动化测试套件延后。
