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

from .api_shared import _guard, mask_secret
from decimal import Decimal, InvalidOperation
from .services import payment_ai as vision


def _same_money(a, b) -> bool:
    try:
        return Decimal(str(a)).quantize(Decimal("0.01")) == Decimal(str(b)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return False


# 付款截图提醒的生成规则；规则或本机识别设置变了，就给所有带付款截图的条目重算一次提醒。
PAYMENT_CHECK_RULES = "payment-check-3"
PAYMENT_CHECK_STATE_KEY = "tidoc.paymentAi.checkState"


def payment_result_current(attachment, version) -> bool:
    """这张截图已有可用结论、不必再上传：本机按当前设置识别过，或随绑定包带来的同规则结果。"""
    saved = str(attachment.get("recognition_version") or "")
    if attachment.get("recognition_status") in {"", None, "error", "unrecognized"}:
        return False
    imported = saved.endswith(":imported") and saved.split(":")[0] == version.split(":")[0]
    return saved == version or imported


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

    def _payment_ai_available(self) -> bool:
        """Key 已保存即可在「云识别」里手动识别；开关只决定添加截图时是否自动识别。"""
        cfg = self._payment_ai_config()
        return bool(cfg['base_url'] and cfg['model'] and self._payment_ai_key(cfg))

    def _payment_ai_enabled(self) -> bool:
        return self._payment_ai_config()['enabled'] and self._payment_ai_available()

    def _refresh_payment_ai_check(self, entry_id):
        # Without a key, screenshots are filled in by hand: no reminders at all. With automatic
        # recognition off, keep the results already recognized but do not ask to re-run the rest.
        if self._payment_ai_available():
            self.entries.refresh_payment_check(entry_id, remind_pending=self._payment_ai_enabled())
        else:
            self.entries.clear_payment_check(entry_id)

    def _sync_payment_checks(self):
        state = f"{PAYMENT_CHECK_RULES}|{int(self._payment_ai_available())}|{int(self._payment_ai_enabled())}"
        if self._preference_value(PAYMENT_CHECK_STATE_KEY) == state:
            return
        rows = self.db.conn.execute(
            "SELECT DISTINCT entry_id FROM attachments WHERE type = 'payment_screenshot'"
        ).fetchall()
        for row in rows:
            self._refresh_payment_ai_check(row["entry_id"])
        self._set_preference_value(PAYMENT_CHECK_STATE_KEY, state)

    def _payment_ai_version(self):
        cfg = self._payment_ai_config()
        facts = vision.config_facts(cfg)
        return vision.VERSION+":"+hashlib.sha256(vision.canonical(facts)).hexdigest()[:12]

    @_guard
    def payment_ai_settings(self):
        cfg = self._payment_ai_config()
        pricing = vision.price_info(cfg)
        # 每张的估算按本机实际用量：优先用当前设置的记录，没有时用全部记录，再没有才用预设用量。
        # 提示词每次相同，服务商会按缓存价计费，所以缓存命中的部分单独按缓存价算。
        try:
            stats = json.loads(self._preference_value('tidoc.paymentAi.usage', '{}'))
        except ValueError:
            stats = {}
        sample = stats.get(self._payment_ai_client.scope(cfg, self._payment_ai_key(cfg)), {})
        count = sample.get('count', 0)
        if count and 'cached' not in sample and pricing['known']:
            # 早期记录没有缓存 Token，直接用当时记下的实际费用平均。
            cost, basis = sample.get('cost', 0)/count, 'recent'
        else:
            if not count:
                sample = {k: sum(row.get(k, 0) for row in stats.values()) for k in ('count', 'input', 'cached', 'output')}
                count = sample['count']
            tokens = {'prompt_tokens': round(sample['input']/count) if count else vision.BUDGET_INPUT,
                      'prompt_cache_hit_tokens': round(sample['cached']/count) if count else 0,
                      'completion_tokens': round(sample['output']/count) if count else vision.BUDGET_OUTPUT}
            cost, basis = vision.usage_cost(tokens, pricing), 'recent' if count else 'budget'
        key = self._payment_ai_key(cfg)
        return {**cfg, 'key_configured': bool(key), 'key_masked': mask_secret(key),
                'default_prompt': vision.DEFAULT_PROMPT, 'balance_supported': vision.supports_balance(cfg),
                'available': self._payment_ai_available(), 'pricing': pricing,
                'estimated_per_100': None if cost is None else cost*100,
                'estimate_basis': basis, 'estimate_count': count,
                'budget_tokens': [vision.BUDGET_INPUT, vision.BUDGET_OUTPUT],
                'total_estimated_cost': sum(x.get('cost', 0) for x in stats.values()),
                'total_calls': sum(x.get('count', 0) for x in stats.values())}

    @_guard
    def save_payment_ai_settings(self, values=None, key=None, clear_key=False):
        old = self._payment_ai_config()
        values = values or {}
        cfg = vision.validate_config({**old, **values})
        if cfg['provider'] == 'custom':
            cfg['custom_base_url'], cfg['custom_model'] = cfg['base_url'], cfg['model']
        # 只切换自动识别开关不影响正在进行的识别；换服务商、Key 等要等识别结束。
        changed = {k for k in cfg if cfg[k] != old[k]} - {'enabled'}
        with self._payment_ai_lock:
            active = any(not job['done'] for job in self._payment_ai_jobs.values())
            if active and (changed or key or clear_key):
                raise ValueError('请等待识别完成后修改设置')
        slot = self._payment_ai_key_slot(cfg)
        if key is not None and (not isinstance(key, str) or len(key) > 1000 or any(c.isspace() for c in key.strip())):
            raise ValueError('Key 格式有误')
        if key and not cfg['base_url']:
            raise ValueError('请先填写接口地址')
        stored = '' if clear_key else (key.strip() if key else self._preference_value(slot))
        # Key 按服务商和接口地址分开保存。换到还没有 Key 的服务商、或清除 Key 时，自动识别随之关闭，
        # 不会在之后补填 Key 时自己重新开始上传；只有用户这次明确要开启时才报缺什么。
        missing = '请填写 Key' if not stored else '请填写接口地址和模型' if not (cfg['base_url'] and cfg['model']) else ''
        if cfg['enabled'] and missing:
            if values.get('enabled') is True and not clear_key:
                raise ValueError(missing)
            cfg['enabled'] = False
        # Key and settings change together; _set_preference_value would commit them separately.
        with self.db.transaction():
            self.db.conn.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)', (slot, stored))
            self.db.conn.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)', (vision.CONFIG_KEY, json.dumps(cfg, ensure_ascii=False)))
        # 清除 Key、开关自动识别后，其他条目上的付款截图提醒也跟着变。
        self._sync_payment_checks()
        return self.payment_ai_settings()['data']

    def payment_ai_balance(self):
        """DeepSeek 账户余额。只在用户点击时联网；请求在 API 锁外进行，不卡住其他操作。"""
        with self._api_lock:
            cfg = self._payment_ai_config()
            key = self._payment_ai_key(cfg)
        if not vision.supports_balance(cfg):
            return {'ok': False, 'error': '只有 DeepSeek 官方接口可以查询余额'}
        if not key:
            return {'ok': False, 'error': '请先保存 Key'}
        try:
            return {'ok': True, 'data': vision.fetch_balance(key)}
        except ValueError as exc:
            return {'ok': False, 'error': str(exc)}

    def _record_payment_ai_usage(self):
        with self._payment_ai_client._lock:
            events = self._payment_ai_client.usage_events[:]
            self._payment_ai_client.usage_events.clear()
        if not events:
            return
        stats = json.loads(self._preference_value('tidoc.paymentAi.usage', '{}'))
        for event in events:
            row = stats.setdefault(event['scope'], {'count': 0, 'input': 0, 'cached': 0, 'output': 0, 'cost': 0})
            row['count'] += 1
            row['input'] += event['usage'].get('prompt_tokens', 0)
            if 'cached' in row:
                row['cached'] += vision.cached_tokens(event['usage'])
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
        # 说明按当前规则从结果重新生成，缓存里的旧说明不再沿用。
        message = vision.result_message(result) if result else value.get('message', '')
        self.attachments.set_recognition(att['id'], version or self._payment_ai_version(), status, amount,
                                         value.get('error') or message)
        self.entries.refresh_payment_check(entry_id)
        return amount

    def _settle_paid_from_payment_ai(self, entry_id) -> str:
        """识别合计可直接作实付时写入；已填的实付和对不上发票金额的合计都不自动写。"""
        from .db.entries import VALUE_SOURCE_PAYMENT_AI, VALUE_SOURCE_PAYMENT_OCR
        overview = self.entries.payment_ai_overview(entry_id)
        amount = overview.get("recognized_total")
        if not overview.get("settled") or overview.get("needs_review") or not amount:
            return ""
        paid, total, source = overview["paid"].strip(), overview["invoice_total"].strip(), overview["paid_source"]
        # 只在实付空着（或上次就是识别填的）且识别合计等于发票金额时自动填；
        # 金额对不上时不替用户做决定，留在识别提醒里，详情页可一键采用。
        if paid and source not in {VALUE_SOURCE_PAYMENT_AI, VALUE_SOURCE_PAYMENT_OCR}:
            return ""
        if total and not _same_money(amount, total):
            return ""
        if paid and _same_money(paid, amount):
            return ""
        self.entries.update_field(entry_id, "paid_amount", amount, "", value_source=VALUE_SOURCE_PAYMENT_AI)
        self.entries.refresh_payment_check(entry_id)
        return amount

    def _payment_ai_entry_summary(self, entry_id, applied=""):
        overview = self.entries.payment_ai_overview(entry_id)
        if not overview.get("items"):
            return None
        from .db.entries import PAYMENT_CHECK_PREFIX
        entry = self.entries.get(entry_id) or {}
        notes = [part[len(PAYMENT_CHECK_PREFIX):].strip()
                 for part in str(entry.get("check_message") or "").split("；")
                 if part.startswith(PAYMENT_CHECK_PREFIX)]
        return {"entry_id": entry_id, "label": entry.get("invoice_no") and f"发票 {entry['invoice_no']}"
                or entry.get("seller") or "条目", "recognized_total": overview["recognized_total"],
                "count": len(overview["items"]), "paid": overview["paid"], "applied": applied, "notes": notes}

    @_guard
    def payment_ai_result(self, entry_id):
        """详情页付款截图识别区块：逐张结果、识别合计与实付/发票金额比对。"""
        overview = self.entries.payment_ai_overview(entry_id)
        overview["enabled"] = self._payment_ai_enabled()
        overview["available"] = self._payment_ai_available()
        return overview

    @_guard
    def apply_payment_ai_total(self, entry_id):
        """用户确认后把识别合计写入实付，留修改记录。"""
        from .db.entries import VALUE_SOURCE_PAYMENT_AI
        overview = self.entries.payment_ai_overview(entry_id)
        if not overview.get("settled"):
            raise ValueError("还有付款截图没有识别出金额，无法采用合计。")
        self.entries.update_field(entry_id, "paid_amount", overview["recognized_total"], "",
                                  value_source=VALUE_SOURCE_PAYMENT_AI)
        self.entries.recompute_status(entry_id)
        self._refresh_payment_ai_check(entry_id)
        return self.entries.get(entry_id)

    @_guard
    def start_payment_ai(self, paths=None, entry_ids=None):
        cfg = self._payment_ai_config()
        key = self._payment_ai_key(cfg)
        if not self._payment_ai_available():
            raise ValueError('请先在设置的开发者选项里填写付款截图 AI 识别的 Key')
        tasks, skipped, version = [], 0, self._payment_ai_version()
        if entry_ids is not None:
            for eid in dict.fromkeys(entry_ids):
                entry = self.entries.get(eid)
                if not entry:
                    continue
                for item in entry.get('attachments') or []:
                    if item['type'] != 'payment_screenshot':
                        continue
                    if payment_result_current(item, version):
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
               'cancelled': False, 'scope': self._payment_ai_client.scope(cfg, key), 'started': time.monotonic(),
               'entries': []}
        requested_entries = list(dict.fromkeys(entry_ids)) if entry_ids is not None else []
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
                    if self.data_root.root == root and self._payment_ai_available():
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
                summaries = []
                with self._api_lock:
                    if self.data_root.root == root:
                        self._record_payment_ai_usage()
                        # 识别完按条目收尾：能直接作实付的写入，其余留在识别提醒里。
                        if self._payment_ai_available() and not job['cancelled']:
                            for eid in requested_entries:
                                try:
                                    applied = self._settle_paid_from_payment_ai(eid)
                                    summary = self._payment_ai_entry_summary(eid, applied)
                                except Exception:
                                    summary = None
                                if summary:
                                    summaries.append(summary)
                with self._payment_ai_lock:
                    job['entries'] = summaries
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
