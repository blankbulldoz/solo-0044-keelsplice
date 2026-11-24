# Keelsplice 3.11 → 3.14 升级工件与运行手册

本目录交付一套可重复执行的升级工件，用**两个真实解释器**完成「在 3.11 上生成旧仓库 → 切到 3.14 → 验证 → 可执行回退」的完整闭环。所有入口都用 `python -I`（隔离模式）运行，不读取 `PYTHONPATH`、当前目录或环境变量，脚本自行把项目根加入 `sys.path`。

## 两个真实运行时

| 角色 | 解释器 | 链接的 SQLite | 位置 |
| --- | --- | --- | --- |
| 旧（legacy） | CPython 3.11.2（系统） | 系统 libsqlite3 **3.40.1** | `/usr/bin/python3.11` |
| 新（target） | CPython **3.14.10**（源码构建） | 自建 libsqlite3 **3.54.1**（rpath 内置） | `/workspace/.python-runtimes/python-3.14.10/bin/python3.14` |

3.14 运行时通过 rpath 固定加载 `/workspace/.python-runtimes/sqlite-3.54.1/lib/libsqlite3.so`，无需设置 `LD_LIBRARY_PATH`。这种「Python 版本和 SQLite 版本同时变化」的组合，正好用来检验升级判断能否区分**运行时（绑定层）差异**与 **SQLite（引擎层）能力差异**。

## 模块职责

| 文件 | 阶段 | 职责 |
| --- | --- | --- |
| `runtime_probe.py` | 1 | 盘点单个解释器，把 Python 运行时事实与 SQLite 库/能力事实分两节输出；`compare` 给出 go/no-go，逐条标注变更层级 |
| `legacy_repo.py` | 2 | 强制在真实 3.11 下生成旧仓库：旧配方、停用版本、封存捕获、认证子库及全套 3.11 报告 |
| `source_transaction.py` | 贯穿 | 直接对 SQLite 验证只读 URI、`query_only`、单快照 `BEGIN`、`trusted_schema=OFF`、进度预算与长度限制 |
| `http_form_probe.py` | 贯穿 | 裸 socket 抓取表单**逐帧字节**，覆盖 Origin/长度/类型拒绝路径与 JSON 归一化等价性，两端可字节级对比 |
| `fixture_probe.py` | 贯穿 | 不经工作台代码，用独立 SQLite 连接（外加独立 `sqlite3` shell）复读认证子库和状态库 |
| `switch.py` | 3–5 | `freeze` / `cutover` / `rollback`：原子快照、指针式切换、副本新捕获、可执行回退 |
| `run_upgrade.py` | 编排 | 以子进程在两个真实解释器间串联全部阶段，端到端一次跑完 |

## 一键执行

```sh
/usr/bin/python3.11 -I /workspace/upgrade/run_upgrade.py \
  --workdir /workspace/upgrade-workdir
```

也可显式指定：`--old-python`、`--new-python`、`--sqlite-cli`（独立复读用的 sqlite3 shell）。

阶段顺序：

1. 两端 `runtime_probe probe`，再 `compare` 出 go/no-go；
2. 真实 3.11 跑 `legacy_repo`，产出旧仓库与 `legacy_manifest.json`；
3. `switch freeze`：WAL checkpoint 后整目录快照到 `backups/pre-cutover-*`，写 `rollback.json`；
4. `switch cutover`（在 3.14 下）：核对旧配方/封存/证书原样可读，在副本上做**新捕获**并构建、认证、独立复读；
5. 3.14 全新临时库再跑一次表单探针，与 3.11 报告做**字节级对比**；
6. `switch rollback` 回退演练并用 3.11 复读；
7. 再次 cutover，使交付目录最终停在 3.14。

## 如何区分运行时差异与 SQLite 能力差异

`reports/runtime-compare.json` 中每条变更都带 `layer` 字段：

