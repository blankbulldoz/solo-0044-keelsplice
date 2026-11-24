# 夜班断连接续演练

这份材料让下一位值班人不需要原操作者的口头交接，就能判断：哪些是原手上的工作、哪些只能算候选、哪些必须重新读源。全部内容由公开样例 `examples/microscope.sql` 生成，并保存在真实持久记录中。

## 怎么用

```sh
python3 -m examples.handover_night replay --probe
```

`replay` 离线运行，不改动交付材料：核对每个文件的 SHA-256，用生产闭包代码从带标签的源快照重算每个封存捕获，在临时副本上逐个完成 build → certify → download，并把推导出的判定与 `expected-verdicts.json` 对照。`--probe` 再把交付仓库复制一份、注册源快照，用真实 HTTP 接口复核同样的结论。任一检查失败时进程以非零状态退出。

重建材料（会替换 `material/`）：

```sh
python3 -m examples.handover_night build --force
```

`build` 启动真实的回环 JSON 与浏览器服务，写入配方、封存捕获和会话记录。它用真实的 socket 重置制造"没收到页面响应"，用 `sqlite3.backup` 固化带标签的源快照。捕获 ID 由服务生成，因此重跑后 ID 会变；`replay` 始终校验当前这份材料，而不是某次历史构建。

## 目录

| 路径 | 内容 |
| --- | --- |
| `material/state/repository.sqlite` | 真实的状态库：2 个配方版本（`relay` 投用中，`attic` 已停用）、4 条封存捕获 |
| `material/sources/00-public-sample.sqlite` | 公开样例的初始状态 |
| `material/sources/01-after-value.sqlite` | 只改了一个存储值（`S-01.length_um`），行数与关系不变 |
| `material/sources/02-after-retarget.sqlite` | 把 `S-02` 的关系目标移到另一个 substrate；各表总行数不变，但 `relay` 闭包的行数改变 |
| `material/journal.jsonl` | 按序重建的会话记录，每条带请求、观察到的响应和证据类型 |
| `material/observations/dropped-response.json` | 丢失页面响应那次请求的客户端视角与服务端证据 |
| `material/observations/previews.json` | 两次行数相同的试算，以及一次行数不同的试算 |
| `material/observations/captures-page.html` | 断连后重新打开捕获列表时的真实页面 |
| `material/manifest.json` | 捕获标签、快照归属、复用摘要与全部文件的 SHA-256 |
| `expected-verdicts.json` | 接手人应得到的判定，供 `replay` 对照 |
| `verdicts.json` | 最近一次 `replay` 的完整报告，每次运行都会重写 |

`manifest.json` 不含自身摘要；`journal.jsonl` 在写完后才进入摘要清单，所以记录本身也受校验保护。

`replay` 的 12 项检查：

1. 交付文件的 SHA-256 与 manifest 一致；
2. 仓库里的捕获与 manifest 条目一一对应；
3. 配方版本与投用状态和 manifest 一致（只有 `attic` v1 已停用）；
4. 每条封存捕获都能从标注的快照逐字节重算；
5. 外观相同的捕获被归组；
6. `relay-lost` 与 `relay-retry` 内容相同但可区分；
7. 每条封存捕获在副本上都能 build → certify → download，并复核外键与逐表计数；
8. 会话记录与观察文件能重建经过；
9. 三份快照之间恰好是声称的两次改动；
10. trace 与行值证据能区分同外观捕获；
11. 推导出的判定与 `expected-verdicts.json` 一致；
12. 5 个负向对照都被拒绝。

## 现场经过

1. 夜班 A 登记 `relay`（根 `east/MICA`，反向纳入 `assay`）和 `attic`（根 `West/Basalt`）。
2. 第一次试算，随后封存 `relay-early`。
3. 封存 `attic-legacy`，然后停用 `attic` 版本 1。停用后再次封存被 `retired_recipe` 拒绝。
4. 源里一个存储值被改动（不影响行数）；第二次试算显示相同行数。
5. 夜班 A 点击「封存当前样本」。请求确实到达服务并写入了状态库，但客户端在读到页面响应前连接被重置：操作者手上没有确认页，也没有捕获 ID。这条记录在材料里叫 `relay-lost`。
6. 接班 B 不知道第 5 步已经成功，又封存了一次，得到 `relay-retry`。两条记录内容完全相同。
7. 源把 `S-02` 的关系目标移走。试算行数随之改变，但这次试算没有被封存。
8. 之后对 `attic` 版本 1 的封存仍被拒绝。

## 判定

