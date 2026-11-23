# Keelsplice 工作台盲审训练卷

一套可反复初始化、可独立复核的盲审训练材料，用于训练学员在“已认证”标签之下
识别**捕获归属错误**，并区分**文件物理重排**与**关系内容变更**。

## 目录结构与材料分层

```
training/
├── build/                     # 可初始化素材（入库）：如何从零生成所有交接包
│   ├── training_source.sql    # 登记源库的建表与数据（可重复灌入）
│   ├── build_kits.py          # 确定性构建：产出 5 个交接包到 generated/
│   └── recheck_kits.py        # 只读独立复核脚本（标准库、不依赖工作台进程）
├── handbook/                  # 学员材料（入库）：盲判阶段即可阅读
│   ├── student_workbook.md    # 学员手册：三选一处置决定与训练流程
│   ├── recheck_guide.md       # 独立复核指南：每条证据的只读 SQL/Python
│   └── answer_sheet.md        # 空白答卷模板
├── grading/                   # 评分材料（入库，但盲判阶段不向学员开放）
│   ├── rubric.md              # 评分判据：只认学员可观察证据
│   ├── answer_key.md          # 答案与陷阱构造说明
│   └── evidence_reference.json# 单次构建的精确哈希/指纹对照（可复现）
└── generated/                 # 构建产物（不入库，被 .gitignore 忽略）
    ├── alpha/ beta/ b1/ b2/ gamma/   # 五个交接包
    └── index.json
```

**素材与评分分离**：`build/` 与 `handbook/` 是学员可用的初始化与复核材料；
`grading/` 是评分方材料，盲判阶段不开放。生成的 `generated/` 不入库，随时可重建。

## 初始化（可反复运行）

在仓库根目录执行：

```sh
python3 training/build/build_kits.py
```

脚本会清空并重建 `training/generated/`，用真实工作台 API 产生全部合法捕获，
再组装两个陷阱包。输出是确定性的：重复构建得到相同的 SHA-256、`capture_digest`、
根与计数（捕获 ID 是随机 UUID，不作为判断依据）。脚本结束时会自检陷阱事实确实成立。

## 学员使用方式

1. 阅读 `handbook/student_workbook.md`，先做盲判并在 `answer_sheet.md` 写预期证据。
2. 按 `handbook/recheck_guide.md` 用独立只读 SQLite 连接与证书材料复核，或运行：
   ```sh
   python3 -I training/build/recheck_kits.py \
     --kit training/generated/alpha --kit training/generated/beta \
     --kit training/generated/b1 --kit training/generated/b2 --kit training/generated/gamma \
     --pair training/generated/alpha training/generated/beta \
     --pair training/generated/b1 training/generated/b2
   ```
3. 定稿交卷。评分方按 `grading/rubric.md` 仅依据可观察证据评分。

## 五个交接包与两组对比

| 包 | 表象 | 实质 | 正确处置 |
| --- | --- | --- | --- |
| alpha | 已认证、文件合规 | 合规基线 | 可交接 |
| beta | 已认证、与 alpha 文件逐字节相同 | 证书真实但**归属错误**（封存指纹不符、根不在交付物） | 暂缓 |
| b1 | 已认证 | 合规（4096 页） | 可交接 |
| b2 | 已认证、SHA 与 b1 不同 | 仅页大小改变的**合法物理重排**，关系内容相同 | 可交接 |
| gamma | 只有封存记录 | 流程停在封存，未构建未认证 | 继续操作 |

## 设计约束

- 所有 SQLite 访问在复核脚本中均为只读（`mode=ro`），脚本只用标准库，可用 `python3 -I` 运行。
- 评分判据不依赖任何学员无法观察的内部状态；每条得分点都映射到具名检查项、字段或 SQL 结果。
- 不修改 Keelsplice 工作台本身的任何代码，陷阱只在训练素材的组装阶段产生。
