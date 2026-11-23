"""Command-line tool: build no-row-text revisit materials at the receiver site.

The delivered database is opened read-only.  The output directory receives four
JSON files (manifest, file, schema, tables) containing DDL text, counts and
digests only -- never row originals.  The manifest anchors every file to the
delivery certificate so the origin workbench can reject foreign material.
"""

import argparse
import json
import re
from pathlib import Path

from .codec import Fault, dumps
from .revisit import MATERIAL_FILES, materialize

HEX64 = re.compile(r'[0-9a-f]{64}\Z')


def write_package(directory, package, *, exist_ok=False):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=exist_ok)
    for name in MATERIAL_FILES:
        target = directory / name
        if target.exists() and not exist_ok:
            raise Fault('invalid_revisit', f'输出目录已存在 {name}，拒绝覆盖。', 400)
        target.write_text(json.dumps(package[name], ensure_ascii=False, indent=2, sort_keys=True),
                          encoding='utf-8')
    return directory


def bundle_bytes(package):
    return dumps({'files': {name: package[name] for name in MATERIAL_FILES}}).encode('ascii')


def main(argv=None):
    parser = argparse.ArgumentParser(description='在接收方现场生成不含行原文的交接回访材料。')
    parser.add_argument('--database', required=True, help='现场保留的已交付 SQLite 文件')
    parser.add_argument('--anchor-sha256', required=True, help='交付证书中的文件 SHA-256')
    parser.add_argument('--anchor-capture', required=True, help='交付证书中的 capture_digest')
    parser.add_argument('--out', required=True, help='材料输出目录（新建空目录）')
    parser.add_argument('--note', default='', help='可选备注，最多 1024 字符')
    parser.add_argument('--exist-ok', action='store_true', help='允许写入并覆盖已存在的输出目录')
    args = parser.parse_args(argv)
    if not HEX64.fullmatch(args.anchor_sha256) or not HEX64.fullmatch(args.anchor_capture):
        parser.error('锚点摘要必须是 64 位小写十六进制字符串')
    if len(args.note) > 1024:
        parser.error('备注不能超过 1024 字符')
    database = Path(args.database).resolve(strict=True)
    package = materialize(database, {'sha256': args.anchor_sha256,
                                     'capture_digest': args.anchor_capture}, note=args.note)
    directory = write_package(args.out, package, exist_ok=args.exist_ok)
    print(f'回访材料已写入 {directory}：' + '、'.join(MATERIAL_FILES))
    print('材料仅含模式文本、计数与摘要，不含任何行原文。')


if __name__ == '__main__':
    main()
