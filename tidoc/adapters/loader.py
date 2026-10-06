"""Strict package loader, domain validator and deterministic package packer."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

from jsonschema import Draft202012Validator

from .models import AdapterPackage, AdapterValidationError, diagnostic
from .registry import (CAPABILITIES, LIMITS, RESERVED_FIELD_IDS, SETTINGS, BUILTIN_ROLES,
                       FIELD_CATALOG, FILENAME_FIELDS, RULE_COMPLETE_FIELDS,
                       RULE_EXPORT_FIELDS, required_capabilities)
from .resolver import canonical_json

SCHEMA_DIR = Path(__file__).resolve().parents[2] / 'schemas' / 'team-adapter' / '1'
JSON_FILES = {'manifest.json':'manifest.schema.json','scheme.json':'scheme.schema.json','fields.json':'fields.schema.json','materials.json':'materials.schema.json','rules.json':'rules.schema.json','outputs.json':'outputs.schema.json'}
ALLOWED_SUFFIXES={'.json','.docx','.png','.jpg','.jpeg','.txt'}

def _diag(code,msg,file='',loc='/',**kwargs):
    return diagnostic(code,msg,file=file,location=loc,**kwargs)

def _safe_name(name):
    if not isinstance(name,str) or not name or '\\' in name or name.startswith('/') or '\x00' in name: return False
    p=PurePosixPath(name)
    return str(p) == name and not p.is_absolute() and all(part not in ('','..','.') for part in p.parts) and ':' not in p.parts[0]

def _read_source(path):
    p=Path(path); files={}; problems=[]
    if p.is_dir():
        for current, dirs, names in os.walk(p, followlinks=False):
            for d in list(dirs):
                candidate=Path(current)/d
                if candidate.is_symlink(): problems.append(_diag('ARCHIVE_SYMLINK','适配包目录不能包含符号链接。',candidate.name)); dirs.remove(d)
            for name in names:
                f=Path(current)/name; rel=f.relative_to(p).as_posix()
                if f.is_symlink() or not f.is_file(): problems.append(_diag('ARCHIVE_SYMLINK','适配包不能包含链接或特殊文件。',rel)); continue
                if not _safe_name(rel): problems.append(_diag('ARCHIVE_PATH_INVALID','包内路径不安全。',rel)); continue
                if len(files)>=LIMITS['files']: problems.append(_diag('ARCHIVE_FILE_LIMIT','适配包文件数量超限。',rel)); break
                if f.stat().st_size > LIMITS['expanded_bytes']: problems.append(_diag('RESOURCE_SIZE_LIMIT','单个资源超出大小限制。',rel)); continue
                if sum(len(v) for v in files.values()) + f.stat().st_size > LIMITS['expanded_bytes']:
                    problems.append(_diag('ARCHIVE_EXPANDED_LIMIT','适配包展开后超过 100 MiB。',rel)); continue
                files[rel]=f.read_bytes()
    elif p.is_file():
        try:
            if p.stat().st_size > LIMITS['compressed_bytes']: raise ValueError('压缩包超过 25 MiB。')
            with zipfile.ZipFile(p) as z:
                seen=set(); folded=set(); expanded=0
                if len(z.infolist())>LIMITS['files']: raise ValueError('适配包文件数量超过 256。')
                for info in z.infolist():
                    n=info.filename
                    if n.endswith('/'):
                        if not _safe_name(n[:-1]): problems.append(_diag('ARCHIVE_PATH_INVALID','包内路径不安全。',n))
                        continue
                    if not _safe_name(n): problems.append(_diag('ARCHIVE_PATH_INVALID','包内路径不安全。',n)); continue
                    mode=(info.external_attr>>16)&0xffff
                    if stat.S_ISLNK(mode): problems.append(_diag('ARCHIVE_SYMLINK','适配包不能包含符号链接。',n)); continue
                    if n in seen: problems.append(_diag('ARCHIVE_DUPLICATE_PATH','包内路径重复。',n)); continue
                    if n.casefold() in folded: problems.append(_diag('ARCHIVE_CASE_COLLISION','包内路径仅大小写不同。',n)); continue
                    seen.add(n); folded.add(n.casefold()); expanded+=info.file_size
                    if expanded>LIMITS['expanded_bytes']: problems.append(_diag('ARCHIVE_EXPANDED_LIMIT','适配包展开后超过 100 MiB。',n)); break
                    if info.file_size>LIMITS['expanded_bytes'] or (info.compress_size and info.file_size/info.compress_size>200): problems.append(_diag('ARCHIVE_RATIO_LIMIT','包内资源压缩比例异常。',n)); continue
                    files[n]=z.read(info)
        except (zipfile.BadZipFile, OSError, ValueError) as e:
            problems.append(_diag('ARCHIVE_INVALID',f'无法安全读取适配包：{e}',str(p)))
    else: problems.append(_diag('SOURCE_NOT_FOUND','适配包路径不存在。',str(p)))
    return files,problems

def _schema_validate(name, data):
    schema=json.loads((SCHEMA_DIR/JSON_FILES[name]).read_text(encoding='utf-8'))
    validator=Draft202012Validator(schema)
    return [_diag('SCHEMA_INVALID',e.message,name,'/'+ '/'.join(map(str,e.absolute_path))) for e in sorted(validator.iter_errors(data),key=lambda x:list(map(str,x.absolute_path)))]

def _check_docx(path, data):
    out=[]
    try:
        import io
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos=z.infolist()
            if len(infos)>LIMITS['files'] or sum(x.file_size for x in infos)>LIMITS['expanded_bytes']:
                return [_diag('DOCX_RESOURCE_LIMIT','DOCX 内部文件数或展开大小超限。',path)]
            seen=set(); folded=set()
            for info in infos:
                n=info.filename
                if not _safe_name(n) or n in seen or n.casefold() in folded:
                    out.append(_diag('DOCX_PATH_INVALID','DOCX 包含不安全、重复或大小写冲突路径。',path,n));continue
                seen.add(n);folded.add(n.casefold())
                mode=(info.external_attr>>16)&0xffff
                if stat.S_ISLNK(mode):out.append(_diag('DOCX_SYMLINK','DOCX 不得包含符号链接。',path,n));continue
                if info.compress_size and info.file_size/info.compress_size>200:
                    out.append(_diag('DOCX_COMPRESSION_RATIO','DOCX 内部资源压缩比例异常。',path,n));continue
                low=n.lower()
                if any(token in low for token in ('vbaproject','activex/','embeddings/','attachedtemplate','altchunk','customui/')) or low.endswith('.bin'):
                    out.append(_diag('DOCX_ACTIVE_CONTENT','DOCX 包含不允许的宏、嵌入对象或活动内容。',path,n));continue
                if not low.endswith(('.xml','.rels')):continue
                raw=z.read(info)
                if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
                    out.append(_diag('DOCX_UNSAFE_XML','DOCX XML 不得包含 DTD 或实体声明。',path,n));continue
                try:root=ElementTree.fromstring(raw)
                except ElementTree.ParseError:
                    out.append(_diag('DOCX_XML_INVALID','DOCX 内部 XML 无效。',path,n));continue
                if n.endswith('.rels'):
                    for rel in root:
                        if rel.attrib.get('TargetMode','').lower()=='external' and rel.attrib.get('Type','').rsplit('/',1)[-1].lower()!='hyperlink':
                            out.append(_diag('DOCX_EXTERNAL_RESOURCE','DOCX 不得引用外部模板或图片资源。',path,n))
                if n.lower().endswith('content_types.xml') and b'macroEnabled' in raw:
                    out.append(_diag('DOCX_MACRO_TYPE','DOCX 不能声明宏启用文档类型。',path,n))
                if b'w:updateFields' in raw or b'updateFields' in raw:
                    out.append(_diag('DOCX_FIELD_UPDATE','DOCX 不能包含自动更新字段设置。',path,n))
                for node in root.iter():
                    tag=node.tag.rsplit('}',1)[-1]
                    if tag=='altChunk':out.append(_diag('DOCX_ACTIVE_CONTENT','DOCX 不允许 altChunk 内容。',path,n))
                    if tag in ('instrText','fldSimple'):
                        instruction=(node.text or node.attrib.get('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}instr','')).strip().split()
                        if instruction and instruction[0].upper() not in ('PAGE','NUMPAGES'):
                            out.append(_diag('DOCX_FIELD_UNSAFE','DOCX 包含不允许的自动更新字段。',path,n))
    except (zipfile.BadZipFile, OSError): out.append(_diag('DOCX_INVALID','模板不是有效的 DOCX 文件。',path))
    return out

def _domain_validate(defn, files, *, structural_only=False):
    d=[]; manifest=defn['manifest']; scheme=defn['scheme']; pkg=manifest['package_id']
    fields=defn['fields']; roles=defn['materials']; rules=defn['rules']; outputs=defn['outputs']
    if not roles or not any(role.get('id') == 'invoice' for role in roles):
        d.append(_diag('MATERIAL_INVOICE_ROLE_INJECTED','核心发票材料角色将使用协议默认定义。','materials.json','/roles',severity='warning'))
    role_ids=set()
    if sum(1 for role in roles if role.get('id','').startswith('custom:'))>LIMITS['custom_materials']:
        d.append(_diag('MATERIAL_ROLE_LIMIT','自定义材料角色最多 20 个。','materials.json','/roles'))
    if sum(1 for role in roles if role.get('quick_action'))>4:
        d.append(_diag('MATERIAL_QUICK_ACTION_LIMIT','材料快捷入口最多 4 个。','materials.json','/roles'))
    for i,r in enumerate(roles):
        rid=r['id']; loc=f'/roles/{i}'
        if rid in role_ids: d.append(_diag('MATERIAL_ID_DUPLICATE',f'材料角色 ID 重复：{rid}','materials.json',loc))
        role_ids.add(rid)
        if rid=='invoice' and (r.get('reclassifiable') or r.get('min_count',0)<1): d.append(_diag('INVOICE_ROLE_RESERVED','发票角色必须保留且至少需要一份材料。','materials.json',loc))
        if rid.startswith('custom:') and not rid.startswith(f'custom:{pkg}:'): d.append(_diag('MATERIAL_NAMESPACE_INVALID','自定义材料角色必须使用当前包的命名空间。','materials.json',loc))
        if r.get('max_count') is not None and r['max_count']<r.get('min_count',0): d.append(_diag('MATERIAL_COUNT_CONFLICT','材料最大份数不能小于最小份数。','materials.json',loc))
    ids=set()
    for i,f in enumerate(fields):
        fid=f['id']; loc=f'/fields/{i}'
        if fid in RESERVED_FIELD_IDS: d.append(_diag('FIELD_ID_RESERVED',f'字段 ID 是核心保留名称：{fid}','fields.json',loc))
        key=(f['scope'],fid)
        if key in ids:d.append(_diag('FIELD_ID_DUPLICATE',f'字段 ID 重复：{fid}','fields.json',loc))
        ids.add(key)
        if f['type'] in ('select','multiselect') and not f.get('options'): d.append(_diag('FIELD_OPTIONS_REQUIRED','选择字段必须定义至少一个选项。','fields.json',loc))
        if 'complete' in f.get('required_at',[]) and f['scope']!='entry':
            d.append(_diag('FIELD_REQUIRED_STAGE_INVALID','只有条目字段可以在 complete 阶段必填。','fields.json',loc))
        if 'default' in f:
            from .policy import validate_field_value
            from .models import FieldValidationError
            try: validate_field_value(f,f['default'])
            except FieldValidationError as exc:
                d.extend(_diag('FIELD_DEFAULT_INVALID',x['message'],'fields.json',loc) for x in exc.diagnostics)
        if f.get('presentation')=='hidden' and f.get('required_at') and f.get('default') is None:
            d.append(_diag('FIELD_HIDDEN_REQUIRED','隐藏的必填字段必须提供默认值或可达的填写入口。','fields.json',loc))
    for i,rule in enumerate(rules):
        for req in rule.get('require',[]):
            rid=req.get('material')
            if rid and rid not in role_ids:d.append(_diag('RULE_MATERIAL_UNKNOWN',f'规则引用未定义材料角色：{rid}','rules.json',f'/rules/{i}/require'))
            if rid in role_ids:
                role=next(r for r in roles if r['id']==rid)
                if role.get('max_count') is not None and req.get('min_count',1)>role['max_count']:
                    d.append(_diag('RULE_MATERIAL_MAX_CONFLICT','规则要求份数超过材料角色最大份数。','rules.json',f'/rules/{i}/require'))
    declared_fields={f"{f['scope']}.fields.{f['id']}":f for f in fields}
    catalog={**FIELD_CATALOG,**{k:v['type'] for k,v in declared_fields.items()}}
    def check_condition(expr, file, loc, stage, form_scope=None):
        count=[0]
        def visit(node, depth, at):
            count[0]+=1
            if depth>8 or count[0]>100:
                d.append(_diag('RULE_COMPLEXITY_LIMIT','条件表达式超过深度或节点上限。',file,at));return
            if not isinstance(node,dict): return
            if 'all' in node or 'any' in node:
                for j,child in enumerate(node.get('all',node.get('any',[]))):visit(child,depth+1,f'{at}/{"all" if "all" in node else "any"}/{j}')
            elif 'not' in node: visit(node['not'],depth+1,at+'/not')
            else:
                ref=node.get('field'); op=node.get('op'); operand=node.get('value')
                if ref not in catalog:
                    d.append(_diag('RULE_FIELD_UNKNOWN',f'条件引用的字段未注册：{ref}',file,at+'/field'));return
                allowed = RULE_COMPLETE_FIELDS if stage == 'complete' else RULE_EXPORT_FIELDS
                if form_scope:
                    allowed |= frozenset(k for k in declared_fields if k.startswith(f'{form_scope}.fields.'))
                    if form_scope=='entry':
                        allowed |= frozenset(k for k in RULE_COMPLETE_FIELDS if k.startswith(('entry.', 'invoice.', 'title.')))
                    else:
                        allowed=frozenset(k for k in allowed if k.startswith(f'{form_scope}.fields.'))
                elif stage == 'complete':
                    allowed |= frozenset(k for k in declared_fields if k.startswith('entry.fields.'))
                else:
                    allowed |= frozenset(declared_fields)
                if file == 'fields.json':
                    allowed = frozenset(k for k in declared_fields if k.startswith(f'{form_scope}.fields.'))
                    if form_scope=='entry':
                        allowed |= frozenset(k for k in RULE_COMPLETE_FIELDS if k.startswith(('entry.', 'invoice.', 'title.')))
                if ref not in allowed:
                    d.append(_diag('RULE_FIELD_STAGE_INVALID',f'条件引用字段不适用于此规则或表单作用域：{ref}',file,at+'/field'))
                if op not in ('present','eq','ne','in','gt','gte','lt','lte'):return
                value_type=catalog[ref]
                numeric=value_type in ('integer','decimal','money')
                if op in ('gt','gte','lt','lte') and not (numeric or value_type=='date'):
                    d.append(_diag('RULE_OPERATOR_TYPE','排序比较只适用于数值或日期字段。',file,at+'/op'))
                if op in ('gt','gte','lt','lte','eq','ne','in'):
                    vals=operand if op=='in' and isinstance(operand,list) else [operand]
                    if op=='in' and not isinstance(operand,list):d.append(_diag('RULE_OPERAND_TYPE','in 操作必须提供数组值。',file,at+'/value'))
                    for value in vals:
                        valid=(type(value)is bool) if value_type=='boolean' else (isinstance(value,str) and bool(re.fullmatch(r'-?(?:0|[1-9]\d*)(?:\.\d+)?',value))) if numeric else (isinstance(value,str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}',value) is not None) if value_type=='date' else isinstance(value,str)
                        if value is None or not valid:d.append(_diag('RULE_OPERAND_TYPE',f'条件值类型与字段 {ref} 不匹配。',file,at+'/value'))
                        field=declared_fields.get(ref)
                        if field and field['type'] in ('select','multiselect') and isinstance(value,str) and value not in {x['value'] for x in field.get('options',[])}:
                            d.append(_diag('RULE_OPTION_UNKNOWN',f'条件值不是字段 {ref} 的选项。',file,at+'/value'))
        visit(expr,1,loc)
    for i,f in enumerate(fields):
        if f.get('visible_when'):check_condition(f['visible_when'],'fields.json',f'/fields/{i}/visible_when','complete',f['scope'])
    for i,rule in enumerate(rules):check_condition(rule.get('when',{}),'rules.json',f'/rules/{i}/when',rule.get('stage','complete'))
    for i,rule in enumerate(rules):
        for j,req in enumerate(rule.get('require',[])):
            ref=req.get('field')
            if not ref: continue
            field=declared_fields.get(ref)
            allowed=RULE_COMPLETE_FIELDS if rule.get('stage')=='complete' else RULE_EXPORT_FIELDS
            if rule.get('stage')=='complete':
                allowed |= frozenset(k for k in declared_fields if k.startswith('entry.fields.'))
            else:
                allowed |= frozenset(declared_fields)
            if ref not in catalog:
                d.append(_diag('RULE_FIELD_UNKNOWN',f'规则必填引用未声明字段：{ref}','rules.json',f'/rules/{i}/require/{j}/field'))
            elif ref not in allowed:
                d.append(_diag('RULE_FIELD_STAGE_INVALID',f'规则阶段不能引用该字段：{ref}','rules.json',f'/rules/{i}/require/{j}/field'))
    settings=scheme.get('settings',{})
    reviewer_required=settings.get('profile.reviewer_required',{}).get('fixed',settings.get('profile.reviewer_required',{}).get('default',SETTINGS['profile.reviewer_required']['default']))
    reviewer_presentation=settings.get('profile.reviewer_presentation',{}).get('fixed',settings.get('profile.reviewer_presentation',{}).get('default',SETTINGS['profile.reviewer_presentation']['default']))
    if reviewer_required is True and reviewer_presentation=='hidden':
        d.append(_diag('PROFILE_HIDDEN_REQUIRED','审核人为必填时不能隐藏其填写入口。','scheme.json','/settings/profile.reviewer_presentation'))
    output_ids=set()
    reserved_output_ids={'generic_overview','generic_attachments','generic_materials'}
    for i,o in enumerate(outputs):
        if o.get('when'):check_condition(o['when'],'outputs.json',f'/outputs/{i}/when','export')
        if o['id'] in reserved_output_ids:
            d.append(_diag('OUTPUT_ID_RESERVED',f'输出 ID 是核心保留名称：{o["id"]}','outputs.json',f'/outputs/{i}/id'))
        if o['id'] in output_ids:d.append(_diag('OUTPUT_ID_DUPLICATE',f'输出 ID 重复：{o["id"]}','outputs.json',f'/outputs/{i}'))
        output_ids.add(o['id'])
        if o.get('type') in ('docx','pdf_bundle') and 'title' not in o.get('group_by',[]):
            d.append(_diag('OUTPUT_TITLE_ISOLATION_REQUIRED','Word 与材料 PDF 必须按抬头分组。','outputs.json',f'/outputs/{i}/group_by'))
        if o.get('type')=='docx' and not o.get('template'):d.append(_diag('OUTPUT_TEMPLATE_REQUIRED','Word 输出必须指定模板。','outputs.json',f'/outputs/{i}'))
        if len(o.get('group_by',[]))>4:d.append(_diag('OUTPUT_GROUP_LIMIT','每个输出最多配置四项分组字段。','outputs.json',f'/outputs/{i}/group_by'))
        group_paths=list(o.get('group_by',[]))+list((o.get('attachments') or {}).get('group_by',[]))
        for group_path in group_paths:
            if not group_path.startswith('batch.fields.'): continue
            field=declared_fields.get(group_path)
            if field is None:
                d.append(_diag('OUTPUT_GROUP_FIELD_INVALID',f'分组字段未声明：{group_path}','outputs.json',f'/outputs/{i}/group_by'))
            elif field['scope']!='batch' or field['type']=='multiselect':
                d.append(_diag('OUTPUT_GROUP_FIELD_INVALID',f'分组字段必须是标量批次字段：{group_path}','outputs.json',f'/outputs/{i}/group_by'))
        if o.get('template') and not structural_only and o['template'] not in files:d.append(_diag('RESOURCE_REFERENCE_MISSING',f'找不到模板资源：{o["template"]}','outputs.json',f'/outputs/{i}/template'))
        if o.get('template') and not o['template'].startswith('templates/'):
            d.append(_diag('TEMPLATE_PATH_INVALID','模板必须位于 templates/ 目录。','outputs.json',f'/outputs/{i}/template'))
        for target in o.get('required_fields',[]):
            if target not in {f'{f["scope"]}.fields.{f["id"]}' for f in fields}:
                d.append(_diag('OUTPUT_FIELD_UNKNOWN',f'输出要求引用未声明字段：{target}','outputs.json',f'/outputs/{i}/required_fields'))
        output_roles=set((o.get('pdf') or {}).get('include_roles',[])) | set((o.get('attachments') or {}).get('roles',[]))
        for role_id in output_roles:
            if role_id not in role_ids:d.append(_diag('OUTPUT_MATERIAL_UNKNOWN',f'输出引用未声明材料角色：{role_id}','outputs.json',f'/outputs/{i}'))
        filename=o.get('filename','')
        if filename and ('/' in filename or '\\' in filename or filename.startswith('.')):
            d.append(_diag('OUTPUT_FILENAME_PATH','输出文件名模板不能包含目录路径。','outputs.json',f'/outputs/{i}/filename'))
        for var in re.findall(r'\{([^{}]+)\}',filename):
            if var not in FILENAME_FIELDS:
                d.append(_diag('OUTPUT_FILENAME_FIELD_UNKNOWN',f'文件名引用未允许的变量：{var}','outputs.json',f'/outputs/{i}/filename'))
        if re.search(r'\{[^{}]*[+*/()\[\]\\][^{}]*\}',filename):
            d.append(_diag('OUTPUT_FILENAME_EXPRESSION','文件名只支持白名单变量，不支持表达式。','outputs.json',f'/outputs/{i}/filename'))
    for s,v in scheme.get('settings',{}).items():
        if s not in SETTINGS:d.append(_diag('SETTING_UNREGISTERED',f'适配包不能设置未注册选项：{s}','scheme.json',f'/settings/{s}'));continue
        reg=SETTINGS[s]
        if type(v.get('editable')) is not bool or v.get('presentation') not in ('visible','advanced','hidden'):
            d.append(_diag('SETTING_DESCRIPTOR_INVALID','设置必须定义 editable 布尔值和有效 presentation。','scheme.json',f'/settings/{s}'))
        if 'fixed' in v and (v.get('editable') is not False or 'default' in v):d.append(_diag('SETTING_FIXED_INVALID','固定设置必须 editable=false，且不能同时提供 default。','scheme.json',f'/settings/{s}'))
        if v.get('editable') is True and 'default' not in v:d.append(_diag('SETTING_DEFAULT_REQUIRED','可编辑设置必须声明 default。','scheme.json',f'/settings/{s}'))
        val=v.get('fixed',v.get('default'))
        if 'fixed' in v or 'default' in v:
            from .resolver import _valid_setting
            if not _valid_setting(s,val):d.append(_diag('SETTING_VALUE_TYPE','设置值类型或范围不符合注册定义。','scheme.json',f'/settings/{s}'))
    seen_titles=set(); seen_title_ids=set()
    for i,title in enumerate(scheme.get('titles',[])):
        identity=(title.get('name','').strip().casefold(),(title.get('tax_id') or '').strip().upper())
        if identity in seen_titles:d.append(_diag('TITLE_DUPLICATE','完全相同的抬头与税号不能重复声明。','scheme.json',f'/titles/{i}'))
        if title.get('id') in seen_title_ids:d.append(_diag('TITLE_ID_DUPLICATE','抬头 ID 重复。','scheme.json',f'/titles/{i}/id'))
        seen_titles.add(identity);seen_title_ids.add(title.get('id'))
        if title.get('color') not in ('neutral','blue','green','amber','purple','red','teal'):d.append(_diag('TITLE_COLOR_INVALID','抬头颜色不在核心语义色列表中。','scheme.json',f'/titles/{i}/color'))
    title_ids=seen_title_ids
    import_defaults=scheme.get('import_defaults',{})
    from .resolver import _valid_setting
    for key,value in import_defaults.items():
        if key not in ('entry.default_title_id','entry.default_paid_to_invoice','entry.suggested_tags'):
            d.append(_diag('IMPORT_DEFAULT_UNKNOWN',f'导入默认值未注册：{key}','scheme.json',f'/import_defaults/{key}'))
        elif not _valid_setting(key,value):
            d.append(_diag('IMPORT_DEFAULT_INVALID',f'导入默认值类型无效：{key}','scheme.json',f'/import_defaults/{key}'))
        if key in scheme.get('settings',{}):
            d.append(_diag('IMPORT_DEFAULT_SHADOWED',f'导入默认值不能与同名方案设置重复：{key}','scheme.json',f'/import_defaults/{key}'))
    default_title=scheme.get('settings',{}).get('entry.default_title_id',{}).get('default',scheme.get('settings',{}).get('entry.default_title_id',{}).get('fixed',import_defaults.get('entry.default_title_id')))
    if default_title and default_title not in title_ids:d.append(_diag('SETTING_TITLE_UNKNOWN','默认抬头不在方案抬头列表中。','scheme.json','/settings/entry.default_title_id'))
    output_ids={o['id'] for o in outputs}
    for output_id in scheme.get('default_outputs',[]):
        if output_id not in output_ids:d.append(_diag('OUTPUT_DEFAULT_UNKNOWN',f'默认输出未定义：{output_id}','scheme.json','/default_outputs'))
    output_defaults=scheme.get('settings',{}).get('print.default_outputs',{})
    for output_id in output_defaults.get('fixed',output_defaults.get('default',[])):
        if output_id not in output_ids:
            d.append(_diag('OUTPUT_DEFAULT_UNKNOWN',f'默认输出未定义：{output_id}','scheme.json','/settings/print.default_outputs'))

    used={'checksums.json',*JSON_FILES}
    for o in outputs:
        if o.get('template'):used.add(o['template'])
    for n,b in files.items():
        if Path(n).suffix.lower() not in {'.json','.docx','.png','.jpg','.jpeg','.txt'}:d.append(_diag('RESOURCE_TYPE_UNSUPPORTED','包中只允许 JSON、DOCX、PNG、JPEG 和 TXT 文件。',n))
        if Path(n).suffix.lower()=='.docx':d.extend(_check_docx(n,b))
        if Path(n).suffix.lower()=='.json' and len(b)>LIMITS['json_bytes']:d.append(_diag('JSON_SIZE_LIMIT','JSON 文件超过 2 MiB。',n))
        if n not in used:d.append(_diag('RESOURCE_UNDECLARED',f'资源未被配置引用：{n}',n))
    declared=set(manifest.get('requires',{}).get('capabilities',[])); needed=set(required_capabilities(defn))
    for c in sorted(needed-declared):d.append(_diag('CAPABILITY_MISSING',f'清单缺少所需能力声明：{c}','manifest.json','/requires/capabilities'))
    for c in sorted(declared-set(CAPABILITIES)):d.append(_diag('CAPABILITY_UNSUPPORTED',f'适配包要求未知能力：{c}','manifest.json','/requires/capabilities'))
    return d


def validate_resolved_definition(definition):
    """Validate a locally composed canonical revision before it can be stored."""
    if not isinstance(definition, dict):
        raise AdapterValidationError([_diag('DEFINITION_INVALID','方案修订必须是对象。')])
    wrappers = {
        'manifest.json': definition.get('manifest'),
        'scheme.json': definition.get('scheme'),
        'fields.json': {'fields': definition.get('fields', [])},
        'materials.json': {'roles': definition.get('materials', [])},
        'rules.json': {'rules': definition.get('rules', [])},
        'outputs.json': {'outputs': definition.get('outputs', [])},
    }
    diagnostics=[]
    for filename,data in wrappers.items():
        if not isinstance(data,dict):
            diagnostics.append(_diag('DEFINITION_INVALID','方案修订中的配置结构无效。',filename))
            continue
        diagnostics.extend(_schema_validate(filename,data))
    if not diagnostics:
        diagnostics.extend(_domain_validate(definition,{},structural_only=True))
    if diagnostics:
        raise AdapterValidationError(diagnostics)
    return definition

def _parse(files):
    diagnostics=[]; parsed={}
    for fn in JSON_FILES:
        if fn not in files:
            if fn in ('manifest.json','scheme.json'):diagnostics.append(_diag('CONFIG_REQUIRED',f'缺少必需文件：{fn}',fn))
            continue
        if len(files[fn])>LIMITS['json_bytes']:
            diagnostics.append(_diag('JSON_SIZE_LIMIT','JSON 文件超过 2 MiB。',fn));continue
        try: parsed[fn]=json.loads(files[fn].decode('utf-8'),parse_constant=lambda token: (_ for _ in ()).throw(ValueError(f'非法数值：{token}')))
        except (UnicodeDecodeError,json.JSONDecodeError,ValueError) as e: diagnostics.append(_diag('JSON_INVALID',f'JSON 无法读取：{e}',fn));continue
        diagnostics.extend(_schema_validate(fn,parsed[fn]))
    return parsed,diagnostics

def load_package(path, *, verify_templates=True) -> AdapterPackage:
    files,diagnostics=_read_source(path)
    if Path(path).is_file() and 'checksums.json' not in files:
        diagnostics.append(_diag('CHECKSUMS_REQUIRED','适配包归档必须包含由打包工具生成的 checksums.json。','checksums.json'))
    if diagnostics: raise AdapterValidationError(diagnostics)
    parsed,diagnostics=_parse(files)
    if diagnostics: raise AdapterValidationError(diagnostics)
    if 'checksums.json' in files:
        try:
            sums=json.loads(files['checksums.json'].decode('utf-8'))
            if not isinstance(sums,dict) or any(not isinstance(k,str) or not isinstance(v,str) or not re.fullmatch(r'[0-9a-f]{64}',v) for k,v in sums.items()): sums=None
        except Exception: sums=None
        expected={n:hashlib.sha256(b).hexdigest() for n,b in files.items() if n!='checksums.json'}
        if sums != expected: diagnostics.append(_diag('CHECKSUM_MISMATCH','checksums.json 与包资源摘要不一致。','checksums.json'))
    def arr(fn,key):return parsed.get(fn,{}).get(key,[])
    material_overrides=arr('materials.json','roles')
    # Built-in roles exist by protocol default; teams configure requirements or add roles.
    materials=[]
    for role_id, base_role in BUILTIN_ROLES.items():
        configured=next((role for role in material_overrides if role.get('id')==role_id),None)
        materials.append({**base_role,**(configured or {})})
    materials.extend(role for role in material_overrides if role.get('id') not in BUILTIN_ROLES)
    definition={'manifest':parsed['manifest.json'],'scheme':parsed['scheme.json'],'fields':arr('fields.json','fields'),'materials':materials,'rules':arr('rules.json','rules'),'outputs':arr('outputs.json','outputs'),'effective_settings':{}}
    diagnostics.extend(_domain_validate(definition,files))
    if verify_templates:
        for output in definition['outputs']:
            template_name=output.get('template')
            if output.get('type')!='docx' or not template_name or template_name not in files:
                continue
            try:
                from tidoc_print.template_validation import validate_template
                with tempfile.TemporaryDirectory(prefix='tidoc-template-check-') as temporary_dir:
                    template_path=Path(temporary_dir)/'template.docx'
                    template_path.write_bytes(files[template_name])
                    template_diagnostics=validate_template(str(template_path),definition)
                for item in template_diagnostics:
                    item['file']=template_name
                diagnostics.extend(template_diagnostics)
            except (ImportError,ModuleNotFoundError):
                # The core remains usable without the optional renderer; template syntax is pending.
                package_warning=_diag('TEMPLATE_VALIDATION_UNAVAILABLE','未安装打印导出组件，已完成 DOCX 容器安全检查；模板变量检查待打印组件提供。',template_name,severity='warning')
                diagnostics.append(package_warning)
    if any(d.get('severity')!='warning' for d in diagnostics):raise AdapterValidationError([d for d in diagnostics if d.get('severity')!='warning'])
    digest_source={n:hashlib.sha256(b).hexdigest() for n,b in sorted(files.items()) if n!='checksums.json'}
    content_hash=hashlib.sha256(canonical_json(digest_source)).hexdigest()
    return AdapterPackage(definition,content_hash,files,[d for d in diagnostics if d.get('severity')=='warning'],str(path))

def validate_package(path) -> dict:
    try:
        p=load_package(path)
        return {'ok':True,'errors':[],'warnings':p.diagnostics,'package_id':p.package_id,'package_version':p.package_version,'content_hash':p.content_hash,'capabilities':required_capabilities(p.definition)}
    except AdapterValidationError as e:return {'ok':False,'errors':e.diagnostics,'warnings':[]}

def pack_package(source, out) -> Path:
    p=Path(source); target=Path(out)
    files,diags=_read_source(p)
    if diags:raise AdapterValidationError(diags)
    files.pop('checksums.json',None)
    files['checksums.json']=canonical_json({n:hashlib.sha256(b).hexdigest() for n,b in sorted(files.items())})
    target.parent.mkdir(parents=True,exist_ok=True)
    import tempfile
    fd,tmp=tempfile.mkstemp(prefix='.adapter-',suffix='.tmp',dir=target.parent);os.close(fd)
    try:
        with zipfile.ZipFile(tmp,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as z:
            for n,b in sorted(files.items()):z.writestr(n,b)
        load_package(tmp)
        os.replace(tmp,target)
    except Exception:
        try:os.unlink(tmp)
        except OSError:pass
        raise
    return target
