"""Render the certificate and the action allowed by the persisted stage.

Presentation mirror of ``keelsplice.handoff``: the stage gates and retries
live in that domain module; here the persisted stage only becomes the next
link or form the browser may use, never a decision of its own.
"""

from .render import code, template


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
