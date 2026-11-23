"""Browse stored primary keys and turn a chosen tuple into a recorded selection.

The page is deliberately explicit about what a listing does and does not prove.
Paging happens against the live source one window at a time; the session marker
that travels with the page carries the witness of the walk so far. A source that
changes mid-walk surfaces as a drift banner and resets the coverage claim, so an
operator never leaves with the impression that a partial browse saw every key.
"""

from ..codec import Fault, dumps, loads
from .directory import source_options, table_links
from .errors import message
from .forms import field, typed_json
from .render import code, location, page, segment, text, typed

STATES = {
    'start': ('info', '从第一个实存键开始', '以下只是当前窗口中实存的主键。继续翻页后，页面会说明这次浏览是否覆盖到表尾。'),
    'jump': ('info', '从指定位置继续', '这是按游标读取的中间窗口，并非从表头开始的完整浏览，不能据此认为看到了全部键。'),
    'same': ('info', '与上一页同一来源状态', '来源内容自上一页以来没有发生变化。'),
    'drift': ('alert', '来源在翻页期间发生了变化',
              '实存主键集在两次翻页之间改变，这次浏览不再代表同一份键集，覆盖判断已重置。请重新从第一页开始。'),
}
COVERAGE = {
    True: ('ready', '已核对到表尾：本次浏览覆盖了该表在这一来源状态下的全部实存主键。'),
    False: ('muted', '尚未覆盖全表：当前只显示已翻到的窗口，不能据此认为列出了所有键。'),
}


def _coverage(result):
    session = result['session']
    if session['complete']:
        return COVERAGE[True]
    if session['reached'] and not session['proved']:
        return ('muted', '已翻到表尾，但该表的键数超过可精确核对的上限，无法排除翻页期间在中间发生的增删，因此不主张覆盖全表。')
    return COVERAGE[False]
SELECTED = {
    'same': ('ready', '选取时与本次浏览处于同一来源状态。'),
    'drift': ('pending', '选取时与本次浏览的来源状态不同；已按当前源复核。'),
    'unverified': ('muted', '这次选取没有绑定浏览会话，无法判断是否与曾显示的窗口一致。'),
}
RECHECK = {
    'present': ('info', '当前来源仍能按这个实存主键找到同一行。'),
    'drift': ('info', '当前来源的主键集已与选取时不同；仍能按这个实存主键找到该行，但当时看到的键集已经变化。'),
    'absent': ('alert', '当前来源已找不到这个实存主键。试算与封存都按当前源判定，因此它已不能作为根。'),
    'schema': ('alert', '当前来源的模式已与选取时不同，需重新核对主键与关系。'),
    'unknown': ('info', '当前无法重读来源：数据源可能已不可用。'),
}


def _decode(name, raw):
    if raw is None or raw == '':
        return None
    try:
        return loads(raw)
    except (ValueError, RecursionError) as exc:
        raise Fault('invalid_cursor', f'{name} 不是有效的 JSON。', 400) from exc


def _raw(values, name):
    entries = values.get(name)
    if not entries:
        return None
    return entries[0]


def _key_cell(values):
    return ' '.join(f'<span class="key-part">{typed(value)}</span>' for value in values)


def _rows(alias, table, result):
    rows = []
    for index, key in enumerate(result['window']['rows'], start=1):
        rows.append(
            f'<tr><th scope="row">{index}</th><td>{_key_cell(key)}</td>'
            f'<td>{_select_form(alias, table, key, result["session"])}</td></tr>')
    return ''.join(rows) or '<tr><td colspan="3" class="empty">此表没有实存主键行。</td></tr>'


def _select_form(alias, table, key, session):
    return (
        '<form class="inline" method="post" action="/workbench/keys/select">'
        f'<input type="hidden" name="source" value="{text(alias)}">'
        f'<input type="hidden" name="table" value="{text(table)}">'
        f'<input type="hidden" name="key" value="{text(dumps(key))}">'
        f'<input type="hidden" name="session" value="{text(dumps(session))}">'
        '<button class="quiet">选取此根</button></form>')


def _navigation(alias, table, result):
    """Prev/next as separate GET forms so typed keys never need to fit a URL."""
    window = result['window']
    session = dumps(result['session'])
    limit = window['limit']

    def step(name, cursor, label, enabled):
        if not enabled or cursor is None:
            return f'<button class="quiet" disabled>{label}</button>'
        return (
            '<form class="inline" method="get" action="/workbench/keys">'
            f'<input type="hidden" name="source" value="{text(alias)}">'
            f'<input type="hidden" name="table" value="{text(table)}">'
            f'<input type="hidden" name="session" value="{text(session)}">'
            f'<input type="hidden" name="limit" value="{limit}">'
            f'<input type="hidden" name="{name}" value="{text(dumps(cursor))}">'
            f'<button class="quiet">{label}</button></form>')

    return (
        '<div class="actions">'
        f'{step("before", window["first"], "上一页", True)}'
        f'{step("after", window["last"], "下一页", window["has_more"])}'
        f'<a class="button quiet" href="{text(location("/workbench/keys", source=alias, table=table, limit=limit))}">从头开始</a>'
        '</div>')


