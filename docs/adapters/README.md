# 团队适配包开发

团队适配包用 JSON、DOCX 模板和静态资源描述报账规则。适配包不能执行代码，也不能访问核心数据库。

完整格式以 JSON Schema 和运行时注册表为准。开发时从一个示例复制，再运行 CLI 检查。

```bash
python -m tidoc.adapter_tools init ./my-team --from generic --id org.example.team
python -m tidoc.adapter_tools validate ./my-team
python -m tidoc.adapter_tools explain ./my-team --fixture examples/adapters/fixtures/generic.json
python -m tidoc.adapter_tools render ./my-team --fixture examples/adapters/fixtures/generic.json --out ./preview
python -m tidoc.adapter_tools test ./my-team --cases ./cases
python -m tidoc.adapter_tools pack ./my-team --out ./dist
```

JSON 输出可用于脚本和 AI 工作流。命令以 `0` 表示通过，以 `1` 表示包或样例失败，以 `2` 表示渲染依赖不可用。`pack` 会验证 DOCX 并执行真实渲染；缺少打印依赖时不会生成发布包。

## 文档

- [包格式](FORMAT.md)：目录、版本、校验和与安全约束。
- [字段与设置](FIELDS.md)：字段作用域和设置来源。
- [条件规则](RULES.md)：有限条件语法及三值逻辑。
- [模板](TEMPLATES.md)：DOCX 变量、循环和渲染要求。
- [输出](OUTPUTS.md)：Word、PDF、Excel 和附件包。
- [兼容性](COMPATIBILITY.md)：核心与打印组件的兼容边界。
- [AI 工作流](AI_WORKFLOW.md)：可复制的适配开发任务说明。
- [注册表参考](REFERENCE.md)：由代码注册表生成的设置、字段和能力清单。

协议 Schema 位于 [`schemas/team-adapter/1/`](../../schemas/team-adapter/1/)。开发者应同时阅读 [适配计划](../TEAM_ADAPTER_PLAN.md) 的公开边界。计划记录实施状态，不替代本目录的协议说明。
