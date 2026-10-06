# AI 适配开发工作流

把适配目录和 fixture 交给 AI。要求它只编辑适配包文件，并用 CLI 的实际诊断修复问题。

## 可复制任务

```text
目标团队和用途：填写单位、经费或报账场景。
适配目录：./my-team
现有 Word 模板：填写文件路径；没有则说明。
抬头：列出名称、税号、简称和默认项。
必须收集的字段：注明作用域、类型、何时必填。
材料要求：列出材料角色、数量和触发条件。
输出样例：说明需要的 Word、材料 PDF、Excel 或附件包。
Fixture：使用虚构姓名、发票和账号，保存到 ./fixtures。

请只修改适配目录和 fixture。不要修改 Tidoc 核心源码、Schema、注册表或打包脚本。
先运行 validate，再运行 explain、render 和 test。根据机器诊断修正包。
最后运行 pack。若包含 Word 模板，必须完成真实渲染。
不要编造未注册字段、设置、条件操作或模板变量。
报告执行的命令、生成文件和未验证事项。不要声称完成了未实际进行的办公软件检查。
```

建议按 `init`、`validate`、`explain`、`render`、`test`、`diff`、`pack` 的顺序操作。示例和 fixture 位于 [`examples/adapters/`](../../examples/adapters/)。格式规范以 [Schema](../../schemas/team-adapter/1/) 和[注册表参考](REFERENCE.md)为准。
