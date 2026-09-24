---
name: image_generation
description: "生成并发送图片"
type: tool
exclude_transports: [telegram]
---

## Tools

### generate_and_send_image (routed)
根据描述生成并发送一张图片，生成的图片会在本轮交给你查看。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| prompt | string | yes | 想生成的图片描述。 |

## Capability Context

每次生成可能产生服务商费用，工具不会自动重试；生成的图片会在本轮作为图像交给你查看，发送结果另行报告。
