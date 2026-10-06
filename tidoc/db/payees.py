"""Whole payee objects; no per-field fallback between people/accounts."""
from __future__ import annotations
import uuid
from .adapters import now

FIELDS = ('name','personnel_id','contact','account_type','bank_name','account_number')
ALIASES = {'person_name':'name','student_id':'personnel_id','personnel_number':'personnel_id','phone':'contact','bank_card':'account_number'}


class PayeeRepo:
    def __init__(self, db):
        self.db = db

    def _values(self, values):
        values = {ALIASES.get(k,k):v for k,v in values.items()}
        unknown = set(values) - set(FIELDS)
        if unknown:
            raise ValueError('未知收款字段：' + ', '.join(sorted(unknown)))
        result = {}
        for key,value in values.items():
            if value is not None and not isinstance(value,str):
                raise ValueError('收款字段必须是字符串，人员编号与账号保留前导零。')
            result[key] = value if value is not None else ''
        if 'account_type' in result and result['account_type'] not in ('personal_bank','corporate_bank','none'):
            raise ValueError('收款账户类型无效。')
        return result

    def create(self, *, commit=True, **values):
        values = self._values(values)
        payee_id = uuid.uuid4().hex
        complete = {k:values.get(k, 'personal_bank' if k=='account_type' else '') for k in FIELDS}
        self.db.conn.execute('INSERT INTO payees VALUES(?,?,?,?,?,?,?,?,?)',
            (payee_id,*(complete[k] for k in FIELDS),now(),now()))
        if commit:
            self.db.conn.commit()
        return self.get(payee_id)

    def update(self, payee_id, *, commit=True, **values):
        self.get(payee_id)
        values = self._values(values)
        if values:
            self.db.conn.execute('UPDATE payees SET ' + ','.join(k+'=?' for k in values) + ',updated_at=? WHERE id=?',
                (*values.values(),now(),payee_id))
        if commit:
            self.db.conn.commit()
        return self.get(payee_id)

    def get(self, payee_id):
        row = self.db.conn.execute('SELECT * FROM payees WHERE id=?',(payee_id,)).fetchone()
        if not row:
            raise ValueError('收款对象不存在。')
        result = dict(row)
        result['personnel_number'] = result['personnel_id']
        result['phone'] = result['contact']
        return result

    def list(self):
        return [self.get(r[0]) for r in self.db.conn.execute('SELECT id FROM payees ORDER BY created_at,id')]

    def delete(self, payee_id, *, commit=True):
        self.get(payee_id)
        if self.db.conn.execute('SELECT 1 FROM scheme_payee_links WHERE payee_id=? UNION ALL SELECT 1 FROM batch_payee_links WHERE payee_id=?', (payee_id,payee_id)).fetchone():
            raise ValueError('收款对象仍被方案或批次使用，请先解除映射。')
        self.db.conn.execute('DELETE FROM payees WHERE id=?', (payee_id,))
        if commit:
            self.db.conn.commit()

    def set_mapping(self, scheme_id, profile_id, payee_id, *, commit=True):
        if not self.db.conn.execute('SELECT 1 FROM schemes WHERE id=?',(scheme_id,)).fetchone():
            raise ValueError('方案不存在。')
        if profile_id is not None and not self.db.conn.execute('SELECT 1 FROM profiles WHERE id=?',(profile_id,)).fetchone():
            raise ValueError('报账人不存在。')
        if payee_id:
            self.get(payee_id)
        self.db.conn.execute('DELETE FROM scheme_payee_links WHERE scheme_id=? AND profile_id IS ?', (scheme_id,profile_id))
        if payee_id:
            self.db.conn.execute('INSERT INTO scheme_payee_links VALUES(?,?,?,?)', (scheme_id,profile_id,payee_id,now()))
        if commit:
            self.db.conn.commit()
        return self.get(payee_id) if payee_id else None

    def set_default(self, scheme_id, payee_id, *, commit=True):
        return self.set_mapping(scheme_id,None,payee_id,commit=commit)

    def set_batch(self, batch_id, scheme_id, payee_id, *, commit=True):
        if payee_id:
            self.get(payee_id)
            self.db.conn.execute('INSERT INTO batch_payee_links VALUES(?,?,?) ON CONFLICT(batch_id,scheme_id) DO UPDATE SET payee_id=excluded.payee_id', (batch_id,scheme_id,payee_id))
        else:
            self.db.conn.execute('DELETE FROM batch_payee_links WHERE batch_id=? AND scheme_id=?',(batch_id,scheme_id))
        if commit:
            self.db.conn.commit()

    def get_default(self, scheme_id):
        return self.get_for_profile(scheme_id,None)

    def get_for_profile(self, scheme_id, profile_id):
        row = self.db.conn.execute('SELECT payee_id FROM scheme_payee_links WHERE scheme_id=? AND profile_id IS ?', (scheme_id,profile_id)).fetchone()
        return self.get(row[0]) if row else None

    def resolve(self, scheme_id, profile_id=None, payee_id=None, batch_id=None, mode='single'):
        if mode == 'none':
            return None
        if payee_id:
            return self.get(payee_id)
        if mode == 'by_claimant':
            return self.get_for_profile(scheme_id,profile_id) if profile_id else None
        if mode != 'single':
            raise ValueError('收款模式无效。')
        if batch_id:
            row = self.db.conn.execute('SELECT payee_id FROM batch_payee_links WHERE batch_id=? AND scheme_id=?',(batch_id,scheme_id)).fetchone()
            if row:
                return self.get(row[0])
        return self.get_default(scheme_id)