| 标签 | 判定 | 依据 |
| --- | --- | --- |
| `relay-early` | **可恢复** | 唯一封存记录，能从 `00-public-sample` 逐字节重算；可完成构建、认证、下载 |
| `relay-lost` | **可恢复** | 页面响应丢失但记录已封存；会话记录里有夜班 A 的请求、服务端状态与仓库行；能从 `01-after-value` 逐字节重算 |
| `attic-legacy` | **可恢复** | 停用前封存；停用后仍可读、可完成构建/认证/下载 |
| `relay-retry` | **仅候选** | 与 `relay-lost` 内容、见证、子库字节完全相同；没有任何材料说明哪一次是操作者本意 |
| `relay-retargeted-closure` | **需重读源** | 只有试算，没有封存；源已再次变化 |
| `attic-fresh-sample` | **需重读源** | 版本已停用，无法再封存；需要新的源读取和新版本 |

「可恢复」只证明该样本能以封存时的内容交付，不证明操作者现在仍需要它。「仅候选」证明存在一个内容正确的样本，但不能把意图归给某一次尝试。

## 外观相同怎么区分

`relay-early`、`relay-lost`、`relay-retry` 在页面上的外观完全一致：同样的模式摘要、逐表计数、行数、见证数和实存根。`replay` 报告分三层给出可区分的证据：

- **值层**：`relay-early` 与后两条的 `assay:["S-01"]` 存储值不同（`capture_digest` 与子库字节都不同），所以早班样本和后改值样本可以分开。
- **关系层**：`relay-lost`、`relay-retry` 与 `relay-early` 的 trace 见证相同，说明这次改值没有改变关系结构；而重定位后的闭包与它们 trace 不同。
- **身份层**：`relay-lost` 与 `relay-retry` 在值层和关系层都相同，唯一差别是仓库捕获 ID 和产生它的会话事件。`separation` 报告把这两条列为同一 `identical_content_cluster`，`separable_by` 为 `capture_id_and_journal`，并给出两条 ID。

复核命令（对交付材料直接跑）：

```sh
python3 -m examples.handover_night replay --report /tmp/report.json
python3 - <<'PY'
import json
report = json.load(open('/tmp/report.json'))
print(json.dumps(report['separation_groups'], ensure_ascii=False, indent=2))
print(json.dumps(report['lost_group'], ensure_ascii=False, indent=2))
print(json.dumps(report['trace_evidence'], ensure_ascii=False, indent=2))
PY
```

`separation_groups` 以"外观"为键：同一键下的捕获在页面上无法区分，`distinct_content_digests` 与 `separable_by` 说明内容层能不能分开，`identical_content_clusters` 把连内容都相同的捕获列成一组，并给出各自的 ID。`trace_evidence` 逐对比值层、关系层与身份层。`lost_group` 单列本次演练里那对必须靠 ID 与会话记录区分的记录。

也可以手工确认（先复制仓库，避免改动交付材料）：

```sh
mkdir -p /tmp/check
cp examples/handover-night/material/state/repository.sqlite /tmp/check/state.sqlite
python3 -m keelsplice --repository /tmp/check/state.sqlite \
  --source microscope=examples/handover-night/material/sources/02-after-retarget.sqlite --port 8794
```

然后在浏览器打开 <http://127.0.0.1:8794/workbench/captures>：两条外观相同的记录都会出现，链接里的捕获 ID 不同；打开任一条的 trace 页签，两者证据一致；用「捕获对比」比较两者，页面显示"这两个捕获的行内容一致"。

## 材料如何自证

`replay` 的 12 项检查全部通过才返回 0，其中包含 5 个负向对照：改动一个快照里的存储值、改掉 manifest 里的捕获摘要、直接改仓库里的封存载荷、把丢失记录的 ID 冒充成重试记录、篡改会话记录里的封存状态。每项都必须被拒绝，否则判定不可信。`--probe` 另外用真实 HTTP 复核：两条记录 compare 无差异、`relay-early` 与丢失记录恰好只有 `S-01` 值不同、丢失记录在未认证前拒绝下载、认证后 ETag 等于独立重算的摘要。

## 已知边界

- 材料里的时间戳来自构建时刻，重跑会变化；捕获 ID 也由服务生成。跨次比较请以 `manifest.json` 与 `replay` 输出为准。
- `relay-retry` 的"仅候选"结论依赖材料里没有意图证据，而不是说它内容有误。若原操作者留下别的凭证（例如记下了捕获 ID），这个判定应当更新。
- 材料只覆盖 `examples/microscope.sql` 的模式；换成其他源库时，快照、配方和判定都需要重新生成。
