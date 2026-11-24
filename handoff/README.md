# 残缺台账交接包（Keelsplice）

接班人收到的材料与核对结论。两组同名配方多次交接，台账公开抄本有缺抄与错抄。

## 文件

- `ledger.md` —— 收到的台账公开抄本（短编号、缺抄的复合键、一条完整证书）
- `reconciliation.md` —— 核对报告：可唯一核对 / 多种解释 / 证据矛盾，
  附真实页面路径与允许用于补全的跨行线索
- `state.sqlite` —— 工作台状态库（5 个配方版本、5 次捕获、1 份证书）
- `microscope.sqlite` —— 源库现状（S-01 已改长 4.75、frame 已加 caption 列）
- `downloads/keelsplice-40d2607bbfcf454896b48e65ac6e6525.sqlite` —— 已认证交付子库（SHA-256 同台账 A1 行证书）
- `generate.py` —— 生成本包的剧本脚本

## 启动工作台核对

```sh
python3 -m keelsplice --repository handoff/state.sqlite \
  --source microscope=handoff/microscope.sqlite --port 8794
# 浏览器打开 http://127.0.0.1:8794/workbench
```

## 重新生成本包

```sh
python3 handoff/generate.py --force
```

捕获编号与证书摘要每次生成都会变化，文档随状态一起重写。
