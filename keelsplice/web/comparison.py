"""Present sealed key differences without converting their SQLite types."""

from . import identity
from .errors import message
from .forms import field
from .render import options, page, template, text
from ..codec import Fault

LABELS = {'added': '新增', 'removed': '移除', 'modified': '修改'}


def changes(result):
    if result is None:
        return ''
    rows = []
    for table, differences in result['changes'].items():
        for kind, keys in differences.items():
            rows.extend(f'<tr><th scope="row">{identity.identifier(table)}</th>'
                        f'<td>{LABELS[kind]}</td><td>{identity.pair(key)}</td></tr>'
                        for key in keys)
    if not rows:
        rows.append('<tr><td colspan="3" class="empty">这两个捕获的行内容一致。</td></tr>')
    return template('comparison_result', rows=''.join(rows), legend=identity.legend())


def show(bench, values=None):
    values = values or {}
    left, right = field(values, 'left'), field(values, 'right')
    recent = bench.repository.recent_captures()
    ids = [(row['id'], f'{row["name"]} 版本 {row["version"]} · {row["id"][:12]}') for row in recent]
    status, error, result = 200, '', None
    if values:
        try:
            result = bench.compare(dict(left=left, right=right))
        except Fault as exc:
            status, error = exc.status, message(exc)
    known = {key for key, _ in ids}

    def choices(selected):
        available = [('', '请选择捕获'), *ids]
        if selected and selected not in known:
            available.append((selected, selected))
        return options(available, selected)

    return status, page('捕获对比', 'compare', 'compare',
                        left=choices(left), right=choices(right), result=changes(result),
                        error=f'<p class="notice" role="alert">{text(error)}</p>' if error else '')