- `python_runtime` —— CPython / `sqlite3` **绑定层**随解释器变化，例如
  - `sqlite3.version` 旧属性在 3.12 被移除（3.11 有、3.14 无）；
  - `Connection.autocommit`、`getconfig/setconfig`、`SQLITE_DBCONFIG_*` 常量在 3.12+ 出现；
- `sqlite_library` —— 链接的**引擎库**变化，与 CPython 无关，例如
  - `sqlite_version_info` 3.40.1 → 3.54.1；
  - `unhex()` 等引擎能力（3.41 引入）在旧库不支持、新库支持。

工作台依赖的能力（`table_list`、`table_xinfo`、生成列、STRICT、WITHOUT ROWID、部分索引、`defer_foreign_keys`）在两端均通过，结论为 `verdict=go`；任一必需能力缺失则 `no-go` 并列出原因。

## 重点核对项与证据位置

- **HTTP 表单字节**：裸 socket 记录每个请求帧的 `frame_sha256/body_sha256` 与响应体哈希；探针自身前缀在对比前归一化为占位符，`reports/http-form-compare.json` 给出稳定响应字节一致、6 个关键帧归一化后全等、拒绝路径状态码一致。
- **独立 SQLite 复读**：`fixture_probe` 用全新只读 URI 连接做 `integrity_check`、`foreign_key_check`、结构/计数/证书核对，并调用独立 `sqlite3` shell（非 Python 读取者）复核；见 `reports/fixture-reread-*.json` 中 `standalone_shell`。
- **源读事务**：`reports/source-transaction-3.11.json` 与 `-3.14.json` 各 9 项检查全过，含并发写不影响 `BEGIN` 快照、只读连接拒绝写入。
- **副本上的新捕获**：`switch_record.json` 的 `new_capture` 记录 3.14 下新封存、构建、认证的捕获及其证书；旧捕获 `legacy_capture` 原样保留。
- **保留旧配方、封存与证书**：`reports/repository-reread-3.14.json` 显示配方版本、停用版本（`swatch@2`）、各捕获状态与 artifact/证书摘要一致；`digest_checks` 证明源库与旧认证子库字节在切换前后不变。

## 回退路径（可执行）

`rollback.json` 写明备份目录、待还原文件与旧解释器。手动执行：

```sh
/usr/bin/python3.11 -I /workspace/upgrade/switch.py rollback \
  --workdir /workspace/upgrade-workdir
```

回退从冻结备份逐字节复制 `state.sqlite`、源库与旧子库，`RUNTIME` 指针改回 3.11，并立刻用独立连接复读。已验证：回退后状态库 SHA-256 与冻结时**完全一致**，且用真实 3.11 服务重启后旧配方、停用状态、旧认证子库（ETag 与原证书一致）均可正常读取。回退演练报告在 `backups/rollback-*.json`。

## 单独运行各探针

```sh
# 运行时盘点与对比
python3.11 -I upgrade/runtime_probe.py probe --out old.json
python3.14 -I upgrade/runtime_probe.py probe --out new.json
python3.14 -I upgrade/runtime_probe.py compare --old old.json --new new.json --out diff.json

# 源读事务
python3.11 -I upgrade/source_transaction.py --source <source.sqlite> --out txn.json

# 独立复读
python3.14 -I upgrade/fixture_probe.py fixture --file <fixture.sqlite> \
  --certificate <cert.json> --sqlite-cli <sqlite3-shell> --out reread.json
python3.14 -I upgrade/fixture_probe.py repository --repository <state.sqlite> --out repo.json
```

## 说明

- 升级不修改 `keelsplice/` 应用代码：现有显式 `BEGIN`/`isolation_level=None` 的事务写法已与 3.14 默认 autocommit 模型兼容，未使用任何被移除的标准库或 `sqlite3` API。
- 不同 libsqlite3 版本生成的数据库文件字节可能不同，因此新捕获子库不要求与旧子库字节相同；每个子库都须通过各自的独立复读与证书核对，对比报告对此显式说明。
