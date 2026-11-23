"""Present per-check coverage: independent entities, basis, and representatives."""

from .render import code, page, segment, text, typed
from ..codec import dumps

KINDS = {'root': '根解析', 'relation': '关系目标', 'generated': '生成值'}


def capture_link(result, index):
    record = result['captures'][index]
    return (f'<a href="/workbench/captures/{segment(record["id"])}">{text(record["name"])} '
            f'版本 {record["version"]} · {code(record["id"][:12])}</a>')


def subject(check):
    found = check['subject']
    if check['kind'] == 'root':
        return '实存根解析'
    if check['kind'] == 'relation':
        child_cols = ', '.join(found['child_cols'])
        parent_cols = ', '.join(found['parent_cols'])
        return (f'{code(found["child"])} 外键 #{found["id"]} → {code(found["parent"])}'
                f'<span class="muted">（{text(child_cols)} → {text(parent_cols)}）</span>')
    return f'{code(found["table"])}.{code(found["column"])} 的生成值'


def entity_text(check, entity):
    if check['kind'] == 'relation':
        origin, target = entity['origin'], entity['target']
        return f'{code(origin[0])} {typed(origin[1])} → {code(target[0])} {typed(target[1])}'
    if check['kind'] == 'generated':
        return (f'{code(entity["table"])} {typed(entity["key"])} · '
                f'{code(entity["column"])} = {typed(entity["value"])}')
    return f'{code(entity["table"])} {typed(entity["key"])}'


def basis(result, entity):
    notes = {}
    for index, note in entity['sightings']:
        notes.setdefault(index, []).append(note)
    items = []
    for index in entity['captures']:
        marks = [str(note + 1) for note in notes[index] if note is not None]
        suffix = f'（根序号 {"、".join(marks)}）' if marks else ''
        items.append(f'<li>{capture_link(result, index)}{suffix}</li>')
    return '<ul class="data-list">' + ''.join(items) + '</ul>'


def coverage(result):
    rows = ''.join(
        f'<tr><td>{KINDS[check["kind"]]}</td><th scope="row">{subject(check)}</th>'
        f'<td>{len(check["entities"])}</td><td>{check["sightings"]}</td><td>{check["captures"]}</td></tr>'
        for check in result['checks']
    )
    return rows or '<tr><td colspan="5" class="empty">尚无封存捕获，审计暂无可统计的观察。</td></tr>'


def representatives(result):
    if not result['checks']:
        return '<p class="empty">尚无检查项。</p>'
    blocks = []
    for check in result['checks']:
        rows = ''.join(
            f'<tr><td>{entity_text(check, entity["entity"])}</td><td>{len(entity["sightings"])}</td>'
            f'<td>{len(entity["captures"])}</td><td>{capture_link(result, entity["representative"])}</td>'
            f'<td>{basis(result, entity)}</td></tr>'
            for entity in check['entities']
        )
        blocks.append(
            f'<details><summary>{KINDS[check["kind"]]} · {subject(check)} — '
            f'独立实体 {len(check["entities"])} · 观察 {check["sightings"]}</summary>'
            '<div class="scroll"><table><thead><tr><th scope="col">实体</th><th scope="col">观察次数</th>'
            '<th scope="col">涉及捕获</th><th scope="col">代表捕获</th><th scope="col">依据</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div></details>'
        )
    return ''.join(blocks)


def duplicates(result):
    found = result['duplicates']
    if not found:
        return '<p class="empty">每个实存根至多被观察一次，没有重复根。</p>'
    sightings = sum(len(entity['sightings']) for entity in found)
    rows = ''.join(
        f'<tr><th scope="row">{code(entity["entity"]["table"])} {typed(entity["entity"]["key"])}</th>'
        f'<td>{len(entity["sightings"])}</td><td>{len(entity["captures"])}</td>'
        f'<td>{capture_link(result, entity["representative"])}</td><td>{basis(result, entity)}</td></tr>'
        for entity in found
    )
    return (f'<p>共 {len(found)} 个实存根存在重复出现项：合计 {sightings} 次出现，'
            f'按实存键合并为 {len(found)} 个独立实体。</p>'
            '<div class="scroll"><table><thead><tr><th scope="col">实存根</th><th scope="col">出现次数</th>'
            '<th scope="col">涉及捕获</th><th scope="col">代表捕获</th><th scope="col">依据</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div>')


def shared(result):
    found = result['shared']
    if not found:
        return '<p class="empty">没有一次实际观察同时支撑多个检查项。</p>'
    distinct = len({dumps(check) for row in found for check in row['checks']})
    rows = ''.join(
        f'<tr><td>{capture_link(result, row["capture"])}</td>'
        f'<td>{code(row["table"])} {typed(row["key"])}</td><td>{len(row["checks"])}</td>'
        f'<td>{"、".join(subject(check) for check in row["checks"])}</td></tr>'
        for row in found
    )
    return (f'<p>共 {len(found)} 次实际观察各自支撑多个检查项，涉及 {distinct} 个检查项；'
            '同一观察不为这些检查项增加独立实体数。</p>'
            '<div class="scroll"><table><thead><tr><th scope="col">捕获</th><th scope="col">实存键</th>'
            '<th scope="col">支撑项数</th><th scope="col">检查项</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div>')


def retargeted(result):
    found = result['retargeted']
    if not found:
        return '<p class="empty">没有关系目标在不同捕获间发生变化。</p>'
    entities = sum(len(group['targets']) for group in found)
    blocks = []
    for group in found:
        origin = group['origin']
        rows = ''.join(
            f'<tr><td>{code(target["entity"]["target"][0])} {typed(target["entity"]["target"][1])}</td>'
            f'<td>{len(target["sightings"])}</td><td>{len(target["captures"])}</td>'
            f'<td>{capture_link(result, target["representative"])}</td><td>{basis(result, target)}</td></tr>'
            for target in group['targets']
        )
        blocks.append(
            f'<details open><summary>{subject(group["check"])} · 来源 {code(origin[0])} {typed(origin[1])}'
            f' — {len(group["targets"])} 个不同目标</summary>'
            '<div class="scroll"><table><thead><tr><th scope="col">关系目标</th><th scope="col">观察次数</th>'
            '<th scope="col">涉及捕获</th><th scope="col">代表捕获</th><th scope="col">依据</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div></details>'
        )
    return (f'<p>共 {len(found)} 个来源行的关系目标在捕获间发生变更，形成 {entities} 个独立实体。</p>'
            + ''.join(blocks))


def show(bench):
    result = bench.audit()
    summary = result['summary']
    return 200, page('覆盖审计', 'audit', 'audit',
                     captures=summary['captures'], checks=summary['checks'],
                     entities=summary['entities'], sightings=summary['sightings'],
                     merged=summary['merged'], coverage=coverage(result),
                     representatives=representatives(result), duplicates=duplicates(result),
                     shared=shared(result), retargeted=retargeted(result))
