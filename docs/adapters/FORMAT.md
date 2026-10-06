# 包格式

适配包源目录包含 `manifest.json`、`scheme.json`，以及可选的字段、材料、规则、输出定义和资源。发布文件使用 `.tidoc-preset` 扩展名，内容是 ZIP。`.tidoc` 扩展名仍用于材料绑定包。

```text
my-team/
├── manifest.json
├── scheme.json
├── fields.json
├── materials.json
├── rules.json
├── outputs.json
├── templates/
└── assets/
```

JSON 使用 UTF-8 和标准 JSON 语法。包内路径使用 `/`，并且必须相对包根目录。`checksums.json` 由 `pack` 生成。包摘要不包含它自身，也不受 ZIP 文件顺序影响。

`manifest.json` 声明 `format: tidoc-team-adapter`、Schema 版本、包 ID、包版本、名称和作者。包 ID 使用小写点分名称。版本使用三段数字，可带预发布后缀。同一包 ID 和版本不能对应不同内容。

Schema 采用 JSON Schema Draft 2020-12。正式属性不接受未知字段。作者备注只能写入 `x_metadata`，运行时不读取其含义。领域检查还会验证跨文件引用、能力声明和模板变量。

加载器限制压缩包大小、展开大小、文件数、单文件大小和压缩比例。它拒绝重复路径、大小写冲突、符号链接、目录穿越、DOCX 宏及外部资源关系。包内不允许 YAML、脚本、可执行文件或运行时 include。
