# 关系转投最小复现包（行数不变，父对象更换）

面向另一位支持人员的可直接重放材料，用于重现这一情形：

> 同一个子对象 `member/m-1` 的外键从父对象 `team-a` 转投到 `team-b`；
> 两次试算（preview）的**总行数与逐表计数完全相同（均为 2 行）**，
> 但封存捕获里的直接关系证据和对比结果明确显示关系换了父对象。

所有数据都是为本案例专门编写的合成数据（见 `relay.sql` 头部的 CC0 1.0 公有领域声明），
不包含任何客户数据，可公开发送。案例只使用 Keelsplice 已有的 JSON 接口与标准 SQLite。

## 1. 文件清单

| 文件 | 作用 |
| --- | --- |
| `relay.sql` | 可公开的最小源库（状态 A）。两张表、三条数据行，一条外键边。 |
| `replay_relay.py` | 完整操作顺序：建临时源库与状态库、起真实 HTTP 服务、登记配方、三次试算/封存、追踪、对比、构建/认证，并执行六组“删除关键事实”的失败演示。全部步骤带断言。 |
| `captures/` | 一次真实重放落盘的人可读证据（JSON）。重跑会覆盖；捕获 ID 是随机不透明字符串，每次不同，其余内容确定性一致。 |

## 2. 源（公开数据，最小两行关系）

```sql
CREATE TABLE team   (id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE member (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES team(id),   -- 唯一一条外键边，边编号 id=0
    skill REAL NOT NULL
);
INSERT INTO team VALUES ('team-a','Alpha 班组'), ('team-b','Bravo 班组');
INSERT INTO member VALUES ('m-1','team-a',1.5);  -- 状态 A：m-1 隶属 team-a
```

- `member.team_id → team.id` 是目录里唯一的外键边（`catalog_edge.json`，`"id": 0`）。
- 配方根选子对象 `member/m-1`，**不选任何 `follow_children`**。
  Keelsplice 对每条外键都自动反向补齐必需父行（`capture.collect` 的 `towards_parent`），
  因此闭包恒为“1 个子行 + 它当前引用的 1 个父行 = 2 行”，父行随外键值更换而更换。

### 三个源状态（同一模式，仅数据不同）

| 状态 | member 行 | m-1 的父对象 | skill | 源快照证据 |
| --- | --- | --- | --- | --- |
| A（`relay.sql` 原样） | `('m-1','team-a',1.5)` | **team-a** | 1.5 | `source_state_a.json` |
| B（事务改写） | `('m-1','team-b',1.5)` | **team-b** | 1.5 | `source_state_b.json` |
| C（对照，事务改写） | `('m-1','team-a',2.25)` | team-a（不变） | 2.25 | `source_state_c.json` |

A→B 的唯一数据变更是 `UPDATE member SET team_id='team-b' WHERE id='m-1'`（关系转投）；
A→C 只改非外键列 `skill`，关系不动（“只是值有变”的对照）。

## 3. 配方（JSON 请求体）

```json
{
  "source": "relay",
  "roots": [{"table": "member", "key": ["m-1"]}],
  "row_limit": 10
}
```

登记为 `POST /recipes/relay-sample/versions`（见 `recipe.json`，版本 1）。
三个状态共用同一个配方版本；封存是对发起时刻读快照的独立读取，已封存内容不随后续源更新而变化。

## 4. 操作顺序（可手工重放）

启动（源库与状态库必须分开，参考仓库 README）：

```sh
python3 -m examples.relay.replay_relay          # 一键全自动重放并写证据
```

或按下面的接口顺序手工执行（`replay_relay.py` 即是该顺序的可执行版本）：

1. `GET  /sources/relay/catalog` —— 确认外键边 `member(team_id) → team(id)`，记下模式摘要。
2. `POST /recipes/relay-sample/versions` —— 用上面的配方体登记，得到版本 1。
3. 状态 A：`POST /preview`（试算）→ `POST /captures`（封存捕获 A）。
4. 对源库执行状态 B 的 `UPDATE`（改外键，不动模式）。
5. `POST /preview` → `POST /captures`（捕获 B），复用同一配方版本。
6. 对源库执行状态 C 的 `UPDATE`（只改 `skill`，外键改回 `team-a`）。
7. `POST /preview` → `POST /captures`（捕获 C）。
8. 行证据：对 A、B 分别 `POST /captures/{id}/trace`，体为 `{"table":"member","key":["m-1"]}`；
   再在 B 上追踪 `{"table":"team","key":["team-a"]}`。
9. 对比：`POST /compare` 分别比较 A/B、A/C、A/A。
10. 对 A、B 分别 `POST /captures/{id}/build`、`/certify`，再 `GET /captures/{id}/database`，
    用独立 SQLite 连接做 `PRAGMA foreign_key_check` 与父子 JOIN 复核。

