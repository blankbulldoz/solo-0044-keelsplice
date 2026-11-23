"""Describe the certificate, operation receipts, and the action allowed by the persisted stage."""

from uuid import uuid4

from .render import code, segment, template

ACTIONS = {'build': '构建子库', 'certify': '认证子库'}
OUTCOMES = {'performed': '推进了状态', 'observed': '未改变状态'}


def stage_line(state):
    return {
        'sealed': '当前阶段：已封存。任何工位都可以构建子库。',
        'built': '当前阶段：已构建。任何工位都可以认证子库。',
        'certified': '当前阶段：已认证。子库可下载交接，没有更多阶段操作。',
    }[state]


def actions(base, state):
    if state == 'certified':
        return f'<a class="button primary" href="{base}/database">下载认证子库</a>'
    action, label = ('build', '构建子库') if state == 'sealed' else ('certify', '认证子库')
    return (f'<form method="post" action="{base}/{action}">'
            f'<input type="hidden" name="request" value="{uuid4().hex}">'
            f'<button class="primary">{label}</button></form>')


def receipt_link(base, operation_id, label=None):
    shown = label if label is not None else f'{operation_id[:12]}…'
    return f'<a href="{base}/operations/{segment(operation_id)}">{code(shown)}</a>'


def certificate(record, base, build_id):
    certificate = record['certificate']
    if certificate is None:
        return ''
    linked = (receipt_link(base, build_id) if build_id
              else '<span class="muted">无回执记录（早于操作回执）</span>')
    return template('certificate', digest=code(certificate['sha256']),
                    bytes=certificate['bytes'], rows=sum(certificate['counts'].values()),
                    build=linked)


def performed_build(operations):
    return next((operation['id'] for operation in operations
                 if operation['action'] == 'build' and operation['outcome'] == 'performed'), None)
