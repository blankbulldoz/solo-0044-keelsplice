"""Describe the certificate and the action allowed by the persisted stage."""

from .render import code, template


def actions(base, state):
    if state == 'certified':
        return (f'<a class="button primary" href="{base}/database">下载认证子库</a>'
                f'<a class="button" href="{base}/receipts">签收交接</a>')
    action, label = ('build', '构建子库') if state == 'sealed' else ('certify', '认证子库')
    return f'<form method="post" action="{base}/{action}"><button class="primary">{label}</button></form>'


def certificate(record):
    certificate = record['certificate']
    if certificate is None:
        return ''
    return template('certificate', digest=code(certificate['sha256']),
                    bytes=certificate['bytes'], rows=sum(certificate['counts'].values()))


def signed(receipts, base):
    """Show the receipt verdict only after a fixture exists to acknowledge."""
    if not receipts['total']:
        return ''
    if receipts['signed']:
        return (f'<dl class="facts"><div><dt>交接签收</dt><dd><span class="status ready">已签收</span> '
                f'<span class="muted">当前 {receipts["current"]} 条记录能唯一确认本捕获</span></dd></div></dl>')
    return (f'<dl class="facts"><div><dt>交接签收</dt><dd><span class="status muted">未签收</span> '
            f'<span class="muted">当前 {receipts["current"]} 条记录均不能唯一确认本捕获</span>'
            f'<br><a href="{base}/receipts">查看或登记签收</a></dd></div></dl>')
