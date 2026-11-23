"""Render parallel-draft provenance, copy reports, and conflict comparisons."""

from .. import draft
from ..codec import Fault, dumps
from .forms import field
from .render import code, location, page, text, typed

STATE_LABELS = {'pending': ('需确认', 'pending'), 'copied': ('已复制', 'ready')}


def entry_html(entry):
    if draft.well_formed(entry):
        return f'{code(entry["table"])} {typed(entry["key"])}'
    return code(dumps(entry)[:160])


def mark_badge(mark):
    label, style = STATE_LABELS[mark['state']]
    return f'<span class="status {style}">{label}</span> <span class="muted">来自 {text(mark["from"])} 的草稿</span>'


def badges(payload, roots_text):
    if not roots_text.strip():
        return ''
    try:
        entries = draft.parse_roots(roots_text)
    except Fault:
        return '<p class="muted">根记录暂无法解析，复制状态将在修正后显示。</p>'
    marks = {mark['entry']: mark for mark in (payload['marks'] if payload else [])
             if draft.well_formed_mark(mark)}
    items = []
    for entry in entries:
        mark = marks.get(draft.identity(entry)) if draft.well_formed(entry) else None
        badge = mark_badge(mark) if mark else '<span class="status muted">本侧</span>'
        items.append(f'<li>{badge} {entry_html(entry)}</li>')
    return '<h2>根记录状态</h2><ul class="data-list">' + ''.join(items) + '</ul>'


def panel(records, active):
    rows = []
    for record in records:
        payload = record['payload']
        try:
            count = str(len(draft.parse_roots(payload['roots']))) if payload['roots'].strip() else '0'
        except Fault:
            count = '—'
        pending = sum(1 for mark in payload['marks']
                      if draft.well_formed_mark(mark) and mark['state'] == 'pending')
        action = ('当前页' if record['source'] == active else
                  f'<a href="{text(location("/workbench/recipes/new", source=record["source"]))}">打开</a>')
        rows.append(f'<tr><th scope="row">{code(record["source"])}</th><td>{record["revision"]}</td>'
                    f'<td>{text(payload["name"]) or "—"}</td><td>{count}</td><td>{pending}</td>'
                    f'<td>{action}</td></tr>')
    body = ''.join(rows) or '<tr><td colspan="6" class="empty">尚无保存的草稿。</td></tr>'
    return ('<section><h2>并行草稿</h2><div class="scroll"><table><thead><tr>'
            '<th scope="col">源</th><th scope="col">修订</th><th scope="col">名称</th>'
            '<th scope="col">根数</th><th scope="col">需确认</th><th scope="col">操作</th>'
            f'</tr></thead><tbody>{body}</tbody></table></div></section>')


def report(result):
    parts = []
    if result['copied']:
        verb = '已创建' if result['created'] else '已并入'
        items = ''.join(f'<li>{entry_html(entry)}</li>' for entry in result['copied'])
        parts.append(f'<p>{verb} {text(result["target"])} 的草稿（修订 {result["revision"]}），'
                     f'按原输入次序复制 {len(result["copied"])} 项，需在该源编辑页确认：</p>'
                     f'<ul class="data-list">{items}</ul>')
    if result['duplicates']:
        parts.append(f'<p>{result["duplicates"]} 项已存在于目标草稿，未重复复制。</p>')
    if result['skipped']:
        items = ''.join(f'<li>{entry_html(entry)} — {text(reason)}</li>' for entry, reason in result['skipped'])
        parts.append(f'<p>跳过 {len(result["skipped"])} 项不兼容的根：</p><ul class="data-list">{items}</ul>')
    if not result['copied'] and not result['skipped'] and not result['duplicates']:
        parts.append('<p>没有需要复制的根。</p>')
    return '<div class="notice info" role="status">' + ''.join(parts) + '</div>'


def safe_entries(roots_text):
    try:
        return draft.parse_roots(roots_text), True
    except Fault:
        return [], False


def conflict(bench, alias, stored, values, intent):
    number = stored['revision'] if stored else 0
    payload = stored['payload'] if stored else draft.blank_payload()
    stored_entries, stored_ok = safe_entries(payload['roots']) if payload['roots'].strip() else ([], True)
    if values is None:
        submitted, submitted_entries, submitted_ok = None, stored_entries, True
    else:
        submitted = dict(name=field(values, 'name'), row_limit=field(values, 'row_limit'),
                         roots=field(values, 'roots'), edges=values.get('edge', []))
        submitted_entries, submitted_ok = safe_entries(submitted['roots']) if submitted['roots'].strip() else ([], True)
    stored_ids = {draft.identity(entry) for entry in stored_entries if draft.well_formed(entry)}
    submitted_ids = {draft.identity(entry) for entry in submitted_entries if draft.well_formed(entry)}
    marks = {mark['entry']: mark for mark in payload['marks'] if draft.well_formed_mark(mark)}
    copied = [f'<li>{mark_badge(marks[draft.identity(entry)])} {entry_html(entry)}</li>'
              for entry in stored_entries
              if draft.well_formed(entry) and draft.identity(entry) in marks]
    theirs = [f'<li>{entry_html(entry)}</li>' for entry in stored_entries
              if not draft.well_formed(entry)
              or (draft.identity(entry) not in submitted_ids and draft.identity(entry) not in marks)]
    mine = [f'<li>{entry_html(entry)}</li>' for entry in submitted_entries
            if not draft.well_formed(entry) or draft.identity(entry) not in stored_ids]
    if not stored_ok:
        theirs.append('<li class="empty">（已保存的根记录无法解析，未逐项对比）</li>')
    if not submitted_ok:
        mine.append('<li class="empty">（本页提交的根记录无法解析，未逐项对比）</li>')
    if submitted is not None:
        for label, key in (('配方名称', 'name'), ('封存行上限', 'row_limit')):
            if payload[key] != submitted[key]:
                theirs.append(f'<li>{label}：{code(payload[key])}</li>')
                mine.append(f'<li>{label}：{code(submitted[key])}</li>')
        if sorted(payload['edges']) != sorted(submitted['edges']):
            theirs.append(f'<li>反向关系：{code(", ".join(sorted(payload["edges"])) or "（无）")}</li>')
            mine.append(f'<li>反向关系：{code(", ".join(sorted(submitted["edges"])) or "（无）")}</li>')
    actions = {
        'save': ('/workbench/drafts', '以本页提交覆盖'),
        'switch': ('/workbench/drafts', '以本页提交覆盖并切换'),
        'register': ('/workbench/recipes/new', f'仍然登记到 {alias}'),
        'confirm': ('/workbench/drafts/confirm', '仍然确认复制项'),
    }
    action, label = actions[intent]
    hidden = [] if values is None else [(key, value) for key, items in values.items()
                                        if key != 'revision' for value in items]
    if values is None:
        hidden.append(('source', alias))
    hidden.append(('revision', str(number)))
    hidden_html = ''.join(f'<input type="hidden" name="{text(key)}" value="{text(value)}">'
                          for key, value in hidden)
    return 409, page('草稿冲突', 'recipe', 'draft_conflict', source=code(alias), revision=number,
                     copied=''.join(copied) or '<li class="empty">已保存版本中没有从并行草稿复制的根。</li>',
                     theirs=''.join(theirs) or '<li class="empty">已保存版本没有本页未包含的内容。</li>',
                     mine=''.join(mine) or '<li class="empty">本页没有提交额外改动。</li>',
                     editor=text(location('/workbench/recipes/new', source=alias)),
                     action=action, hidden=hidden_html, label=text(label))
