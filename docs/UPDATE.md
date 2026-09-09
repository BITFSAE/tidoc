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
tidoc/core/macos/tidoc-core-macos-v0.1.20.dmg
tidoc/print/windows/tidoc-print-windows-v0.1.20.exe
tidoc/print/macos/tidoc-print-macos-v0.1.20.zip
tidoc/ocr/windows/tidoc-ocr-windows-v0.2.1.exe
tidoc/ocr/macos/tidoc-ocr-macos-v0.2.1.zip
```

## 客户端行为

- 设置 -> 软件更新会读取公开 manifest；「启动后自动检查」默认关闭。用户开启后，软件启动完成再检查，每 24 小时最多联网一次，不会自动下载或安装。
- 自动发现核心更新时会显示一次本次更新；升级后的首次启动会再次展示更新完成与版本变化。弹窗内提供醒目的可折叠使用指南，完整覆盖导入、补材料、核对、批次、打印与查找流程。
- 核心更新会下载并校验安装包，校验通过后打开更新包；未重启前仍显示有更新，但状态会标为“已下载待安装”。
- 打印组件可直接下载安装到本机数据目录的 `components/print/<platform>/` 下；安装成功后自动清理同平台旧版本目录，只保留当前版本，个别目录删除失败时不影响安装结果。OCR 识别组件同构，安装到 `components/ocr/<platform>/`。
- 核心与打印、OCR 组件使用独立版本。`scripts/set_version.py` 只写入核心版本；只有 `tidoc_print` 代码、`requirements-print.txt` 或核心与组件的 JSON 调用协议变化时，才在 `tidoc_print/__init__.py` 增加组件版本；`tidoc_ocr` 同理，版本在 `tidoc_ocr/__init__.py` 维护。普通核心发布即使重新构建组件包，也不会让客户端误报组件更新。
- 客户端会同时检查各组件的版本标记、可执行文件和安装校验值；文件缺失或损坏时显示“需要修复”，最新版本也允许重新安装。
- OCR 识别组件只负责执行阿里云识别调用（`ocr-api.cn-hangzhou.aliyuncs.com`），密钥由用户在设置内自填并仅存本机；组件自检（`--self-test`）不联网。0.2.0 起，多页 PDF 临时拆页逐页识别并合并，`pypdf` 随组件打包；0.2.1 起，空名称负数折扣行会并回上一商品，重复两遍的完整商品名称会折叠。核心会按实际页数提示并记录调用次数。识别结果（含原始 JSON 和自动补齐 / 历史修正快照）落库在 `ocr_results` 表，与更新通道无关。Windows 核心以不创建控制台窗口的方式启动组件，避免批量识别逐张闪出终端黑框。
- 下载 / 安装过程会在设置弹窗里显示进行中状态，组件安装完成后立即刷新状态。
- 软件内始终提供 GitHub Releases 手动下载入口；更新服务不可用时仍可访问。
- 高级数据维护可清理拖拽中转文件与旧更新包，但会保留业务数据、导出文件、组件和待安装更新包。
- 客户端不保存腾讯云密钥，只做 HTTPS 下载和 SHA256 完整性校验。
- macOS 上如果应用内 Python/OpenSSL 不能验证系统已信任的证书链，会用系统 `curl` 重试读取清单和下载文件；不会关闭证书校验。

## 发布自动化

- 推送 `v*.*.*` tag 后，GitHub Actions 自动测试、构建 macOS DMG 与 Windows 安装器、生成更新清单、上传 COS，并发布带安装包的 GitHub Release。
- Windows / macOS 打印组件与 OCR 组件打包后必须执行 `--self-test`，确认组件代码与重依赖能从最终成品加载；自检失败会中止发布。
- macOS 核心包构建后剥离实际存在的 Mach-O 符号（`*.so` / `*.dylib` / 名为 `Python` 的解释器，以及主可执行文件），再以 ad-hoc 签名重签；没有匹配文件时跳过，不中断发布。DMG 使用 ULMO（LZMA）压缩。打印组件在 PyInstaller 收集阶段剥离符号，由 `--self-test` 兜底校验。
- Release 说明和客户端 What’s changed 都从 `CHANGELOG.md` 最新一节生成，避免三处手工维护后内容不一致。
- Windows 核心改为 Inno Setup 的按用户安装器；安装到用户目录，不要求管理员权限，并提供开始菜单、可选桌面快捷方式和卸载入口。
- Windows 安装器把 `.tidoc` 扩展名注册为「Tidoc 绑定包」（按用户写入 `Software\Classes`）：资源管理器显示应用内嵌 logo 图标，双击调用 `tidoc.exe "<文件>"` 进入导入预览；卸载时自动删除关联键。Windows 构建的应用图标来自 `tidoc/web/assets/tidoc-logo.ico`（多尺寸 16–256），macOS 使用同目录的 `tidoc-logo.icns`；两者由 `scripts/generate_brand_assets.py` 从同一张方形源图生成。
