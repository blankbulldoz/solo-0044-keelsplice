"""Describe the certificate, recorded reviews, and stage-allowed hand-off actions."""

from .render import code, template, text

VERDICTS = {'transferable': ('ready', '可转交'), 'blocked': ('error', '不可转交')}
CONCLUSIONS = {
    'uncertified': ('pending', '尚未认证，不能交接。'),
    'historical_only': ('pending', '只看到历史证书：当前没有有效证书，不能交接，请重新认证后再审核。'),
    'unreviewed': ('pending', '证书有效，等待 A 工位审核。'),
    'transferable': ('ready', '可转交：最近一次审核核验通过，实存字节与证书一致。'),
    'blocked': ('error', '不可转交：最近一次审核发现实存字节与证书摘要不一致。'),
    'expired': ('pending', '结果过期：最近的判断基于历史证书，请重新审核当前证书。'),
}
REVIEW_STATUS = {'current': '<span class="status muted">当前</span>',
                 'expired': '<span class="status pending">已过期</span>'}
CERTIFICATE_STATUS = {True: '<span class="status ready">有效</span>',
                      False: '<span class="status muted">历史</span>'}


def actions(base, state):
    if state == 'certified':
        return f'<a class="button primary" href="{base}/database">下载认证子库</a>'
    action, label = ('build', '构建子库') if state == 'sealed' else ('certify', '认证子库')
    return f'<form method="post" action="{base}/{action}"><button class="primary">{label}</button></form>'


def certificate(record):
    certificate = record['certificate']
    if certificate is None:
        return ''
    return template('certificate', digest=code(certificate['sha256']),
                    bytes=certificate['bytes'], rows=sum(certificate['counts'].values()))


def reviews(records):
    if not records:
        return '<tr><td colspan="7" class="empty">尚无审核记录。</td></tr>'
    rows = []
    for record in records:
        style, label = VERDICTS[record['verdict']]
        digest = record['artifact_sha256']
        rows.append(
            f'<tr><td>{text(record["created"])}</td><td>{text(record["station"])}</td>'
            f'<td><span class="status {style}">{label}</span></td>'
            f'<td>{code(record["certificate_sha256"][:12])}</td>'
            f'<td>{code(digest[:12]) if digest else "—"}</td><td>{record["artifact_bytes"]}</td>'
            f'<td>{REVIEW_STATUS[record["status"]]}</td></tr>'
        )
    return ''.join(rows)


def certificates(records):
    if not records:
        return '<tr><td colspan="4" class="empty">尚无证书记录。</td></tr>'
    return ''.join(
        f'<tr><td>{text(record["created"] or "—")}</td><td>{code(record["sha256"][:12])}</td>'
        f'<td>{record["bytes"]}</td><td>{CERTIFICATE_STATUS[record["current"]]}</td></tr>'
        for record in records
    )


def panel(base, view):
    style, label = CONCLUSIONS[view['conclusion']['code']]
    forms = ''
    if view['state'] == 'certified':
        forms = (
            f'<form class="trace-form" method="post" action="{base}/review">'
            f'<label>审核工位<input required name="station" maxlength="32" value="A"></label>'
            f'<button class="primary">审核当前证书</button></form>'
            f'<form class="inline" method="post" action="{base}/rebuild">'
            f'<button class="quiet">重建子库（当前证书归档为历史）</button></form>'
        )
    return template('handoff', conclusion=f'<span class="status {style}">{label}</span>',
                    forms=forms, reviews=reviews(view['reviews']),
                    certificates=certificates(view['certificates']))
