"""CLI entrypoint for tidoc_hub: serve, init-admin, create-invite."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .db import HubDB
from .server import HubConfig, create_server


def _load_env_file(path: str | Path) -> None:
    env_path = Path(path)
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def _config_from_env() -> HubConfig:
    return HubConfig(
        host=os.environ.get("TIDOC_HUB_HOST", "127.0.0.1"),
        port=int(os.environ.get("TIDOC_HUB_PORT", "8767")),
        prefix=os.environ.get("TIDOC_HUB_PREFIX", "/tidoc-hub"),
        data_dir=os.environ.get("TIDOC_HUB_DATA_DIR", "./data"),
        storage=os.environ.get("TIDOC_HUB_STORAGE", "cos").strip().lower(),
        public_base_url=os.environ.get("TIDOC_HUB_PUBLIC_BASE_URL", ""),
        cos_secret_id=os.environ.get("COS_SECRET_ID", ""),
        cos_secret_key=os.environ.get("COS_SECRET_KEY", ""),
        cos_bucket=os.environ.get("COS_BUCKET", ""),
        cos_region=os.environ.get("COS_REGION", "ap-beijing"),
        local_secret=os.environ.get("TIDOC_HUB_LOCAL_SECRET", ""),
        trust_proxy=os.environ.get("TIDOC_HUB_TRUST_PROXY", "1") != "0",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tidoc_hub", description="Tidoc 团队协同共享区轻量服务端")
    parser.add_argument("--env-file", default=os.environ.get("TIDOC_HUB_ENV_FILE", ""), help="可选 .env 配置文件路径")
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="启动 HTTP 服务")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument("--prefix", default=None)
    p_serve.add_argument("--data-dir", default=None)
    p_serve.add_argument("--storage", choices=("cos", "local"), default=None)

    p_admin = sub.add_parser("init-admin", help="创建初始管理员并输出一次性访问令牌")
    p_admin.add_argument("--name", required=True, help="管理员姓名")
    p_admin.add_argument("--reviewer", default="", help="对应审核人（可选）")
    p_admin.add_argument("--space", default="", help="同时创建一个初始空间名称（可选）")
    p_admin.add_argument("--data-dir", default=None)

    p_inv = sub.add_parser("create-invite", help="创建成员/管理员邀请码")
    p_inv.add_argument("--role", choices=("member", "admin"), default="member")
    p_inv.add_argument("--max-uses", type=int, default=20)
    p_inv.add_argument("--expires-days", type=int, default=30)
    p_inv.add_argument("--note", default="")
    p_inv.add_argument("--data-dir", default=None)

    args = parser.parse_args(argv)
    if args.env_file:
        _load_env_file(args.env_file)
    cfg = _config_from_env()
    if getattr(args, "data_dir", None):
        cfg.data_dir = args.data_dir

    if args.command == "init-admin":
        db = HubDB(Path(cfg.data_dir) / "tidoc_hub.sqlite")
        try:
            user, token = db.create_user(args.name, role="admin", reviewer=args.reviewer, device_name="cli-init")
            print(f"管理员已创建：{user['name']} (id={user['id']})")
            print(f"访问令牌（仅显示一次，请妥善保存并在 Tidoc「设置 → 协同共享区 → 凭管理员令牌接入」填入）：")
            print(token)
            if args.space:
                space = db.create_space(args.space, created_by=user["id"])
                print(f"已创建初始空间：{space['name']} (id={space['id']})")
        finally:
            db.close()
        return 0

    if args.command == "create-invite":
        db = HubDB(Path(cfg.data_dir) / "tidoc_hub.sqlite")
        try:
            inv = db.create_invite(
                role=args.role,
                max_uses=args.max_uses,
                expires_days=args.expires_days,
                note=args.note,
                created_by="cli",
            )
            print(f"邀请码：{inv['code']}")
            print(f"角色：{inv['role']}  可用次数：{inv['max_uses']}  过期时间：{inv['expires_at'] or '永不过期'}")
        finally:
            db.close()
        return 0

    if args.host is not None:
        cfg.host = args.host
    if args.port is not None:
        cfg.port = args.port
    if args.prefix is not None:
        cfg.prefix = args.prefix
    if args.storage is not None:
        cfg.storage = args.storage

    server = create_server(cfg)
    print(
        f"tidoc-hub listening on http://{cfg.host}:{cfg.port}{server.prefix or '/'} "
        f"(storage={server.store.mode}, data={cfg.data_dir})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.close_resources()
    return 0


if __name__ == "__main__":
    sys.exit(main())
