"""Browser view for reviewing draft foreign-key proposals against a live source."""

from .directory import source_options
from .errors import message
from .forms import field
from .render import code, options, page, text, typed
from ..codec import Fault

VERDICT_LABELS = {
    'ready': ('可接入', 'ready'),
    'blocked_catalog': ('目录阻断', 'muted'),
    'blocked_data': ('数据阻断', 'muted'),
    'inconclusive': ('证据不足', 'pending'),
}
FINDING_LABELS = {
    'dimension_mismatch': '维度不一致',
    'repeated_column': '存在重复列',
    'collation_difference': '排序规则不同',
    'affinity_difference': '类型亲和性不同',
    'nullable_child_key': '子键可空',
    'parent_key_guaranteed': '父键唯一性有保证',
    'parent_key_not_guaranteed': '父键唯一性无保证',
    'orphan_child_keys': '存在缺父子键',
    'duplicate_parent_keys': '存在重复父键',
    'ambiguous_child_keys': '子键匹配多个父行',
    'nocase_only_matches': '仅排序规则下匹配',
    'null_component_exempt': 'NULL 子键豁免',
}


def _finding_rows(findings):
    rows = []
    for item in findings:
        evidence = item.get('evidence')
        detail = item['detail']
        if evidence and 'witnesses' in evidence:
            detail += ' 证据样本：' + '、'.join(typed(value) for value in evidence['witnesses'])
            if evidence.get('truncated'):
                detail += '……'
        badge = '<span class="status muted">阻断</span>' if item['blocking'] else '<span class="status ready">提示</span>'
        rows.append(
            f'<tr><td>{badge}</td><td>{text(FINDING_LABELS.get(item["code"], item["code"]))}</td>'
            f'<td>{text(detail)}</td></tr>')
    return ''.join(rows) or '<tr><td colspan="3" class="empty">无发现。</td></tr>'


def _metric_rows(metrics):
    labels = (
        ('child_rows', '子表行数'),
        ('complete_child_keys', '完整非空子键'),
        ('null_component_rows', '含 NULL 分量（豁免）'),
        ('semantic_matches', '按父列语义匹配的不同子键'),
        ('binary_matches', '按 BINARY 匹配的不同子键'),
        ('orphans', '缺父子键'),
        ('ambiguous_parent_keys', '匹配多父的子键'),
        ('duplicate_parent_keys', '重复父键'),
    )
    return ''.join(f'<tr><th scope="row">{text(label)}</th><td>{metrics[key]}</td></tr>'
                   for key, label in labels)


def show(bench, query=None, values=None, error='', status=200):
    query = query or {}
    values = values or {}
    alias = field(values, 'source') or field(query, 'source', next(iter(sorted(bench.repository.sources)), ''))
    child = field(values, 'child') or field(query, 'child')
    parent = field(values, 'parent') or field(query, 'parent')
    child_cols = field(values, 'child_cols') or field(query, 'child_cols')
    parent_cols = field(values, 'parent_cols') or field(query, 'parent_cols')
    result = None
    digest = ''
    if values:
        try:
            child_list = [item.strip() for item in child_cols.split(',') if item.strip()]
            parent_list = [item.strip() for item in parent_cols.split(',') if item.strip()]
            if not child_list or not parent_list:
                raise Fault('invalid_request', '子列与父列都至少需要填写一个候选列。', 400)
            body = dict(source=alias, child=child, parent=parent,
                        child_cols=child_list, parent_cols=parent_list)
            result = bench.evaluate_proposal(body)
            digest = result['schema_digest']
        except Fault as exc:
            if exc.code in {'unknown_source', 'unsupported_schema'}:
                raise
            error = message(exc)
            status = exc.status
    verdict_html = ''
    if result is not None:
        label, style = VERDICT_LABELS[result['verdict']]
        verdict_html = (
            f'<div class="verdict {style}"><span class="status {style}">{text(label)}</span>'
            f'<p>{text(result["recommendation"])}</p>'
            f'<p class="muted">模式摘要 <code>{text(result["schema_digest"])}</code>，行级结论仅对应本次源读快照。</p></div>')
    return status, page('外键草案评估', 'proposal', 'proposal',
                        options=source_options(bench, alias), source=text(alias),
                        child=text(child), parent=text(parent), child_cols=text(child_cols),
                        parent_cols=text(parent_cols), digest=text(digest), verdict=verdict_html,
                        catalog_rows=_finding_rows(result['catalog_findings']) if result else '',
                        row_rows=_finding_rows(result['row_findings']) if result else '',
                        metric_rows=_metric_rows(result['metrics']) if result else '',
                        error=f'<p role="alert" class="notice">{text(error)}</p>' if error else '')
