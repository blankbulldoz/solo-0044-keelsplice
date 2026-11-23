# 独立复核指南

所有步骤都只使用 Python 标准库或 `sqlite3` 命令行，以**只读**方式打开材料，
不需要启动 Keelsplice 工作台。路径一律作为参数传入；建议用 `python3 -I` 隔离模式，
避免当前目录中的同名模块被加载。下面的 `<KIT>` 代表某个交接包目录，例如
`training/generated/beta`。

## 1. 文件字节是否等于证书声明的 SHA-256

```sh
python3 -I - <<'EOF'
import hashlib, json, sys
from pathlib import Path
kit = Path(sys.argv[1])
cert = json.loads((kit / 'certificate.json').read_text())
actual = hashlib.sha256((kit / 'deliverable.sqlite').read_bytes()).hexdigest()
print('recomputed :', actual)
print('certificate:', cert['sha256'])
print('match      :', actual == cert['sha256'])
EOF
```

判断点：不匹配说明文件字节被改动；匹配**只能**证明字节与证书一致，不能证明证书归属正确。

## 2. 子库能否独立打开、完整性与外键

```sh
python3 -I - <<'EOF'
import sqlite3, sys
from pathlib import Path
file = Path(sys.argv[1]).resolve()
con = sqlite3.connect(file.as_uri() + '?mode=ro', uri=True)
print('integrity     :', con.execute('PRAGMA integrity_check').fetchall())
print('fk violations :', con.execute('PRAGMA foreign_key_check').fetchall())
print('page_size     :', con.execute('PRAGMA page_size').fetchone()[0])
print('tables        :', [r[0] for r in con.execute(
    "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")])
con.close()
EOF
```

判断点：`integrity_check` 必须是 `ok`，`foreign_key_check` 必须为空。
记录 `page_size`，B 对的差异来源就在这里。

## 3. 子库实际内容（用类型化主键核对根与闭包）

```sql
-- 对 deliverable.sqlite 只读执行
SELECT proj_id FROM project ORDER BY proj_id;
SELECT site_id, proj_id FROM site ORDER BY site_id;
SELECT reading_id, site_id, reviewer_id FROM reading ORDER BY reading_id;
SELECT staff_id, lead_id FROM staff ORDER BY staff_id;
```

判断点：交付物里的 `project` 行是不是交接声明里的那个根。
若声明根是 `P3` 而子库只有 `P1`，归属矛盾成立。

## 4. 状态库里的封存记录是否被当前证书认证（归属核对，核心步骤）

状态库 `state_copy.sqlite` 的 `captures` 表把封存原文存在 `sealed` 列（JSON 文本），
证书存在 `certificate` 列。重新计算封存原文的规范化指纹，与证书的 `capture_digest` 比较：

```sh
python3 -I - <<'EOF'
import hashlib, json, sqlite3, sys
from pathlib import Path
kit = Path(sys.argv[1])
con = sqlite3.connect((kit / 'state_copy.sqlite').resolve().as_uri() + '?mode=ro', uri=True)
row = con.execute('SELECT state, sealed, certificate FROM captures').fetchone()
con.close()
state, sealed_text, stored_cert_text = row
sealed = json.loads(sealed_text)
recomputed = hashlib.sha256(
    json.dumps(sealed, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('ascii')
).hexdigest()
handed_cert = json.loads((kit / 'certificate.json').read_text())
print('capture state            :', state)
print('sealed roots             :', sealed['result']['roots'])
print('sealed fingerprint       :', recomputed)
print('certificate capture_digest:', handed_cert['capture_digest'])
print('fingerprint match        :', recomputed == handed_cert['capture_digest'])
print('stored cert == handed    :', json.loads(stored_cert_text) == handed_cert)
EOF
```

规范化规则必须与工作台一致：`json.dumps(..., ensure_ascii=True, sort_keys=True,
separators=(',', ':'))` 再做 SHA-256。判断点：

- 指纹相等：证书确实认证了当前这条封存记录。
- 指纹不等：证书是别的捕获的，当前封存记录**没有被这份证书认证**，即使文件 SHA 仍然匹配。

## 5. 声明根、封存根、交付物行三方对齐

把三处放在一起：

1. `handoff_manifest.json` 的 `claimed_root`；
2. 第 4 步读出的 `sealed['result']['roots']`；
3. 第 3 步读出的交付物 `project` 行。

三者应指向同一个根。任一处与另外两处不一致就是归属问题，而不是“显示已认证”能掩盖的。

## 6. 两个包并排对比：物理重排还是内容变更

```sh
python3 -I - <<'EOF'
import hashlib, json, sqlite3, sys
from pathlib import Path
left, right = Path(sys.argv[1]), Path(sys.argv[2])
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
cl = json.loads((left / 'certificate.json').read_text())
cr = json.loads((right / 'certificate.json').read_text())
print('bytes identical      :', sha(left / 'deliverable.sqlite') == sha(right / 'deliverable.sqlite'))
print('same capture_digest  :', cl['capture_digest'] == cr['capture_digest'])
print('same schema_digest   :', cl['schema_digest'] == cr['schema_digest'])
print('same counts          :', cl['counts'] == cr['counts'])
for d in (left, right):
    con = sqlite3.connect((d / 'deliverable.sqlite').resolve().as_uri() + '?mode=ro', uri=True)
    print(d.name, 'page_size =', con.execute('PRAGMA page_size').fetchone()[0])
    con.close()
EOF
```

判断点：

- 字节不同、`capture_digest` 相同、`schema_digest` 相同、逐表计数相同、逻辑行相同，
  且 `page_size` 等物理参数不同 —— 这是**合法物理重排**，关系内容未变，不构成暂缓理由。
- 若 `capture_digest` 或逐表逻辑行不同 —— 才是关系内容变更。

可进一步用第 3 步的查询逐表比对两个交付物的全部行，确认逻辑内容确实相等。

## 7. 未完成包（gamma）的状态机核对

```sql
-- 对 state_copy.sqlite 只读执行
SELECT id, name, version, state,
       (artifact IS NOT NULL) AS has_artifact,
       (certificate IS NOT NULL) AS has_certificate
FROM captures;
```

判断点：状态为 `sealed` 且无 artifact、无 certificate，说明流程停在封存之后、构建之前。
这不是数据缺陷，处置应是“继续操作（先构建再认证）”，而不是当作坏包暂缓，也不能在未认证时交接。
工作台状态机本身会拒绝未构建即认证（返回 `fixture_not_built`）和未认证即下载。
