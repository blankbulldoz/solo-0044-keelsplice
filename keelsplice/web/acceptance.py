"""Browser acceptance sessions: freeze clauses, cite certificates, see gaps."""

import json

from .errors import message
from .forms import field, typed_json
from .render import code, options, page, segment, text, typed
from ..acceptance import ACTION_TEXT
from ..codec import Fault

KIND_TEXT = {
    'single': '普通条款：任一认证文件单独举证即可，允许不同快照分别举证',
    'joint': '同一文件条款：两项关联必须在同一实际文件中同时成立',
}


def recipe_choices(bench):
    choices, seen = [], set()
    for row in bench.repository.recent_recipes():
        if (row['name'], row['version']) in seen:
            continue
        seen.add((row['name'], row['version']))
        suffix = '' if row['active'] else '（已停用）'
        choices.append((f'{row["name"]}/{row["version"]}',
                        f'{row["name"]} 版本 {row["version"]}{suffix}'))
    return choices


def badge(clause):
    if clause['status'] == 'satisfied':
        return '<span class="status ready">已满足</span>'
    return '<span class="status pending">存在缺口</span>'


def requirement_rows(clause):
    rows = []
    for index, required in enumerate(clause['requirements']):
        if required['kind'] == 'row':
            detail = f'行 {code(required["table"])} {typed(required["key"])}'
        else:
            origin = f'{code(required["origin"]["table"])} {typed(required["origin"]["key"])}'
            target = f'{code(required["target"]["table"])} {typed(required["target"]["key"])}'
            arrow = '子行 → 父行' if required['direction'] == 'parent' else '父行 → 子行'
            detail = f'外键 {code(required["edge"][0])} #{required["edge"][1]}（{arrow}）{origin} → {target}'
        rows.append(f'<tr><th scope="row">要求 {index + 1}</th><td>{detail}</td></tr>')
    return ''.join(rows)


def certificate_rows(pieces):
    if not pieces:
        return '<tr><td colspan="5" class="empty">没有分项证书。</td></tr>'
    rows = []
    for piece in pieces:
        rows.append(
            f'<tr><th scope="row">{code(piece.get("capture") or "—")}</th>'
            f'<td>{text(piece.get("name") or "—")} · v{piece.get("version") if piece.get("version") else "?"}</td>'
            f'<td>{code(piece.get("sha256") or "—")}</td>'
            f'<td>{text(piece.get("group") or "未引用")}</td>'
            f'<td>{", ".join("要求 " + str(index + 1) for index in piece.get("covers", [])) or "—"}</td></tr>')
    return ''.join(rows)


def combination_block(clause):
    combination = clause['combination']
    if clause['status'] == 'satisfied':
        return (f'<p class="notice info">可以并用。{text(combination["reason_text"])}</p>'
                '<div class="scroll"><table><thead><tr><th scope="col">认证捕获</th>'
                '<th scope="col">配方版本</th><th scope="col">同一文件 SHA-256</th>'
                '<th scope="col">引用组</th><th scope="col">覆盖</th></tr></thead><tbody>'
                f'{certificate_rows(combination["certificates"])}</tbody></table></div>')
    body = f'<p class="notice" role="alert">{text(combination["reason_text"])}</p>'
    if clause['kind'] == 'joint':
        body += ('<div class="scroll"><table><caption>两项的分项证据（分属不同文件，不能合并为同一文件）</caption>'
                 '<thead><tr><th scope="col">认证捕获</th><th scope="col">配方版本</th>'
                 '<th scope="col">文件 SHA-256</th><th scope="col">引用组</th>'
                 '<th scope="col">覆盖</th></tr></thead><tbody>'
                 f'{certificate_rows(combination["pieces"])}</tbody></table></div>')
    return body


def evidence_rows(clause):
    rows = []
    for item in clause['evidence']:
        state = ('<span class="status ready">可用</span>' if item['usable']
                 else f'<span class="status muted">{text(item["reason_text"])}</span>')
        rows.append(
            f'<tr><th scope="row">{code(item["capture"] or "—")}</th>'
            f'<td>{text(item["name"] or "—")} · v{item["version"] or "?"}</td>'
            f'<td>{code(item["sha256"] or "—")}</td><td>{text(item["group"])}</td>'
            f'<td>{", ".join("要求 " + str(index + 1) for index in item.get("covers", [])) or "—"}</td>'
            f'<td>{state}</td></tr>')
    return ''.join(rows) or '<tr><td colspan="6" class="empty">尚未引用任何认证捕获。</td></tr>'


