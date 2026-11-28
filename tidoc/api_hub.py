"""共享区协同 PyWebView 桥接 API。

显式用户触发：未配置时不发起任何网络请求，已配置时也只在用户点击按钮时联网。
凭证仅存本机数据库 meta，绝不随绑定包导出。
"""

from __future__ import annotations

import json
import socket
from uuid import uuid4

from .api_shared import _bridge, _guard, mask_secret
from .services import hub_sync
from .services.hub_batch_sync import HUB_BATCH_LINKS_META_KEY, HubBatchSyncManager

HUB_CONFIG_META_KEY = "tidoc.hub.config"
HUB_TOKEN_META_KEY = "tidoc.hub.token"
HUB_DEFAULT_URL = "https://www.bitfsae.com/tidoc-hub"


class HubApi:
    """Mixin for Api exposing shared-space sync and admin operations."""

    def _hub_sync_manager(self) -> HubBatchSyncManager:
        with self._api_lock:
            mgr = getattr(self, "_hub_sync_mgr", None)
            db_id = getattr(self, "_hub_sync_db_id", None)
            if mgr is None or db_id != id(self.db):
                if mgr is not None:
                    mgr.stop()
                mgr = HubBatchSyncManager(
                    self.data_root.db_path,
                    self.data_root,
                    payment_ai_flags_getter=self._hub_payment_ai_flags,
                )
                self._hub_sync_mgr = mgr
                self._hub_sync_db_id = id(self.db)
                self.db.add_close_listener(self._stop_hub_sync)
            return mgr

    def _hub_payment_ai_flags(self) -> tuple[bool, bool]:
        with self._api_lock:
            return self._payment_ai_available(), self._payment_ai_enabled()

    def _init_hub_sync(self) -> None:
        self._hub_sync_manager()

    def _stop_hub_sync(self) -> None:
        mgr = getattr(self, "_hub_sync_mgr", None)
        if mgr is not None:
            mgr.stop()
        transport = getattr(self, "_hub_transport", None)
        self._hub_transport = None
        if transport is not None:
            transport.close()

    def _attach_shared_batches(self, batches: list[dict]) -> list[dict]:
        return self._hub_sync_manager().attach_to_batches(self.db.conn, batches)

    def _hub_meta_get(self, key: str) -> str:
        with self._api_lock:
            row = self.db.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return str(row["value"] or "") if row else ""

    def _hub_meta_set(self, key: str, value: str) -> None:
        with self._api_lock:
            self.db.conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self.db.conn.commit()

    def _hub_config(self) -> dict:
        raw = self._hub_meta_get(HUB_CONFIG_META_KEY)
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    def _save_hub_config(self, config: dict) -> None:
        self._hub_meta_set(HUB_CONFIG_META_KEY, json.dumps(config, ensure_ascii=False))

    def _hub_token(self) -> str:
        return self._hub_meta_get(HUB_TOKEN_META_KEY).strip()

    def _save_hub_token(self, token: str) -> None:
        self._hub_meta_set(HUB_TOKEN_META_KEY, str(token or "").strip())

    def _hub_client(self) -> hub_sync.HubClient:
        with self._api_lock:
            cfg = self._hub_config()
            token = self._hub_token()
            url = str(cfg.get("server_url") or "").strip()
            if not url or not token:
                raise ValueError("尚未连接到团队共享区，请先在「设置 → 协同共享区」中填写服务器地址与邀请码。")
            client = getattr(self, "_hub_transport", None)
            if client is None or client.base_url != url or client.token != token:
                client = self._hub_transport = hub_sync.HubClient(url, token)
            return client

    def _hub_public_state(self) -> dict:
        with self._api_lock:
            cfg = self._hub_config()
            token = self._hub_token()
        connected = bool(cfg.get("server_url") and token)
        return {
            "connected": connected,
            "default_url": HUB_DEFAULT_URL,
            "server_url": str(cfg.get("server_url") or ""),
            "user_id": str(cfg.get("user_id") or ""),
            "name": str(cfg.get("name") or ""),
            "reviewer": str(cfg.get("reviewer") or ""),
            "role": str(cfg.get("role") or ""),
            "last_space_id": str(cfg.get("last_space_id") or ""),
            "token_masked": mask_secret(token) if token else "",
        }

    @_guard
    def hub_settings(self):
        return self._hub_public_state()

    @_bridge
    def hub_join(self, server_url: str, invite_code: str, name: str = "", reviewer: str = ""):
        url = hub_sync.normalize_server_url(server_url)
        client = hub_sync.HubClient(url)
        display_name = str(name or "").strip()
        display_reviewer = str(reviewer or "").strip()
        if not display_name:
            with self._api_lock:
                default_profile = self.profiles.get_default()
            if default_profile:
                display_name = str(default_profile.get("name") or "").strip()
                display_reviewer = display_reviewer or str(default_profile.get("reviewer") or "").strip()
        if not display_name:
            raise ValueError("请填写姓名后再加入共享区。")
        device_name = ""
        try:
            device_name = socket.gethostname()
        except OSError:
            pass
        res = client.join(invite_code, display_name, display_reviewer, device_name)
        user = res["user"]
        token = str(res["token"])
        with self._api_lock:
            self._hub_pull_cache = None
            self._hub_push_plan = None
            old_transport = getattr(self, "_hub_transport", None)
            self._hub_transport = None
            self._save_hub_token(token)
            self._save_hub_config({
                "server_url": url,
                "user_id": user.get("id", ""),
                "name": user.get("name", display_name),
                "reviewer": user.get("reviewer", display_reviewer),
                "role": user.get("role", "member"),
                "last_space_id": "",
            })
            self._hub_sync_manager().on_account_changed(self.db.conn, url, str(user.get("id", "")))
        if old_transport is not None:
            old_transport.close()
        return {"hub": self._hub_public_state(), "message": f"已加入共享区（{user.get('name')}）。"}

    @_bridge
    def hub_connect_token(self, server_url: str, token: str):
        """供首次初始化的管理员直接凭 CLI 生成的一次性令牌接入共享区。"""
        url = hub_sync.normalize_server_url(server_url)
        tok = str(token or "").strip()
        if not tok:
            raise ValueError("请填写访问令牌。")
        client = hub_sync.HubClient(url, tok)
        try:
            res = client.me()
        finally:
            client.close()
        user = res["user"]
        with self._api_lock:
            self._hub_pull_cache = None
            self._hub_push_plan = None
            old_transport = getattr(self, "_hub_transport", None)
            self._hub_transport = None
            self._save_hub_token(tok)
            self._save_hub_config({
                "server_url": url,
                "user_id": user.get("id", ""),
                "name": user.get("name", ""),
                "reviewer": user.get("reviewer", ""),
                "role": user.get("role", "member"),
                "last_space_id": "",
            })
            self._hub_sync_manager().on_account_changed(self.db.conn, url, str(user.get("id", "")))
        if old_transport is not None:
            old_transport.close()
        return {"hub": self._hub_public_state(), "message": f"已连接到共享区（{user.get('name')}）。"}

    @_bridge
    def hub_refresh_me(self):
        client = self._hub_client()
        res = client.me()
        user = res["user"]
        with self._api_lock:
            cfg = self._hub_config()
            prev_uid = str(cfg.get("user_id") or "")
            new_uid = str(user.get("id", prev_uid) or "")
            cur_url = str(cfg.get("server_url") or "")
            cfg.update({
                "user_id": new_uid,
                "name": user.get("name", cfg.get("name", "")),
                "reviewer": user.get("reviewer", cfg.get("reviewer", "")),
                "role": user.get("role", cfg.get("role", "")),
            })
            self._save_hub_config(cfg)
            if new_uid != prev_uid:
                self._hub_sync_manager().on_account_changed(self.db.conn, cur_url, new_uid)
        return {"hub": self._hub_public_state()}

    @_guard
    def hub_disconnect(self):
        self._hub_pull_cache = None
        self._hub_push_plan = None
        transport = getattr(self, "_hub_transport", None)
        self._hub_transport = None
        if transport is not None:
            transport.close()
        self._save_hub_token("")
        self._save_hub_config({})
        self._hub_sync_manager().on_disconnected(self.db.conn)
        return {"hub": self._hub_public_state(), "message": "已断开共享区连接（本机数据不受影响）。"}

    @_bridge
    def hub_list_spaces(self, include_archived: bool = False):
        client = self._hub_client()
        spaces = client.list_spaces(include_archived=bool(include_archived))
        return {"spaces": spaces, "hub": self._hub_public_state()}

    @_bridge
    def hub_list_space_entries(self, space_id: str):
        result = self._hub_client().list_space_entries(space_id, include_withdrawn=False, summary=True)
        rows = []
        for entry in result.get("entries", []):
            # Older servers ignore the summary parameter and return the existing full shape.
            payload = entry.get("payload") or entry
            rows.append({
                "invoice_no": entry.get("invoice_no") or payload.get("invoice_no") or "",
                "seller": payload.get("seller") or "",
                "title": payload.get("title") or "",
                "total": payload.get("total", ""),
                "owner_name": entry.get("owner_name") or "",
                "review_state": entry.get("review_state") or "",
                "review_note": entry.get("review_note") or "",
            })
        return {"entries": rows}

    @_bridge
    def hub_create_space(self, name: str, description: str = ""):
        client = self._hub_client()
        space = client.create_space(name, description)
        return {"space": space}

    @_bridge
    def hub_update_space(self, space_id: str, fields: dict | None = None):
        client = self._hub_client()
        space = client.update_space(space_id, **(fields or {}))
        return {"space": space}

    @_bridge
    def hub_list_invites(self):
        client = self._hub_client()
        return {"invites": client.list_invites()}

    @_bridge
    def hub_create_invite(self, role: str = "member", max_uses: int = 20, expires_days: int = 30, note: str = ""):
        client = self._hub_client()
        return {"invite": client.create_invite(role=role, max_uses=max_uses, expires_days=expires_days, note=note)}

    @_bridge
    def hub_revoke_invite(self, code: str):
        client = self._hub_client()
        client.revoke_invite(code)
        return {"revoked": True}

    @_bridge
    def hub_list_users(self):
        client = self._hub_client()
        return {"users": client.list_users()}

    @_bridge
    def hub_update_user(self, user_id: str, fields: dict | None = None):
        client = self._hub_client()
        return {"user": client.update_user(user_id, **(fields or {}))}

    @_guard
    def hub_prepare_push(self, space_id: str, entry_ids: list[str]):
        client = self._hub_client()
        remote_ids = set()
        for entry_id in dict.fromkeys(entry_ids):
            link = hub_sync.get_hub_link(self.db.conn, entry_id)
            if link and link["server_url"] == client.base_url and link["space_id"] == space_id:
                remote_ids.add(link["remote_entry_id"])
        states = client.upload_state(space_id, sorted(remote_ids)) if remote_ids else None
        plan_id = uuid4().hex
        self._hub_push_plan = (plan_id, client, space_id, states)
        return {"plan_id": plan_id}

    @_guard
    def hub_push_entries(self, space_id: str, entry_ids: list[str], space_name: str = "", plan_id: str = ""):
        client = self._hub_client()
        states = None
        if plan_id:
            plan = getattr(self, "_hub_push_plan", None)
            if not plan or plan[:3] != (plan_id, client, space_id):
                raise ValueError("上传检查已过期，请重试。")
            states = plan[3]
        result = hub_sync.push_entries(
            client,
            self.entries,
            self.attachments,
            space_id,
            entry_ids,
            space_name=space_name,
            adapter_service=self.adapters,
            upload_state=states,
        )
        cfg = self._hub_config()
        if space_id and cfg.get("last_space_id") != space_id:
            cfg["last_space_id"] = space_id
            self._save_hub_config(cfg)
        self._hub_sync_manager().on_manual_push_applied(
            self.entries,
            self.attachments,
            self.adapters,
            client.base_url,
            str(cfg.get("user_id") or ""),
            space_id,
            result.get("pushed") or [],
        )
        return result

    @_guard
    def hub_preview_pull(self, space_id: str, scheme_id: str | None = None):
        client = self._hub_client()
        norm_scheme = str(scheme_id or "").strip() or None
        preview = hub_sync.preview_pull(
            client,
            self.entries,
            space_id,
            adapter_service=self.adapters,
            target_scheme_id=norm_scheme,
        )
        cfg = self._hub_config()
        if space_id and cfg.get("last_space_id") != space_id:
            cfg["last_space_id"] = space_id
            self._save_hub_config(cfg)
        self._hub_pull_cache = {
            "server_url": client.base_url,
            "space_id": space_id,
            "scheme_id": norm_scheme,
            "preview": preview,
        }
        return preview

    @_guard
    def hub_apply_pull(self, space_id: str, profile_id: str = "", options: dict | None = None):
        client = self._hub_client()
        pid = str(profile_id or "").strip()
        if not pid:
            default_profile = self.profiles.get_default()
            pid = default_profile["id"] if default_profile else ""
        cached = getattr(self, "_hub_pull_cache", None)
        norm_scheme = str((options or {}).get("scheme_id") or "").strip() or None
        cached_preview = (
            cached["preview"]
            if cached
            and cached.get("server_url") == client.base_url
            and cached.get("space_id") == space_id
            and (norm_scheme is None or cached.get("scheme_id") in (None, norm_scheme))
            else None
        )
        result = hub_sync.apply_pull(
            client,
            self.entries,
            self.attachments,
            space_id,
            pid,
            options=options,
            preview=cached_preview,
            adapter_service=self.adapters,
        )
        self._hub_pull_cache = None
        for entry_id in list(dict.fromkeys((result.get("entry_ids") or []) + (result.get("updated_entry_ids") or []))):
            self._refresh_payment_ai_check(entry_id)
        cfg = self._hub_config()
        self._hub_sync_manager().on_manual_pull_applied(
            self.entries,
            self.attachments,
            self.adapters,
            client.base_url,
            str(cfg.get("user_id") or ""),
            space_id,
            result,
        )
        return result

    @_guard
    def hub_review_entry(self, entry_id: str, review_state: str, review_note: str = "", push_local: bool = True):
        client = self._hub_client()
        result = hub_sync.review_entry(
            client,
            self.entries,
            self.attachments,
            entry_id,
            review_state=review_state,
            review_note=review_note,
            push_local=bool(push_local),
            adapter_service=self.adapters,
        )
        cfg = self._hub_config()
        lnk = result.get("link") or {}
        if lnk.get("space_id"):
            self._hub_sync_manager().on_manual_push_applied(
                self.entries,
                self.attachments,
                self.adapters,
                client.base_url,
                str(cfg.get("user_id") or ""),
                str(lnk["space_id"]),
                [{
                    "entry_id": entry_id,
                    "remote_entry_id": str(lnk.get("remote_entry_id") or ""),
                    "revision": int(lnk.get("remote_revision") or 0),
                }],
            )
        return result

    @_guard
    def hub_withdraw_entry(self, entry_id: str):
        client = self._hub_client()
        result = hub_sync.withdraw_entry(client, self.entries, entry_id)
        cfg = self._hub_config()
        lnk = result.get("link") or {}
        if lnk.get("space_id"):
            self._hub_sync_manager().on_manual_push_applied(
                self.entries,
                self.attachments,
                self.adapters,
                client.base_url,
                str(cfg.get("user_id") or ""),
                str(lnk["space_id"]),
                [{
                    "entry_id": entry_id,
                    "remote_entry_id": str(lnk.get("remote_entry_id") or ""),
                    "revision": int(lnk.get("remote_revision") or 0),
                }],
            )
        return result

    # ------------------------------------------------------------------ Shared batches & auto-sync
    @_guard
    def hub_batch_links(self):
        return self._hub_sync_manager().batch_links(self.db.conn)

    @_bridge
    def hub_link_batch(self, batch_id: str = "", space_id: str = "", auto_sync: bool = False):
        return self._hub_sync_manager().link_batch(
            self, batch_id=batch_id, space_id=space_id, auto_sync=bool(auto_sync)
        )

    @_guard
    def hub_unlink_batch(self, batch_id: str):
        return self._hub_sync_manager().unlink_batch(self.db.conn, batch_id)

    @_guard
    def hub_set_batch_auto_sync(self, batch_id: str, enabled: bool):
        return self._hub_sync_manager().set_batch_auto_sync(self.db.conn, batch_id, bool(enabled))

    @_guard
    def hub_sync_batch(self, batch_id: str):
        return self._hub_sync_manager().sync_batch(self.db.conn, batch_id)

    @_guard
    def hub_stop_batch_sync(self, batch_id: str):
        return self._hub_sync_manager().stop_batch_sync(self.db.conn, batch_id)

    @_guard
    def hub_batch_sync_report(self, batch_id: str):
        return self._hub_sync_manager().batch_sync_report(self.db.conn, batch_id)

    @_guard
    def hub_detach_other_space_entries(self, batch_id: str):
        return self._hub_sync_manager().detach_other_space_entries(self.db.conn, batch_id)

    @_guard
    def hub_sync_tick(self):
        return self._hub_sync_manager().tick(self.db.conn)
