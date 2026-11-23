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
    'unknown_route': '找不到指定页面。',
    'unknown_acceptance': '验收会话不存在。',
    'invalid_groups': '接收组须为 1 至 16 个互不相同的名称，每个 1 至 64 个可打印字符。',
    'invalid_clause': '条款须为 1 至 64 项；编号唯一，普通条款 1 项要求，同一文件条款恰好 2 项。',
    'invalid_requirement': '条款要求须为有效的行或外键关联，方向与目录中的外键一致。',
    'unknown_clause': '本验收会话没有这个条款。',
    'invalid_group': '所选接收组不属于本验收会话。',
    'citation_uncertified': '只有已认证的捕获才能被引用。',
    'citation_version': '该捕获来自同名配方的其他版本，不能用于本会话。',
    'citation_schema': '该捕获的模式摘要与本会话不一致。',
    'citation_integrity': '该捕获的证书与实际文件或封存内容不一致。',
    'clause_satisfied': '该条款已由同一认证文件满足，无需补证。',
    'clause_awaiting_citation': '已有可用认证捕获覆盖该条款，请引用证书而不是再封存样本。',
}


def message(exc):
    return MESSAGES.get(exc.code, str(exc) if exc.code in {
        'browser_form', 'browser_origin', 'browser_schema', 'invalid_json', 'invalid_version'
    } else '工作台无法完成这次操作。')


def invalid_route():
    return Fault('unknown_route', '找不到指定页面。', 404)
