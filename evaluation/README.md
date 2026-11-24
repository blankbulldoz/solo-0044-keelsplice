# 证据复核存取方式评估（可运行）

本目录是**评估副产品**，不属于 `keelsplice` 产品代码，也不改变其契约。目的：为“浏览器经常复查少数行”这一用法，用真实封存数据比较三种读取方式，而不是从一次小样本外推。

## 三种方式

| 代号 | 含义 | 复核一次读什么 |
| --- | --- | --- |
| `sealed` | 现状：`captures.sealed` 存整段 JSON，`trace` 每次全量读入并全量扫描见证 | 整段封存（含全部行值/BLOB） |
| `split` | 仍保留封存，但把**行值**与**证据**（行身份、见证、根）分开存表 | 只读证据部分，仍需在内存里扫全部见证 |
| `index` | 在封存之外另建派生证据索引（只存身份与见证，不存行值） | 按主键定位到节点，再按节点取见证 |

`split` 与 `index` 都是原型，位于 `split_sealed.py` / `evidence_index.py`，用与 `capture.trace` 相同的类型化主键规范化，测试会逐行比对三者答案完全一致。

## 命令

```sh
# 端到端：真实 HTTP 服务 + 浏览器追踪循环（只测现状）
python3 -m evaluation.http_cost --output evaluation/results-http.json

# 逐项：三方式 × 多输入类，含详情页、构建、认证、下载
python3 -m evaluation.run --suite default --output evaluation/results-default.json
python3 -m evaluation.run --suite sweep   --output evaluation/results-sweep.json

# 仓库随重复封存/构建/认证的增长
python3 -m evaluation.retention --captures 4 --output evaluation/results-retention.json

# 逐行等价、缺失键拒绝、防篡改与 split 无损重组
python3 -m evaluation.conformance

# 仓库行布局造成的读取放大（列表页 / 下载）
python3 -m evaluation.layout

# 对单个捕获给出“该用哪种”的可运行判定（不做曲线外推）
python3 -m evaluation.advise --fixture fanout:4500
python3 -m evaluation.advise --fixture blob:1.5625
python3 -m evaluation.advise --repository state.sqlite --source s=src.sqlite --capture CAPTURE_ID
```

设计结论与全部数字见 [`DESIGN.md`](DESIGN.md)。

`advise` 退出码 0 表示三条路径在全部抽样答案上一致（含缺失主键的 404 拒绝），2 表示不一致。它打印**本捕获实测**的中位字节/耗时、相对封存与 artifact 的存储增量，以及由这些数字直接得出的选择。

## 测量口径（为什么这些数字可信）

- 读量用 `/proc/self/io` 的 `rchar`，即进程向内核索取的字节数；即使页缓存是热的也计入，不随磁盘速度漂移，因此包含整段 JSON/BLOB 的拉取。
- 每个样本都用**新连接**，与浏览器请求一致；不复用连接或页缓存。
- 每个被测函数先热身一次不入样，再取中位数与 p90，至少 11 次。
- 输入覆盖两个轴：高扇出（1 根 → N 子行，每行 8 条外键）、大 BLOB（少量行、极多字节），另加显微镜样例作对照，并用扫点覆盖 10→4500 子行、16 KiB→1.6 MiB 载荷。
- 结果写入 `results-*.json`，含平台、SQLite 版本与每个样本的 n。

## 已知边界

- 计时为单机单进程顺序执行，不含并发争用；结论只用“每页读取字节数”和“是否必须整段读”这类结构性事实，不据此推断吞吐。
- 计时前会 `gc.collect()`：留在堆上的已解码封存会把 45,000 见证的一次追踪从约 90 ms 放大到约 350 ms。那是测量脚手架伪影，真实服务端单次请求不携带上次请求的解码结果。
- `index` 的存储增量是实测页数×页大小，含其全部索引与溢出页。
- 索引格式为原型 `FORMAT=1`，未做产品级迁移与向后兼容。