## 5. 供人检查的捕获对比（如何区分“换了关系”与“只是值有变”）

三次试算的计数完全一致（`preview_a/b/c.json`）：

```json
{"counts": {"member": 1, "team": 1}, "rows": 2, "witnesses": 1, "root_occurrences": 1}
```

行数不变时，判别完全落在对比结果的**父表增减**与**行证据**上：

| 比较 | member（子，按主键 m-1 归类） | team（父） | 结论 |
| --- | --- | --- | --- |
| A vs B（`compare_a_b.json`） | `modified: [["m-1"]]`，无增减 | **`added [["team-b"]]`、`removed [["team-a"]]`** | **关系确实转投**：同一子对象身份未变，必需父行整体换了一个 |
| A vs C（`compare_a_c.json`） | `modified: [["m-1"]]`，无增减 | 全部为空 | **只是值有变**：父表无增减，非外键列变化 |
| A vs A（`compare_same.json`） | 全空 | 全空 | 自比基线，证明对比器对无差异场景输出空集 |

行证据进一步独立佐证（同一条边 `["member",0]`，根序号都是 0）：

- `trace_a_member.json`：m-1 的 `dependencies` 指向 `["team",["team-a"]]`；
- `trace_b_member.json`：同一 m-1 的 `dependencies` 指向 `["team",["team-b"]]`；
- `trace_b_team_a_not_found.json`：旧父行 `team-a` 在捕获 B 中 `row_not_captured`（404），
  即它不再是该闭包的成员，而不是“还在库里但值变了”。

构建/认证复核（`build_a.json`、`build_b.json`）：两个交付库 `foreign_key_check` 均无违规，
独立连接的 `member JOIN team` 分别得到 `("m-1","team-a")` 与 `("m-1","team-b")`，
下载字节的 ETag 与证书 SHA-256 一致。

**判别规则一句话**：子对象主键保持 `modified`（身份不变）而父表出现一对 `removed/added`，
才是同一对象的关系转投；若父表零增减、只有子行 `modified`，则只是值有变。

## 6. 删除任一关键事实即失效（已在重放中实际演示）

| # | 关键事实 | 删除后会怎样 | 失败演示证据 |
| | --- | --- | --- |
| F1 | 两个候选父行 team-a、team-b 都实存 | 转投目标 `team-x` 不存在时，外键拒绝写入，状态 B 根本建不出来 | `negative_orphan_target.json`：`FOREIGN KEY constraint failed` |
| F2 | 来源父行 team-a 受外键保护 | m-1 仍引用 team-a 时无法删除它；可删除则“转投前的关系”无从证明 | `negative_delete_parent.json`：`FOREIGN KEY constraint failed` |
| F3 | `member.team_id REFERENCES team(id)` 这条外键声明 | 删掉 REFERENCES 后目录里没有边，闭包只剩根行 1 条、`witnesses=0`，父对象不再进入捕获，无从谈“换父” | `negative_no_fk.json`：`edges=[]`，计数 `{member:1, team:0}` |
| F4 | 同一模式（无 DDL 漂移） | 源加列后模式摘要改变，跨模式对比被 409 拒绝，两次捕获不能并排归类 | `negative_drift.json`：`schema_conflict` |
| F5 | 行预算足以容纳闭包（≥2 行） | `row_limit=1` 时试算直接 422 `row_budget`，拿不到任何试算计数 | `negative_row_budget.json` |
| F6 | 子对象身份稳定（主键 m-1 不变） | 删掉 m-1 再插入 m-2 是身份更替：对比里 member 同时出现 `removed m-1` / `added m-2` 而非 `modified m-1`，不能再表述为“m-1 转投” | `negative_rekey_compare.json` |
| F7 | 非外键值列 `skill` 实存 | 没有与关系无关的值列，就构造不出 A/C 对照，无法向对方证明“仅值变化不会引起父表增减” | 主流程 A→C 与 `compare_a_c.json`（删除该列即无对照可跑） |

## 7. 确定性与边界说明

- 每次重放使用全新临时目录；除捕获 ID（`uuid4().hex`）外，所有计数、对比归类、
  模式摘要、追踪结构、错误码均确定性复现。
- 全程只触碰临时库；`replay_relay.py` 不修改 `relay.sql`，状态 B/C 在临时副本上以事务生成。
- 浏览器表单可完成同等操作；本包固定走 JSON 接口，便于无界面重放和逐字比对。
- 复跑命令与仓库既有演练一致：

```sh
python3 -m compileall -q keelsplice examples
python3 -m examples.relay.replay_relay
```
