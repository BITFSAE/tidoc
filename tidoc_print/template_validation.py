"""DOCX container, run boundaries, all rendered parts and Jinja AST validation."""
from __future__ import annotations

import re
import zipfile
from pathlib import Path, PurePosixPath

from docxtpl import DocxTemplate
from jinja2 import nodes
from lxml import etree

from .template_renderer import FILTERS, make_environment
from .context import context_field_catalog

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
RENDER_PARTS = re.compile(r'^(word/(document|header\d*|footer\d*|footnotes)\.xml|docProps/core\.xml)$')
TOKENS = re.compile(r'({{.*?}}|{%.*?%}|{#.*?#})', re.S)
ROOTS = {'schema_version', 'scheme', 'title', 'batch', 'payee', 'export', 'entries', 'rows', 'totals', 'diagnostics'}


def validate_syntax(source, definition=None, context=None):
    env = make_environment()
    tree = env.parse(source)
    # Runtime context keys never expand the public field namespace.
    catalog = context_field_catalog(definition)
    aliases = {}
    allowed = (nodes.Template, nodes.Output, nodes.TemplateData, nodes.Name,
               nodes.Getattr, nodes.Getitem, nodes.Const, nodes.Filter, nodes.For,
               nodes.If, nodes.Compare, nodes.Operand, nodes.And, nodes.Or,
               nodes.Not, nodes.List, nodes.Tuple)

    def path(node):
        if isinstance(node, nodes.Name):
            return aliases.get(node.name, node.name)
        if isinstance(node, nodes.Getattr):
            if node.attr.startswith('_'):
                raise ValueError('禁止访问私有属性')
            return path(node.node) + '.' + node.attr
        if isinstance(node, nodes.Getitem):
            if (not isinstance(node.arg, nodes.Const) or
                    not isinstance(node.arg.value, str) or node.arg.value.startswith('_')):
                raise ValueError('仅允许已声明字段的字符串键')
            return path(node.node) + '.' + node.arg.value
        raise ValueError('变量只能引用已声明上下文字段')

    def check_path(value, loops=0):
        if value == 'loop.index' and loops:
            return
        if value.split('.')[0].removesuffix('[]') not in ROOTS or value not in catalog:
            raise ValueError('UNKNOWN_CONTEXT_FIELD:' + value)
        # A catalog alias must not allow attributes on a scalar (for example
        # row.claimant is text while entry.claimant is an identity object).
        parts = value.split('.')
        for index in range(1, len(parts)):
            parent = '.'.join(parts[:index])
            if catalog.get(parent) not in ('object', 'array'):
                raise ValueError('UNKNOWN_CONTEXT_FIELD:' + value)

    def visit(node, loops=0):
        if not isinstance(node, allowed):
            raise ValueError('禁止的模板语法：' + type(node).__name__)
        if isinstance(node, nodes.For):
            if (loops >= 3 or node.recursive or node.test or
                    not isinstance(node.target, nodes.Name) or
                    node.target.name in ROOTS | {'loop'}):
                raise ValueError('循环嵌套最多 3 层，禁止递归、解包、保留变量或过滤循环')
            iterator = path(node.iter)
            check_path(iterator, loops)
            if (catalog.get(iterator) != 'array' or
                    iterator not in ('entries', 'rows', 'diagnostics') and
                    not iterator.endswith(('.items', '.materials', '.defaults'))):
                raise ValueError('循环仅允许有界条目、行、明细和材料集合')
            old = dict(aliases)
            aliases[node.target.name] = iterator + '[]'
            for child in node.body:
                visit(child, loops + 1)
            aliases.clear()
            aliases.update(old)
            # Jinja for-else does not define the loop target or loop object.
            for child in node.else_:
                visit(child, loops)
            return
        if isinstance(node, (nodes.Getattr, nodes.Getitem)):
            check_path(path(node), loops)
            return
        if isinstance(node, nodes.Name) and node.ctx == 'load':
            check_path(path(node), loops)
        if isinstance(node, nodes.Filter):
            if node.name not in FILTERS or node.kwargs or node.dyn_args or node.dyn_kwargs:
                raise ValueError('禁止的过滤器：' + node.name)
            if any(not isinstance(arg, nodes.Const) for arg in node.args):
                raise ValueError('过滤器参数必须是常量')
        if isinstance(node, nodes.Operand) and node.op not in {
                'eq', 'ne', 'gt', 'gteq', 'lt', 'lteq', 'in', 'notin'}:
            raise ValueError('禁止的比较运算')
        for child in node.iter_child_nodes():
            visit(child, loops)

    if sum(1 for _ in tree.find_all(nodes.Node)) > 10000:
        raise ValueError('模板语法节点过多')
    visit(tree)
    return tree


