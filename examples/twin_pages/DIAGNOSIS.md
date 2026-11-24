# 双胞胎错误页诊断：同一复合键、同一 404、三个来源

本目录是一份公开的支持诊断样例。`examples/twin_pages.sql` 布置好源库，`python3 -m examples.twin_pages` 通过浏览器表单完整复现并自检；`submissions/`、`pages/`、`evidence/` 是一次实跑留下的记录。

## 现象

支持同事拿到两张一模一样的错误页报告：正文都只有

> 操作未完成 —— 封存结果中没有这个实存主键，请核对类型与拼写。（404）

报告里唯一能看到的具体数据是用户追踪的复合键显示值 `R1 / 7`——错误页本身不含捕获编号，也不含任何键。`pages/` 保存了三个来源各自产生的错误页，字节完全一致：

| 文件 | 来源 | SHA-256 |
| --- | --- | --- |
| `pages/error-1-nocase-spelling.html` | ① NOCASE 输入拼写 | `125771c5…99c10` |
| `pages/error-2-type-flip.html` | ② TEXT/INTEGER 类型 | `125771c5…99c10` |
| `pages/error-3-row-absent.html` | ③ 捕获不含该行 | `125771c5…99c10` |

三页完整的 SHA-256 均为 `125771c565fadd82f3ac9f82b7ed0583bc8d86c5d46f81f0a10bf57becc99c10`。

## 样例布置的三个来源

源库只有三行关键数据（`examples/twin_pages.sql`）：

- `lot ('East','Mica')`：复合 NOCASE 主键；
- `vial ('R1','7')`：`slot` 列未声明类型（BLOB 亲和），数字 7 按 **TEXT** 存储；
- `vial ('R1',7)`：同一显示数字 7 按 **INTEGER** 存储。

两行 vial 的复合键显示出来都是 `R1 / 7`。

**来源①：同一 NOCASE 实存记录的两种输入拼写。** 配方 v1 的两个根 `["east","MICA"]` 与 `["EAST","mica"]` 是同一实存行的两种拼写（`submissions/01`）。NOCASE 排序让登记和封存都命中实存行，捕获 A 的根序号 1、2 对应同一个实存键 `["East","Mica"]`（`evidence/capture-a.json` 中 `root_occurrences` 为 2）。但追踪按实存键精确比较：用户照自己登记时的拼写填 `["east","MICA"]`（`submissions/06`）→ 404。

**来源②：同一显示数字对应 TEXT 和 INTEGER。** 捕获 A 收的是 TEXT 行 `(R1,'7')`。用户看着显示出的 7，在追踪表单里填了整数 `["R1",7]`（`submissions/08`，表单体是 `key=%5B%22R1%22%2C7%5D`）。封存键带类型精确匹配，`7` 与 `'7'` 是两个不同的键 → 404。改用文本键 `["R1","7"]`（`submissions/07`）立即成功。

**来源③：另一次捕获不含该行。** 配方 v2 的根是 INTEGER 行 `(R1,7)`，封存出捕获 B（`submissions/03`、`04`）。对捕获 B 追踪 `["R1","7"]`（`submissions/09`）→ 404；这次换任何类型、任何拼写都没有用，因为这次捕获里根本没有那行（`evidence/capture-b.json` 中 lot 计数为 0，vial 只有整数行）。

## 为什么仅凭粘贴的文本必然无法判断

错误页模板（`keelsplice/web/templates/error.html`）只渲染固定文案与状态码：不含捕获编号、不含用户提交的键、不含实存键。三个来源映射到同一份字节——这不是观察不够仔细，而是信息已经丢失：

- 显示出的 `7` 不是存储类的函数：TEXT `'7'` 与 INTEGER `7` 显示相同，JSON 引号一丢就无法区分；
- 用户口述的拼写是**输入**拼写，不一定是**实存**拼写：NOCASE 在登记时接受 `east`/`MICA`，实存是 `East`/`Mica`；
- 页面不说明这是哪一次捕获，而各次捕获的内容互不相干。

「粘贴的错误页文本 → 来源」不存在任何解码函数。要下结论，必须增加页面之外的现实观察。

## 增加哪项现实观察能把两种可能隔开

404 背后只有两种可能：**甲**、行在这次捕获里，但提交的键与实存键的类型或拼写不符；**乙**、这次捕获根本没有该行。以下任一项现实观察即可把甲乙隔开：

1. **浏览器提交**（`submissions/`）：追踪表单的 POST 地址含捕获编号，表单体保留 JSON 类型与大小写——`key=%5B%22R1%22%2C7%5D` 是整数 7，`%227%22` 才是文本 `'7'`。浏览器开发者工具的「网络」面板或本机访问日志都能留下这一层。
2. **持久证据**（`evidence/`）：状态库中的封存记录不可变。`GET /captures/{id}` 给出实存根与逐表计数（`capture-a.json`、`capture-b.json`）；`sealed-a.json`、`sealed-b.json` 是状态库 `captures` 表里的原样记录，`result.rows.vial` 的键 `["R1","7"]` 与 `["R1",7]` 直接显示类型。用**实存键**对同一捕获再追踪一次：成功 ⇒ 甲（键的类型或拼写不符）；仍然 404 ⇒ 乙（该换捕获）。
3. **源库观察**（`evidence/source-storage-classes.json`）：`SELECT quote(slot), typeof(slot) FROM vial` 把显示出的 7 与存储类分开——`'7'` 是 text、`7` 是 integer；`quote(region)` 给出实存大小写。

区分来源①与②靠第 1、3 项（类型与大小写）；识别来源③靠第 2 项（这次捕获里有没有该行）。

## 复跑与刷新

```sh
python3 -m examples.twin_pages                              # 临时目录复现并自检（断言三个错误页一致且与 pages/ 相同）
python3 -m examples.twin_pages --record examples/twin_pages # 重新生成本目录的提交、错误页与证据
```
