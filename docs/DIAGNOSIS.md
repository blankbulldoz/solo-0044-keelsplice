# 封存后父关系不一致：三种解释的对照与排除

## 现象

1. 值班在浏览器试算（`POST /preview`），页面给出逐表计数、实存根与关系见证。
2. 之后在同一配方上封存（`POST /captures`），再构建、认证、下载。
3. 接收方用独立 SQLite 连接打开已认证文件时，某条测量的**父关系**（例如 `assay S-01 → substrate`）与核对时记下的父键不同。
4. 两次页面显示的总行数与模式摘要相同；另有同事说可能是另一个同名配方版本。

这三个观察只能说明结果集合的**形状**没变，不能说明父边没变。行数、模式摘要都不包含父键的目标值。

## 对照怎么搭

`examples/triage.py` 用 `examples/microscope.sql` 建两个同模式源库（`primary` / `twin`），通过真实 HTTP 服务制造三条互斥的路径，再用同一张判断表回头判定：

| 代号 | 机制 | 关键动作 |
| --- | --- | --- |
| A | 两次读之间源数据变化 | 试算后、封存前改 `S-01` 的 `(region,code)` |
| B | 用了同名配方的另一版本 | 版本 1 指向 `primary`，版本 2 指向 `twin` |
| C | 接收方拿错已交付文件 | 交付的是另一个已认证捕获的字节 |

复跑：

```sh
python3 -m compileall -q keelsplice examples
python3 -m examples.triage                      # 打印全部观察
python3 -m examples.triage --expect examples/triage_observations.json   # 回归比对（忽略随机捕获编号）
python3 -m examples.triage --output /tmp/keelsplice-triage             # 留下 observations.json、两份文件、状态库
python3 -m examples.triage --diagnose /tmp/keelsplice-triage/repository.sqlite \
  --delivered /tmp/keelsplice-triage/drifted.sqlite \
  --sealing <封存动作产生的捕获编号> --reviewed-version 1 --reviewed-capture <核对时那次捕获编号>
```

`--diagnose` 只读状态库与交付文件，不访问源库；它也是事后唯一能落地的判断方式。

## 每种解释在哪个观察上被排除

判断顺序固定为 **C → B → A**，因为 C、B 都能在状态库里找到直接证据，A 恰恰缺证据。下表是 `examples/triage_observations.json` 中实际产生的判定（`confirmed` = 该解释成立，`excluded` = 被排除，`consistent_unconfirmed` = 与观察相容但历史无法证实）。

| 场景 | A 判定 | B 判定 | C 判定 |
| --- | --- | --- | --- |
| A（真源漂移） | confirmed | excluded | excluded |
| B（换版本） | excluded | confirmed | excluded |
| C（拿错文件） | excluded | excluded | confirmed |

### A：源数据在两次读之间变化

- **排除它的观察**：封存记录里的版本 == 核对时的版本，且该版本存下来的配方（`source`、`roots`、`follow_children`、`row_limit`）与核对版本逐字相同。只要绑定变了，A 就被排除（B 或 C 接管）。
- **确认它需要的观察**：同一绑定下，封存记录的父键 ≠ 核对父键，**并且**有一份仓库之外的源文件前后摘要（`--fingerprint-before/--fingerprint-after`）。仓库不保存源文件字节，所以没有这份外部证据时只能得到 `consistent_unconfirmed`，见下节。
- **为什么行数/摘要挡不住**：`microscope.sql` 的 `S-01` 从 `(east,MICA)` 改到 `(west,basalt)` 后，闭包仍是 `assay 1 / cassette 2 / frame 0 / substrate 1`，`schema_digest` 不变，`witnesses` 不变（4）。`compare` 才暴露真相：`assay.modified=[["S-01"]]`、`substrate.removed=[["East","Mica"]]`、`added=[["West","Basalt"]]`。
- **本条真的会改行数**：把 `S-01` 的 `(region,code)` 置 NULL 时，`substrate` 计数从 1 变 0、总行数 4→3。也就是说“行数相同”本身已经排除了“父边断开”这一类漂移，只剩“父边换到另一条已存在的父行”这一类。这正是 A 唯一可能藏身的形式。

### B：误用了同名配方的另一个版本

- **排除它的观察**：封存行的 `version` == 核对时的版本。捕获表存了 `name` 和 `version`，`sealed` 里还嵌了该版本的规范化配方，所以“用了哪个版本”永远可查。
- **确认它的观察**：封存行的 `version` 不同，且该版本的规范化配方与核对版本不同（例如 `source` 从 `primary` 变 `twin`）。此时父键不同是必然的，不需要看源文件。
- **“同名不同版本其实等价”的边界**：如果另一版本规范化后与核对版本逐字相同（triage 里的版本 3），判定是 `confirmed_but_equivalent`——版本号确实不同，但唯一能造成的差别只能来自别名指向的源文件本身，那就退化成 A/别名重绑。这一组观察在 `observations.json` 的 `areas.equivalent_version`。
- **按名字找版本不够**：用户说的“另一个同名配方版本”可能只是一个已被停用、但捕获仍引用的历史版本；`GET /recipes/{name}/versions/{n}` 与捕获行都能读到，不依赖近期列表（近期列表只显示最后 50 个版本）。

