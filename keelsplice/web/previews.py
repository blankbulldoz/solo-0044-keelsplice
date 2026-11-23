"""Browse durable preview jobs and start cancellable closure trials."""

import re

from . import preview
from .errors import invalid_route
from .render import page, redirect, segment, text

JOB_ID = re.compile('[0-9a-f]{32}\\Z')


def index(bench):
    records = bench.repository.recent_previews()
    rows = ''.join(
        f'<tr><th scope="row"><a href="/workbench/previews/{segment(record["id"])}">'
        f'{text(record["id"][:12])}…</a></th><td>{text(record["name"])}</td>'
        f'<td>{record["version"]}</td><td>{preview.state_badge(record["state"])}</td></tr>'
        for record in records
    )
    return 200, page('试算任务', 'preview', 'previews',
                     rows=rows or '<tr><td colspan="4" class="empty">尚无试算任务。</td></tr>')


def require_job(job_id):
    if not JOB_ID.fullmatch(job_id):
        raise invalid_route()
    return job_id


def start(bench, name, number):
    job = bench.start_preview(dict(name=name, version=number))
    return redirect(f'/workbench/previews/{segment(job["id"])}')


def detail(bench, job_id):
    return preview.show(bench, require_job(job_id))


def cancel(bench, job_id):
    bench.cancel_preview(require_job(job_id))
    return redirect(f'/workbench/previews/{segment(job_id)}')
