"""Browser-facing messages for stable backend fault codes."""

from ..codec import Fault

MESSAGES = {
    'unknown_source': '所选数据源未注册。',
    'unsupported_schema': '这个源库的表结构超出工作台支持范围。',
    'invalid_name': '配方名须以小写字母开头，只能包含小写字母、数字、短横线或下划线，最多 64 字符。',
    'invalid_request': '提交的数据形状不符合此操作的要求。',
    'unknown_identifier': '输入的表名或列名不在当前目录中。',
    'invalid_key': '请按目录中的列顺序填写完整主键，各分量均须有值。',
    'invalid_value': '主键值须使用有效的 SQLite 类型编码。',
    'invalid_roots': '根记录须为包含 1 至 256 项的 JSON 数组。',
    'invalid_edge': '选中的外键已不在当前目录中。',
    'invalid_edges': '最多可选 256 条反向关系。',
    'invalid_limit': '封存行上限须为 1 至 5,000 的整数。',
    'unknown_recipe': '配方版本不存在。',
    'retired_recipe': '此配方版本已停止投用。',
    'schema_conflict': '模式摘要不一致，无法沿用当前配方或直接比较这两次捕获。',
    'unknown_capture': '找不到指定的捕获。',
    'row_not_captured': '封存结果中没有这个实存主键，请核对类型与拼写。',
    'fixture_not_built': '需先构建子库。',
    'fixture_not_certified': '需先认证子库。',
    'artifact_integrity': '实存文件与证书摘要不一致。',
    'artifact_build': 'SQLite 无法按封存的结构和数据重建子库。',
    'artifact_unreadable': 'SQLite 无法复读生成的子库。',
    'database_busy': '数据库正忙，请稍后重试。',
    'database_input': 'SQLite 无法处理当前数据或文件。',
    'scan_budget': '读取工作量超过源库预算。',
    'row_budget': '闭包行数超出配方上限。',
    'payload_budget': '选中的数据及关系证据超过体积上限。',
    'edge_budget': '关系证据数量超过上限。',
    'missing_root': '在当前源快照中找不到唯一对应的根记录。',
    'null_identity': '选中的行存在空主键分量。',
    'invalid_reference': '选中的数据存在无法补齐的父引用。',
    'artifact_reference': '子库的外键复核失败。',
    'artifact_schema': '子库模式与封存模式不一致。',
    'artifact_content': '子库行内容与封存内容不一致。',
    'unknown_batch': '找不到指定的交付批次。',
    'batch_still_open': '已有交付中的批次，请先关闭当前批次再开新批。',
    'batch_closed': '批次已关闭，只能回看当时的交付记录。',
    'batch_entries_pending': '仍有待判断的交付项，请逐项选择继续、暂缓或销账。',
    'batch_budget': '单个批次的交付项不能超过 256 笔。',
    'duplicate_delivery': '同一组在本批只能核销一笔该捕获。',
    'unknown_delivery': '找不到指定的交付项。',
    'deliver_not_certified': '子库尚未认证，不能核销交付。',
    'invalid_label': '批次标签须为 1 至 64 个可打印字符。',
    'invalid_recipient': '交付组别须为 1 至 64 个可打印字符。',
    'invalid_note': '备注须为不超过 200 个字符的可打印文本。',
    'invalid_action': '未知的交付判断，请使用继续、暂缓或销账。',
    'hold_needs_reason': '暂缓交付须填写未发原因。',
    'unknown_route': '找不到指定页面。',
}


def message(exc):
    return MESSAGES.get(exc.code, str(exc) if exc.code in {
        'browser_form', 'browser_origin', 'browser_schema', 'invalid_json', 'invalid_version'
    } else '工作台无法完成这次操作。')


def invalid_route():
    return Fault('unknown_route', '找不到指定页面。', 404)
