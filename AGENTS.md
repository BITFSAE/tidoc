DO NOT send optional commentary

Repository workflow:
- For the current project stage, routine changes may be made and committed directly on `main`; do not create a feature branch unless the user explicitly asks for one.
- A commit does not authorize pushing, tagging, building a release, or publishing a new version. Perform those actions only when the user explicitly requests them.
- Do not use Playwright or other browser automation for UI review or verification in this repository. Do not create one-off preview/mock pages or inject synthetic UI solely to inspect a component; review the real application surface when available, otherwise rely on source-level checks, tests, and user-provided screenshots.

Tidoc product/design guidance learned from this project:
- Treat Tidoc as an operator tool for repeated reimbursement work. The primary path is invoice import -> material completion -> review/filter -> batch/export/print, so new UI should reduce movement along that path rather than add separate "feature pages".
- Keep one clear working surface. Prefer filters, presets, inline/card actions, context menus, and compact dialogs over duplicate navigation entries for the same reimbursement list.
- Put the most common material operations closest to the invoice card and detail header: attach payment screenshots, attach inspection files, mark paid amount, edit notes, batch select, export, and print. Low-frequency maintenance belongs under settings or advanced sections.
- Design for messy real inputs. Users may import PDF first, XML later or never, drag mixed files, batch by folder, fix recognition mistakes, and revisit incomplete material days later. Flows should make those states visible and recoverable.
- Keep flexibility concrete. Batch operations, tags, notes, drag/drop, right-click, double-click, and quick filters are valuable because they map to real operator shortcuts; avoid abstract customization that does not make reimbursement faster.
- User-facing text should help first-time users without sounding like implementation notes. Use terms like "打印导出组件", "材料", "报账批次", "检查更新"; avoid exposing dependency/package language unless it is in a troubleshooting-only path.
- Hints should be timely and dismissible. Do not show first-run guidance repeatedly after a user has acknowledged it; keep expert shortcuts discoverable through titles, subtle hints, and context menus.
- For update/install flows, keep state honest immediately after an action. If a component is installed, refresh the visible status in the same dialog and show an in-progress state while downloading, verifying, or installing.
- Network behavior should stay explicit unless intentionally changed. Today the app should only touch the network when the user opens software update or starts an update/install action; do not add automatic background checks without making the behavior clear in settings.
- Visual hierarchy should stay quiet, dense, and useful: restrained blocks, clear grouping, small status badges, real data first, no decorative clutter, and no redundant images or copy that compete with the reimbursement work.
- Keep docs in sync with code in the same change. When code alters behavior, dependencies, status, or a documented flow, update the affected docs (README status list, DESIGN, CHANGELOG, docs/UPDATE.md, requirements*) as part of that change — do not leave them for later. Each fact has one authoritative home: implementation status lives in the README status list, design intent and architecture in DESIGN, release/ops detail in docs/UPDATE.md; other docs cross-reference rather than restate. Do not add a dependency to requirements* or DESIGN unless code actually imports it, and remove entries that code no longer uses.