def rejected_rows(clause):
    return ''.join(
        f'<tr><th scope="row">{code(item["capture"] or "—")}</th>'
        f'<td>{code(item["sha256"] or "—")}</td><td>{text(item["group"])}</td>'
        f'<td>{text(item["reason_text"])}</td></tr>'
        for item in clause['rejected'])


def gap_rows(clause):
    if not clause['gaps']:
        return '<tr><td colspan="2" class="empty">每项要求的样本都已具备。</td></tr>'
    rows = []
    empty = '<li class="empty">没有同配方的候选捕获。</li>'
    for gap in clause['gaps']:
        candidates = ''.join(
            f'<li>{code(candidate["capture"])} · {text(candidate["classification_text"])}'
            f'（覆盖 {", ".join(str(index + 1) for index in candidate["covers"])}）</li>'
            for candidate in gap['candidates'])
        rows.append(f'<tr><th scope="row">要求 {gap["requirement"] + 1}<br>'
                    f'<span class="muted">{text(gap["reason_text"])}</span></th>'
                    f'<td>{gap["text"]}<ul class="data-list">{candidates or empty}</ul></td></tr>')
    return ''.join(rows)


def needed_sample(clause, base):
    suggestion = clause['needed_sample']
    if suggestion is None:
        return ''
    requirements = ''.join(f'<li>{text(item)}</li>' for item in suggestion['requirements'])
    if suggestion['action'] == 'cite':
        targets = '、'.join(code(item) for item in suggestion['captures']) or '见上方候选'
        return f'<p class="notice info">补证方式：引用已有的认证捕获 {targets}。</p>'
    if suggestion['action'] == 'certify':
        targets = '、'.join(code(item) for item in suggestion['captures'])
        return (f'<div class="needed"><p>需要补的样本：认证已封存捕获 {targets}</p>'
                f'<ul class="data-list">{requirements}</ul>'
                f'<form method="post" action="{base}/{segment(clause["id"])}/supplement">'
                f'<input type="hidden" name="clause" value="{text(clause["id"])}">'
                f'<button class="primary">{text(suggestion["text"])}</button></form></div>')
    if not suggestion['body']['roots']:
        return '<p class="muted">没有可自动构造的样本；请手动封存能同时包含这些要求的捕获。</p>'
    body = json.dumps(suggestion['body'], ensure_ascii=False, sort_keys=True)
    return (f'<div class="needed"><p>需要补的样本：{text(suggestion["text"])}</p>'
            f'<ul class="data-list">{requirements}</ul><pre>{text(body)}</pre>'
            f'<form method="post" action="{base}/{segment(clause["id"])}/supplement">'
            f'<input type="hidden" name="clause" value="{text(clause["id"])}">'
            f'<button class="primary">封存这个样本</button></form></div>')


def cite_form(session, clause, base):
    choices = []
    for item in clause['combination']['certificates']:
        choices.append((item['capture'], f'{item["name"]} · v{item["version"]} · {(item["sha256"] or "")[:12]}'))
    for gap in clause['gaps']:
        for candidate in gap['candidates']:
            if candidate['classification'] == 'eligible':
                entry = (candidate['capture'],
                         f'{candidate["name"]} · v{candidate["version"]} · {(candidate["sha256"] or "")[:12]}')
                if entry not in choices:
                    choices.append(entry)
    if not choices:
        return '<p class="muted">暂无可用认证捕获可引用；请先按缺口补证。</p>'
    groups = [(name, name) for name in session['groups']]
    return (
        f'<form class="inline" method="post" action="{base}/{segment(clause["id"])}/cite">'
        f'<input type="hidden" name="clause" value="{text(clause["id"])}">'
        f'<label>认证捕获<select name="capture" required>{options(choices, "")}</select></label>'
        f'<label>接收组<select name="group" required>{options(groups, "")}</select></label>'
        f'<button>引用证书</button></form>')


def clause_card(session, clause, base):
    return f'''
<section class="clause">
  <header class="pagehead"><div><h2><a href="{base}/{segment(clause["id"])}">{text(clause["label"])}</a>
    <span class="minor">{text(clause["id"])}</span></h2>
    <p class="muted">{text(KIND_TEXT[clause["kind"]])}</p></div>{badge(clause)}</header>
  <div class="scroll"><table><caption>条款要求（创建会话时冻结）</caption><tbody>{requirement_rows(clause)}</tbody></table></div>
  <h3>并用判定</h3>{combination_block(clause)}
  <h3>已引用证书</h3>
  <div class="scroll"><table><thead><tr><th scope="col">认证捕获</th><th scope="col">配方版本</th>
    <th scope="col">文件 SHA-256</th><th scope="col">引用组</th><th scope="col">覆盖</th>
    <th scope="col">状态</th></tr></thead>
    <tbody>{evidence_rows(clause)}</tbody></table></div>
  <h3>缺口与需要补的样本</h3>
  <div class="scroll"><table><tbody>{gap_rows(clause)}</tbody></table></div>
  {rejected_block(clause)}
  {needed_sample(clause, base)}
  <div class="actions">{cite_form(session, clause, base)}
    <a class="button" href="{base}/{segment(clause["id"])}">查看条款详情</a></div>
</section>'''


