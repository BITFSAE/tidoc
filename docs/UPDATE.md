# tidoc 联网更新发布说明

更新源使用车队已有腾讯云 COS：

- Bucket：`bitfsae-1416420925`
- 地域：`ap-beijing`
- 公开地址：`https://img.bitfsae.com/tidoc`
- 清单：`https://img.bitfsae.com/tidoc/manifest.json`（正式版）
- 测试版清单：`https://img.bitfsae.com/tidoc/manifest-beta.json`（仅测试版发布时写入）

CDN 缓存规则：

- `/tidoc/manifest.json` 和 `/tidoc/manifest-beta.json` 不缓存
- `zip` / `exe` / `dmg` 缓存 30 天

## 发布

仓库 Secrets 需要存在：

- `TENCENT_SECRET_ID`
- `TENCENT_SECRET_KEY`

打 tag 后推送即可触发：

```bash
git tag v0.1.1
git push origin v0.1.1
```

### 测试版（预发布）

版本号带预发布后缀的 tag（如 `v0.1.39-beta.1`、`v0.1.39-rc.1`）走测试版通道：

```bash
git tag -a v0.1.39-beta.1 -m "Tidoc 0.1.39-beta.1"
git push origin v0.1.39-beta.1
```

- 构建、自检和上传与正式版相同；区别是只写入 `manifest-beta.json`，**不会改写 `manifest.json`**，所以没有打开「接收测试版更新」的用户完全不受影响。
- 版本号必须是 `1.2.3` 或 `1.2.3-beta.1`、`1.2.3-rc.2` 这种形式：连字符后只能是字母、数字和点（`1.2.3-rc-1` 会被拒绝）。发布流程第一步就会检查，不合格时不会开始构建。
- 打印、OCR 组件每次发布都会重新构建，并以固定的「组件+版本」文件名上传；PyInstaller 的产物每次字节都不同。如果测试版把正式版清单正在使用的组件版本再传一遍，正式版清单里的 sha256 就对不上了。因此测试版发布时 `build_manifest.py` 会读取线上 `manifest.json`（`--stable-manifest`），**跳过正式版已经在用的组件版本**；只有组件号比正式版新时才随测试版上传。读不到正式版清单时发布直接失败，不会冒险上传。
- GitHub Release 标为预发布（Pre-release），不会成为 Latest。
- 更新说明取 `CHANGELOG.md` 最新一节，测试版发布时可以保留「Unreleased」标题；正式版发布时再改成带版本号的标题。
- 客户端打开「接收测试版更新」后同时读取两份清单，每个组件取较新的版本。版本按 semver 比较：`0.1.39-beta.1 < 0.1.39-beta.2 < 0.1.39 < 0.1.40-beta.1`，所以同版本的正式版发布后，测试版用户会被提示更新到正式版。
- Windows 安装器的文件版本信息只能是数字，测试版用其数字部分（`0.1.39`）；安装包文件名和应用内版本仍带完整后缀。CI 编译测试版安装器后读取四个数字版本字段校验，避免版本文本的尾部填充空格导致误报。
- 软件更新窗口在读取本地信息前即显示，并可立即关闭；加载中关闭不会被返回结果重新打开。返回设置后只刷新组件与更新摘要，保留设置表单。
- 用户在设置里关闭「接收测试版更新」时，已下载但还没安装的测试版核心更新包会被丢弃（正在安装的撤不回，正在下载的下载完后丢弃）；已经装上的测试版不降级。

GitHub Actions 会并行打包 macOS / Windows，汇总后运行
`scripts/build_manifest.py` 生成 `manifest.json` 和 `upload_plan.tsv`，再用腾讯云官方
`coscli` 上传。发布文件先上传，`manifest.json` 最后上传，避免客户端读到半成品清单。

## COS 路径

以下为路径格式，版本占位符分别替换为实际发布的核心、打印和 OCR 版本；不表示对应产物已上传。

```text
tidoc/manifest.json
tidoc/core/windows/tidoc-core-windows-v<core_version>.exe
tidoc/core/windows/tidoc-core-windows-v<core_version>-update.zip
tidoc/core/macos/tidoc-core-macos-v<core_version>.dmg
tidoc/core/macos/tidoc-core-macos-v<core_version>-update.zip
tidoc/print/windows/tidoc-print-windows-v<print_version>.exe
tidoc/print/macos/tidoc-print-macos-v<print_version>.zip
tidoc/ocr/windows/tidoc-ocr-windows-v<ocr_version>.exe
tidoc/ocr/macos/tidoc-ocr-macos-v<ocr_version>.zip
```

