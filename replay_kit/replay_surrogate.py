#!/usr/bin/env python3
"""可公开重放的关系争议样本驱动脚本。

用合成替代源库（replay_kit/microscope_surrogate.sql）启动真实 Keelsplice
JSON HTTP 服务，依次驱动：

  * 三个正确结论（correct_a/b/c）：NOCASE 复合关系、部分 NULL 关系、盒循环；
  * 五类错误结论诱导：输入拼写追踪、不完整复合键、NULL/缺失根、行预算截断、
    外键编号与模式冲突；
  * 构建 -> 认证 -> 下载，得到可独立打开的认证子库；
  * 用独立只读 SQLite 连接复核外键完整性、生成列、BLOB 与各表行数；
  * 源变更隔离与同模式捕获对比（修改值 -> 差异落在真实主键上）；
  * 模式漂移拒绝封存/对比。

用法（在仓库根目录）：
    python3 -I replay_kit/replay_surrogate.py            # 全量重放
    python3 -I replay_kit/replay_surrogate.py --keep DIR # 保留全部产物到 DIR

脚本只使用标准库与本仓库 keelsplice 包；不读取任何工位内部库。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from keelsplice.api import create_server  # noqa: E402

KIT = Path(__file__).resolve().parent
SQL_FILE = KIT / "microscope_surrogate.sql"
CONTROLS = KIT / "control_inputs"
ALIAS = "surrogate"


def seed(path: Path) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(SQL_FILE.read_text(encoding="utf-8"))


def call(base, route, body=None, status=200):
    headers = {"Content-Type": "application/json"}
    data = None if body is None else json.dumps(body).encode("utf-8")
    try:
        response = urlopen(Request(base + route, data, headers), timeout=10)
    except HTTPError as exc:
        response = exc
    with response:
        payload = response.read()
        assert response.status == status, (route, response.status, payload[:400])
        if response.headers.get_content_type() == "application/vnd.sqlite3":
            return payload, response.headers["ETag"].strip('"')
        return json.loads(payload)


def load(name):
    return json.loads((CONTROLS / name).read_text(encoding="utf-8"))


def assert_eq(actual, expected, label):
    assert actual == expected, f"{label}: 期望 {expected!r}，实得 {actual!r}"
    shown = actual.hex() if isinstance(actual, bytes) else json.dumps(actual, ensure_ascii=False)
    print(f"  ok - {label}: {shown}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", metavar="DIR", help="保留产物目录（默认临时目录自动清理）")
    args = parser.parse_args()

    work = Path(args.keep).resolve() if args.keep else None
    if work is not None:
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        temp = None
        root = work
    else:
        temp = TemporaryDirectory(prefix="keelsplice-surrogate-")
        root = Path(temp.name)

    try:
        source = root / "surrogate_source.sqlite"
        repo = root / "workbench_state.sqlite"
        seed(source)
        print(f"[1/8] 替代源库已生成: {source}")

        with create_server(("127.0.0.1", 0), repo, {ALIAS: source}) as server:
            thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                run_workflows(base, source, root)
            finally:
                server.shutdown()
                thread.join()
        print("\n全部断言通过。产物目录:", root)
    finally:
        if temp is not None:
            temp.cleanup()


def run_workflows(base, source, root):
    # --- 目录：结构与工位内部库同构，但键值全部替换 ---------------------
    print("[2/8] 目录核对")
    catalog = call(base, f"/sources/{ALIAS}/catalog")
    edges = {(e["child"], e["id"]): (e["parent"], e["child_cols"], e["parent_cols"])
             for e in catalog["schema"]["edges"]}
    assert edges[("assay", 0)] == ("substrate", ["region", "code"], ["region", "code"])
    assert edges[("assay", 1)] == ("cassette", ["cassette"], ["serial"])
    assert edges[("frame", 0)] == ("assay", ["mark"], ["mark"])
    assert edges[("cassette", 0)] == ("cassette", ["prior"], ["serial"])
    print("  ok - 复合 NOCASE 边 assay(region,code)->substrate 与盒自引用边 cassette.prior->cassette 均可识别")

    # --- 正确结论 A -----------------------------------------------------
    print("[3/8] 正确结论 A：单 NOCASE 根 + 盒循环对 + 大小写折叠")
    spec_a = load("correct_a_single_nocase_root.json")
    va = call(base, "/recipes/alpha/versions", spec_a["request"], 201)
    assert va["version"] == 1
    preview_a = call(base, "/preview", {"name": "alpha", "version": 1})
    assert_eq(preview_a["roots"], spec_a["expect"]["normalized_roots"], "A 实存根（折叠输入拼写）")
    assert_eq(preview_a["summary"]["counts"], spec_a["expect"]["preview_counts"], "A 闭包计数")
    cap_a = call(base, "/captures", {"name": "alpha", "version": 1}, 201)
    id_a = cap_a["id"]

    traced = call(base, f"/captures/{id_a}/trace", spec_a["expect"]["trace_actual_key"])
    assert_eq(traced["roots"], [0], "A 根序号")
    assert_eq(len(traced["causes"]), 2, "A 直接入选原因数（两个 assay 子行各一弧）")

    # --- 错误诱导 1：追踪必须用实存键 -----------------------------------
    print("[4/8] 错误诱导对照：输入拼写 / 不完整键 / NULL 与缺失根 / 预算 / 边与模式")
    wrong1 = load("wrong_1_trace_input_spelling.json")
    err = call(base, f"/captures/{id_a}/trace", wrong1["request"], 404)
    assert_eq(err["error"], "row_not_captured", "wrong_1 错误拼写追踪被拒")

    # --- 错误诱导 2：不完整复合键 ---------------------------------------
    wrong2 = load("wrong_2_partial_composite_key.json")
    err = call(base, "/recipes/bad/versions", wrong2["request"], 422)
    assert_eq(err["error"], "invalid_key", "wrong_2 复合键维数不足被拒")

    # --- 错误诱导 3：NULL 根分量 / 缺失根 -------------------------------
    wrong3 = load("wrong_3_null_and_missing_roots.json")
    for case in wrong3["cases"]:
        phase = case["expect"].get("reject_phase", "register")
        if phase == "register":
            err = call(base, "/recipes/bad/versions", case["request"], case["expect"]["http_status"])
            assert_eq(err["error"], case["expect"]["error"], f"wrong_3 {case['label']}")
        else:
            # 键形状合法时登记成功，缺失根在试算读取源快照时才暴露
            version = call(base, "/recipes/bad/versions", case["request"], 201)
            err = call(base, "/preview", {"name": "bad", "version": version["version"]},
                       case["expect"]["http_status"])
            assert_eq(err["error"], case["expect"]["error"], f"wrong_3 {case['label']}（试算阶段）")

    # --- 错误诱导 4：行预算截断必须整体拒绝 -----------------------------
    wrong4 = load("wrong_4_row_budget_truncation.json")
    call(base, "/recipes/tight/versions", wrong4["request"], 201)
    err = call(base, "/preview", {"name": "tight", "version": 1}, 422)
    assert_eq(err["error"], "row_budget", "wrong_4 预算不足整体拒绝，无部分捕获")

    # --- 错误诱导 5a：未知外键编号 --------------------------------------
    bad_edge = load("wrong_5_edge_and_schema_conflict.json")["cases"][0]
    err = call(base, "/recipes/bad/versions", bad_edge["request"], 422)
    assert_eq(err["error"], "invalid_edge", "wrong_5 未知外键编号被拒")

    # --- 正确结论 B：双 NOCASE 根 + frame 扇出 + NULL 关系 --------------
    print("[5/8] 正确结论 B：双 NOCASE 根、部分 NULL 关系、NULL 边与尾盒区分")
    spec_b = load("correct_b_dual_nocase_roots.json")
    call(base, "/recipes/bravo/versions", spec_b["request"], 201)
    preview_b = call(base, "/preview", {"name": "bravo", "version": 1})
    assert_eq(preview_b["roots"], spec_b["expect"]["normalized_roots"], "B 实存根")
    assert_eq(preview_b["summary"]["counts"], spec_b["expect"]["preview_counts"], "B 闭包计数")
    cap_b = call(base, "/captures", {"name": "bravo", "version": 1}, 201)
    id_b = cap_b["id"]

    trace_t12 = call(base, f"/captures/{id_b}/trace", {"table": "assay", "key": ["T-12"]})
    parent_arcs = [a for a in trace_t12["causes"] if a["direction"] == "parent"
                   and a["edge"] == ["assay", 0]]
    assert_eq(parent_arcs, [], "T-12 复合子键含 NULL，无 substrate 父行弧")
    trace_204 = call(base, f"/captures/{id_b}/trace", {"table": "frame", "key": [204]}, 404)
    assert_eq(trace_204["error"], "row_not_captured", "frame 204（mark NULL）未被纳入")
    trace_43 = call(base, f"/captures/{id_b}/trace", {"table": "cassette", "key": [43]})
    origins = sorted({tuple(arc["origin"][0:1] + [tuple(arc["origin"][1])]) for arc in trace_43["causes"]})
    print("  ok - 尾盒 43 入选来源:", origins)
    assert any(arc["origin"] == ["assay", ["T-12"]] for arc in trace_43["causes"]), "尾盒 43 仅经 T-12/T-13 父边到达"

    # --- 正确结论 C：NULL 关系行自己做根 --------------------------------
    print("[6/8] 正确结论 C：部分 NULL 关系行做根，父弧为空但盒父行仍强制纳入")
    spec_c = load("correct_c_null_relation_root.json")
    call(base, "/recipes/charlie/versions", spec_c["request"], 201)
    preview_c = call(base, "/preview", {"name": "charlie", "version": 1})
    assert_eq(preview_c["summary"]["counts"], spec_c["expect"]["preview_counts"], "C 闭包计数")
    cap_c = call(base, "/captures", {"name": "charlie", "version": 1}, 201)
    id_c = cap_c["id"]
    trace_c = call(base, f"/captures/{id_c}/trace", spec_c["expect"]["trace_actual_key"])
    substrate_deps = [a for a in trace_c["dependencies"] if a["edge"] == ["assay", 0]]
    assert_eq(substrate_deps, [], "C T-12 无 substrate(edge 0) 父依赖弧")
    cassette_deps = [a for a in trace_c["dependencies"] if a["edge"] == ["assay", 1]]
    assert_eq(len(cassette_deps), 1, "C T-12 的 cassette 父边仍强制纳入尾盒 43")
    assert_eq(len(trace_c["causes"]), 0, "C T-12 为根且无入弧，causes 为空（根身份由 roots 字段证明）")
    assert_eq(trace_c["roots"], [0], "C T-12 根序号")

    # --- 构建 / 认证 / 下载 A，并以独立连接复核 -------------------------
    print("[7/8] 构建、认证、下载独立子库并独立复核")
    call(base, f"/captures/{id_a}/build", {}, 200)
    err = call(base, f"/captures/{id_a}/database", None, 409)
    assert_eq(err["error"], "fixture_not_certified", "未认证拒绝下载")
    certified = call(base, f"/captures/{id_a}/certify", {}, 200)
    certificate = certified["certificate"]
    data, etag = call(base, f"/captures/{id_a}/database")
    assert_eq(sha256(data).hexdigest(), etag, "下载字节 SHA-256 与 ETag 一致")
    assert_eq(etag, certificate["sha256"], "ETag 与证书摘要一致")
    fixture = root / "surrogate_handoff_A.sqlite"
    fixture.write_bytes(data)

    # 同步交付 B、C 的认证子库，便于复核组分别打开
    for cid, suffix in ((id_b, "B"), (id_c, "C")):
        call(base, f"/captures/{cid}/build", {}, 200)
        call(base, f"/captures/{cid}/certify", {}, 200)
        bytes_c, _ = call(base, f"/captures/{cid}/database")
        (root / f"surrogate_handoff_{suffix}.sqlite").write_bytes(bytes_c)

    verify_fixture(fixture, spec_a)

    # 三个认证子库各复制一份到交付目录 build/，供不开服务时直接打开
    shipped_dir = KIT / "build"
    shipped_dir.mkdir(parents=True, exist_ok=True)
    for handoff in root.glob("surrogate_handoff_*.sqlite"):
        shutil.copyfile(handoff, shipped_dir / handoff.name)
        print(f"  ok - 认证子库已复制到 {(shipped_dir / handoff.name).relative_to(KIT.parent)}")

    # --- 源变更隔离 + 同模式对比 + 模式漂移 -----------------------------
    print("[8/8] 源变更隔离、同模式对比与模式漂移拒绝")
    with closing(sqlite3.connect(source)) as conn, conn:
        conn.execute("UPDATE assay SET length_um=8.125 WHERE mark=?", ("T-10",))
    cap_a2 = call(base, "/captures", {"name": "alpha", "version": 1}, 201)
    diff = call(base, "/compare", {"left": id_a, "right": cap_a2["id"]})
    assert_eq(diff["changes"]["assay"]["modified"], [["T-10"]], "同模式对比按实存主键定位修改行")
    # 已认证的交付字节保持封存时刻的值，不受源后续更新影响
    with closing(sqlite3.connect(fixture.as_uri() + "?mode=ro", uri=True)) as conn:
        row = conn.execute("SELECT length_um,doubled FROM assay WHERE mark='T-10'").fetchone()
    assert_eq(list(row), [4.5, 9.0], "交付子库隔离源后续变更（含生成列）")

    with closing(sqlite3.connect(source)) as conn, conn:
        conn.execute("ALTER TABLE frame ADD COLUMN caption TEXT")
    err = call(base, "/captures", {"name": "alpha", "version": 1}, 409)
    assert_eq(err["error"], "schema_conflict", "模式漂移拒绝用旧配方封存")
    call(base, "/recipes/alpha/versions", spec_a["request"], 201)
    cap_a3 = call(base, "/captures", {"name": "alpha", "version": 2}, 201)
    err = call(base, "/compare", {"left": id_a, "right": cap_a3["id"]}, 409)
    assert_eq(err["error"], "schema_conflict", "wrong_5 异模式捕获拒绝直接对比")

    # 写出证书与重放结果清单
    handoffs = {}
    for cid, suffix in ((id_a, "A_single_nocase"), (id_b, "B_dual_nocase"), (id_c, "C_null_relation")):
        record = call(base, f"/captures/{cid}")
        handoffs[suffix] = {
            "file": f"surrogate_handoff_{suffix[0]}.sqlite",
            "sha256": record["certificate"]["sha256"],
            "bytes": record["certificate"]["bytes"],
            "counts": record["certificate"]["counts"],
        }
    manifest = {
        "surrogate_sql": str(SQL_FILE.relative_to(KIT.parent)),
        "schema_digest": certificate["schema_digest"],
        "captures": {"A_single_nocase": id_a, "B_dual_nocase": id_b, "C_null_relation": id_c},
        "certificate_A": certificate,
        "handoff_files": handoffs,
    }
    (root / "replay_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("  ok - 清单与证书已写入 replay_manifest.json")


def verify_fixture(fixture, spec_a):
    with closing(sqlite3.connect(fixture.as_uri() + "?mode=ro", uri=True)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        present = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        assert_eq(sorted(present), spec_a["expect"]["fixture_tables"], "子库保留全部表（含 0 行表）")
        assert_eq(conn.execute("SELECT count(*) FROM frame").fetchone()[0], 0, "frame 表结构保留但 0 行")
        serials = [r[0] for r in conn.execute("SELECT serial FROM cassette ORDER BY serial")]
        assert_eq(serials, spec_a["expect"]["fixture_cassette_serials"], "盒循环对 41<->42，尾盒 43 未纳入")
        row = conn.execute("SELECT length_um,doubled FROM assay WHERE mark='T-10'").fetchone()
        assert_eq(list(row), spec_a["expect"]["fixture_generated"]["T-10"], "生成列 doubled 随封存值重建")
        blob = conn.execute("SELECT texture FROM substrate").fetchone()[0]
        assert_eq(blob, b"\x0a\x1b\x2c", "BLOB 键值原样重建")
        # 循环装载后自引用外键仍然闭合
        pairs = conn.execute("SELECT serial,prior FROM cassette ORDER BY serial").fetchall()
        assert_eq([list(p) for p in pairs], [[41, 42], [42, 41]], "盒循环对在独立子库中闭合")


if __name__ == "__main__":
    sys.exit(main())
