"""Frozen export snapshots and retained revision references."""
from __future__ import annotations
import json
import uuid
from .adapters import encode, now


class ExportJobRepo:
    def __init__(self, db):
        self.db=db

    def create(self, snapshot=None, revision_ids=None, resources=None, options=None, job_id=None, **kwargs):
        job_id=job_id or uuid.uuid4().hex
        snapshot = snapshot if snapshot is not None else kwargs.get('context',{})
        status=kwargs.get('status','planned')
        with self.db.transaction():
            self.db.conn.execute('INSERT INTO export_jobs VALUES(?,?,?,?,?,?,?,?,?)',
                (job_id,status,encode(snapshot),encode(resources or []),encode(options or {}),encode(kwargs.get('files',[])),encode(kwargs.get('diagnostics',[])),now(),now()))
            for revision in set(revision_ids or kwargs.get('revisions',[])):
                self.db.conn.execute('INSERT INTO export_job_revisions VALUES(?,?)',(job_id,revision))
        return self.get(job_id)

    def get(self, job_id):
        row=self.db.conn.execute('SELECT * FROM export_jobs WHERE id=?',(job_id,)).fetchone()
        if not row:
            raise ValueError('导出任务不存在。')
        result=dict(row)
        result['job_id']=result['id']
        for key in ('snapshot','resources','options','files','diagnostics'):
            result[key]=json.loads(result.pop(key+'_json'))
        result['revision_ids']=[r[0] for r in self.db.conn.execute('SELECT revision_id FROM export_job_revisions WHERE job_id=? ORDER BY revision_id',(job_id,))]
        return result

    def list(self, limit=100):
        return [self.get(r[0]) for r in self.db.conn.execute('SELECT id FROM export_jobs ORDER BY created_at DESC,id LIMIT ?',(min(max(int(limit),1),1000),))]

    def update(self, job_id, status=None, files=None, diagnostics=None, **kwargs):
        old=self.get(job_id)
        transitions={'planned':{'planned','running','failed','cancelled','completed'},'running':{'running','completed','failed','cancelled'},'completed':{'completed'},'failed':{'failed'},'cancelled':{'cancelled'}}
        if status and status not in transitions[old['status']]:
            raise ValueError('导出任务状态变更无效。')
        fields={}
        if status:
            fields['status']=status
        for key,val in [('files',files),('diagnostics',diagnostics)]+[(k,v) for k,v in kwargs.items() if k in ('snapshot','resources','options')]:
            if val is not None:
                if old['status']=='completed':
                    raise ValueError('完成任务的快照和结果不可修改。')
                fields[key+'_json']=encode(val)
        if fields:
            fields['updated_at']=now()
            with self.db.transaction():
                self.db.conn.execute('UPDATE export_jobs SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',(*fields.values(),job_id))
        return self.get(job_id)

    def complete(self, job_id, files, diagnostics=None):
        return self.update(job_id,status='completed',files=files,diagnostics=diagnostics or [])

    def fail(self, job_id, diagnostics):
        return self.update(job_id,status='failed',diagnostics=diagnostics)

    def delete(self, job_id):
        with self.db.transaction():
            self.db.conn.execute('DELETE FROM export_jobs WHERE id=?',(job_id,))