`0.1.39` 使用打印组件 `0.1.22`（含单价修复，`0.1.39-beta.1` 起先在测试版提供）和 OCR 组件 `0.2.1`。预发布流程只上传新组件号，并保留正式版组件文件；正式版发布时重新构建并上传各组件。正式版与测试版清单指向同一组件版本时，测试版客户端以正式版清单为准。兼容性仍需检查 `--capabilities`，不能只依赖版本号。

## 客户端行为

- 软件启动完成后默认读取公开 manifest，应用持续运行时也会按到期时间继续检查，每小时最多联网一次；用户可在设置关闭。自动检查只显示设置旁的下载动作，不会自动下载或安装。
- 自动发现核心更新时只显示顶栏下载动作；升级后的首次启动显示升级前后版本号和本次更新内容。完整使用指南仍可从设置打开。
- 核心发布除首次安装用的 EXE / DMG 外，还包含 `-update.zip` 一键更新包。用户点击顶栏下载动作后，程序在后台下载、显示百分比、已下载大小和速度，再校验 SHA256 并解压到暂存目录；顶栏与更新弹窗会分别显示「下载」「校验」「准备更新文件」，完成后动作切换为「重启更新」。Windows 对支持 HTTP Range 的更新包默认四路分段下载，并保留各分段进度供失败后续传；服务器不支持分段或并发连接失败时，会把已有连续数据交给单连接继续下载。macOS 保持单连接下载并支持从已有临时文件续传。
- Windows 发布版由隐藏 PowerShell 助手等待旧进程退出、交换安装目录、保留 Inno Setup 卸载文件并启动新版本；新界面写入版本与进程健康标记后才清理旧目录，启动失败则恢复并打开旧版本。macOS 安装位置可写时由同构 shell 助手替换 `.app`；不可写时继续使用 DMG 覆盖安装。整个一键流程不显示首次安装向导。
- 新版本首次启动会按当前核心版本重算上次检查缓存并清理失效的待更新标记，避免设置按钮继续显示旧的橙色提示。
- 打印组件可直接下载安装到本机数据目录的 `components/print/<platform>/` 下；安装成功后自动清理同平台旧版本目录，只保留当前版本，个别目录删除失败时不影响安装结果。OCR 识别组件同构，安装到 `components/ocr/<platform>/`。
- 核心与打印、OCR 组件使用独立版本。`scripts/set_version.py` 只写入核心版本；只有 `tidoc_print` 代码、`requirements-print.txt` 或核心与组件的 JSON 调用协议变化时，才在 `tidoc_print/__init__.py` 增加组件版本；`tidoc_ocr` 同理，版本在 `tidoc_ocr/__init__.py` 维护。普通核心发布即使重新构建组件包，也不会让客户端误报组件更新。
- 客户端会同时检查各组件的版本标记、可执行文件和安装校验值；文件缺失或损坏时显示“需要修复”，最新版本也允许重新安装。
- OCR 识别组件只负责执行阿里云识别调用（`ocr-api.cn-hangzhou.aliyuncs.com`），密钥由用户在设置内自填并仅存本机；组件自检（`--self-test`）不联网。0.2.0 起，多页 PDF 临时拆页逐页识别并合并，`pypdf` 随组件打包；0.2.1 起，空名称负数折扣行会并回上一商品，重复两遍的完整商品名称会折叠。核心会按实际页数提示并记录调用次数。识别结果（含原始 JSON 和自动补齐 / 历史修正快照）落库在 `ocr_results` 表，与更新通道无关。Windows 核心以不创建控制台窗口的方式启动组件，避免批量识别逐张闪出终端黑框。
- 核心下载点击后立即在顶栏显示 0%，随后在顶栏动作和更新弹窗同步百分比；下载完成后顶栏原位切换为「校验」和「准备」，避免把本地处理时间误认为网络下载。组件安装过程仍在设置弹窗显示，完成后立即刷新状态。
- 软件内始终提供 GitHub Releases 手动下载入口；更新服务不可用时仍可访问。
- 高级数据维护可清理拖拽中转文件与旧更新包，但会保留业务数据、导出文件、组件和待安装更新包。
- 客户端不保存腾讯云密钥，只做 HTTPS 下载和 SHA256 完整性校验。
- macOS 上如果应用内 Python/OpenSSL 不能验证系统已信任的证书链，会用系统 `curl` 重试读取清单和下载文件；不会关闭证书校验。

