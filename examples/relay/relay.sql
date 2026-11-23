-- Keelsplice 关系转投最小复现源（状态 A：起始关系）
--
-- 本文件是为支持复现而专门编写的合成数据，不含任何客户数据。
-- 作者（本仓库）将其投入公有领域，适用 CC0 1.0 通用公共领域贡献：
-- https://creativecommons.org/publicdomain/zero/1.0/
--
-- 设计目标：两行即可表达一条明确的外键关系；
--   team   = 父对象表（两个候选父对象：team-a / team-b）
--   member = 子对象表，team_id 为指向 team.id 的外键，m-1 为试算根。
-- 状态 B/C 不修改本文件，由 replay_relay.py 在同一模式下以事务改写。
PRAGMA foreign_keys=ON;

CREATE TABLE team (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE member (
    id      TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES team(id),
    skill   REAL NOT NULL
);

-- 状态 A：成员 m-1 属于父对象 team-a
INSERT INTO team VALUES ('team-a', 'Alpha 班组'), ('team-b', 'Bravo 班组');
INSERT INTO member VALUES ('m-1', 'team-a', 1.5);
