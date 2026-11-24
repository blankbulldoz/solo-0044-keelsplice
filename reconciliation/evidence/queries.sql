-- Q-INTEG：完整性校验
PRAGMA integrity_check;

-- Q-FKCHECK：外键一致性校验
PRAGMA foreign_key_check;

-- Q-DDL：全部表定义
SELECT name, sql FROM sqlite_schema WHERE type='table' ORDER BY name;

-- Q-FRAME-COLS：frame 列清单（新增列证据）
PRAGMA table_info(frame);

-- Q-ROWCOUNTS：分表行数
SELECT 'substrate' AS tab, count(*) AS n FROM substrate UNION ALL SELECT 'assay', count(*) FROM assay UNION ALL SELECT 'cassette', count(*) FROM cassette UNION ALL SELECT 'frame', count(*) FROM frame;

-- Q-LEN-AGG：assay.length_um 聚合
SELECT count(*) AS n, total(length_um) AS total_um, avg(length_um) AS avg_um, min(length_um) AS min_um, max(length_um) AS max_um FROM assay;

-- Q-CASSETTE：各 cassette 的 assay 数
SELECT c.serial, c.label, count(a.mark) AS assays FROM cassette c LEFT JOIN assay a ON a.cassette = c.serial GROUP BY c.serial, c.label ORDER BY c.serial;

-- Q-FRAME-PER-ASSAY：各 assay 的 frame 数
SELECT a.mark, count(f.frame_id) AS frames FROM assay a LEFT JOIN frame f ON f.mark = a.mark GROUP BY a.mark ORDER BY a.mark;

-- Q-PER-SUBSTRATE：各 substrate 身份的 assay 数
SELECT s.region, s.code, count(a.mark) AS assays FROM substrate s LEFT JOIN assay a ON s.region = a.region AND s.code = a.code GROUP BY s.region, s.code ORDER BY s.region, s.code;

-- Q-SUBSTRATE-ROWS：substrate 身份与 texture 清单
SELECT region, code, hex(texture) AS texture_hex FROM substrate ORDER BY region, code;

-- Q-ASSAY-ROWS：assay 行级内容（含外键拼写）
SELECT mark, region, code, cassette, length_um, doubled FROM assay ORDER BY mark;

-- Q-FRAME-ROWS：frame 旧列行级内容
SELECT frame_id, mark, note FROM frame ORDER BY frame_id;

-- Q-PROBE-NOCASE：排序规则探针：EaSt/MiCa 命中数
SELECT count(*) AS n FROM substrate WHERE region = 'EaSt' AND code = 'MiCa';

-- Q-PROBE-FOLD：折叠旁证：East/Mica 族 assay 总数
SELECT count(*) AS n FROM assay WHERE region = 'east' COLLATE NOCASE AND code = 'mica' COLLATE NOCASE;

-- Q-CAPTION-NONNULL：v2 独有口径：caption 非空 frame 数（仅 v2 可执行）
SELECT count(*) AS n FROM frame WHERE caption IS NOT NULL;