def validate_template(path, definition=None, context=None):
    path = Path(path)
    diagnostics = []
    def error(code, location, message, **extra):
        diagnostics.append({'code': code, 'file': str(path), 'location': location, 'message': message, 'severity': 'blocked', **extra})
    try:
        context_field_catalog(definition)
    except (FileNotFoundError, ValueError) as exc:
        error('MISSING_COMPONENT_RESOURCE' if isinstance(exc, FileNotFoundError)
              else 'INVALID_CONTEXT_CATALOG', 'context_fields.json', str(exc))
        return diagnostics
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > 256 or sum(info.file_size for info in infos) > 100 * 1024 * 1024:
                raise ValueError('DOCX 展开大小或文件数超过限制')
            names = set()
            template = DocxTemplate(str(path))
            for info in infos:
                name = info.filename
                normalized = name.casefold()
                pure = PurePosixPath(name)
                if pure.is_absolute() or '..' in pure.parts or '\\' in name or ':' in name or normalized in names or (info.external_attr >> 16) & 0o170000 == 0o120000:
                    error('UNSAFE_DOCX_PATH', name, 'DOCX 包含不安全或重复路径'); continue
                names.add(normalized)
                if any(piece in normalized for piece in ('vbaproject','activex/','embeddings/','attachedtemplate','altchunk','customui/')) or normalized.endswith('.bin'):
                    error('UNSAFE_DOCX_PART', name, '不允许宏、ActiveX 或嵌入对象'); continue
                if not name.endswith(('.xml', '.rels')):
                    continue
                if info.compress_size and info.file_size/info.compress_size>200:
                    error('DOCX_COMPRESSION_RATIO',name,'DOCX 资源压缩比例异常'); continue
                data = archive.read(info)
                if b'<!DOCTYPE' in data or b'<!ENTITY' in data:
                    error('UNSAFE_XML', name, '禁止 XML 实体和文档类型'); continue
                root = etree.fromstring(data, parser=etree.XMLParser(resolve_entities=False, no_network=True))
                if name.endswith('.rels'):
                    for rel in root:
                        if (rel.get('TargetMode') or '').lower() == 'external' and not rel.get('Type', '').endswith('/hyperlink'):
                            error('EXTERNAL_DOCX_RESOURCE', name, '不允许外部模板、图片或自动资源')
                if 'macroEnabled' in data.decode('utf-8', 'ignore'):
                    error('UNSAFE_DOCX_PART', name, '不允许宏文档类型')
                if any(True for _ in root.iter(W+'updateFields')) or any(True for _ in root.iter(W+'altChunk')):
                    error('ACTIVE_DOCX_CONTENT',name,'禁止自动更新字段和外部内容块')
                for field in root.iter(W + 'instrText'):
                    instruction = (field.text or '').strip().split()
                    if instruction and instruction[0].upper() not in {'PAGE','NUMPAGES'}:
                        error('AUTOMATIC_FIELD', name, '禁止自动更新字段')
                for field in root.iter(W + 'fldSimple'):
                    instruction = (field.get(W + 'instr') or '').strip().split()
                    if instruction and instruction[0].upper() not in {'PAGE','NUMPAGES'}:
                        error('AUTOMATIC_FIELD', name, '禁止自动更新字段')
                raw = data.decode('utf-8')
                if '{{' not in raw and '{%' not in raw and '{#' not in raw:
                    continue
                if not RENDER_PARTS.match(name):
                    error('UNSUPPORTED_TEMPLATE_PART', name, '该 DOCX 部件不支持模板标记'); continue
                for index, paragraph in enumerate(root.iter(W + 'p')):
                    runs = [''.join(run.itertext()) for run in paragraph.iter(W + 'r')]
                    # Word text is examined per run before docxtpl's permissive repair.
                    joined = ''.join(runs)
                    offsets, end = [], 0
                    for run in runs:
                        offsets.append((end, end + len(run))); end += len(run)
                    for token in TOKENS.finditer(joined):
                        if not any(start <= token.start() and token.end() <= finish for start, finish in offsets):
                            error('SPLIT_TEMPLATE_TAG', f'{name}:paragraph[{index}]', '模板标记跨越 Word run；请在同一个 run 内重新输入标记')
                    structural = re.findall(r'{%\s*(p|tr|tc|r)\s', joined)
                    if structural.count('p') > 1:
                        error('DUPLICATE_STRUCTURE_TAG', f'{name}:paragraph[{index}]', '同一段落只能有一个结构标记')
                    if '{{r ' in joined or '{{p ' in joined:
                        error('RAW_TEMPLATE_OBJECT', f'{name}:paragraph[{index}]', '不允许富文本、子文档或原始 XML 对象')
                for index, row in enumerate(root.iter(W + 'tr')):
                    if len(re.findall(r'{%\s*tr\s', ''.join(row.itertext()))) > 1:
                        error('DUPLICATE_STRUCTURE_TAG', f'{name}:row[{index}]', '同一行只能有一个 tr 标记')
                try:
                    patched = template.patch_xml(raw)
                    validate_syntax(patched, definition, context)
                except Exception as exc:
                    message = str(exc)
                    code = 'UNKNOWN_CONTEXT_FIELD' if message.startswith('UNKNOWN_CONTEXT_FIELD:') else 'UNSAFE_TEMPLATE_SYNTAX'
                    error(code, name + ':line[' + str(getattr(exc, 'lineno', 0)) + ']', message, **({'field': message.split(':',1)[1], 'suggested_action': '使用上下文字段目录中的已声明字段。'} if code == 'UNKNOWN_CONTEXT_FIELD' else {}))
            if 'word/document.xml' not in archive.namelist():
                error('INVALID_DOCX', '/', 'DOCX 缺少正文部件')
    except Exception as exc:
        error('INVALID_DOCX', '/', str(exc))
    return diagnostics
