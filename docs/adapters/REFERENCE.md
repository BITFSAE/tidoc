# 注册表参考

设置、字段和能力表由 `python -m tidoc.adapter_tools reference` 生成，本文补充来源与权限说明。设置由 `tidoc/adapters/registry.py` 定义；上下文字段及文件名变量由核心与组件共用 `tidoc_print/context_fields.json` 定义。

## 设置

下表列出注册表基础默认值。`print.default_outputs` 在缺省时还会继承方案及输出声明；显式 `[]` 表示全不选，完整规则见[输出说明](OUTPUTS.md#默认输出与本次选择)。

| 设置 ID | 类型 | 核心默认值 | 允许作用域 |
| --- | --- | --- | --- |
| `entry.default_title_id` | `text` | `null` | scheme |
| `entry.default_paid_to_invoice` | `boolean` | `true` | scheme |
| `entry.suggested_tags` | `multiselect` | `[]` | scheme |
| `profile.reviewer_required` | `boolean` | `false` | scheme |
| `profile.reviewer_presentation` | `select` | `"visible"` | scheme |
| `profile.default_view` | `select` | `"self"` | scheme |
| `payee.personnel_number_label` | `text` | `"人员编号"` | scheme |
| `print.default_outputs` | `multiselect` | `[]` | scheme |
| `print.numbering` | `boolean` | `true` | scheme, batch, export |
| `print.image_layout` | `select` | `"a4_landscape_2"` | scheme, batch, export |
| `print.content_order` | `select` | `"entry"` | scheme, batch, export |
| `print.amount_basis` | `select` | `"invoice"` | scheme, batch, export |
| `print.payee_mode` | `select` | `"none"` | scheme, batch, export |
| `print.sort_by` | `select` | `"selection"` | scheme, batch, export |
| `assist.cloud_ocr_visible` | `boolean` | `true` | scheme |
| `assist.verification_visible` | `boolean` | `true` | scheme |
| `assist.payment_ocr` | `select` | `"manual"` | scheme |
| `transfer.include_notes` | `boolean` | `true` | scheme |
| `transfer.include_tags` | `boolean` | `true` | scheme |

## 模板及列映射字段

下面是完整模板目录。规则条件另受阶段白名单约束，见 [规则语义](RULES.md)；表中出现的字段不一定能用于条件。

| 字段 | 类型 |
| --- | --- |
| `batch` | `object` |
| `batch.fields` | `object` |
| `batch.id` | `text` |
| `batch.name` | `text` |
| `batch.note` | `text` |
| `batch.notes` | `text` |
| `diagnostics` | `array` |
| `diagnostics[]` | `object` |
| `diagnostics[].code` | `text` |
| `diagnostics[].file` | `text` |
| `diagnostics[].location` | `text` |
| `diagnostics[].message` | `text` |
| `diagnostics[].rule_id` | `text` |
| `diagnostics[].severity` | `text` |
| `diagnostics[].stage` | `text` |
| `diagnostics[].suggested_action` | `text` |
| `diagnostics[].target` | `text` |
| `entries` | `array` |
| `entries[]` | `object` |
| `entries[].actual_item_name` | `text` |
| `entries[].actual_name` | `text` |
| `entries[].attachment_count` | `integer` |
| `entries[].batch` | `object` |
| `entries[].batch.fields` | `object` |
| `entries[].batch.id` | `text` |
| `entries[].batch.name` | `text` |
| `entries[].batch.note` | `text` |
| `entries[].batch.notes` | `text` |
| `entries[].buyer_name` | `text` |
| `entries[].buyer_tax_id` | `text` |
| `entries[].check_status` | `text` |
| `entries[].claimant` | `object` |
| `entries[].claimant.id` | `text` |
| `entries[].claimant.name` | `text` |
| `entries[].claimant.reviewer` | `text` |
| `entries[].completeness` | `object` |
| `entries[].completeness.missing` | `array` |
| `entries[].completeness.missing[]` | `text` |
| `entries[].completeness.ready` | `boolean` |
| `entries[].created_at` | `text` |
| `entries[].entry_id` | `text` |
| `entries[].fields` | `object` |
| `entries[].id` | `text` |
| `entries[].invoice` | `object` |
| `entries[].invoice.buyer_name` | `text` |
| `entries[].invoice.buyer_tax_id` | `text` |
| `entries[].invoice.date` | `text` |
| `entries[].invoice.number` | `text` |
| `entries[].invoice.paid_amount` | `money` |
| `entries[].invoice.seller` | `text` |
| `entries[].invoice.title_id` | `text` |
| `entries[].invoice.total` | `money` |
| `entries[].invoice_date` | `date` |
| `entries[].invoice_no` | `text` |
| `entries[].items` | `array` |
| `entries[].items[]` | `object` |
| `entries[].items[].actual_name` | `text` |
| `entries[].items[].defaults` | `array` |
| `entries[].items[].defaults[]` | `text` |
| `entries[].items[].index` | `integer` |
| `entries[].items[].is_summary` | `boolean` |
| `entries[].items[].product_name` | `text` |
| `entries[].items[].quantity` | `decimal` |
| `entries[].items[].spec` | `text` |
| `entries[].items[].total` | `money` |
| `entries[].items[].unit` | `text` |
| `entries[].items[].unit_price` | `money` |
| `entries[].materials` | `array` |
| `entries[].materials[]` | `object` |
| `entries[].materials[].id` | `text` |
| `entries[].materials[].label` | `text` |
| `entries[].materials[].name` | `text` |
| `entries[].materials[].role_id` | `text` |
| `entries[].materials[].type` | `text` |
| `entries[].notes` | `text` |
| `entries[].paid_amount` | `money` |
| `entries[].product_name` | `text` |
| `entries[].revision_id` | `text` |
| `entries[].scheme_id` | `text` |
| `entries[].seller` | `text` |
| `entries[].status` | `text` |
| `entries[].title` | `object` |
| `entries[].title.color` | `text` |
| `entries[].title.id` | `text` |
| `entries[].title.name` | `text` |
| `entries[].title.short_name` | `text` |
| `entries[].title.tax_id` | `text` |
| `entries[].title_id` | `text` |
| `entries[].total` | `money` |
| `export` | `object` |
| `export.date` | `date` |
| `export.fields` | `object` |
| `export.options` | `object` |
| `export.output_id` | `text` |
| `invoice` | `object` |
| `invoice.buyer_name` | `text` |
| `invoice.buyer_tax_id` | `text` |
| `invoice.date` | `text` |
| `invoice.number` | `text` |
| `invoice.paid_amount` | `money` |
| `invoice.seller` | `text` |
| `invoice.title_id` | `text` |
| `invoice.total` | `money` |
| `payee` | `object` |
| `payee.account_number` | `text` |
| `payee.account_type` | `select` |
| `payee.bank_name` | `text` |
| `payee.contact` | `text` |
| `payee.created_at` | `text` |
| `payee.fields` | `object` |
| `payee.id` | `text` |
| `payee.name` | `text` |
| `payee.person_name` | `text` |
| `payee.personnel_id` | `text` |
| `payee.personnel_number` | `text` |
| `payee.phone` | `text` |
| `payee.updated_at` | `text` |
| `rows` | `array` |
| `rows[]` | `object` |
| `rows[].actual_name` | `text` |
| `rows[].amount` | `money` |
| `rows[].claimant` | `text` |
| `rows[].defaults` | `array` |
| `rows[].entry` | `object` |
| `rows[].entry.actual_item_name` | `text` |
| `rows[].entry.actual_name` | `text` |
| `rows[].entry.attachment_count` | `integer` |
| `rows[].entry.batch` | `object` |
| `rows[].entry.batch.fields` | `object` |
| `rows[].entry.batch.id` | `text` |
| `rows[].entry.batch.name` | `text` |
| `rows[].entry.batch.note` | `text` |
| `rows[].entry.batch.notes` | `text` |
| `rows[].entry.buyer_name` | `text` |
| `rows[].entry.buyer_tax_id` | `text` |
| `rows[].entry.check_status` | `text` |
| `rows[].entry.claimant` | `object` |
| `rows[].entry.claimant.id` | `text` |
| `rows[].entry.claimant.name` | `text` |
| `rows[].entry.claimant.reviewer` | `text` |
| `rows[].entry.completeness` | `object` |
| `rows[].entry.completeness.missing` | `array` |
| `rows[].entry.completeness.missing[]` | `text` |
| `rows[].entry.completeness.ready` | `boolean` |
| `rows[].entry.created_at` | `text` |
| `rows[].entry.entry_id` | `text` |
| `rows[].entry.fields` | `object` |
| `rows[].entry.id` | `text` |
| `rows[].entry.invoice` | `object` |
| `rows[].entry.invoice.buyer_name` | `text` |
| `rows[].entry.invoice.buyer_tax_id` | `text` |
| `rows[].entry.invoice.date` | `text` |
| `rows[].entry.invoice.number` | `text` |
| `rows[].entry.invoice.paid_amount` | `money` |
| `rows[].entry.invoice.seller` | `text` |
| `rows[].entry.invoice.title_id` | `text` |
| `rows[].entry.invoice.total` | `money` |
| `rows[].entry.invoice_date` | `date` |
| `rows[].entry.invoice_no` | `text` |
| `rows[].entry.items` | `array` |
| `rows[].entry.items[]` | `object` |
| `rows[].entry.items[].actual_name` | `text` |
| `rows[].entry.items[].defaults` | `array` |
| `rows[].entry.items[].defaults[]` | `text` |
| `rows[].entry.items[].index` | `integer` |
| `rows[].entry.items[].is_summary` | `boolean` |
| `rows[].entry.items[].product_name` | `text` |
| `rows[].entry.items[].quantity` | `decimal` |
| `rows[].entry.items[].spec` | `text` |
| `rows[].entry.items[].total` | `money` |
| `rows[].entry.items[].unit` | `text` |
| `rows[].entry.items[].unit_price` | `money` |
| `rows[].entry.materials` | `array` |
| `rows[].entry.materials[]` | `object` |
| `rows[].entry.materials[].id` | `text` |
| `rows[].entry.materials[].label` | `text` |
| `rows[].entry.materials[].name` | `text` |
| `rows[].entry.materials[].role_id` | `text` |
| `rows[].entry.materials[].type` | `text` |
| `rows[].entry.notes` | `text` |
| `rows[].entry.paid_amount` | `money` |
| `rows[].entry.product_name` | `text` |
| `rows[].entry.revision_id` | `text` |
| `rows[].entry.scheme_id` | `text` |
| `rows[].entry.seller` | `text` |
| `rows[].entry.status` | `text` |
| `rows[].entry.title` | `object` |
| `rows[].entry.title.color` | `text` |
| `rows[].entry.title.id` | `text` |
| `rows[].entry.title.name` | `text` |
| `rows[].entry.title.short_name` | `text` |
| `rows[].entry.title.tax_id` | `text` |
| `rows[].entry.title_id` | `text` |
| `rows[].entry.total` | `money` |
| `rows[].entry_id` | `text` |
| `rows[].first_for_invoice` | `boolean` |
| `rows[].id` | `text` |
| `rows[].index` | `integer` |
| `rows[].invoice` | `object` |
| `rows[].invoice.buyer_name` | `text` |
| `rows[].invoice.buyer_tax_id` | `text` |
| `rows[].invoice.date` | `text` |
| `rows[].invoice.number` | `text` |
| `rows[].invoice.paid_amount` | `money` |
| `rows[].invoice.seller` | `text` |
| `rows[].invoice.title_id` | `text` |
| `rows[].invoice.total` | `money` |
| `rows[].invoice_date` | `date` |
| `rows[].invoice_no` | `text` |
| `rows[].invoice_total` | `money` |
| `rows[].is_first` | `boolean` |
| `rows[].is_summary` | `boolean` |
| `rows[].paid_amount` | `money` |
| `rows[].product_name` | `text` |
| `rows[].quantity` | `decimal` |
| `rows[].reviewer` | `text` |
| `rows[].seller` | `text` |
| `rows[].spec` | `text` |
| `rows[].storage_location` | `text` |
| `rows[].tax_id` | `text` |
| `rows[].title` | `text` |
| `rows[].total` | `money` |
| `rows[].unit` | `text` |
| `rows[].unit_price` | `money` |
| `schema_version` | `integer` |
| `scheme` | `object` |
| `scheme.department` | `text` |
| `scheme.fields` | `object` |
| `scheme.id` | `text` |
| `scheme.name` | `text` |
| `scheme.organization` | `object` |
| `scheme.organization.department` | `text` |
| `scheme.organization.name` | `text` |
| `scheme.organization.purpose` | `text` |
| `scheme.organization.storage_location` | `text` |
| `scheme.package_id` | `text` |
| `scheme.package_version` | `text` |
| `scheme.purpose` | `text` |
| `scheme.revision_id` | `text` |
| `scheme.storage_location` | `text` |
| `title` | `object` |
| `title.color` | `text` |
| `title.id` | `text` |
| `title.name` | `text` |
| `title.short_name` | `text` |
| `title.tax_id` | `text` |
| `totals` | `object` |
| `totals.amount` | `money` |
| `totals.amount_basis` | `text` |
| `totals.count` | `integer` |
| `totals.entry_count` | `integer` |
| `totals.invoice` | `money` |
| `totals.known_invoice` | `money` |
| `totals.known_paid` | `money` |
| `totals.missing_invoice` | `integer` |
| `totals.missing_paid` | `integer` |
| `totals.missing_paid_count` | `integer` |
| `totals.missing_selected` | `integer` |
| `totals.paid` | `money` |
| `totals.row_count` | `integer` |
| `totals.selected` | `money` |
| `entry` | `object` |
| `entry.actual_item_name` | `text` |
| `entry.actual_name` | `text` |
| `entry.attachment_count` | `integer` |
| `entry.batch` | `object` |
| `entry.batch.fields` | `object` |
| `entry.batch.id` | `text` |
| `entry.batch.name` | `text` |
| `entry.batch.note` | `text` |
| `entry.batch.notes` | `text` |
| `entry.buyer_name` | `text` |
| `entry.buyer_tax_id` | `text` |
| `entry.check_status` | `text` |
| `entry.claimant` | `object` |
| `entry.claimant.id` | `text` |
| `entry.claimant.name` | `text` |
| `entry.claimant.reviewer` | `text` |
| `entry.completeness` | `object` |
| `entry.completeness.missing` | `array` |
| `entry.completeness.missing[]` | `text` |
| `entry.completeness.ready` | `boolean` |
| `entry.created_at` | `text` |
| `entry.entry_id` | `text` |
| `entry.fields` | `object` |
| `entry.id` | `text` |
| `entry.invoice` | `object` |
| `entry.invoice.buyer_name` | `text` |
| `entry.invoice.buyer_tax_id` | `text` |
| `entry.invoice.date` | `text` |
| `entry.invoice.number` | `text` |
| `entry.invoice.paid_amount` | `money` |
| `entry.invoice.seller` | `text` |
| `entry.invoice.title_id` | `text` |
| `entry.invoice.total` | `money` |
| `entry.invoice_date` | `date` |
| `entry.invoice_no` | `text` |
| `entry.items` | `array` |
| `entry.items[]` | `object` |
| `entry.items[].actual_name` | `text` |
| `entry.items[].defaults` | `array` |
| `entry.items[].defaults[]` | `text` |
| `entry.items[].index` | `integer` |
| `entry.items[].is_summary` | `boolean` |
| `entry.items[].product_name` | `text` |
| `entry.items[].quantity` | `decimal` |
| `entry.items[].spec` | `text` |
| `entry.items[].total` | `money` |
| `entry.items[].unit` | `text` |
| `entry.items[].unit_price` | `money` |
| `entry.materials` | `array` |
| `entry.materials[]` | `object` |
| `entry.materials[].id` | `text` |
| `entry.materials[].label` | `text` |
| `entry.materials[].name` | `text` |
| `entry.materials[].role_id` | `text` |
| `entry.materials[].type` | `text` |
| `entry.notes` | `text` |
| `entry.paid_amount` | `money` |
| `entry.product_name` | `text` |
| `entry.revision_id` | `text` |
| `entry.scheme_id` | `text` |
| `entry.seller` | `text` |
| `entry.status` | `text` |
| `entry.title` | `object` |
| `entry.title.color` | `text` |
| `entry.title.id` | `text` |
| `entry.title.name` | `text` |
| `entry.title.short_name` | `text` |
| `entry.title.tax_id` | `text` |
| `entry.title_id` | `text` |
| `entry.total` | `money` |
| `row` | `object` |
| `row.actual_name` | `text` |
| `row.amount` | `money` |
| `row.claimant` | `text` |
| `row.defaults` | `array` |
| `row.entry` | `object` |
| `row.entry.actual_item_name` | `text` |
| `row.entry.actual_name` | `text` |
| `row.entry.attachment_count` | `integer` |
| `row.entry.batch` | `object` |
| `row.entry.batch.fields` | `object` |
| `row.entry.batch.id` | `text` |
| `row.entry.batch.name` | `text` |
| `row.entry.batch.note` | `text` |
| `row.entry.batch.notes` | `text` |
| `row.entry.buyer_name` | `text` |
| `row.entry.buyer_tax_id` | `text` |
| `row.entry.check_status` | `text` |
| `row.entry.claimant` | `object` |
| `row.entry.claimant.id` | `text` |
| `row.entry.claimant.name` | `text` |
| `row.entry.claimant.reviewer` | `text` |
| `row.entry.completeness` | `object` |
| `row.entry.completeness.missing` | `array` |
| `row.entry.completeness.missing[]` | `text` |
| `row.entry.completeness.ready` | `boolean` |
| `row.entry.created_at` | `text` |
| `row.entry.entry_id` | `text` |
| `row.entry.fields` | `object` |
| `row.entry.id` | `text` |
| `row.entry.invoice` | `object` |
| `row.entry.invoice.buyer_name` | `text` |
| `row.entry.invoice.buyer_tax_id` | `text` |
| `row.entry.invoice.date` | `text` |
| `row.entry.invoice.number` | `text` |
| `row.entry.invoice.paid_amount` | `money` |
| `row.entry.invoice.seller` | `text` |
| `row.entry.invoice.title_id` | `text` |
| `row.entry.invoice.total` | `money` |
| `row.entry.invoice_date` | `date` |
| `row.entry.invoice_no` | `text` |
| `row.entry.items` | `array` |
| `row.entry.items[]` | `object` |
| `row.entry.items[].actual_name` | `text` |
| `row.entry.items[].defaults` | `array` |
| `row.entry.items[].defaults[]` | `text` |
| `row.entry.items[].index` | `integer` |
| `row.entry.items[].is_summary` | `boolean` |
| `row.entry.items[].product_name` | `text` |
| `row.entry.items[].quantity` | `decimal` |
| `row.entry.items[].spec` | `text` |
| `row.entry.items[].total` | `money` |
| `row.entry.items[].unit` | `text` |
| `row.entry.items[].unit_price` | `money` |
| `row.entry.materials` | `array` |
| `row.entry.materials[]` | `object` |
| `row.entry.materials[].id` | `text` |
| `row.entry.materials[].label` | `text` |
| `row.entry.materials[].name` | `text` |
| `row.entry.materials[].role_id` | `text` |
| `row.entry.materials[].type` | `text` |
| `row.entry.notes` | `text` |
| `row.entry.paid_amount` | `money` |
| `row.entry.product_name` | `text` |
| `row.entry.revision_id` | `text` |
| `row.entry.scheme_id` | `text` |
| `row.entry.seller` | `text` |
| `row.entry.status` | `text` |
| `row.entry.title` | `object` |
| `row.entry.title.color` | `text` |
| `row.entry.title.id` | `text` |
| `row.entry.title.name` | `text` |
| `row.entry.title.short_name` | `text` |
| `row.entry.title.tax_id` | `text` |
| `row.entry.title_id` | `text` |
| `row.entry.total` | `money` |
| `row.entry_id` | `text` |
| `row.first_for_invoice` | `boolean` |
| `row.id` | `text` |
| `row.index` | `integer` |
| `row.invoice` | `object` |
| `row.invoice.buyer_name` | `text` |
| `row.invoice.buyer_tax_id` | `text` |
| `row.invoice.date` | `text` |
| `row.invoice.number` | `text` |
| `row.invoice.paid_amount` | `money` |
| `row.invoice.seller` | `text` |
| `row.invoice.title_id` | `text` |
| `row.invoice.total` | `money` |
| `row.invoice_date` | `date` |
| `row.invoice_no` | `text` |
| `row.invoice_total` | `money` |
| `row.is_first` | `boolean` |
| `row.is_summary` | `boolean` |
| `row.paid_amount` | `money` |
| `row.product_name` | `text` |
| `row.quantity` | `decimal` |
| `row.reviewer` | `text` |
| `row.seller` | `text` |
| `row.spec` | `text` |
| `row.storage_location` | `text` |
| `row.tax_id` | `text` |
| `row.title` | `text` |
| `row.total` | `money` |
| `row.unit` | `text` |
| `row.unit_price` | `money` |

## 文件名变量

变量来自共享目录的 `filename_fields`，核心与组件共用 `filename_field_catalog()`。下表以字段权限为准，条目及材料角色变量仅在相应命名作用域使用。

| 变量 |
| --- |
| `scheme.name` |
| `title.id` |
| `title.name` |
| `title.short_name` |
| `batch.name` |
| `export.date` |
| `export.output_id` |
| `entry.id` |
| `entry.invoice_no` |
| `entry.claimant.name` |
| `role.id` |
| `role.label` |

## 能力 ID

- `fields.v1`
- `material-roles.v1`
- `rules.v1`
- `output.docx.v1`
- `output.pdf.v1`
- `output.xlsx.v1`
- `output.attachments.v1`
