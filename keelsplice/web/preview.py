"""Render preview jobs: live closure results, cancellation and durable outcome states."""

from .render import code, page, segment, text, typed

JOB_STATES = {
    'running': ('试算进行中', 'pending'),
    'completed': ('试算完成', 'ready'),
    'cancelled': ('已取消', 'muted'),
    'inconclusive': ('结论丢失', 'muted'),
    'failed': ('试算失败', 'muted'),
}
REFRESH_SECONDS = 2


def counts(summary):
    rows = ''.join(f'<tr><th scope="row">{code(name)}</th><td>{count}</td></tr>'
                   for name, count in summary['counts'].items())
    return rows


def canonical_roots(roots):
    return ''.join(f'<li>{code(table)} {typed(key)}</li>' for table, key in roots)


def state_badge(state):
    label, style = JOB_STATES[state]
    return f'<span class="status {style}">{label}</span>'


def describe_state(record):
    state = record['state']
    if state == 'running':
        return '<p class="notice info">试算仍在读取源库快照，页面会自动刷新；也可随时取消本次读事务。</p>'
    if state == 'completed':
        return '<p class="notice info">以下结果只对应本次读取。封存时会重新读取当前数据源。</p>'
    if state == 'cancelled':
        return '<p class="notice">本次试算已取消，对应读事务已释放，未产生封存记录。</p>'
    if state == 'inconclusive':
        return '<p class="notice">服务在试算结束前重启，原操作没有留下结论。请重新发起一次试算。</p>'
    detail = record['detail'] or {}
    return f'<p class="notice" role="alert">试算未能完成：{text(detail.get("message", detail.get("error", "未知原因")))}</p>'


def show(bench, job_id):
    record = bench.preview_status(job_id)
    return 200, render(record)


def render(record):
    state = record['state']
    job_id = record['id']
    base = f'/workbench/previews/{segment(job_id)}'
    refresh = f'<meta http-equiv="refresh" content="{REFRESH_SECONDS};url={base}">' if state == 'running' else ''
    detail = record['detail']
    recipe_href = f'/workbench/recipes/{segment(record["name"])}/{record["version"]}'
    if state == 'completed' and detail is not None:
        summary = detail['summary']
        body = (
            f'<dl class="facts"><div><dt>入选行</dt><dd>{summary["rows"]}</dd></div>'
            f'<div><dt>关系见证</dt><dd>{summary["witnesses"]}</dd></div>'
            f'<div class="wide"><dt>模式摘要</dt><dd><code>{text(detail["schema_digest"])}</code></dd></div></dl>'
            '<div class="editor-grid">'
            f'<section><h2>源库实存根</h2><ol class="data-list">{canonical_roots(detail["roots"])}</ol></section>'
            '<section><h2>逐表计数</h2><div class="scroll"><table><thead><tr>'
            '<th scope="col">表</th><th scope="col">入选行</th></tr></thead>'
            f'<tbody>{counts(summary)}</tbody></table></div></section></div>'
        )
    else:
        body = ''
    if state == 'running':
        actions = (
            f'<form class="actions" method="post" action="{base}/cancel"><button class="quiet">取消本次试算</button></form>'
        )
    elif state == 'completed':
        actions = (
            f'<form class="actions" method="post" action="{recipe_href}/capture">'
            '<button class="primary">封存当前样本</button></form>'
        )
    else:
        actions = (
            f'<form class="actions" method="post" action="{recipe_href}/preview">'
            '<button class="primary">重新发起试算</button></form>'
        )
    return page('闭包试算', 'preview', 'preview_job', refresh=refresh,
                recipe=text(recipe_href), name=text(record['name']), number=record['version'],
                job=text(job_id), badge=state_badge(state), notice=describe_state(record),
                body=body, actions=actions)
