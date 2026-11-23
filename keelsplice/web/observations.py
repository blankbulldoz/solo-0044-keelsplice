"""Browser views for retained preview observations and their recheck."""

from .errors import message
from .forms import field
from .render import code, options, page, segment, text, template, typed
from ..codec import Fault

STATUS = {'consistent': ('一致', 'ready'), 'different': ('不同', 'pending'),
          'unknown': ('未知', 'muted')}
VERDICT = {'consistent': '所选检查项一致。', 'different': '所选检查项存在具体不同。',
           'unknown': '所选检查项不足以判定，未保留证据的部分标为未知。'}


def label(check):
    if check['kind'] == 'counts':
        return '逐表计数'
    if check['kind'] == 'roots':
        return '实存根'
    if check['kind'] == 'generated':
        return f'生成值 · {check["table"]}.{check["column"]}'
    return f'关系 · {check["child"]} 外键 {check["id"]}'


def badge(status):
    caption, style = STATUS[status]
    return f'<span class="status {style}">{caption}</span>'


def check_rows(result):
    rows = []
    for item in result['checks']:
        rows.append(f'<tr><th scope="row">{code(label(item))}</th><td>{badge(item["status"])}</td>'
                    f'<td>{detail(item)}</td></tr>')
    return ''.join(rows) or '<tr><td colspan="3" class="empty">没有共同保留的检查项。</td></tr>'


def detail(item):
    parts = []
    if item.get('reason') == 'unretained':
        parts.append('两次观察未同时保留此项证据。')
    for change in item.get('differences', []):
        if 'table' in change:
            parts.append(f'{code(change["table"])}：左 {value_text(change["left"])}，'
                         f'右 {value_text(change["right"])}')
        elif 'index' in change:
            parts.append(f'根序号 {change["index"] + 1}：左 {value_text(change["left"])}，'
                         f'右 {value_text(change["right"])}')
        elif item['kind'] == 'generated':
            parts.append(f'{typed(change["row"])} 值改变（左摘要 {code(change["left"][:12])}，'
                         f'右摘要 {code(change["right"][:12])}）')
        else:
            parts.append(f'{typed(change["row"])} 由 {value_text(change["left"])} '
                         f'指向 {value_text(change["right"])}')
    for gap in item.get('unknown', []):
        parts.append(f'{typed(gap["row"])} 只在一侧保留。')
    return '<br>'.join(parts) or '—'


def value_text(value):
    return '（无）' if value is None else typed(value)


def index(bench):
    records = bench.repository.recent_observations()
    rows = ''.join(
        f'<tr><th scope="row"><a href="/workbench/observations/{segment(record["id"])}">'
        f'{code(record["id"])}</a></th><td>{text(record["name"])}</td><td>{record["version"]}</td>'
        f'<td>{record["summary"]["count"]}</td><td>{text(record["created"])}</td></tr>'
        for record in records
    )
    return 200, page('试算观察', 'observe', 'observations',
                     rows=rows or '<tr><td colspan="5" class="empty">尚未保留观察记录。</td></tr>')


def show(bench, observation_id):
    record = bench.observation(observation_id)
    recipe = f'/workbench/recipes/{segment(record["name"])}/{record["version"]}'
    items = ''.join(f'<li>{code(label(check))}</li>' for check in record['checks'])
    return 200, page('试算观察', 'observe', 'observation', id=text(observation_id),
                     name=text(record['name']), number=record['version'], recipe=recipe,
                     created=text(record['created']), digest=text(record['schema_digest']),
                     checks=items, count=record['summary']['count'],
                     rows=record['summary']['rows'])


def choices(records, active):
    known = {record['id'] for record in records}
    available = [('', '请选择观察记录'), *(
        (record['id'], f'{record["name"]} 版本 {record["version"]} · {record["id"][:12]}')
        for record in records)]
    if active and active not in known:
        available.append((active, active))
    return options(available, active)


def recheck(bench, values=None):
    values = values or {}
    left, right = field(values, 'left'), field(values, 'right')
    records = bench.repository.recent_observations()
    status, error, result = 200, '', None
    if left and right:
        try:
            result = bench.recheck(dict(left=left, right=right))
        except Fault as exc:
            status, error = exc.status, message(exc)
    body = ''
    if result is not None:
        outcome = result['result']
        body = template('recheck_result', verdict=text(VERDICT[outcome['verdict']]),
                        summary=summary(outcome['summary']), rows=check_rows(outcome))
    return status, page('复查观察', 'observe', 'observe_compare',
                        left=choices(records, left), right=choices(records, right), result=body,
                        error=f'<p class="notice" role="alert">{text(error)}</p>' if error else '')


def summary(counts):
    return (f'一致 {counts["consistent"]} · 不同 {counts["different"]} · 未知 {counts["unknown"]}')