def show(bench, query):
    alias = field(query, 'source', next(iter(sorted(bench.repository.sources)), ''))
    selected = field(query, 'table')
    try:
        catalog = bench.catalog(alias)
    except Fault as exc:
        return exc.status, page('实存主键', 'keys', 'keys_unavailable',
                                options=source_options(bench, alias), error=text(message(exc)))
    tables = catalog['schema']['tables']
    if selected and selected not in tables:
        exc = Fault('unknown_identifier', 'Unknown table', 404)
        return exc.status, page('实存主键', 'keys', 'keys_unavailable',
                                options=source_options(bench, alias), error=text(message(exc)))
    selected = selected or next(iter(tables))
    try:
        raw_limit = _raw(query, 'limit')
        limit = int(raw_limit) if raw_limit is not None and raw_limit.isascii() and raw_limit.isdecimal() else None
        result = bench.scan(alias, selected,
                            after=_decode('after', _raw(query, 'after')),
                            before=_decode('before', _raw(query, 'before')),
                            limit=limit,
                            session=_decode('session', _raw(query, 'session')))
    except Fault as exc:
        return exc.status, page('实存主键', 'keys', 'keys_error', alias=text(alias),
                                selected=text(selected), error=text(message(exc)),
                                browse=text(location('/workbench/keys', source=alias, table=selected)))
    style, headline, explanation = STATES[result['state']]
    covered, coverage_text = _coverage(result)
    cursor_note = ''
    if not result['window']['cursor_present']:
        cursor_note = '<p class="notice alert" role="status">翻页游标已不在当前表中（该行可能刚被删除），本窗口从它原来的位置继续。</p>'
    return 200, page('实存主键', 'keys', 'keys',
                     options=source_options(bench, alias),
                     table_links=table_links(tables, alias, selected, '/workbench/keys'),
                     alias=text(alias), selected=text(selected), layout=text(result['layout_text']),
                     digest=text(result['schema_digest'][:16]), state_style=style,
                     headline=text(headline), explanation=text(explanation),
                     coverage_style=covered, coverage=text(coverage_text), cursor_note=cursor_note,
                     window_count=len(result['window']['rows']), walked=result['session']['keys'],
                     rows=_rows(alias, result['table'], result),
                     navigation=_navigation(alias, result['table'], result),
                     recipe=text(location('/workbench/recipes/new', source=alias)))


def choose(bench, values):
    """Record the chosen page key, then send the operator to its evidence."""
    alias = field(values, 'source')
    table = field(values, 'table')
    note = ', '.join(values.get('note', []))
    record = bench.select(dict(source=alias, table=table, key=typed_json(field(values, 'key')),
                               session=_decode('session', _raw(values, 'session')), note=note))
    return f'/workbench/selections/{segment(record["id"])}'


def _evidence(record):
    session = record['evidence'].get('session')
    if not session:
        return ''
    return (
        f'<tr><th scope="row">选取时模式摘要</th><td>{code(session["schema"])}</td></tr>'
        f'<tr><th scope="row">选取时键序摘要</th><td>{code(session["layout"])}</td></tr>'
        f'<tr><th scope="row">选取时来源见证</th><td>{code(session["witness"])}</td></tr>')


def detail(bench, selection_id):
    record = bench.selection(selection_id)
    evidence = record['evidence']
    spelling = ('与目录中的拼写一致' if evidence['same_spelling']
                else '按 SQLite 比较语义命中了另一拼写，以下是实存写法')
    style, selected_text = SELECTED[evidence['state']]
    recheck_style, recheck_text = RECHECK[record['recheck']]
    current = record['current']
    current_rows = ''
    if current:
        current_rows = (
            f'<tr><th scope="row">当前模式摘要</th><td>{code(current["schema"])}</td></tr>'
            f'<tr><th scope="row">当前键序摘要</th><td>{code(current["layout"])}</td></tr>'
            f'<tr><th scope="row">当前来源见证</th><td>{code(current["witness"])}</td></tr>')
    count = record['witness']['count']
    counted = (f'恰好 {count["exact"]} 行' if 'exact' in count
               else f'至少 {count["at_least"]} 行（已达计数上限）')
    root_json = dumps([{'table': record['table'], 'key': record['key']}])
    return 200, page('选取证据', 'keys', 'selection',
                     source=text(record['source']), table=code(record['table']), key=typed(record['key']),
                     requested=typed(evidence['requested']), spelling=text(spelling),
                     note=text(evidence.get('note') or '无'),
                     selected_style=style, selected=text(selected_text),
                     session_rows=_evidence(record), counted=text(counted),
                     recheck_style=recheck_style, recheck=text(recheck_text), current_rows=current_rows,
                     root_json=text(root_json), root_value=text(root_json),
                     editor=text(location('/workbench/recipes/new', source=record['source'])),
                     browse=text(location('/workbench/keys', source=record['source'], table=record['table'])))


def index(bench, query):
    alias = field(query, 'source') or None
    table = field(query, 'table') or None
    records = bench.repository.recent_selections(alias, table)
    rows = ''.join(
        f'<tr><th scope="row"><a href="/workbench/selections/{segment(record["id"])}">{code(record["id"][:12])}</a></th>'
        f'<td>{code(record["source"])}</td><td>{code(record["table"])}</td>'
        f'<td>{typed(record["key"])}</td><td>{text(SELECTED_LABEL.get(record["evidence"].get("state"), "—"))}</td></tr>'
        for record in records)
    return 200, page('选取记录', 'keys', 'selections',
                     rows=rows or '<tr><td colspan="5" class="empty">尚无选取记录。</td></tr>')


SELECTED_LABEL = {'same': '与浏览一致', 'drift': '浏览后变化', 'unverified': '未绑定浏览'}
