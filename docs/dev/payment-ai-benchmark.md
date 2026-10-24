---
title: 付款截图 AI 识别评测
description: 面向开发者，比较付款截图模型的金额、材料判断、深浅色、延迟和并发。
---

# 付款截图 AI 识别评测

面向开发者的独立评测脚本。截图只交给 AI 识别，本地负责图片处理、校验和计算。实现状态见 [README](https://github.com/BITFSAE/tidoc#状态)。

## 运行

使用已安装 `requirements.txt` 的 Python 环境。设置 `DASHSCOPE_API_KEY`、百炼业务空间的 `DASHSCOPE_BASE_URL` 和 `DEEPSEEK_API_KEY`。需要多个密钥时，设置对应的 `*_API_KEYS`，支持 JSON 数组、逗号或换行分隔，优先于单密钥变量。

脚本会上传指定图片并产生付费调用。真实材料、标注、缓存和报告放在已忽略的 `output/payment-ai/`，不要提交到仓库。

准备 `output/payment-ai/samples.json`，路径相对于清单目录：

```json
{
  "samples": [{
    "id": "receipt-01",
    "path": "receipt-01.jpg",
    "expected": {
      "source": "wechat",
      "amount": "6.00",
      "acceptable": true,
      "theme": "light"
    }
  }]
}
```

`expected` 只填人工确认的字段。`amount: null` 表示无法确定金额；不填则不计分。旧识别结果不能直接当作正确答案。

在仓库根目录运行：

```bash
python -m scripts.benchmark_payment_ai \
  --manifest output/payment-ai/samples.json \
  --output output/payment-ai/results.json \
  --concurrency 8 --per-key-concurrency 8 --no-cache
```

| 参数 | 用途 |
| --- | --- |
| `--models` | 选择 `qwen3.8-flash`、`qwen3.8-omni-flash`、`deepseek-flash`，默认依次比较三个模型 |
| `--concurrency` / `--per-key-concurrency` | 总并发和单密钥并发，实际线程数取可用容量 |
| `--max-edge 2000` / `--quality 85` | 缩放后转 JPEG；长边设为 `0` 发送原图，原文件不改动 |
| `--image-layout tiles` | 竖图分成有重叠的上下两块，同一次请求读取，小字更清楚 |
| `--repeats 3` | 重复样本并统计多数结果，无共识则留给人工核对；需配合 `--no-cache` |
| `--retries 1` | 网络失败重试；默认不重试，鉴权和参数错误不重试 |
| `--allowed-sources wechat alipay` | 限定凭证来源，默认还允许银行 |
| `--requirements "截图需显示支付时间"` | 追加材料要求 |
| `--profile fast` | 默认短提示，本地计算净额和材料判断；其他 profile 用于对照 |
| `--detail low` | 比较服务端低分辨率处理；支持情况以接口响应为准 |

接口测速带 `--no-cache`，同时关闭缓存读写。去掉该参数、相同设置运行两次，可测首次调用与缓存命中。缓存区分接口、模型、提示词、规则、图片和密钥范围；失败不缓存。密钥不写入报告或缓存。

## 结果

模型只提取来源、类型、付款、退款、完成状态、币种、深浅色和异常。本地计算净额、判断合格与核对提醒。使用非思考模式和 JSON 输出。

明确显示原付款和累计退款时，本地用 `Decimal` 计算净支出；退款仍提示人工确认。只有退款而无原付款时，净支出留空。余额和未扣款的订单合计不作为实付。

深色付款截图仍可合格，另提示“建议用浅色截图，打印更省墨”。深浅色按凭证主体判断，忽略手机状态栏、头像和商品图。

| 指标 | 含义 |
| --- | --- |
| `api_seconds` / `ttft_seconds` | 完整结果 / 首段答案耗时，包含连接、上传和服务端等待 |
| `task_seconds` | 单张任务耗时，包含缓存、重试和退避 |
| `wall_seconds` / `api_success_per_minute` | 整批耗时 / 成功吞吐，包含排队和失败 |
| `api_median_seconds` / `api_p95_seconds` | 成功请求的延迟中位数 / 最近秩 P95 |
| `field_accuracy` / `all_labeled_fields_correct` | 各字段正确数 / 所有标注均正确的图片数，失败计错 |
| `prepare_seconds` / `sent_bytes` | 图片预处理耗时 / 上传图片字节数 |
| `resolved_model` / `usage` | 服务端返回的模型标识和用量 |

缓存命中不计为接口延迟或吞吐。重试时 `api_seconds` 只记最后成功的一次，整体体验看任务与整批耗时。报告保留金额和短摘录，按私人材料保管。

比较时分开看金额、材料误判和网络失败，并与本地识别对照。样本先去重，提示词调试与验证分组；退款、订单页、余额、深色和模糊图需要单独覆盖。缺少的类型不推断效果。

接口参考：[百炼图像理解](https://help.aliyun.com/zh/model-studio/vision)、[百炼结构化输出](https://help.aliyun.com/zh/model-studio/qwen-structured-output)、[DeepSeek 图像输入](https://api-docs.deepseek.com/guides/vision/)和[思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)。
