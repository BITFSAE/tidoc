import json
import sys

try:
    from .cli import main
except ModuleNotFoundError as exc:
    print(json.dumps({"ok": False, "errors": [{"code": "ENVIRONMENT_UNAVAILABLE", "file": "", "location": "/", "message": f"缺少运行依赖：{exc.name}"}], "warnings": []}, ensure_ascii=False), file=sys.stderr)
    raise SystemExit(2) from exc

raise SystemExit(main())