### C：接收方拿错了已交付文件

- **排除它的观察**：交付字节 == 状态库里该封存捕获保存的 `artifact`，并且 `bundle.certify(delivered, sealed)` 通过。字节相同即证明父键不可能不同。
- **确认它的观察**：交付字节等于**另一个**已认证捕获的 `artifact` / 证书 `sha256`；或字节与所有证书都不同、但内容能通过另一个捕获的认证。判定结果里的 `actual_captures` 就是拿错的那份文件的捕获编号，可直接回查其页面。
- **证书只在字节层面成立**：`certificate.sha256` 是对交付字节的摘要，不是对内容的承诺。接收方把文件另存（如 `VACUUM INTO`）后，SHA-256 会变而内容不变；triage 里这种文件判 `C excluded, byte_bound_to_certificate=false`，提示去要比对的是**当时交付的** SHA-256，而不是现在这份副本。
- **字节相同 ≠ 证据相同**：triage 的 `occurrence` 一组里，同一条根出现一次和两次会产出**完全相同的 SQLite 字节**和相同 `sha256`，但 `capture_digest` 不同、`roots` 不同（1 次 vs 2 次）。所以“拿到的文件字节对得上”能排除父键不同，却不能单独认定是哪一次捕获；`capture_digest` 才能区分。

## 无法从历史记录确认的范围

以下事实**没有落进状态库**，因此事后只能标 `consistent_unconfirmed`，不能证伪也不能证实：

1. **源文件当时的字节**。`captures` 只存 `sealed`（模式、配方、结果行、见证）和 `artifact`；`recipes` 只存 `name`、`version`、`active`、`schema_digest`、`recipe`。没有任何源文件内容、mtime、大小或每表校验和。A 的“源在两次读之间变过”在时间上只有两个事务边界，没有可回放的证据。
2. **别名到文件的映射**。配方里存的是 `source` 别名（如 `primary`），不是解析后的绝对路径。triage 的 `alias_remap` 演示：同一个仓库、同一个版本 1、同一个 `schema_digest`，重启后把 `lab` 别名从 A 文件改指 B 文件，两次封存的父键不同、行数相同、证书 SHA 不同。历史记录里**两次都是版本 1、配方逐字相同**，从状态库看与 A 无法区分。服务启动时的 `--source ALIAS=FILE` 参数不写进状态库，也不留历史。
3. **试算与封存各自的时间点**。`/preview` 不落库（整轮 triage 里 `captures` 只增于 `/captures`，没有任何试算行），所以无法证明“先试算后封存”的先后，也无法证明两次读之间经过了多久。页面上的“未封存/已封存”只是同一次请求的渲染状态。
4. **两次读是否真的落在同一源快照**。`storage.source` 每次调用新开只读连接、`BEGIN` 一次；试算和封存是两个独立连接、两个快照。库中没有快照号、WAL 帧号或事务时间戳可对比。
5. **谁提交了哪个请求**。状态库没有操作者、会话或客户端 IP 列。B 能确认“捕获行写的是版本 2”，但不能确认“是谁在哪个页面点的”。

因此当 C 被排除、B 被排除（或仅有等价版本）、A 又没有外部摘要时，正确结论是：**能确认的只有“封存那次读到的父键是什么”；“源何时变成那样”无法从历史记录确定**。要补上这个缺口，只能靠仓库外的材料，见下节。

## 复跑材料

- `examples/triage.py`：场景构造 + `apply_decision` 判断表 + `--diagnose` 离线判定 + `--expect` 回归比对。
- `examples/triage_observations.json`：一次真实运行的规范观察（捕获编号已掩成 `CAPTURE`），`--expect` 用它做回归；`--output` 会生成完整版，含随机编号。
- `examples/microscope.sql`：源库模式与数据，A/B/C 都基于它。
- `python3 -m examples.triage --output DIR` 留下的材料：`observations.json`（判定与事实）、`reviewed.sqlite`、`drifted.sqlite`（两份可独立打开的交付文件）、`repository.sqlite`（含两者 `sealed` 与 `artifact`，可直接喂给 `--diagnose`）。

### 现有演练的边界

- `examples/walkthrough.py` 和 `examples/browser_walkthrough.py` 已覆盖：试算计数、封存、追踪、构建、认证、下载、`compare` 的 `modified`、模式漂移拒绝、停用后仍可读。它们**不**构造“行数与模式相同但父键不同”的情形，也没有别名重绑与拿错文件的判定——这些只在 `examples/triage.py` 里。
- 本仓库仍按 `PROJECT_SPEC.md` 延后独立自动化测试套件；`examples/triage.py` 的断言与 `--expect` 是当前唯一的情景回归。
