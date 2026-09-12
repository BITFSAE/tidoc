# tidoc

[![CI](https://github.com/totok22/tidoc/actions/workflows/ci.yml/badge.svg)](https://github.com/totok22/tidoc/actions/workflows/ci.yml)
[![Release](https://github.com/totok22/tidoc/actions/workflows/release.yml/badge.svg)](https://github.com/totok22/tidoc/actions/workflows/release.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> 通用的报账凭证管理与整理工具。

tidoc 是一款跨平台桌面程序，以一张发票为工作单元，完成发票导入、材料补齐、识别提醒、筛选和批次整理，并导出可交换或打印的报账材料。适合个人、团队和社团使用。

- **整理**：把一张发票和对应付款截图、实物图、查验单放在同一个条目中。
- **核对**：集中查看待补材料、识别提醒、实付差异和阿里云识别结果。
- **协作**：通过 `.tidoc` 绑定包交接完整条目和附件。
- **输出**：生成总览 Excel、附件整理包、合并 PDF、报账说明和验收单。
- **存储**：业务数据和附件保存在本机，可整体迁移和备份。

## 开始使用

- [官网下载](https://www.bitfsae.com/)：首页底部选择「报账软件」。
- [GitHub Releases](https://github.com/totok22/tidoc/releases/latest)：下载最新 Windows 或 macOS 安装包。
- [完整使用指南](docs/USER_GUIDE.md)：从安装、导入到批次、打印、阿里云 OCR 和数据备份。
- [Bilibili 操作演示](https://www.bilibili.com/video/BV1XN3q69EPi/)：视频为早期版本，操作流程仍可参考。

Windows 下载 `tidoc-core-windows-v{version}.exe` 后按提示安装。macOS 下载 `tidoc-core-macos-v{version}.dmg`，打开后把 Tidoc 拖入“应用程序”。

## 基本概念

- **条目**：一张发票对应一个条目，也就是主界面中的一张卡片。
- **材料**：发票 PDF/XML、付款截图、实物图、查验单和实付金额。
- **报账人**：发票的归属人，包含姓名和审核人。
- **标签**：用于分类和筛选，一个条目可以有多个标签。
- **报账批次**：准备一起提交、导出或打印的一组条目。
- **绑定包**：扩展名为 `.tidoc` 的交接文件，包含条目、附件、报账人和完整性校验信息。
- **识别提醒**：提示发票内容需要核对；**材料齐备**：表示当前材料要求已经满足。

## 五步入门

1. **创建报账人**：首次打开时填写姓名和审核人。多人共用时继续添加报账人。
2. **导入发票**：点击「导入发票」选择 PDF、XML 或整个文件夹，也可以拖拽、粘贴文件。
3. **补齐材料**：在卡片或详情中添加付款截图、实物图和查验单，确认实付金额。
4. **核对并整理**：处理「待补材料」和「识别提醒」，再把条目加入报账批次。
5. **导出或打印**：导出 `.tidoc` 绑定包、总览 Excel、附件整理包，或生成合并 PDF 和 Word。

打印和 Word 需要安装打印导出组件。阿里云 OCR 用于补齐本地识别遗漏，两项都可以在「设置 → 组件与更新」中按需安装。

所有详细操作、常见问题、阿里云 AccessKey 开通步骤和数据备份方法都集中在[《Tidoc 使用指南》](docs/USER_GUIDE.md)。

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
icon/             方形母版及 Windows 圆角 / macOS 方形平台图标
tests/            pytest 测试
```

## 状态

- [x] 发票解析、付款截图金额识别、按识别规则版本重试、识别提醒与在线查验辅助
- [x] 报账抬头与税号可配置（多学校 / 单位通用），默认内置北理工与教育基金会
- [x] Windows / macOS 关联 `.tidoc` 文件并显示平台适配图标（Windows 双击进入导入预览）
- [x] 单实例启动与重复启动激活；运行中再次打开 `.tidoc` 会转交给已有窗口
- [x] 条目、付款截图张数、带控件激活提示的筛选、标签、批次（在办 / 已归档）、默认抬头和材料要求配置
- [x] 浅色、深色与跟随系统的外观主题（即时切换、启动防闪烁）
- [x] `.tidoc` 绑定包（含阿里云识别结果与状态；选择、拖拽或粘贴导入）、Excel / 附件导出与 HMAC 校验
- [x] 可选打印导出组件（PDF / Word）
- [x] 可选阿里云 OCR 识别组件（软件内填密钥、结果落库、差异比对采用）
- [x] Windows / macOS 安装包与 CI 发布流程
- [x] 核心与打印、OCR 组件独立更新、SHA256 校验和组件修复
- [x] macOS 核心安装包体积优化（依赖库剥离符号、DMG 使用 LZMA 压缩）与成品启动自检

设计细节见 [DESIGN.md](DESIGN.md)，变更记录见 [CHANGELOG.md](CHANGELOG.md)，更新发布流程见 [docs/UPDATE.md](docs/UPDATE.md)。

## 参与贡献

欢迎提交 Issue 和 Pull Request。请先阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证

[MIT](LICENSE)