## 发布自动化

团队适配资源、迁移和成品核对要求见下节。用户安装的方案、包资源、修订和导出任务位于用户数据目录，核心升级不覆盖这些目录。

核心 `--self-test` 检查前端入口、适配包 Schema、共享 context Schema 和两个内置包是否可加载。该检查确认资源能读取，不会执行办公软件界面检查。CI 和发布测试配置运行旧 IPC 转换、新 IPC、内置包、示例和适配 CLI 测试，并安装 `requirements-print.txt`，使打印用例具备真实依赖。最新源码全库回归及核心／打印源码自检已通过，结果见[计划记录](TEAM_ADAPTER_PLAN.md#23-实施与验收覆盖记录)；真实 Windows / macOS 发布包和办公软件验收仍须另行记录。

打印组件 `--self-test` 在临时目录通过正式 IPC v2 实际渲染一份中文／金额 DOCX，以及一份包含样例发票 PDF、图片和编号的材料 PDF。它核对 DOCX 文本及金额和材料 PDF 的两页结果，然后清理临时目录。成功结果包含 `smoke_outputs: ["docx", "pdf_bundle"]`；依赖实际加载或渲染失败时报告 `COMPONENT_SELF_TEST_FAILED`，退出码为 1。能找到依赖模块但无法加载其运行库的情况也会被此流程发现。

源码打印自检已成功报告 `smoke_outputs: ["docx", "pdf_bundle"]`，相关用例已纳入当前完整全库通过运行。该流程进入发布成品自检配置；实际渲染自检不代表原生 Word／WPS、字体回退、长表分页或平台 UI 已验收。

- 推送 `v*.*.*` tag 后，GitHub Actions 自动测试、构建 macOS DMG 与 Windows 安装器、生成更新清单、上传 COS，并发布带安装包的 GitHub Release。
- Windows / macOS 核心、打印组件和 OCR 组件打包后执行 `--self-test`，确认最终成品中的代码、资源与依赖可加载；自检失败会中止发布。
- Windows 核心在生成安装器前通过 PowerShell 启动成品并等待退出，显式检查进程退出码。配置存在不等于对应成品已经运行成功，结果须随实际构建记录。
- macOS 核心包构建后只剥离实际存在的依赖库 Mach-O 符号（`*.so` / `*.dylib` / 名为 `Python` 的解释器），保留主可执行文件末尾的 PyInstaller 内嵌归档，再以 ad-hoc 签名重签；没有匹配文件时跳过，不中断发布。DMG 使用 ULMO（LZMA）压缩。打印组件在 PyInstaller 收集阶段剥离符号，由 `--self-test` 兜底校验。
- Release 说明和客户端 What’s changed 都从 `CHANGELOG.md` 最新一节生成；`scripts/set_version.py` 同时把该节嵌入核心程序，更新完成弹窗优先使用检查缓存、缓存缺失或过期时回退到安装包内说明，确保一键更新和手动安装都能显示，避免多处手工维护后内容不一致。
- 文档站的更新日志页在构建时读取同一个 `CHANGELOG.md`，所以版本标题必须写成 `## 2026-10-08 · v0.1.39`、`## 2026-09-14`（没有版本号的日期记录）或 `## Unreleased`，小节标题用 `### Added`、`### Changed`、`### Fixed`，条目写成单行 `- ` 列表；标题写成其他形式会让文档站构建失败。`Unreleased` 一节在改成带版本号的标题之前不会出现在站点上。条目的第一句会在页面里作为要点单独突出，所以第一句要能独立说明改了什么。
- Windows 核心首次安装仍使用 Inno Setup 的按用户安装器；安装到用户目录，不要求管理员权限，并提供开始菜单、可选桌面快捷方式和卸载入口。发布任务同时把 `dist/tidoc` 打成带单一 `tidoc/` 根目录的一键更新 ZIP。macOS 同时用 `ditto` 生成保留应用包元数据的 `tidoc.app/` 更新 ZIP。
- 平台图标集中在 `icon/`：`source/icon-master.png` 是完整方形母版；Windows 使用 `windows/icon-rounded.png` 和多尺寸 `windows/icon.ico`，透明圆角由素材提供；macOS 使用不手工裁圆角的 `macos/icon-1024.png` 和 `macos/icon.icns`，交给系统裁切。`scripts/generate_brand_assets.py` 可从方形源图重建这些文件及 Web 用 PNG。

## 团队适配资源与迁移

核心资源根必须包含 `schemas/team-adapter/1/`、`tidoc/builtin_adapters/` 和 `tidoc_print/context_fields.json`。共享目录通过轻量 `tidoc_print.context` 读取，因此核心不能排除整个 `tidoc_print` 包。Word 和 PDF 的渲染重依赖仍属于可选组件。

旧独立校验调用的兼容名称及税号常量也读取同一 BITFSAE 内置 `scheme.json`，不能因移除代码中的字面量而省略该资源。应用业务路径继续使用显式条目修订上下文。

独立打印组件必须包含 `tidoc_print/context_fields.json`、`schemas/team-adapter/1/context.schema.json`、`tidoc_print/templates/`，以及旧 IPC v1 所需的 `tidoc/builtin_adapters/org.bitfsae.reimbursement/scheme.json`。核心与组件使用唯一的 context Schema，不维护独立副本定义。Windows / macOS 打印构建将这份源文件放入相同的 `schemas/team-adapter/1/` 资源路径。

核心的四类输出预检和打印组件的实际 IPC 均通过 `tidoc_print.context_validation` 加载唯一 context Schema，使用 `jsonschema` 检查完整上下文。打印依赖由 `requirements-print.txt` 安装；v1 转换后的 v2、源码和外部进程均在渲染前执行同一检查。IPC v2 的模板仍来自请求资源表。组件无需导入核心的 `adapters.registry`、数据库或 API，也无需打包其他适配包 Schema。

缺少 `jsonschema` 或 context Schema、Schema 损坏时，`--capabilities` 报告缺项并停用 DOCX 和 PDF 渲染能力，`--self-test` 返回失败；核心自检也检查 context Schema。frozen 运行不会借用相邻源码目录的资源。上下文校验错误只给 JSON Pointer 和约束，不回显账号、备注等资料。

共享 Schema 的 `$defs`、闭合选项和齐备属性由核心与打印组件使用同一源文件，无需在发布脚本中维护第二份定义。两者自检覆盖该资源；每次发布仍需核对对应构建中的 Windows 和 macOS 成品自检结果。

数据库从 v11 迁移到 v12 前使用 SQLite 备份接口保存一致性副本。同一次启动里已经保存过升级前备份时，初始化报账方案不再重复保存第二份；没有任何用户数据的全新安装不保存备份。备份超过 2 份时，设置的“数据位置”提供“清理旧备份”（保留最近 2 份，需确认，不会自动删除）。旧抬头、材料要求、输出偏好和完整或部分收款资料迁入“原有报账方案”。显式空值保留，收款资料按原对象迁移。旧库配置和前端偏好尚未补齐时，先完成迁移确认再创建或打印条目。新代码拒绝写入高于当前版本的数据库。

数据迁移和备份应包含 `adapters/`、`export_jobs/`、附件、数据库及原交付文件。回退到旧程序前，恢复迁移前备份到独立位置，并保留升级后的数据供人工迁回。直接用旧程序打开 v12 数据库不属于受支持的回退方式。

发布前分别在 Windows 和 macOS 成品上执行核心与组件 `--self-test`，再检查空目录首次选择、旧库迁移、组件安装与修复、四类导出、取消、历史重新生成和数据目录迁移。源码测试、模拟 frozen 资源定位和成功构建都不能替代这些检查。Word、WPS 或 LibreOffice 的中文字体、长表分页和实际打印版式需记录办公软件及平台版本。

当前验收证据和未完成项集中保存在[计划覆盖记录](TEAM_ADAPTER_PLAN.md#23-实施与验收覆盖记录)。新增资源参数已写入工作流也不等于对应平台成品通过验收。

## 文件关联

- Windows 安装器把 `.tidoc` 扩展名注册为「Tidoc 绑定包」（按用户写入 `Software\Classes`）：资源管理器直接从已嵌入多尺寸 ICO 的 `tidoc.exe` 取文件图标，不依赖会被原位更新替换的独立图标文件。双击调用 `tidoc.exe "<文件>"` 进入导入预览；Tidoc 已运行时，第二个进程只把路径转交给已有窗口并退出。已有安装在首次启动新版本时会把旧的 `tidoc-file.ico` 注册值迁移到主程序并通知资源管理器刷新关联。卸载时自动删除关联键。macOS 构建启用文档启动参数接收，把 `tidoc-file.icns` 放进应用资源、注册 `com.bitfsae.tidoc.bindle` 文档类型，并在修改后重新签名应用包。单实例锁使用系统运行目录，不放在可迁移的数据根内，避免迁移数据时失效。
