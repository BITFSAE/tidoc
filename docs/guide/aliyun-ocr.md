# 阿里云 OCR（可选）

阿里云 OCR 是阿里云提供的发票识别服务。Tidoc 可以把发票 PDF 交给它识别，作为本地识别之外的补充和核对手段，界面里的按钮叫「云识别」。本页说明如何开通服务、配置密钥和发起识别。

## 要不要用

Tidoc 默认在本机读取发票 XML 或 PDF 里的文字，导入、整理材料和打印都可以直接使用本地识别的结果，日常使用不需要阿里云 OCR。

阿里云 OCR 是可选的收费功能，适合这些情况：

- 本地识别漏掉了规格、单位、数量等明细，需要补齐。
- 想用第二份识别结果核对本地结果。
- 发票 PDF 是扫描件，本地读不到字段。

只会在你点击「云识别」时联网并产生调用。不使用时，可以不开通、不配置。

::: warning 注意
云识别按调用量向阿里云付费。多页发票按页数计算，确认前请先看计费说明。
:::

## 开通 OCR 服务

1. 登录阿里云，并完成账号要求的实名认证。
2. 打开[增值税发票识别产品页](https://ai.aliyun.com/ocr/invoice)。
3. 按页面提示开通文字识别 OCR 和增值税发票识别服务。
4. 在 [OCR 控制台](https://ocr.console.aliyun.com/overview)确认服务状态。

Tidoc 调用的是阿里云的 `RecognizeInvoice` 接口，说明见[阿里云 RecognizeInvoice 文档](https://help.aliyun.com/zh/ocr/developer-reference/api-ocr-api-2021-07-07-recognizeinvoice)。

## 创建专用 RAM 用户

为 Tidoc 单独创建一个 RAM 用户，可以把权限限制在 OCR 范围内。

1. 打开 [RAM 控制台](https://ram.console.aliyun.com/)，进入「身份管理 → 用户」。
2. 点击「创建用户」，登录名称可以填写 `tidoc-ocr`。
3. 创建后进入该用户的详情页。
4. 在「权限管理」中新增授权，搜索并选择 `AliyunOCRFullAccess`。

`AliyunOCRFullAccess` 是阿里云提供的 OCR 系统策略，权限范围是文字识别 OCR 产品。可以参考[策略说明](https://help.aliyun.com/zh/ram/developer-reference/aliyunocrfullaccess)和[创建 RAM 用户说明](https://help.aliyun.com/zh/ram/user-guide/create-a-ram-user)。

## 创建 AccessKey

1. 在 `tidoc-ocr` 用户的详情页打开「凭证管理」。
2. 在 AccessKey 区域点击「创建 AccessKey」。
3. 按页面提示完成安全验证。
4. 保存 AccessKey ID 和 AccessKey Secret。

AccessKey Secret 只在创建时显示一次，建议保存在可靠的密码管理工具里。详细步骤见[阿里云 AccessKey 文档](https://help.aliyun.com/zh/ram/user-guide/create-an-accesskey-pair)。

## 在 Tidoc 中配置

1. 打开「设置 → 组件与更新 → 软件与组件 → 管理」，在「OCR 识别组件」一行点击「安装组件」。
2. 回到「设置」，在「阿里云 OCR」一栏填写 AccessKey ID 和 AccessKey Secret，点击「保存」。

保存后，这一栏显示脱敏后的 AccessKey ID 和本机累计调用次数。安装 OCR 识别组件后，批量工具条里会出现「云识别」，状态视图里会出现「已识别」和「OCR 待确认」。

密钥只保存在本机，不会随导出文件或绑定包带出，调用只在点击「云识别」时发生。需要停用时，点击「清除」删除本机密钥，并在阿里云 RAM 控制台禁用或删除对应的 AccessKey。

## 发起云识别

1. 勾选一个或多个包含发票 PDF 的[条目](/guide/concepts)。
2. 点击批量工具条中的「云识别」。
3. 在「阿里云识别」窗口查看将识别的发票数和调用次数，按需勾选选项，然后点击「开始识别」。
4. 在「阿里云识别完成」窗口查看每张发票已补齐、待确认或失败的情况，点击某一行可以打开该条目的详情。

确认窗口里的选项：

- 「跳过当前发票已有识别结果的 N 条（避免重复计费）」：默认勾选。
- 「包含已有 XML 数据的 N 条（结果仅作比对，同样计费）」：默认不勾选。

没有发票 PDF 的条目会自动跳过。多页 PDF 逐页调用，按页序合并结果。

也可以在「识别提醒」视图点击「识别全部」，右键卡片选择「阿里云识别」，或在条目详情里点击「识别此发票」。

识别结果与本地结果的核对，见[核对识别结果](/guide/check)。已保存的结果可以随时查看，不会产生新的调用；再次发起识别才会产生新的调用。

## 计费与调用量

阿里云 OCR 按成功调用量计费，多页发票按实际调用的页数计算。阿里云通常按「免费额度、专用资源包、共享资源包、按量后付费」的顺序抵扣，具体额度和价格以阿里云当前页面为准。

- [OCR 产品计费说明](https://help.aliyun.com/zh/ocr/product-overview/product-billing/)
- [OCR 资源包说明](https://help.aliyun.com/zh/ocr/product-overview/resource-plans)
- [OCR 计费常见问题](https://help.aliyun.com/zh/ocr/support/billing-faq)

可以在 OCR 控制台查看调用量、资源包余量和账单，建议按使用量设置余额或资源包告警。

## 常见错误

| 提示 | 处理办法 |
| --- | --- |
| 「OCR 识别组件未安装」或「需要修复」 | 在「软件与组件」里安装或修复 OCR 识别组件 |
| 「尚未配置阿里云密钥」 | 点击「打开设置」，填写 AccessKey |
| 「阿里云 AccessKey 无效」 | 重新核对 AccessKey ID 和 Secret，Secret 区分大小写 |
| 「该 AccessKey 没有文字识别权限」 | 确认 AccessKey 属于已授权 `AliyunOCRFullAccess` 的 RAM 用户 |
| 服务未开通 | 回到 OCR 产品页开通增值税发票识别服务 |
| 「请求过于频繁」 | 稍后重试 |
| 「请求超时」 | 检查网络后重试 |
| 「账户余额或额度不足」 | 到阿里云控制台处理余额或资源包 |
| 条目被跳过 | 检查条目是否有发票 PDF，以及确认窗口里「跳过当前发票已有识别结果」是否勾选 |
| 结果需要确认 | 打开条目详情，在「阿里云识别」区域比较「当前值」和「阿里云值」 |
