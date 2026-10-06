"""Immutable packages/revisions and independent local scheme heads."""
from __future__ import annotations

import json
import uuid
from datetime import datetime


def now():
    return datetime.now().isoformat(timespec='microseconds')


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


class AdapterRepo:
    def __init__(self, db):
        self.db = db

    def get_package(self, content_hash=None, *, package_id=None, package_version=None):
        if content_hash:
            row = self.db.conn.execute('SELECT * FROM adapter_packages WHERE content_hash=?', (content_hash,)).fetchone()
        else:
            row = self.db.conn.execute('SELECT * FROM adapter_packages WHERE package_id=? AND package_version=?', (package_id, package_version)).fetchone()
        if not row:
            return None
        result = dict(row)
        result['definition'] = json.loads(result.pop('definition_json'))
        result['diagnostics'] = json.loads(result.pop('diagnostics_json'))
        return result

    def store_package(self, package, resource_path, source='local', commit=True):
        manifest = package.definition['manifest']
        old = self.get_package(package_id=manifest['package_id'], package_version=manifest['package_version'])
        if old:
            if old['content_hash'] != package.content_hash:
                raise ValueError('同一个包 ID 和版本对应不同内容，请增加版本或另存新包。')
            return old
        self.db.conn.execute('''INSERT INTO adapter_packages(content_hash,package_id,package_version,schema_version,
            source,resource_path,definition_json,diagnostics_json,installed_at) VALUES(?,?,?,?,?,?,?,?,?)''',
            (package.content_hash,manifest['package_id'],manifest['package_version'],manifest['schema_version'],
             source,str(resource_path),encode(package.definition),encode(package.diagnostics),now()))
        if commit:
            self.db.conn.commit()
        return self.get_package(package.content_hash)

    def store_revision(self, definition, content_hash, commit=True):
        from ..adapters.resolver import revision_hash
        from ..adapters.loader import validate_resolved_definition
        validate_resolved_definition(definition)
        revision_id = revision_hash(definition)
        row = self.db.conn.execute('SELECT definition_json FROM scheme_revisions WHERE revision_id=?', (revision_id,)).fetchone()
        if row and row[0] != encode(definition):
            raise ValueError('修订摘要冲突。')
        if not row:
            self.db.conn.execute('INSERT INTO scheme_revisions VALUES(?,?,?,?)', (revision_id,content_hash,encode(definition),now()))
        if commit:
            self.db.conn.commit()
        return revision_id

    def get_revision(self, revision_id):
        row = self.db.conn.execute('SELECT definition_json FROM scheme_revisions WHERE revision_id=?', (revision_id,)).fetchone()
        if not row:
            raise ValueError('方案修订不存在。')
        return json.loads(row[0])

    def revision_record(self, revision_id):
        row = self.db.conn.execute('SELECT * FROM scheme_revisions WHERE revision_id=?', (revision_id,)).fetchone()
        if not row:
            raise ValueError('方案修订不存在。')
        return dict(row)

    def create_scheme(self, name, revision_id, overrides=None, is_default=False, commit=True):
        if not str(name).strip():
            raise ValueError('方案名称不能为空。')
        scheme_id = uuid.uuid4().hex
        if is_default:
            self.db.conn.execute('UPDATE schemes SET is_default=0 WHERE is_default=1')
        self.db.conn.execute('INSERT INTO schemes VALUES(?,?,?,?,?,0,?,?)',
            (scheme_id,str(name).strip(),revision_id,encode(overrides or {}),int(is_default),now(),now()))
        self.link_revision(scheme_id,revision_id,commit=False)
        if commit:
            self.db.conn.commit()
        return self.get_scheme(scheme_id)

    def link_revision(self, scheme_id, revision_id, commit=True):
        self.db.conn.execute('INSERT OR IGNORE INTO scheme_revision_links VALUES(?,?,?)', (scheme_id,revision_id,now()))
        if commit:
            self.db.conn.commit()

    def get_scheme(self, scheme_id=None):
        if scheme_id is None:
            row = self.db.conn.execute('SELECT * FROM schemes WHERE is_default=1 AND disabled=0').fetchone()
        else:
            row = self.db.conn.execute('SELECT * FROM schemes WHERE id=?', (scheme_id,)).fetchone()
        if not row:
            raise ValueError('报账方案不存在。')
        value = dict(row)
        value['scheme_id'] = value['id']
        value['revision_id'] = value['current_revision_id']
        value['overrides'] = json.loads(value.pop('overrides_json'))
        value['is_default'] = bool(value['is_default'])
        value['disabled'] = bool(value['disabled'])
        value['definition'] = self.get_revision(value['current_revision_id'])
        manifest = value['definition']['manifest']
        value['package_id'] = manifest['package_id']
        value['package_version'] = manifest['package_version']
        return value

    def list_schemes(self, include_disabled=False):
        sql = 'SELECT id FROM schemes' + ('' if include_disabled else ' WHERE disabled=0') + ' ORDER BY is_default DESC,created_at,id'
        return [self.get_scheme(r[0]) for r in self.db.conn.execute(sql)]

    def default_binding(self):
        scheme = self.get_scheme()
        return scheme['id'],scheme['current_revision_id']

    def set_current_revision(self, scheme_id, revision_id, overrides=None, expected_revision=None, commit=True):
        scheme = self.get_scheme(scheme_id)
        if expected_revision is not None and scheme['current_revision_id'] != expected_revision:
            raise ValueError('方案已经发生变化，请刷新后重新确认。')
        self.link_revision(scheme_id,revision_id,commit=False)
        self.db.conn.execute('UPDATE schemes SET current_revision_id=?,overrides_json=?,updated_at=? WHERE id=?',
            (revision_id,encode(scheme['overrides'] if overrides is None else overrides),now(),scheme_id))
        if commit:
            self.db.conn.commit()
        return self.get_scheme(scheme_id)

    def set_default(self, scheme_id, commit=True):
        if self.get_scheme(scheme_id)['disabled']:
            raise ValueError('停用方案不能设为默认。')
        self.db.conn.execute('UPDATE schemes SET is_default=0 WHERE is_default=1')
        self.db.conn.execute('UPDATE schemes SET is_default=1,updated_at=? WHERE id=?', (now(),scheme_id))
        if commit:
            self.db.conn.commit()
        return self.get_scheme(scheme_id)

    def disable(self, scheme_id, commit=True):
        if self.get_scheme(scheme_id)['is_default']:
            raise ValueError('请先选择另一个默认方案，再停用当前方案。')
        self.db.conn.execute('UPDATE schemes SET disabled=1,updated_at=? WHERE id=?', (now(),scheme_id))
        if commit:
            self.db.conn.commit()
        return self.get_scheme(scheme_id)

    def enable(self, scheme_id, commit=True):
        self.get_scheme(scheme_id)
        self.db.conn.execute('UPDATE schemes SET disabled=0,updated_at=? WHERE id=?', (now(),scheme_id))
        if commit:
            self.db.conn.commit()
        return self.get_scheme(scheme_id)

    def revision_history(self, scheme_id):
        return [dict(r) for r in self.db.conn.execute('''SELECT r.revision_id,r.content_hash,r.created_at
            FROM scheme_revisions r JOIN scheme_revision_links l ON l.revision_id=r.revision_id
            WHERE l.scheme_id=? ORDER BY l.created_at DESC''', (scheme_id,))]
