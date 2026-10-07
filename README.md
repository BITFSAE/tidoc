# tidoc

[![CI](https://github.com/totok22/tidoc/actions/workflows/ci.yml/badge.svg)](https://github.com/totok22/tidoc/actions/workflows/ci.yml)
[![Release](https://github.com/totok22/tidoc/actions/workflows/release.yml/badge.svg)](https://github.com/totok22/tidoc/actions/workflows/release.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> 通用的报账凭证管理与整理工具。

tidoc 是一款跨平台桌面程序，以一张发票为工作单元，完成发票导入、材料补齐、识别提醒、筛选和批次整理，并导出可交换或打印的报账材料。适合个人、团队和社团使用。

- **整理**：把一张发票和对应付款截图、实物图、查验单放在同一个条目中。
- **核对**：集中查看待补材料、识别提醒、实付差异和阿里云识别结果。
- **协作**：通过 `.tidoc` 绑定包交接完整条目和附件。
- **输出**：生成总览 Excel、附件整理包、材料 PDF，以及报账方案定义的 Word 文档。
- **存储**：业务数据和附件保存在本机，可整体迁移和备份。

## 开始使用

- [官网下载](https://www.bitfsae.com/)：首页底部选择「报账软件」。
- [GitHub Releases](https://github.com/totok22/tidoc/releases/latest)：下载最新 Windows 或 macOS 安装包。
- [完整使用指南](docs/USER_GUIDE.md)：从安装、导入到批次、打印、阿里云 OCR 和数据备份。
- [Bilibili 操作演示](https://www.bilibili.com/video/BV1XN3q69EPi/)：视频为早期版本，操作流程仍可参考。

Windows 下载 `tidoc-core-windows-v{version}.exe` 后按提示安装。macOS 下载 `tidoc-core-macos-v{version}.dmg`，打开后把 Tidoc 拖入“应用程序”。

## 基本概念

- **条目**：一张发票对应一个条目，也就是主界面中的一张卡片。
- **材料**：发票 PDF/XML、付款截图、实物图、查验单，以及方案声明的审批单、合同等文件。实付金额单独记录。
- **报账人**：发票的归属人，姓名必填，审核人按方案要求填写。
- **标签**：用于分类和筛选，一个条目可以有多个标签。
- **报账批次**：准备一起提交、导出或打印的一组条目。
- **绑定包**：扩展名为 `.tidoc` 的交接文件，包含条目、附件、报账人和完整性校验信息。
- **识别提醒**：提示发票内容需要核对；**材料齐备**：表示当前材料要求已经满足。

## 五步入门

1. **选择方案并创建报账人**：首次打开选择通用、BITFSAE 或导入团队方案，再填写姓名。审核人按方案要求填写。
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

团队适配包开发入口见[开发手册](docs/adapters/README.md)，目标、测试覆盖和待验收事项见[《团队适配包实施与验收记录》](docs/TEAM_ADAPTER_PLAN.md)。

```bash
python -m tidoc.adapter_tools --help
python -m tidoc.adapter_tools validate examples/adapters/generic
```

## 项目结构

```text
tidoc/            核心（pywebview + pypdf + Send2Trash + jsonschema）
├─ engine/        发票 XML / PDF 解析与金额校验
├─ adapters/      适配包协议、规则、内置包和方案服务
├─ adapter_tools/ 适配包命令行工具
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
- [x] 报账抬头与税号可配置，首次可选通用方案或含北理工与教育基金会抬头的 BITFSAE 方案
- [x] Windows / macOS 关联 `.tidoc` 文件并显示平台适配图标（Windows 双击进入导入预览）
- [x] 单实例启动与重复启动激活；运行中再次打开 `.tidoc` 会转交给已有窗口
- [x] 条目、详情内粘贴材料、付款截图张数、带控件激活提示的筛选、标签、导入时选择单一归属批次（在办 / 已归档）、弹窗内新建报账人、默认抬头和材料要求配置
- [x] 浅色、深色与跟随系统的外观主题（即时切换、启动防闪烁）
- [x] `.tidoc` 绑定包（可按报账人或逐条选择导入、为个别条目指定不同批次，并把新增材料、备注、标签等补充到已有发票）、Excel / 附件导出与 HMAC 校验
- [x] 可选打印导出组件（PDF / Word）
- [x] 可选阿里云 OCR 识别组件（软件内填密钥、结果落库、差异比对采用）
- [x] Windows / macOS 安装包与 CI 发布流程
- [x] 核心与打印、OCR 组件独立更新、SHA256 校验和组件修复；核心支持后台下载、Windows 四路加速与断点续传、分阶段进度显示及无安装向导的重启更新
- [x] 可选测试版更新通道（设置中开启，默认关闭）：预发布 tag 只写入测试版清单，不改写正式版清单，也不重复上传正式版在用的组件版本
- [x] 可选在条目卡片显示精确到分钟的创建时间（默认关闭）
- [x] macOS 核心安装包体积优化（依赖库剥离符号、DMG 使用 LZMA 压缩）与成品启动自检
- [x] 团队适配包、不可变修订、类型化字段、自定义材料、条件规则、完整收款对象、四类输出和绑定包 v5
- [x] 团队适配包 CLI、真实示例模板、开发手册、历史导出、取消与失败恢复及资源自检
- [x] 核心四类输出预检与打印 IPC 校验完整共享 context Schema；发布测试安装打印依赖，Windows 核心在生成安装器前执行成品自检并检查退出码
- [x] 打印组件自检经正式 IPC 生成中文／金额 DOCX 和含 PDF、图片及编号的材料 PDF，核对内容与页数后清理临时文件
- [x] 团队适配 UI 入口：按方案显示审核人、动态字段、批次输出与收款、本次调整和失败取消反馈；运行回归及真实应用核对见计划记录
- [x] 导出显示实际生成阶段，长导出期间仍可查询进度和请求取消；线程与 API 锁回归用例见计划记录
- [x] 默认输出区分缺省继承和显式全不选，预检与界面保留空选择；具体语义见[输出说明](docs/adapters/OUTPUTS.md#默认输出与本次选择)
- [x] 完整全库回归、Node 源码语法检查及核心／打印源码自检通过；当前结果见计划记录
- [ ] 团队适配完整验收：逐项确认 W1～W12 和 A01～A26，复验 XML 单独扫描导入、兼容组合、升级迁移及真实应用
- [ ] Windows / macOS 人工办公软件版式核对及完整跨平台发布验收

上面的已实现项表示代码和功能入口已存在，不表示团队适配计划已经全部验收。源码全库回归、预览时序调整的源码检查及来源集成测试已通过；旧测试引用的本机真实发票样本目录缺失，相关测试跳过，适配测试没有跳过。实际 UI、兼容迁移、发布成品和办公软件检查仍待验收。已有性能结果只覆盖单机内存数据库，具体证据见[验收覆盖表](docs/TEAM_ADAPTER_PLAN.md#23-实施与验收覆盖记录)。

设计细节见 [DESIGN.md](DESIGN.md)，变更记录见 [CHANGELOG.md](CHANGELOG.md)，更新发布流程见 [docs/UPDATE.md](docs/UPDATE.md)。

## 参与贡献

欢迎提交 Issue 和 Pull Request。请先阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证

[MIT](LICENSE)
