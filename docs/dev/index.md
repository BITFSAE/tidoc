---
title: 开发相关：参与开发、发布与团队适配包
description: 面向参与开发、发布软件或编写团队报账方案的人。只使用软件的读者请看使用指南。
---

# 开发相关

这一部分写给要参与开发、发布软件或为团队编写报账方案的人。只是使用软件的话，不需要阅读这里的内容，请看[使用指南](/guide/concepts)。

## 参与开发

- [参与贡献](/dev/contributing)：提交问题、改动流程和代码风格。
- [设计文档](/dev/design)：整体架构、数据模型和主要功能的设计取舍。
- 源码、问题反馈和版本下载在 [GitHub 仓库](https://github.com/BITFSAE/tidoc)，本地开发环境的搭建命令见仓库 README 的[开发一节](https://github.com/BITFSAE/tidoc#开发)。

## 发布

- [发布与更新机制](/dev/release)：安装包和组件的发布流程、测试版通道，以及软件检查和安装更新的方式。

## 团队适配包

团队可以用适配包定义自己的抬头、材料要求和输出文档（Word、PDF、Excel 等）。适配包只描述规则，不能执行代码，也不能访问软件的数据库。

- [适配包开发](/dev/adapters/README)：从示例开始，用命令行工具检查和打包。
- [包格式](/dev/adapters/FORMAT)、[字段与设置](/dev/adapters/FIELDS)、[条件规则](/dev/adapters/RULES)、[模板](/dev/adapters/TEMPLATES)、[输出](/dev/adapters/OUTPUTS)。
- [兼容性](/dev/adapters/COMPATIBILITY)、[AI 工作流](/dev/adapters/AI_WORKFLOW)、[注册表参考](/dev/adapters/REFERENCE)。

适配包的使用方法（导入和选择方案）面向普通用户，见[报账方案](/guide/schemes)。
