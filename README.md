# tidoc

[![CI](https://github.com/totok22/tidoc/actions/workflows/ci.yml/badge.svg)](https://github.com/totok22/tidoc/actions/workflows/ci.yml)
[![Release](https://github.com/totok22/tidoc/actions/workflows/release.yml/badge.svg)](https://github.com/totok22/tidoc/actions/workflows/release.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> 通用的报账凭证管理与整理工具。

tidoc 是一款跨平台桌面程序，以一张发票为工作单元，完成发票导入、材料补齐、识别提醒、筛选和批次整理，并导出可交换或打印的报账材料。适合个人、团队和社团使用。

- **界面**：PyWebView 原生窗口 + HTML/CSS/JS
- **核心**：Python，本地运行，不监听网络端口
- **数据**：SQLite + 本地附件仓库
- **交换**：带 HMAC 签名的 `.tidoc` 绑定包
- **打印**：可选独立组件，生成合并 PDF 与 Word
- **云识别**：可选阿里云 OCR 组件，补齐明细的数量、规格等字段（按量计费，自填密钥）

## 下载与安装

- [官网下载](https://www.bitfsae.com/)：首页底部「报账软件」入口，自动选择 Windows / macOS。
- [GitHub Releases](https://github.com/totok22/tidoc/releases/latest)

- macOS：下载 `tidoc-core-macos-v{version}.dmg`，拖入“应用程序”。
- Windows：下载 `tidoc-core-windows-v{version}.exe`，按用户安装，默认不需要管理员权限。

核心安装包不含打印导出组件和 OCR 识别组件；均可在「设置 → 组件与更新」中安装，也可在开发环境执行 `pip install -r requirements-print.txt` / `pip install -r requirements-ocr.txt`。云识别还需在「设置 → 阿里云 OCR」中填写自己的阿里云 AccessKey。生成报账说明或验收单 Word 前，还需在「收款信息」中维护姓名、学号、电话、开户行和卡号。

完整操作演示见 [Bilibili 使用说明视频](https://www.bilibili.com/video/BV1XN3q69EPi/)。

## 工作流

1. **导入发票**：PDF 是创建条目的必要材料，XML 用于提高识别准确度，不能单独创建条目。支持单条、文件夹、多选、拖拽和粘贴导入；单个 `.tidoc` 绑定包也可直接拖入或粘贴并进入导入预览。
2. **补齐材料**：在卡片或详情添加付款截图、实物图和查验单。付款截图可用系统本地 OCR 尝试识别实付金额，识别失败或无法唯一匹配时手动选择。卡片上的「查验」会打开国家税务总局平台并预填发票字段；完成验证码和官网打印后，保存到下载、桌面、文档或自定义目录的 PDF 会自动识别归档，已有查验单也可直接上传。
3. **核对与整理**：在任一列表视图选中条目后都可点「重新识别」，并分别选择发票或付款截图；已经由当前识别规则处理且原文件未变化的材料自动跳过。付款截图未识别到金额，或各截图识别合计与发票总额不一致时进入识别提醒；阿里云云识别仍用于补齐发票明细的数量、规格（选中条目点「云识别」，或识别提醒视图一键识别全部）。条目有多张付款截图时，卡片付款按钮用不同颜色显示张数，高级筛选可只看「多张（2 张及以上）」；也可按抬头、报账人、状态、日期、金额、标签和备注筛选。卡片上的报账人、批次可直接编辑，材料按钮右键可打开已有文件。批次栏用「在办 / 已归档」切换工作面，点击批次右侧「⋯」可编辑批次、填写批次备注或归档，已归档批次可恢复到在办。设置 → 扩展可修改新建默认抬头，并分别设置付款截图、实物图、查验单和实付金额是否必需；发票固定必需，默认要求付款截图、查验单和实付金额，实物图可选。
4. **交换、导出与打印**：选中条目后可创建批次、导出 `.tidoc` 绑定包、总览 Excel 或附件整理包。绑定包导入前可预览并调整报账人、审核人、批次和标签，重复发票跳过并校验 HMAC。打印组件默认按条目合并发票、付款截图和查验单，也支持按材料分开导出，并生成报账说明、验收单 Word；若当前聚焦某个批次，会自动带入该批次已保存的批次备注；Word 与 PDF 按抬头隔离。

## 识别与数据规则

- 发票识别以 XML 优先、PDF 文本兜底，兼容常见数电发票的错位、分列明细和折扣行；缺明细、金额不闭合、抬头异常，以及北理工 / 教育基金会购买方税号缺失或不符等会形成识别提醒。
- 识别提醒与材料齐备是两套状态；严重问题会单独标出。全库按发票号或文件内容拦截重复发票，附件类型、发票归属和导入事务也会校验。
- 默认不依赖云端通用 OCR：系统 OCR 只用于付款截图金额和查验单发票号的轻量兜底，发票本身仍以 XML / PDF 文本解析为主。阿里云发票识别作为可选付费组件（见下），识别结果永久保存在条目里，可随时比对、采用，不会为查看结果重复计费。
- **报账人**是发票归属，只需姓名和审核人；**收款信息**只供打印 Word 使用。条目备注记录单张发票，标签用于筛选，附件备注只描述具体文件。

## 网络与更新

应用不做默认后台联网；税务查验、外部链接和更新均由用户触发。「启动后检查更新」默认关闭，开启后每 24 小时最多检查一次，且不会自动下载或安装。设置中的「软件与组件」分别显示核心和打印、OCR 组件状态，下载后校验 SHA256；组件缺失或损坏时可直接修复或重新安装。

阿里云云识别只在用户点击「云识别」等入口时访问阿里云接口（`ocr-api.cn-hangzhou.aliyuncs.com`），使用用户自填的 AccessKey，按量计费；应用不内置密钥、不做任何后台识别，密钥只保存在本机数据里。识别前会显示本次将调用的张数，已有 XML 权威数据的条目默认跳过，重复识别会提示再次计费。

## 开发

```bash
git clone https://github.com/totok22/tidoc.git
cd tidoc
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
pip install -r requirements-print.txt   # 可选：启用打印导出组件
pip install -r requirements-ocr.txt     # 可选：启用阿里云云识别组件

python -m tidoc                    # 启动应用
python -m tidoc --debug            # 打开 WebView 调试
pytest                             # 运行测试
```

从源码运行时优先使用当前工作区的 `tidoc_print` / `tidoc_ocr`；发布版只调用已安装并校验过的独立组件。

## 项目结构

```text
tidoc/            核心（pywebview + pypdf + Send2Trash）
├─ engine/        发票 XML / PDF 解析与金额校验
├─ db/            SQLite、附件仓库、报账人、条目和批次
├─ services/      导入、汇总、绑定包、导出、打印适配和更新
├─ web/           HTML/CSS/JS 前端
├─ api.py         PyWebView JS ↔ Python 桥
└─ app.py         应用入口
tidoc_print/      打印导出组件（可选，含 Word / PDF 重依赖）
tidoc_ocr/        OCR 识别组件（可选，含阿里云 SDK，只在用户点击时联网）
scripts/          构建、版本号注入和打包入口
tests/            pytest 测试
```

## 状态

- [x] 发票解析、付款截图金额识别、按识别规则版本重试、识别提醒与在线查验辅助
- [x] 条目、付款截图张数、带控件激活提示的筛选、标签、批次（在办 / 已归档）、默认抬头和材料要求配置
- [x] `.tidoc` 绑定包（选择、拖拽或粘贴导入）、Excel / 附件导出与 HMAC 校验
- [x] 可选打印导出组件（PDF / Word）
- [x] 可选阿里云 OCR 识别组件（软件内填密钥、结果落库、差异比对采用）
- [x] Windows / macOS 安装包与 CI 发布流程
- [x] 核心与打印、OCR 组件独立更新、SHA256 校验和组件修复
- [x] macOS 核心安装包体积优化（构建剥离符号、DMG 使用 LZMA 压缩）

设计细节见 [DESIGN.md](DESIGN.md)，变更记录见 [CHANGELOG.md](CHANGELOG.md)，更新发布流程见 [docs/UPDATE.md](docs/UPDATE.md)。

## 参与贡献

欢迎提交 Issue 和 Pull Request。请先阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证

[MIT](LICENSE)
