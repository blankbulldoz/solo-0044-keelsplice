"""Browser forms and reports for the two-group alternative closure trial."""

from .forms import field, typed_json
from .render import code, page, segment, text, typed
from ..codec import Fault, dumps

GROUP_FIELDS = ('group_a', 'group_b')
GROUP_TITLES = {'group_a': '候选关系组 A', 'group_b': '候选关系组 B'}
SCENARIO_TITLES = {
    'baseline': '原配方已选关系',
    'group_a': '单独开启 A',
    'group_b': '单独开启 B',
    'union': '联合开启 A + B',
}
FINDING_TITLES = {
    'solo_a_vs_solo_b': '单独开启对比',
    'union_vs_solos': '联合开启相对两组单独结果',
    'group_a_vs_baseline': '单独 A 相对原配方',
    'group_b_vs_baseline': '单独 B 相对原配方',
    'union_vs_baseline': '联合开启相对原配方',
}
FINDING_COLUMNS = {
    'only_group_a': '仅 A 单独开启时入选',
    'only_group_b': '仅 B 单独开启时入选',
    'shared_identity': '两组都入选（共享父行只计一次）',
    'reached_only_together': '只有联合开启才沿另一组边纳入的行',
    'covered_by_a_solo': '至少一组单独开启已覆盖',
    'added_vs_baseline': '相对原配方新增',
    'removed_vs_baseline': '相对原配方不再入选',
}


def parse_tokens(values, name):
    chosen = []
    for item in values.get(name, []):
        number, _, child = item.partition(':')
        if not number.isascii() or not number.isdecimal() or len(number) > 10 or not child:
            raise Fault('invalid_edge', 'Invalid edge')
        chosen.append(dict(child=child, id=int(number)))
    return chosen


def edge_choices(schema, values, name):
    picked = set(values.get(name, []))
    items = []
    for edge in schema['edges']:
        token = f'{edge["id"]}:{edge["child"]}'
        checked = ' checked' if token in picked else ''
        content = f'{edge["parent"]} → {edge["child"]} ({", ".join(edge["child_cols"])})'
        items.append(f'<label class="edge-choice"><input type="checkbox" name="{name}" value="{text(token)}"'
                     f'{checked}> {code(content)}</label>')
    return ''.join(items) or '<p class="empty">此源库没有可选关系。</p>'


def editor(bench, name, number, values=None, error='', status=200):
    values = values or {}
    saved = bench.version(name, number)
    catalog = bench.catalog(saved['recipe']['source'])
    href = f'/workbench/recipes/{segment(name)}/{number}/alternatives'
    groups = ''.join(
        f'<fieldset><legend>{text(GROUP_TITLES[field_name])}</legend>'
        f'<div class="edge-grid">{edge_choices(catalog["schema"], values, field_name)}</div></fieldset>'
        for field_name in GROUP_FIELDS)
    previous = field(values, 'basis')
    return status, page('关系分组试算', 'recipe', 'alternatives',
                        action=text(href), digest=text(saved['schema_digest']),
                        limit=saved['recipe']['row_limit'], groups=groups,
                        basis_hidden=f'<input type="hidden" name="basis" value="{text(previous)}">' if previous else '',
                        error=f'<p role="alert" class="notice">{text(error)}</p>' if error else '')


def identity_list(identities):
    if not identities:
        return '<p class="empty">无行身份。</p>'
    items = ''.join(f'<li>{code(table)} {typed(key)}</li>' for table, key in identities)
    return f'<ol class="data-list">{items}</ol>'


def attribution_rows(attribution):
    rows = []
    for entry in attribution:
        table, key = entry['row']
        if entry['root_occurrences']:
            roots = '、'.join(str(index + 1) for index in entry['root_occurrences'])
            rows.append(f'<tr><th scope="row">{code(table)} {typed(key)}</th><td>根记录（第 {roots} 项）</td></tr>')
        for reason in entry['parent_reasons']:
            rows.append(f'<tr><th scope="row">{code(table)} {typed(key)}</th>'
                        f'<td>必需父行补齐（闭包自动）· {code(reason["edge"])} · '
                        f'来源 {code(reason["from"][0])} {typed(reason["from"][1])}</td></tr>')
        for reason in entry['child_reasons']:
            rows.append(f'<tr><th scope="row">{code(table)} {typed(key)}</th>'
                        f'<td>反向关系纳入 · {code(reason["edge"])} · 分组 {code(reason["group"])} · '
                        f'来源 {code(reason["from"][0])} {typed(reason["from"][1])}</td></tr>')
    return ''.join(rows) or '<tr><td class="empty">无入选行。</td></tr>'


def count_rows(summary):
    return ''.join(f'<tr><th scope="row">{code(table)}</th><td>{count}</td></tr>'
                   for table, count in summary['counts'].items())


