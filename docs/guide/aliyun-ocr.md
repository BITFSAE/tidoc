---
title: 阿里云 OCR（可选）：开通与配置云识别
description: 说明如何开通阿里云 OCR、创建专用 RAM 用户和 AccessKey，并在 Tidoc 中配置后发起云识别。该功能可选，有每月免费额度，超出后由阿里云按调用量计费。
---

# 阿里云 OCR（可选）

阿里云 OCR 是阿里云的发票识别服务。Tidoc 可以把发票 PDF 交给它识别，用来补齐或核对本地识别的结果，界面里的按钮叫「云识别」。本页说明怎么开通、配置和使用。

## 要不要用

Tidoc 默认在本机识别发票，日常导入、整理和打印都不需要阿里云 OCR。这些情况可以考虑开通：

- 本地识别漏了规格、单位、数量等明细。
- 想用第二份结果核对本地结果。
- 发票 PDF 是扫描件，本地读不到字段。

只有点击「云识别」时才会联网和计费，不用就不需要开通。免费额度和计费方式见[计费与调用量](#计费与调用量)。

以下截图拍摄于 2026 年 9 月，阿里云界面可能有调整，以实际页面为准。截图里的账号和密钥已隐去。

## 开通 OCR 服务

1. 注册并登录阿里云，完成实名认证。
2. 打开[票据凭证识别产品页](https://ai.aliyun.com/ocr/invoice)，点击「开通享免费额度」。
3. 在 [OCR 控制台](https://ocr.console.aliyun.com/overview)确认已开通。

![阿里云票据凭证识别产品页，「开通享免费额度」按钮](/images/aliyun-ocr-1-product.png)

Tidoc 调用的是阿里云的 `RecognizeInvoice` 接口，说明见[阿里云 RecognizeInvoice 文档](https://help.aliyun.com/zh/ocr/developer-reference/api-ocr-api-2021-07-07-recognizeinvoice)。

## 创建专用 RAM 用户

单独给 Tidoc 建一个 RAM 用户，权限只限 OCR。

### 创建用户

1. 打开 [RAM 控制台](https://ram.console.aliyun.com/)，或点击阿里云右上角的头像，在「权限与安全」里点击「访问控制」。

   ![头像面板里的「权限与安全」，「访问控制」入口](/images/aliyun-ocr-2-access-control.png)

2. 进入「身份管理 → 用户」，点击「创建用户」。
3. 「登录名称」填 `tidoc-ocr`，支持英文、数字、下划线、连字符和点号。
4. 「访问配置」勾选「使用永久 AccessKey 访问」，不勾选「使用控制台访问」。Tidoc 只用 AccessKey，不需要登录控制台。
5. 按提示完成创建。如果页面直接显示了 AccessKey，保存好 ID 和 Secret，就可以跳过下面的「创建 AccessKey」。

![创建用户页面，登录名称与访问配置](/images/aliyun-ocr-3-create-user.png)

### 授权

1. 在用户列表里点击刚建的用户，进入详情页。

   ![用户列表中新建的用户](/images/aliyun-ocr-4-user-list.png)

2. 打开「权限管理」，点击「新增授权」。

   ![账户详情页的「权限管理」标签与「新增授权」按钮](/images/aliyun-ocr-5-permissions.png)

3. 在「权限策略」里搜索 `ocr`，只勾选 `AliyunOCRFullAccess`，其他保持默认，点击「确认新增授权」。

![新增授权面板，勾选 AliyunOCRFullAccess](/images/aliyun-ocr-6-grant.png)

`AliyunOCRFullAccess` 是阿里云的 OCR 系统策略，只涉及文字识别 OCR 产品。参考[策略说明](https://help.aliyun.com/zh/ram/developer-reference/aliyunocrfullaccess)和[创建 RAM 用户说明](https://help.aliyun.com/zh/ram/user-guide/create-a-ram-user)。

## 创建 AccessKey

1. 在该用户详情页的「凭证管理」里点击「创建 AccessKey」。

   ![凭证管理标签与「创建 AccessKey」按钮](/images/aliyun-ocr-7-credentials.png)

2. 在弹出的窗口里选「本地开发环境中使用」，勾选「我确认必须创建 AccessKey」，点击「继续创建」，再按提示完成安全验证。图中是该用户已有 AccessKey 时的窗口，文字可能略有不同。

   ![选择使用场景并确认创建](/images/aliyun-ocr-8-scenario.png)

3. 在「保存 AccessKey」窗口里保存 AccessKey ID 和 AccessKey Secret，「下载 CSV 文件」或「复制」都可以，然后点击「确定」。

![保存 AccessKey 窗口，AccessKey ID 与 Secret 已隐去](/images/aliyun-ocr-9-key-created.png)

Secret 关闭窗口后就看不到了，丢了只能重新创建，建议存进密码管理工具。详见[阿里云 AccessKey 文档](https://help.aliyun.com/zh/ram/user-guide/create-an-accesskey-pair)。

窗口下方的「安全建议」可以不设置。设了来源 IP 限制，换网络或换电脑后调用会失败；设了闲置自动禁用，长期不用的 AccessKey 会被停用。

## 在 Tidoc 中配置

1. 打开「设置 → 组件与更新 → 软件与组件 → 管理」，在「OCR 识别组件」一行点击「安装组件」。
2. 回到「设置」，展开「开发者选项」，在「发票阿里云 OCR」一栏填写 AccessKey ID 和 AccessKey Secret，点击「保存」。

保存后，这一栏显示 AccessKey ID 开头「LTAI」之后的两位和末尾四位，以及本机累计调用次数。批量工具条里会出现「云识别」，状态视图里会出现「已识别」和「OCR 待确认」。

密钥只保存在本机，不会随导出文件或绑定包带出。不再使用时，点击「清除」删除本机密钥，并在阿里云禁用或删除对应的 AccessKey。

## 发起云识别

1. 勾选一个或多个包含发票 PDF 的[条目](/guide/concepts)。
2. 点击批量工具条中的「云识别」。
3. 在「云识别」窗口勾选「发票 · 阿里云 OCR」，确认发票数和调用次数，点击「开始识别」。同一窗口里的「付款截图 · AI」见[付款截图 AI 识别](/guide/settings#付款截图-ai-识别)。
4. 在「云识别完成」窗口查看每张发票已补齐、待确认或失败的情况，点击某一行打开该条目的详情。

确认窗口里有两个选项：

- 「跳过当前发票已有识别结果的 N 条（避免重复计费）」：默认勾选。
- 「包含已有 XML 数据的 N 条（结果仅作比对，同样计费）」：默认不勾选。

没有发票 PDF 的条目会自动跳过，多页 PDF 逐页调用后按页序合并。

也可以在「识别提醒」视图点击「识别全部」，右键卡片选择「云识别」，或在条目详情里点击「识别此发票」。

结果与本地结果的核对方法见[核对识别结果](/guide/check)。已保存的结果随时可以查看，不会再次计费，重新识别才会产生新的调用。

## 计费与调用量

阿里云 OCR 按成功调用量计费，多页发票按页数算。目前发票识别每月有 200 次免费调用，免费额度、专用资源包、共享资源包、按量后付费依次抵扣。额度和价格以阿里云当前页面为准。

- [OCR 产品计费说明](https://help.aliyun.com/zh/ocr/product-overview/product-billing/)
- [OCR 资源包说明](https://help.aliyun.com/zh/ocr/product-overview/resource-plans)
- [OCR 计费常见问题](https://help.aliyun.com/zh/ocr/support/billing-faq)

可以在 OCR 控制台查看调用量、资源包余量和账单，并设置余额告警。

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
