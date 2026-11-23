# 工作台盲审训练卷 · 学员手册

## 目标

你将收到五个独立的**交接包（kit）**，它们来自 Keelsplice 关系样本工作台的不同批次。
你的任务不是找出“哪个文件长得不一样”，而是对每一个包作出三选一的处置决定，
并且**每一个决定都必须能被你亲自观察到的证据支撑**：

| 决定 | 含义 |
| --- | --- |
| 继续操作 | 材料本身没有问题，但流程尚未走完（例如还停在封存阶段），应回到正常的构建/认证流程，而不是拒收。 |
| 可交接 | 子库、证书与封存归属三者自洽，接收方可凭证书独立复核，可以交付。 |
| 暂缓 | 出现了无法由现有材料自证的矛盾（例如证书不归属当前封存记录、声明根不在交付物里），不得交接，需退回核实。 |

五个包分为两组对比和一个单独项：

- **A 对：`alpha` 与 `beta`** —— 两个包都显示“已认证”，子库文件看起来完全相同。
- **B 对：`b1` 与 `b2`** —— 两个包都显示“已认证”，但文件摘要（SHA-256）不同。
- **单独项：`gamma`** —— 没有子库文件，只有封存记录。

注意：标签“已认证”和“文件能打开、外键干净”都只是必要条件，不自动等于“可交接”。

## 交接包内容

每个 `training/generated/<kit>/` 目录包含：

| 文件 | 作用 |
| --- | --- |
| `handoff_manifest.json` | 交接声明：声称的配方、声称的根、是否声称已认证、捕获 ID。 |
| `capture_snapshot.json` | 工作台状态接口视角的捕获快照（状态、根、计数、摘要）。 |
| `certificate.json` | 认证记录：`sha256`、`bytes`、`schema_digest`、`capture_digest`、`counts`。 |
| `deliverable.sqlite` | 实际要交付的独立 SQLite 子库（`gamma` 没有此文件）。 |
| `state_copy.sqlite` | 该批次状态库的独立副本，含配方表与封存记录原文。 |
| `source_snapshot.sqlite` | 登记源库快照，可用于核对闭包与根是否真实存在。 |
| `kit_manifest.json` | 打包清单，仅列出文件，不含任何结论。 |

## 训练流程

### 第一阶段：盲判（不得打开 `training/grading/`，不得运行构建脚本）

仅依据交接包内材料，对五个包分别给出：处置决定 + 你打算依赖的证据线索。
先不要跑现成复核脚本，把你**预期会看到什么**写进答卷（例如“我预期文件 SHA 等于证书 SHA”）。
允许你自己用 `sqlite3` 命令行或 Python 标准库直接打开只读连接查看。

### 第二阶段：独立复核

按 `recheck_guide.md` 用独立 SQLite 连接和证书材料逐项验证你的预期。
可运行仓库提供的只读复核脚本（它不依赖工作台进程，只用标准库）：

```sh
python3 -I training/build/recheck_kits.py \
  --kit training/generated/alpha --kit training/generated/beta \
  --kit training/generated/b1 --kit training/generated/b2 --kit training/generated/gamma \
  --pair training/generated/alpha training/generated/beta \
  --pair training/generated/b1 training/generated/b2
```

脚本只输出可观察事实，不给结论。你也可以完全不用它，按指南手写等价查询。

### 第三阶段：定稿

用复核到的事实修正决定，在答卷中逐条引用证据（具体字段名、SQL 结果或检查项名称），
然后交卷。评分方只按 `training/grading/rubric.md` 中**可观察证据**给分。

## 关键概念：证书到底认证了什么

`certificate.json` 中有两个不同的摘要，切勿混淆：

- `sha256`：认证的是**交付文件的字节**。文件改一个字节就不匹配。
- `capture_digest`：认证的是**封存记录**（模式、入选行、根出现项、关系证据）的规范化指纹。
- `schema_digest`：认证模式结构。

因此存在两种不同性质的问题：

1. 字节与 `sha256` 不符 —— 文件被改动。
2. 字节与 `sha256` 相符，但状态库里当前封存记录的指纹与 `capture_digest` 不符 ——
   证书是真的，却**不归属当前这条封存记录**。这正是盲审要抓的归属错误，
   单看“已认证”标签或文件哈希是发现不了的。

另外，SHA-256 不同不必然代表内容不同：SQLite 的物理页布局（如 `page_size`、`VACUUM`）
改变会改变字节，但逻辑行、关系与 `capture_digest` 可以完全不变。需要区分
**物理重排**与**关系内容变更**。

## 答卷模板

复制 `answer_sheet.md` 填写。每个包至少给出：处置决定、支持证据、反方证据（为什么不是另外两个决定）。
