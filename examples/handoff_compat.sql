-- 接收方兼容验收的可复跑 SQLite 源输入。
-- 由 examples/acceptance_compat.py 载入临时库，经真实工作台封存、构建、认证后独立复核。
--
-- 设计意图：
--   registry / event_log  —— 有外键关联，配方闭包会选中其中一部分行；
--   staging_area          —— 源库里有行，但闭包选不中任何一行，用于区分
--                            “没有入选行”与“结构仍在”；
--   两个显式索引          —— 属交付范围，子库中定义保留且可被查询计划使用；
--   recent_events 视图、
--   event_log_insert_note 触发器 —— 源库可用但不属子库交付范围。
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE registry (
    tenant TEXT NOT NULL,
    asset_id INTEGER NOT NULL,
    label TEXT NOT NULL,
    PRIMARY KEY (tenant, asset_id)
) WITHOUT ROWID;

CREATE TABLE event_log (
    event_id INTEGER PRIMARY KEY,
    tenant TEXT NOT NULL,
    asset_id INTEGER NOT NULL,
    detail TEXT NOT NULL,
    FOREIGN KEY (tenant, asset_id) REFERENCES registry(tenant, asset_id)
);

-- 闭包无法到达的独立表：源库有 2 行，交付子库预期 0 行，但 DDL、约束和索引都在。
CREATE TABLE staging_area (
    slot_id INTEGER PRIMARY KEY,
    tenant TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX staging_tenant_lookup ON staging_area(tenant) WHERE payload IS NOT NULL;

-- 显式索引随子库交付，接收方的索引行为应与源库一致。
CREATE INDEX event_log_tenant_lookup ON event_log(tenant, asset_id);

-- 功能对象：源库可查询、可触发；认证子库中根本不存在。
CREATE VIEW recent_events AS
    SELECT event_id, tenant, asset_id, detail
    FROM event_log
    ORDER BY event_id DESC;

CREATE TRIGGER event_log_insert_note
AFTER INSERT ON event_log
BEGIN
    UPDATE registry
       SET label = label
     WHERE tenant = NEW.tenant AND asset_id = NEW.asset_id;
END;

INSERT INTO registry VALUES
    ('core', 7, 'pump'),
    ('core', 8, 'valve');
INSERT INTO event_log VALUES
    (1001, 'core', 7, 'start'),
    (1002, 'core', 7, 'recalibrate'),
    (1003, 'core', 8, 'install');
INSERT INTO staging_area VALUES
    (1, 'core', 'seed-a'),
    (2, 'misc', 'seed-b');
