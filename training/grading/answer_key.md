# 盲审答案与陷阱设计说明（评分方使用）

> 本文件解释每个包是如何构造的、正确决定是什么、以及决定性的可观察证据。
> 学员答卷不得引用本文件；评分按 `rubric.md` 只认可观察证据。
> 精确哈希见 `evidence_reference.json`，可由 `build_kits.py` + `recheck_kits.py` 复现。

## 陷阱总览

所有合法捕获都由真实的 Keelsplice 工作台 API 产生（登记配方 → 封存 → 构建 → 认证），
只有两处是在**组装阶段**人为植入的偏差，且都能被独立复核发现：

1. `beta`：替换状态库封存记录的根归属，制造“证书真实但归属错误”。
2. `b2`：对合规子库做仅改变物理页大小的 `VACUUM` 重排，再独立重新认证。

## 逐包答案

### alpha —— 可交接（合规基线）

- 配方根 `P1`，跟随 `site`（FK 0）与 `reading→site`（FK 1），闭包
  project=1, site=2, reading=3, staff=2（staff 由 reading.reviewer_id 父行补齐）。
- 决定性证据：`file_sha256 == certificate.sha256`；
  状态库封存记录重算指纹 `== certificate.capture_digest`；
  声明根 P1 = 封存根，且交付物 `project` 仅含 P1；完整性/外键通过。
- 它同时是 beta 的“真品来源”：beta 的证书和字节就来自这条合规捕获。

### beta —— 暂缓（捕获归属错误）★核心陷阱

- 构造：先在同一状态库合规产出 alpha 捕获与一个 P3 的 beta 捕获；删除 alpha 记录后，
  把 beta 记录的封存原文根改写成 P3，却仍挂着 **alpha 捕获的证书与字节**。
- 表面现象：状态显示 certified、文件能打开、外键干净、文件 SHA 与证书 SHA 完全匹配，
  且与 alpha 字节逐字节相同。只看这些会误判为可交接。
- 决定性证据（任一条即可，标准答案应两条都引用）：
  1. 状态库封存记录重算指纹 `5952…840` ≠ 证书 `capture_digest` `c55d…4f2`，
     即这份证书并不认证当前封存记录（检查项 `sealed_fingerprint_matches_certificate` = F）。
  2. 封存/声明根是 P3，但交付物 `project` 实际只有 P1
     （检查项 `sealed_root_present_in_deliverable` = F）。
- 反方澄清：文件 SHA 仍匹配，说明不是文件损坏，而是**归属被调包**；因此处置是暂缓退回核实，
  而不是“文件坏了重做”，更不能交接。

### b1 —— 可交接（合规，默认 4096 页布局）

- 配方根 `P2`，闭包 project=1, site=2, reading=3, staff=3。
- 决定性证据：四项自洽（字节 SHA、封存指纹=`capture_digest`、根一致且在交付物、完整性/外键）。

### b2 —— 可交接（合法物理重排，关系内容相同）

- 构造：与 b1 同配方同源产生等价捕获，构建后执行 `PRAGMA page_size=8192; VACUUM`，
  再用工作台的 `bundle.certify` 对重排后的字节**独立重新认证**。
- 表面现象：文件 SHA-256 与 b1 不同、字节数 81920 vs 40960。
- 决定性证据：两包 `capture_digest` 相同、`schema_digest` 相同、逐表计数相同、
  逻辑内容指纹相同（逐表行一致）；唯一差异是 `page_size` 4096→8192；
  b2 自身的文件 SHA 仍与其新证书 SHA 匹配，完整性/外键通过。
- 结论：这是物理布局重排而非关系内容变更，不构成暂缓理由，仍可交接。
  学员若仅凭“SHA 不同”判暂缓，属于把物理差异误判为内容差异。

### gamma —— 继续操作（流程未走完，非数据缺陷）

- 构造：只封存（`gamma-pull`，根 P1，仅跟随 site），不构建、不认证。
- 决定性证据：状态库 `state=sealed`，无 artifact、无 certificate，目录中无 deliverable.sqlite；
  工作台状态机会对未构建即认证返回 `fixture_not_built`、对未认证下载返回
  `fixture_not_certified`。
- 结论：数据与封存本身没有问题，正确处置是回到正常流程先构建再认证（继续操作）；
  不能在未认证时交接，也不应当作坏包暂缓。

## 成对结论要点

- **A 对**：alpha 与 beta 字节相同、`capture_digest` 字段值也相同（因为 beta 挂的是 alpha 的证书），
  但 beta 状态库里的实际封存记录指纹不同、根不在交付物。**不可同等交接**：alpha 可交接，beta 暂缓。
  这训练学员“证书值相同 ≠ 归属正确”，必须把证书与当前封存记录重新对账。
- **B 对**：SHA 不同但 `capture_digest`、`schema_digest`、逻辑指纹、计数全相同，
  差异仅页大小。**两者都可交接**，训练学员区分物理重排与关系内容变更。
