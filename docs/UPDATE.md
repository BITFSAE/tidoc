# tidoc 联网更新发布说明

更新源使用车队已有腾讯云 COS：

- Bucket：`bitfsae-1416420925`
- 地域：`ap-beijing`
- 公开地址：`https://img.bitfsae.com/tidoc`
- 清单：`https://img.bitfsae.com/tidoc/manifest.json`

CDN 缓存规则：

- `/tidoc/manifest.json` 不缓存
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

GitHub Actions 会并行打包 macOS / Windows，汇总后运行
`scripts/build_manifest.py` 生成 `manifest.json` 和 `upload_plan.tsv`，再用腾讯云官方
`coscli` 上传。发布文件先上传，`manifest.json` 最后上传，避免客户端读到半成品清单。

## COS 路径

```text
tidoc/manifest.json
tidoc/core/windows/tidoc-core-windows-v0.1.20.exe
tidoc/core/windows/tidoc-core-windows-v0.1.20-update.zip
tidoc/core/macos/tidoc-core-macos-v0.1.20.dmg
tidoc/core/macos/tidoc-core-macos-v0.1.20-update.zip
tidoc/print/windows/tidoc-print-windows-v0.1.20.exe
tidoc/print/macos/tidoc-print-macos-v0.1.20.zip
tidoc/ocr/windows/tidoc-ocr-windows-v0.2.1.exe
tidoc/ocr/macos/tidoc-ocr-macos-v0.2.1.zip
```

## 客户端行为

- 软件启动完成后默认读取公开 manifest，应用持续运行时也会按到期时间继续检查，每小时最多联网一次；用户可在设置关闭。自动检查只显示设置旁的下载动作，不会自动下载或安装。
- 自动发现核心更新时只显示顶栏下载动作；升级后的首次启动显示升级前后版本号和本次更新内容。完整使用指南仍可从设置打开。
- 核心发布除首次安装用的 EXE / DMG 外，还包含 `-update.zip` 一键更新包。用户点击顶栏下载动作后，程序在后台下载、显示百分比、校验 SHA256 并解压到暂存目录；完成后动作切换为「重启更新」。
- Windows 发布版由隐藏 PowerShell 助手等待旧进程退出、交换安装目录、保留 Inno Setup 卸载文件并启动新版本；新界面写入版本与进程健康标记后才清理旧目录，启动失败则恢复并打开旧版本。macOS 安装位置可写时由同构 shell 助手替换 `.app`；不可写时继续使用 DMG 覆盖安装。整个一键流程不显示首次安装向导。
- 新版本首次启动会按当前核心版本重算上次检查缓存并清理失效的待更新标记，避免设置按钮继续显示旧的橙色提示。
- 打印组件可直接下载安装到本机数据目录的 `components/print/<platform>/` 下；安装成功后自动清理同平台旧版本目录，只保留当前版本，个别目录删除失败时不影响安装结果。OCR 识别组件同构，安装到 `components/ocr/<platform>/`。
- 核心与打印、OCR 组件使用独立版本。`scripts/set_version.py` 只写入核心版本；只有 `tidoc_print` 代码、`requirements-print.txt` 或核心与组件的 JSON 调用协议变化时，才在 `tidoc_print/__init__.py` 增加组件版本；`tidoc_ocr` 同理，版本在 `tidoc_ocr/__init__.py` 维护。普通核心发布即使重新构建组件包，也不会让客户端误报组件更新。
- 客户端会同时检查各组件的版本标记、可执行文件和安装校验值；文件缺失或损坏时显示“需要修复”，最新版本也允许重新安装。
- OCR 识别组件只负责执行阿里云识别调用（`ocr-api.cn-hangzhou.aliyuncs.com`），密钥由用户在设置内自填并仅存本机；组件自检（`--self-test`）不联网。0.2.0 起，多页 PDF 临时拆页逐页识别并合并，`pypdf` 随组件打包；0.2.1 起，空名称负数折扣行会并回上一商品，重复两遍的完整商品名称会折叠。核心会按实际页数提示并记录调用次数。识别结果（含原始 JSON 和自动补齐 / 历史修正快照）落库在 `ocr_results` 表，与更新通道无关。Windows 核心以不创建控制台窗口的方式启动组件，避免批量识别逐张闪出终端黑框。
- 核心下载点击后立即在顶栏显示 0%，随后在顶栏动作和更新弹窗同步进度；组件安装过程仍在设置弹窗显示，完成后立即刷新状态。
- 软件内始终提供 GitHub Releases 手动下载入口；更新服务不可用时仍可访问。
- 高级数据维护可清理拖拽中转文件与旧更新包，但会保留业务数据、导出文件、组件和待安装更新包。
- 客户端不保存腾讯云密钥，只做 HTTPS 下载和 SHA256 完整性校验。
- macOS 上如果应用内 Python/OpenSSL 不能验证系统已信任的证书链，会用系统 `curl` 重试读取清单和下载文件；不会关闭证书校验。

## 发布自动化

- 推送 `v*.*.*` tag 后，GitHub Actions 自动测试、构建 macOS DMG 与 Windows 安装器、生成更新清单、上传 COS，并发布带安装包的 GitHub Release。
- Windows / macOS 打印组件、OCR 组件以及 macOS 核心应用打包后必须执行 `--self-test`，确认最终成品中的代码、资源与重依赖可加载；自检失败会中止发布。
- macOS 核心包构建后只剥离实际存在的依赖库 Mach-O 符号（`*.so` / `*.dylib` / 名为 `Python` 的解释器），保留主可执行文件末尾的 PyInstaller 内嵌归档，再以 ad-hoc 签名重签；没有匹配文件时跳过，不中断发布。DMG 使用 ULMO（LZMA）压缩。打印组件在 PyInstaller 收集阶段剥离符号，由 `--self-test` 兜底校验。
- Release 说明和客户端 What’s changed 都从 `CHANGELOG.md` 最新一节生成，避免三处手工维护后内容不一致。
- Windows 核心首次安装仍使用 Inno Setup 的按用户安装器；安装到用户目录，不要求管理员权限，并提供开始菜单、可选桌面快捷方式和卸载入口。发布任务同时把 `dist/tidoc` 打成带单一 `tidoc/` 根目录的一键更新 ZIP。macOS 同时用 `ditto` 生成保留应用包元数据的 `tidoc.app/` 更新 ZIP。
- 平台图标集中在 `icon/`：`source/icon-master.png` 是完整方形母版；Windows 使用 `windows/icon-rounded.png` 和多尺寸 `windows/icon.ico`，透明圆角由素材提供；macOS 使用不手工裁圆角的 `macos/icon-1024.png` 和 `macos/icon.icns`，交给系统裁切。`scripts/generate_brand_assets.py` 可从方形源图重建这些文件及 Web 用 PNG。
- Windows 安装器把 `.tidoc` 扩展名注册为「Tidoc 绑定包」（按用户写入 `Software\Classes`）：资源管理器使用安装目录中的 `tidoc-file.ico`，双击调用 `tidoc.exe "<文件>"` 进入导入预览；Tidoc 已运行时，第二个进程只把路径转交给已有窗口并退出。安装器本身与应用可执行文件也使用同一套圆角 ICO，卸载时自动删除关联键。macOS 构建启用文档启动参数接收，把 `tidoc-file.icns` 放进应用资源、注册 `com.bitfsae.tidoc.bindle` 文档类型，并在修改后重新签名应用包。单实例锁使用系统运行目录，不放在可迁移的数据根内，避免迁移数据时失效。
