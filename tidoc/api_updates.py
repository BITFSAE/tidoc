"""Core and component update bridge methods."""
from __future__ import annotations
import json
import time
import threading
from tidoc import __version__
from .api_shared import (
    APP_LAST_SEEN_VERSION_KEY,
    AUTO_UPDATE_INTERVAL_SECONDS,
    AUTO_UPDATE_PREF_KEY,
    UPDATE_CHANNEL_PREF_KEY,
    UPDATE_LAST_CHECK_KEY,
    UPDATE_LAST_RESULT_KEY,
    _guard,
)


class UpdateApi:
    def _update_channel(self) -> str:
        from .services.updater import CHANNEL_BETA, CHANNEL_STABLE
        return CHANNEL_BETA if self._preference_value(UPDATE_CHANNEL_PREF_KEY, CHANNEL_STABLE) == CHANNEL_BETA else CHANNEL_STABLE

    def _load_update_manifest(self):
        from .services.updater import load_update_manifest
        return load_update_manifest(self._update_channel())

    @_guard
    def update_channel(self):
        return {"channel": self._update_channel()}

    @_guard
    def set_update_channel(self, channel):
        """选择更新通道：stable 只收正式版，beta 同时收测试版（预发布）。"""
        from .services.updater import CHANNEL_BETA, CHANNEL_STABLE
        value = CHANNEL_BETA if str(channel) == CHANNEL_BETA else CHANNEL_STABLE
        self._set_preference_value(UPDATE_CHANNEL_PREF_KEY, value)
        # 换通道后，之前的检查结果不再适用，下次立即重新检查。
        self._set_preference_value(UPDATE_LAST_CHECK_KEY, "0")
        self._set_preference_value(UPDATE_LAST_RESULT_KEY, "")
        if value == CHANNEL_STABLE:
            # 退出测试版通道：已下载还没安装的测试版包不再提示安装。
            self._core_updater.discard_prerelease()
        return {"channel": value}

    @_guard
    def check_updates(self):
        from .services.updater import check_updates
        status = check_updates(
            self.data_root.components_dir, updates_dir=self.data_root.updates_dir, channel=self._update_channel(),
        )
        return self._record_update_check(status)

    @_guard
    def core_update_status(self):
        return self._core_updater.status()

    @_guard
    def start_core_update_download(self):
        from .services.updater import COMPONENT_CORE, get_platform_asset

        # 自动检查已经把完整资源信息写入缓存。优先复用它，点击下载后便可
        # 立即启动后台任务，不再先同步请求一次 manifest。
        asset = next((
            item.get("asset")
            for item in (self._cached_update_result().get("updates") or [])
            if item.get("component") == COMPONENT_CORE
            and item.get("available")
            and (item.get("asset") or {}).get("auto_update")
        ), None)
        if not asset:
            asset = get_platform_asset(self._load_update_manifest(), COMPONENT_CORE)
        return self._core_updater.start(asset)

    @_guard
    def install_core_update(self):
        result = self._core_updater.install()

        def close_window():
            try:
                if self._window is not None:
                    self._window.destroy()
            except Exception:
                pass

        timer = threading.Timer(1.2, close_window)
        timer.name = "tidoc-update-exit"
        timer.daemon = True
        timer.start()
        return result

    @_guard
    def auto_check_updates(self):
        """按用户偏好执行低频启动检查；只检查，不下载或安装。"""
        if self._preference_value(AUTO_UPDATE_PREF_KEY, "1") != "1":
            return {"checked": False, "reason": "disabled", "updates": []}

        now = int(time.time())
        try:
            last_check = int(self._preference_value(UPDATE_LAST_CHECK_KEY, "0") or 0)
        except ValueError:
            last_check = 0
        if last_check and now - last_check < AUTO_UPDATE_INTERVAL_SECONDS:
            cached = self._cached_update_result()
            return {
                **cached,
                "checked": False,
                "reason": "recent",
                "checked_at": last_check,
                "next_check_at": last_check + AUTO_UPDATE_INTERVAL_SECONDS,
            }

        from .services.updater import check_updates
        try:
            status = check_updates(
                self.data_root.components_dir,
                updates_dir=self.data_root.updates_dir,
                channel=self._update_channel(),
            )
        except Exception as exc:
            # 启动检查失败时仍恢复上次发现的更新入口，并尽快重试；自动检查
            # 不应因为一次临时网络错误把已有更新提示一并藏掉。
            return {
                **self._cached_update_result(),
                "checked": False,
                "reason": "error",
                "error": str(exc),
                "checked_at": last_check,
                "next_check_at": now + 10 * 60,
            }
        status = self._record_update_check(status, now)
        status.update({
            "checked": True,
            "reason": "due",
            "next_check_at": now + AUTO_UPDATE_INTERVAL_SECONDS,
        })
        return status

    @_guard
    def startup_update_state(self):
        """记录已启动版本，并在真正升级后的首次启动返回本次变化。"""
        from .services.updater import version_gt
        from .release_info import RELEASE_NOTES, RELEASE_VERSION

        previous = self._preference_value(APP_LAST_SEEN_VERSION_KEY, "")
        upgraded = bool(previous and version_gt(__version__, previous))
        if not previous or version_gt(__version__, previous):
            self._set_preference_value(APP_LAST_SEEN_VERSION_KEY, __version__)

        notes: list[str] = []
        cached = self._cached_update_result()
        if upgraded:
            for item in cached.get("updates") or []:
                if item.get("component") != "core" or item.get("latest_version") != __version__:
                    continue
                raw_notes = (item.get("asset") or {}).get("notes") or []
                notes = raw_notes if isinstance(raw_notes, list) else [str(raw_notes)]
                break
            # 安装包升级或旧缓存尚未刷新时，缓存里可能没有当前版本说明。
            # 当前版本的说明随核心程序一起打包，保证任何升级路径都能展示。
            if not notes and RELEASE_VERSION == __version__:
                notes = [str(note) for note in RELEASE_NOTES if str(note).strip()]
            # A cached result belongs to the old binary. Normalize it immediately
            # so the settings indicator cannot remain orange after a real upgrade.
            self._set_preference_value(
                UPDATE_LAST_RESULT_KEY,
                json.dumps(self._normalize_cached_update_result(cached), ensure_ascii=False),
            )
        return {
            "upgraded": upgraded,
            "first_launch": not previous,
            "previous_version": previous,
            "current_version": __version__,
            "notes": notes,
        }

    @_guard
    def download_core_update(self):
        from .services.updater import COMPONENT_CORE, download_update, launch_core_update_package
        manifest = self._load_update_manifest()
        result = download_update(manifest, COMPONENT_CORE, self.data_root.updates_dir)
        launch_core_update_package(result.file_path)
        data = result.to_dict()
        data["launched"] = True
        return data

    @_guard
    def open_downloaded_core_update(self):
        from .services.updater import open_downloaded_core_update
        info = open_downloaded_core_update(self.data_root.updates_dir)
        return {"launched": True, **info}

    @_guard
    def install_print_component(self):
        from .services.updater import install_print_component
        manifest = self._load_update_manifest()
        result = install_print_component(
            manifest, self.data_root.components_dir, self.data_root.updates_dir
        )
        data=result.to_dict()
        data["template_validation"]=self.adapters.revalidate_templates()
        return data

    @_guard
    def revalidate_adapter_templates(self,component_fingerprint=None):
        return self.adapters.revalidate_templates(component_fingerprint or '')
