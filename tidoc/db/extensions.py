"""Typed entity values in revision-derived namespaces, with audit history."""
from __future__ import annotations
from contextlib import nullcontext
import hashlib
import json
from decimal import Decimal
from .adapters import AdapterRepo, encode, now

OWNER_TABLES = {'scheme':'schemes','payee':'payees','entry':'entries','batch':'batches','export':'export_jobs'}


class ExtensionRepo:
    def __init__(self, db):
        self.db = db

    def check_owner(self, scope, owner_id, scheme_id):
        table = OWNER_TABLES.get(scope)
        if not table:
            raise ValueError('附加字段作用域无效。')
        if not self.db.conn.execute('SELECT 1 FROM '+table+' WHERE id=?',(owner_id,)).fetchone():
            raise ValueError('附加字段所属记录不存在。')
        if scope == 'scheme' and owner_id != scheme_id:
            raise ValueError('方案字段所属记录与方案不一致。')
        if scope == 'entry':
            row = self.db.conn.execute('SELECT scheme_id FROM entries WHERE id=?',(owner_id,)).fetchone()
            if row[0] and row[0] != scheme_id:
                raise ValueError('条目不属于指定方案。')

    def list_values(self, scope, owner_id, scheme_id, package_id=None, revision_id=None):
        sql = 'SELECT * FROM extension_values WHERE scope=? AND owner_id=? AND scheme_id=?'
        args = [scope,owner_id,scheme_id]
        if package_id is not None:
            sql += ' AND package_id=?'
            args.append(package_id)
        rows = self.db.conn.execute(sql+' ORDER BY package_id,field_id',args)
        result = []
        for row in rows:
            value = dict(row)
            value['value'] = json.loads(value.pop('value_json'))
            result.append(value)
        return result

    def get_values(self, scope, owner_id, scheme_id, package_id, revision_id=None):
        rows = self.list_values(scope,owner_id,scheme_id,package_id)
        if revision_id:
            definition = AdapterRepo(self.db).get_revision(revision_id)
            fields = {f['id']:f for f in definition['fields'] if f['scope']==scope}
            valid = []
            for row in rows:
                source = AdapterRepo(self.db).get_revision(row['definition_revision_id'])
                old = next((f for f in source['fields'] if f['id']==row['field_id'] and f['scope']==scope),None)
                current = fields.get(row['field_id'])
                if old and current and old['type']==current['type']:
                    from ..adapters.policy import validate_field_value
                    try:
                        validate_field_value(current,row['value'])
                        valid.append(row)
                    except ValueError:
                        pass
            rows = valid
        return {r['field_id']:r['value'] for r in rows}

    def version(self, scope, owner_id, scheme_id, package_id=None):
        return hashlib.sha256(encode(self.list_values(scope,owner_id,scheme_id,package_id)).encode()).hexdigest()

    def save_values(self, scope, owner_id, scheme_id, revision_id, values, actor_id='', commit=True):
        self.check_owner(scope,owner_id,scheme_id)
        if not isinstance(values,dict):
            raise ValueError('附加字段值必须是对象。')
        definition = AdapterRepo(self.db).get_revision(revision_id)
        if not self.db.conn.execute('SELECT 1 FROM scheme_revision_links WHERE scheme_id=? AND revision_id=?',(scheme_id,revision_id)).fetchone():
            raise ValueError('该方案没有指定修订。')
        package_id = definition['manifest']['package_id']
        fields = {f['id']:f for f in definition['fields'] if f['scope']==scope}
        from ..adapters.policy import validate_field_value
        normalized = {}
        for field_id,value in values.items():
            if field_id not in fields:
                raise ValueError('当前修订没有字段：'+field_id)
            # Null is an explicit empty value, not deletion/inheritance.
            parsed = validate_field_value(fields[field_id],value)
            normalized[field_id] = format(parsed,'f') if isinstance(parsed,Decimal) else parsed
        with self.db.transaction() if commit else nullcontext():
            old = self.get_values(scope,owner_id,scheme_id,package_id)
            for field_id,value in normalized.items():
                if field_id in old and old[field_id] == value:
                    continue
                self.record_history(scope,owner_id,scheme_id,field_id,old.get(field_id),value,
                                    revision_id,package_id,actor_id=actor_id)
                self.db.conn.execute('''INSERT INTO extension_values VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(scope,owner_id,scheme_id,package_id,field_id) DO UPDATE SET
                    definition_revision_id=excluded.definition_revision_id,value_json=excluded.value_json,updated_at=excluded.updated_at''',
                    (scope,owner_id,scheme_id,package_id,field_id,revision_id,encode(value),now()))
        # transaction commits only when this method was not called in another transaction.
        return self.get_values(scope,owner_id,scheme_id,package_id,revision_id)

    def record_history(self, scope, owner_id, scheme_id, field_id, old_value, new_value,
                       revision_id=None, package_id='', kind='value', actor_id=''):
        self.db.conn.execute('''INSERT INTO extension_history(scope,owner_id,scheme_id,package_id,field_id,
            definition_revision_id,kind,old_value_json,new_value_json,actor_id,changed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
            (scope,owner_id,scheme_id,package_id,field_id,revision_id,kind,encode(old_value),encode(new_value),actor_id,now()))

    def history(self, scope, owner_id, scheme_id=None):
        sql = 'SELECT * FROM extension_history WHERE scope=? AND owner_id=?'
        params = [scope,owner_id]
        if scheme_id:
            sql += ' AND scheme_id=?'
            params.append(scheme_id)
        result=[]
        for row in self.db.conn.execute(sql+' ORDER BY id',params):
            value=dict(row)
            value['old_value']=json.loads(value.pop('old_value_json'))
            value['new_value']=json.loads(value.pop('new_value_json'))
            result.append(value)
        return result