def scenario_card(name, scenario):
    title = SCENARIO_TITLES[name]
    edges = '、'.join(f'{edge["child"]} #{edge["id"]}' for edge in scenario['edges']) or '无反向关系（仅父行闭包）'
    if scenario['status'] != 'ok':
        body = (f'<p class="notice" role="alert">试算未完成：{text(scenario["error"]["code"])} — '
                f'{text(scenario["error"]["message"])}</p>'
                f'<p class="muted">该方案不参与行身份集合比较，不能据此作决定。</p>')
    else:
        summary = scenario['summary']
        identities = [[table, key] for table, keys in scenario['row_identities'].items() for key in keys]
        body = (
            f'<dl class="facts"><div><dt>入选行</dt><dd>{summary["rows"]}</dd></div>'
            f'<div><dt>关系见证</dt><dd>{summary["witnesses"]}</dd></div>'
            f'<div><dt>根出现项</dt><dd>{summary["root_occurrences"]}</dd></div></dl>'
            f'<details open><summary>逐表计数</summary><div class="scroll"><table><thead><tr>'
            f'<th scope="col">表</th><th scope="col">入选行</th></tr></thead><tbody>{count_rows(summary)}'
            f'</tbody></table></div></details>'
            f'<details><summary>行身份（实存带类型主键）</summary>{identity_list(identities)}</details>'
            f'<details><summary>边界归因（直接入选原因）</summary><div class="scroll"><table><thead><tr>'
            f'<th scope="col">行身份</th><th scope="col">直接原因</th></tr></thead><tbody>'
            f'{attribution_rows(scenario["attribution"])}</tbody></table></div></details>'
        )
    return (f'<section class="scenario"><h3>{text(title)} <span class="status '
            f'{"ready" if scenario["status"] == "ok" else "muted"}">'
            f'{"可用于决定" if scenario["status"] == "ok" else "不可用于决定"}</span></h3>'
            f'<p class="muted">启用边：{text(edges)}</p>{body}</section>')


def findings_block(findings):
    if not findings:
        return '<p class="empty">没有可比较的成功试算（至少需要两个方案成功）。</p>'
    blocks = []
    for name, sets in findings.items():
        rows = ''.join(
            f'<tr><th scope="row">{text(FINDING_COLUMNS[column])}</th>'
            f'<td>{identity_list([tuple(identity) for identity in identities])}</td></tr>'
            for column, identities in sets.items()
        )
        blocks.append(f'<details><summary>{text(FINDING_TITLES[name])}</summary>'
                      f'<div class="scroll"><table><tbody>{rows}</tbody></table></div></details>')
    return ''.join(blocks)


def basis_block(basis_status, basis):
    files = ''.join(
        f'<li>{code(item["file"])} · {item["bytes"]} 字节 · mtime {item["mtime_ns"]}'
        f'{(" · sha256 " + item["sha256"]) if "sha256" in item else "（瞬时文件）"}</li>'
        for item in basis['files']
    )
    if basis_status['stale']:
        notice = (f'<p class="notice" role="alert">{text(basis_status["reason"])}</p>'
                  '<p class="muted">本页四个方案仍在同一个新读事务内计算、彼此可比较，'
                  '但不能与旧试算的结果共同用于决定。</p>')
    else:
        notice = ('<p class="notice info">四个方案在同一个源读事务内计算，'
                  '共享同一读取依据，可直接比较。</p>')
    return (f'{notice}<details><summary>本次读取依据（可回传复核源是否被写入）</summary>'
            f'<ul class="data-list">{files}</ul>'
            f'<p class="muted">模式摘要 <code>{text(basis["schema_digest"])}</code></p></details>')


def show(bench, name, number, values):
    try:
        body = {'name': name, 'version': number,
                'group_a': parse_tokens(values, 'group_a'),
                'group_b': parse_tokens(values, 'group_b')}
        raw_basis = field(values, 'basis')
        if raw_basis:
            body['basis'] = typed_json(raw_basis)
        result = bench.preview_alternatives(body)
    except Fault as exc:
        if exc.code in {'unknown_recipe', 'retired_recipe', 'schema_conflict', 'unknown_source',
                        'unsupported_schema', 'overlapping_groups'}:
            raise
        return editor(bench, name, number, values, f'{exc.code} — {exc}', exc.status)
    href = f'/workbench/recipes/{segment(name)}/{number}/alternatives'
    cards = ''.join(scenario_card(label, result['scenarios'][label])
                    for label in ('baseline', 'group_a', 'group_b', 'union'))
    decidable = (
        '<p class="notice info">本次分组试算可用于决定。</p>' if result['decidable']
        else '<p class="notice" role="alert">本次分组试算不完整或读取依据已变化，不能用于决定。</p>'
    )
    hidden = ''.join(
        f'<input type="hidden" name="{group}" value="{text(token)}">'
        for group in GROUP_FIELDS for token in values.get(group, [])
    )
    report = (
        f'{decidable}{basis_block(result["basis_status"], result["basis"])}'
        f'<form class="actions" method="post" action="{href}">{hidden}'
        f'<input type="hidden" name="basis" value=\'{text(dumps(result["basis"]))}\'>'
        '<button class="quiet">携带本次读取依据重新复核</button></form>'
        f'<div class="scenario-grid">{cards}</div>'
        f'<section><h2>行身份集合比较</h2>{findings_block(result["findings"])}</section>'
    )
    return 200, page('关系分组试算', 'recipe', 'alternatives_report',
                     back=f'/workbench/recipes/{segment(name)}/{number}',
                     name=text(name), number=number, report=report)
