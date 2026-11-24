"""Describe the certificate and the action allowed by the persisted stage."""

from .render import code, template


def actions(base, state, *, read_only=False):
    if read_only:
        if state == 'certified':
            return (f'<form method="post" action="{base}/reverify"><button class="primary">'
                    '复核已认证子库</button></form>'
                    '<p class="muted">备用实例不直接交接文件，恢复写入后由权威实例下载。</p>')
        return '<p class="notice info">备用实例只复核此前已认证的子库，不构建或认证新成果。</p>'
    if state == 'certified':
        return f'<a class="button primary" href="{base}/database">下载认证子库</a>'
    action, label = ('build', '构建子库') if state == 'sealed' else ('certify', '认证子库')
    return f'<form method="post" action="{base}/{action}"><button class="primary">{label}</button></form>'


def certificate(record, *, reverified=False):
    certificate = record['certificate']
    if certificate is None:
        return ''
    note = '<p class="notice info">本次备用实例已用存储字节重新计算摘要并与证书逐表计数核对。</p>' \
        if reverified else ''
    return template('certificate', digest=code(certificate['sha256']),
                    bytes=certificate['bytes'], rows=sum(certificate['counts'].values()),
                    note=note)
