"""Render trusted templates with explicit text and URL escaping."""

from functools import lru_cache
from html import escape
from pathlib import Path
from string import Template
from urllib.parse import quote, urlencode

from ..codec import dumps

DIRECTORY = Path(__file__).parent
NAVIGATION = (
    ('catalog', '关系目录', '/workbench'),
    ('proposal', '外键草案', '/workbench/proposals'),
    ('recipe', '配方版本', '/workbench/recipes'),
    ('capture', '封存捕获', '/workbench/captures'),
    ('compare', '捕获对比', '/workbench/compare'),
)


@lru_cache
def load_template(name):
    return Template((DIRECTORY / 'templates' / f'{name}.html').read_text(encoding='utf-8'))


def template(name, /, **fields):
    return load_template(name).substitute(fields)


def text(value):
    return escape(str(value), quote=True)


def location(path, **query):
    if not query:
        return path
    return path + '?' + urlencode(query)


def segment(value):
    return quote(str(value), safe='')


def code(value):
    return f'<code>{text(value)}</code>'


def typed(value):
    return code(dumps(value))


def options(values, active):
    return ''.join(
        '<option value="{}"{}>{}</option>'.format(text(value), ' selected' if active == value else '', text(label))
        for value, label in values
    )


def page(title, section, content, **fields):
    navigation = ''.join(
        '<a href="{}"{}>{}</a>'.format(path, ' aria-current="page"' if key == section else '', label)
        for key, label, path in NAVIGATION
    )
    return template('layout', title=text(title), navigation=navigation,
                    main=template(content, **fields)).encode('utf-8')
