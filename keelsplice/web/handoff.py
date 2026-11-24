"""Describe each delivery layout, its certificate, and the allowed next action."""

from .render import code, text

STAGES = {'pending': '未构建', 'built': '待认证', 'certified': '已认证'}
NEXT = {'pending': '构建', 'built': '认证'}


def profile(row):
    page = row.get('page_size')
    free = row.get('free_pages', 0)
    return f'页大小 {page}，空闲页 {free}' if page is not None else f'默认页大小，空闲页 {free}'


def action(base, name, state):
    if name == 'default':
        if state == 'certified':
            return f'<a class="button primary" href="{base}/database">下载认证子库</a>'
        path = {'pending': 'build', 'built': 'certify'}[state]
        label = '构建子库' if state == 'pending' else '认证子库'
        return f'<form method="post" action="{base}/{path}"><button class="primary">{label}</button></form>'
    if state == 'certified':
        return f'<a class="button primary" href="{base}/layouts/{name}/database">下载 {name} 布局</a>'
    path = {'pending': 'build', 'built': 'certify'}[state]
    return (f'<form method="post" action="{base}/{path}"><input type="hidden" name="layout" value="{name}">'
            f'<button>{NEXT[state]} {name}</button></form>')


def proof(row):
    certificate = row['certificate']
    if certificate is None:
        return '<span class="muted">尚无证书</span>'
    shown = row | {key: certificate[key] for key in ('page_size', 'free_pages') if key in certificate}
    return (f'{code(certificate["sha256"])}<br><span class="muted">{certificate["bytes"]} 字节 · '
            f'{text(profile(shown))}</span>')


def rows(record):
    base = f'/workbench/captures/{record["id"]}'
    return ''.join(
        f'<tr><th scope="row">{code(name)}<br><span class="muted">{text(row["label"])}</span></th>'
        f'<td>{text(profile(row))}</td><td>{text(STAGES[row["state"]])}</td>'
        f'<td>{proof(row)}</td><td>{action(base, name, row["state"])}</td></tr>'
        for name, row in record['layouts'].items()
    )