def rejected_block(clause):
    if not clause['rejected']:
        return ''
    return ('<details><summary>被拒绝的引用</summary><div class="scroll"><table><thead><tr>'
            '<th scope="col">认证捕获</th><th scope="col">文件 SHA-256</th>'
            '<th scope="col">引用组</th><th scope="col">原因</th></tr></thead>'
            f'<tbody>{rejected_rows(clause)}</tbody></table></div></details>')


def detail(bench, session_id):
    session = bench.acceptance(session_id)
    base = f'/workbench/acceptance/{segment(session_id)}'
    clauses = ''.join(clause_card(session, clause, base) for clause in session['clauses'])
    summary = session['summary']
    return 200, page('验收会话', 'acceptance', 'acceptance',
                     id=text(session_id), recipe=text(session['recipe']), number=session['version'],
                     digest=text(session['schema_digest']), created=text(session['created']),
                     groups=text('、'.join(session['groups'])),
                     satisfied=summary['satisfied'], total=summary['clauses'],
                     gaps=summary['gaps'], certificates=summary['certificates'],
                     clauses=clauses,
                     recipe_link=f'/workbench/recipes/{segment(session["recipe"])}/{session["version"]}')


def clause_page(bench, session_id, clause_id):
    """Zoom on one gap: what the requirement needs and which sample to add."""
    session = bench.acceptance(session_id)
    clause = next((item for item in session['clauses'] if item['id'] == clause_id), None)
    if clause is None:
        raise Fault('unknown_clause', 'This acceptance session has no such clause', 404)
    base = f'/workbench/acceptance/{segment(session_id)}'
    return 200, page('条款缺口', 'acceptance', 'acceptance_clause', base=base,
                     id=text(clause['id']), label=text(clause['label']),
                     kind=text(KIND_TEXT[clause['kind']]), badge=badge(clause),
                     requirements=requirement_rows(clause),
                     combination=combination_block(clause),
                     gaps=gap_rows(clause),
                     rejected=rejected_block(clause),
                     needed=needed_sample(clause, base) or '<p class="muted">此条款已满足。</p>',
                     cite=cite_form(session, clause, base),
                     session=text(session_id))


def index(bench, values=None, error='', status=200):
    values = values or {}
    records = bench.recent_acceptances()
    rows = ''.join(
        f'<tr><th scope="row"><a href="/workbench/acceptance/{segment(record["id"])}">'
        f'{code(record["id"])}</a></th><td>{text(record["recipe_name"])} · v{record["recipe_version"]}</td>'
        f'<td>{text("、".join(record["groups"]))}</td><td>{text(record["created"])}</td></tr>'
        for record in records)
    choices = recipe_choices(bench)
    default = field(values, 'recipe_version', choices[0][0] if choices else '')
    return status, page('验收会话', 'acceptance', 'acceptances',
                        rows=rows or '<tr><td colspan="4" class="empty">尚无验收会话。</td></tr>',
                        recipe_options=options(choices, default),
                        groups=text(field(values, 'groups')),
                        clauses=text(field(values, 'clauses')),
                        error=f'<p role="alert" class="notice">{text(error)}</p>' if error else '',
                        disabled='' if choices else ' disabled',
                        hint='' if choices else '<p class="muted">先登记一个配方版本，再创建验收会话。</p>')


def create(bench, values):
    try:
        picked = field(values, 'recipe_version')
        name, _, number = picked.partition('/')
        if not name or not number.isascii() or not number.isdecimal() or len(number) > 18:
            raise Fault('invalid_version', '请选择一个配方版本。', 400)
        groups = [item.strip() for item in field(values, 'groups').split(',') if item.strip()]
        body = dict(name=name, version=int(number), groups=groups,
                    clauses=typed_json(field(values, 'clauses')))
        session = bench.accept(body)
    except Fault as exc:
        if exc.code in {'unknown_source', 'unsupported_schema', 'unknown_recipe'}:
            raise
        return index(bench, values, message(exc), exc.status)
    return f'/workbench/acceptance/{segment(session["id"])}'
