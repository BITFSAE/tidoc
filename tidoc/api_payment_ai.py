"""Opt-in vision settings and background recognition bridge."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
import hashlib
import json
from pathlib import Path
import threading
import time
import uuid

from .api_shared import _guard
from .services import payment_ai as vision


class PaymentAiApi:
    def _ensure_payment_ai_idle(self):
        with self._payment_ai_lock:
            if any(not job['done'] for job in self._payment_ai_jobs.values()):
                raise ValueError('请等待付款截图识别完成后迁移数据')

    def _payment_ai_config(self):
        try:
            return vision.validate_config(json.loads(self._preference_value(vision.CONFIG_KEY, "{}")))
        except (ValueError, TypeError):
            return dict(vision.DEFAULT_CONFIG)

    def _payment_ai_key_slot(self, cfg):
        return vision.KEY_PREFIX + hashlib.sha256((cfg['provider']+'\n'+cfg['base_url']).encode()).hexdigest()

    def _payment_ai_key(self, cfg):
        return self._preference_value(self._payment_ai_key_slot(cfg))

    def _payment_ai_enabled(self) -> bool:
        cfg = self._payment_ai_config()
        return cfg['enabled'] and bool(self._payment_ai_key(cfg))

    def _refresh_payment_ai_check(self, entry_id):
        # With recognition off, screenshots are filled in by hand; no reminder to re-run it.
        if self._payment_ai_enabled():
            self.entries.refresh_payment_check(entry_id)
        else:
            self.entries.clear_payment_check(entry_id)

    def _payment_ai_version(self):
        cfg = self._payment_ai_config()
        facts = [cfg[k] for k in ('provider', 'base_url', 'model', 'requirements')]
        return vision.VERSION+":"+hashlib.sha256(vision.canonical(facts)).hexdigest()[:12]

    @_guard
    def payment_ai_settings(self):
        cfg = self._payment_ai_config()
        pricing = vision.price_info(cfg)
        # Prior usage determines a per-image estimate. Before that use an explicit token budget.
        try:
            stats = json.loads(self._preference_value('tidoc.paymentAi.usage', '{}'))
        except ValueError:
            stats = {}
        sample = stats.get(self._payment_ai_client.scope(cfg, self._payment_ai_key(cfg)), {})
        count = sample.get('count', 0)
        tokens = {'prompt_tokens': round(sample.get('input', 0)/count) if count else 3000,
                  'completion_tokens': round(sample.get('output', 0)/count) if count else 150}
        cost = vision.usage_cost(tokens, pricing)
        return {**cfg, 'key_configured': bool(self._payment_ai_key(cfg)), 'pricing': pricing,
                'estimated_per_100': None if cost is None else cost*100,
                'estimate_basis': 'recent' if count else 'budget',
                'total_estimated_cost': sum(x.get('cost', 0) for x in stats.values()),
                'total_calls': sum(x.get('count', 0) for x in stats.values())}

    @_guard
    def save_payment_ai_settings(self, values=None, key=None, clear_key=False):
        cfg = vision.validate_config({**self._payment_ai_config(), **(values or {})})
        with self._payment_ai_lock:
            active = any(not job['done'] for job in self._payment_ai_jobs.values())
            if active and cfg['enabled']:
                raise ValueError('请等待识别完成后修改设置')
            if not cfg['enabled']:
                for job in self._payment_ai_jobs.values():
                    if not job['done']:
                        job['cancelled'] = True
        slot = self._payment_ai_key_slot(cfg)
        if key is not None and (not isinstance(key, str) or len(key) > 1000 or any(c.isspace() for c in key.strip())):
            raise ValueError('Key 格式有误')
        stored = '' if clear_key else (key.strip() if key else self._preference_value(slot))
        if cfg['enabled'] and not stored:
            raise ValueError('请填写 Key')
        # Key and settings change together; _set_preference_value would commit them separately.
        with self.db.transaction():
            self.db.conn.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)', (slot, stored))
            self.db.conn.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)', (vision.CONFIG_KEY, json.dumps(cfg, ensure_ascii=False)))
        return self.payment_ai_settings()['data']

    def _record_payment_ai_usage(self):
        with self._payment_ai_client._lock:
            events = self._payment_ai_client.usage_events[:]
            self._payment_ai_client.usage_events.clear()
        if not events:
            return
        stats = json.loads(self._preference_value('tidoc.paymentAi.usage', '{}'))
        for event in events:
            row = stats.setdefault(event['scope'], {'count': 0, 'input': 0, 'output': 0, 'cost': 0})
            row['count'] += 1
            row['input'] += event['usage'].get('prompt_tokens', 0)
            row['output'] += event['usage'].get('completion_tokens', 0)
            row['cost'] += event['cost'] or 0
        self._set_preference_value('tidoc.paymentAi.usage', json.dumps(stats))

    def _payment_ai_cached(self, path):
        """Read the local result cache only; uploads happen in background jobs, never under the API lock."""
        cfg = self._payment_ai_config()
        try:
            return self._payment_ai_client.cached(path, cfg, self._payment_ai_key(cfg))
        except OSError:
            return None

    def _prepared_payment_ai_value(self, job_id, att):
        if not job_id or not self._payment_ai_enabled():
            return None
        cfg = self._payment_ai_config()
        scope = self._payment_ai_client.scope(cfg, self._payment_ai_key(cfg))
        with self._payment_ai_lock:
            job = self._payment_ai_jobs.get(job_id)
            if not job or job['scope'] != scope:
                return None
            return next((dict(row) for row in job['results']
                         if row['digest'] == att['sha256'] and not row.get('cancelled')), None)

    def _save_payment_ai_result(self, entry_id, att, value, version=None):
        result = value.get('result') or {}
        amount = result.get('amount') or ''
        # An advisory about dark mode does not turn a valid payment into a review failure.
        status = 'review' if amount and result.get('needs_review') else 'recognized' if amount else 'unrecognized'
        if value.get('error'):
            status = 'error'
        self.attachments.set_recognition(att['id'], version or self._payment_ai_version(), status, amount,
                                         value.get('error') or value.get('message', ''))
        self.entries.refresh_payment_check(entry_id)
        return amount

    @_guard
    def start_payment_ai(self, paths=None, entry_ids=None):
        cfg = self._payment_ai_config()
        key = self._payment_ai_key(cfg)
        if not cfg['enabled'] or not key:
            raise ValueError('请先在设置中开启付款截图 AI 识别')
        tasks, skipped, version = [], 0, self._payment_ai_version()
        if entry_ids is not None:
            for eid in dict.fromkeys(entry_ids):
                entry = self.entries.get(eid)
                if not entry:
                    continue
                for item in entry.get('attachments') or []:
                    if item['type'] != 'payment_screenshot':
                        continue
                    if item.get('recognition_version') == version and item.get('recognition_status') not in {'error', 'unrecognized'}:
                        skipped += 1
                        continue
                    att = self.attachments.get(item['id'])
                    tasks.append({'path': att['abs_path'], 'entry_id': eid, 'att_id': att['id'], 'sha': att['sha256']})
        else:
            tasks = [{'path': str(Path(p).resolve())} for p in dict.fromkeys(paths or [])]
        if len(tasks) > 10000:
            raise ValueError('一次最多识别 10000 张')
        for task in tasks:
            if Path(task['path']).suffix.lower() not in {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif'}:
                raise ValueError('请选择图片文件')
        for task in tasks:
            task['sha'] = hashlib.sha256(Path(task['path']).read_bytes()).hexdigest()
        job_id = uuid.uuid4().hex
        job = {'id': job_id, 'total': len(tasks), 'completed': 0, 'recognized': 0, 'failed': 0,
               'unrecognized': 0, 'cached': 0, 'cancelled_count': 0, 'skipped_current': skipped, 'cost': 0.0,
               'unknown_cost': False, 'states': {}, 'results': [], 'done': False,
               'cancelled': False, 'scope': self._payment_ai_client.scope(cfg, key), 'started': time.monotonic()}
        with self._payment_ai_lock:
            for old in list(self._payment_ai_jobs):
                if len(self._payment_ai_jobs) < 20:
                    break
                if self._payment_ai_jobs[old]['done']:
                    del self._payment_ai_jobs[old]
            self._payment_ai_jobs[job_id] = job
        root, client = self.data_root.root, self._payment_ai_client

        def worker(index, task):
            def stage(name):
                with self._payment_ai_lock:
                    if job['cancelled']:
                        raise ValueError('已取消')
                    job['states'][index] = name
            try:
                stage('queued')
                value = client.recognize(task['path'], cfg, key, stage)
                if value.get('digest', task['sha']) != task['sha']:
                    raise ValueError('图片已变化，请重试')
            except Exception as exc:
                # Only client-owned errors reach the UI. File errors must not reveal request data.
                message = str(exc) if type(exc) is ValueError else '无法读取图片或识别失败'
                value = {'error': message, 'result': None, 'cost': None, 'cached': False}
                with self._payment_ai_lock:
                    if job['cancelled']:
                        value['cancelled'] = True
            if 'att_id' in task and not value.get('cancelled'):
                with self._api_lock:
                    if self.data_root.root == root and self._payment_ai_config()['enabled']:
                        current = self.attachments.get(task['att_id'])
                        if current and current['type'] == 'payment_screenshot' and current['sha256'] == task['sha']:
                            self._save_payment_ai_result(task['entry_id'], current, value, version)
            with self._payment_ai_lock:
                job['states'].pop(index, None)
                job['completed'] += 1
                job['cancelled_count'] += bool(value.get('cancelled'))
                job['failed'] += bool(value.get('error')) and not value.get('cancelled')
                job['recognized'] += bool((value.get('result') or {}).get('amount'))
                job['unrecognized'] += not value.get('error') and not (value.get('result') or {}).get('amount')
                job['cached'] += bool(value.get('cached'))
                job['cost'] += value.get('cost') or 0
                job['unknown_cost'] |= value.get('cost') is None
                job['results'].append({'path': task['path'], 'digest': task['sha'], **value})

        def run():
            try:
                with ThreadPoolExecutor(max_workers=cfg['concurrency'], thread_name_prefix='payment-ai') as pool:
                    futures = [pool.submit(worker, i, t) for i, t in enumerate(tasks)]
                    for future in as_completed(futures):
                        future.result()
            except Exception:
                with self._payment_ai_lock:
                    job['error'] = '识别任务中断，请重试'
            finally:
                with self._api_lock:
                    if self.data_root.root == root:
                        self._record_payment_ai_usage()
                with self._payment_ai_lock:
                    job['done'] = True
        threading.Thread(target=run, daemon=True, name='payment-ai-job').start()
        return {'id': job_id, 'total': len(tasks)}

    def payment_ai_progress(self, job_id):
        # Deliberately independent of the DB/API lock, including during a single attachment import.
        with self._payment_ai_lock:
            job = self._payment_ai_jobs.get(str(job_id))
            if job is None:
                return {'ok': False, 'error': '识别任务不存在'}
            # Per-image results can be thousands of rows; send them once, when the job is done.
            result = copy.deepcopy({k: v for k, v in job.items() if k != 'results' or job['done']})
            result.setdefault('results', [])
        result.pop('scope', None)
        states = result.pop('states')
        result['active'] = sum(s in {'recognizing', 'retrying', 'preparing'} for s in states.values())
        result['retrying'] = sum(s == 'retrying' for s in states.values())
        result['queued'] = result['total']-result['completed']-result['active']
        result['elapsed'] = round(time.monotonic()-result.pop('started'), 1)
        return {'ok': True, 'data': result}

    def cancel_payment_ai(self, job_id):
        with self._payment_ai_lock:
            job = self._payment_ai_jobs.get(str(job_id))
            if job:
                job['cancelled'] = True
        return {'ok': True, 'data': {'cancelled': bool(job)}}
