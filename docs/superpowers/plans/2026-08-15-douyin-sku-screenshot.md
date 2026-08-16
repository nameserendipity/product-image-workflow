# 抖音 SKU 截图参考 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让抖音链接表格中的 SKU 截图成为可审计的 SKU 参数和视觉参考来源。

**Architecture:** 扩展直链表格解析以携带截图路径；在视觉客户端增加严格 JSON 的 SKU 截图解析；在批处理阶段裁剪并执行清晰度门控，将可用裁剪图写入生成 manifest，将低清条目降级到商品主图并记录原因。

**Tech Stack:** Python 3、openpyxl、Pillow、现有视觉 API 客户端、unittest。

## Global Constraints

- 抖音采集包没有 SKU 图时，截图是唯一 SKU 视觉来源。
- 清晰缩略图才能作为视觉参考；低清缩略图不能被 AI 超分辨率伪造。
- SKU 最多生成 8 张；淘宝、天猫、京东流程不变。

### Task 1: 表格截图识别

**Files:**
- Modify: `batch_workflow.py`
- Test: `test_batch_workflow.py`

- [ ] 写失败测试：无表头 A 列 HTTP 链接、B 列 `file:///` 图片应生成带 `sku_screenshot` 的 `DirectLinkBatchItem`；普通直链表格仍无截图。
- [ ] 运行 `python -m unittest test_batch_workflow.BatchWorkflowTests.test_direct_link_extracts_sku_screenshot -v`，确认因字段不存在而失败。
- [ ] 扩展 `DirectLinkBatchItem` 和 `extract_direct_link_items`，支持本地路径、`file:///` 和嵌入图片，必要时将截图复制到批次 source-images 目录。
- [ ] 运行该测试和现有直链解析测试，确认通过。

### Task 2: SKU 截图解析和质量门控

**Files:**
- Modify: `image_workflows.py`
- Test: `test_image_workflows.py`

- [ ] 写失败测试：视觉 JSON 中的两个 SKU 能被规范化；64x64 以下、`confidence < 0.65` 或 `is_clear=false` 的裁剪不能作为参考。
- [ ] 运行 `python -m unittest test_image_workflows.ImageWorkflowTests.test_sku_screenshot_quality_gate -v`，确认因方法不存在而失败。
- [ ] 增加 `VisionClient.analyze_sku_screenshot` 和裁剪辅助函数，限制坐标、尺寸和字段类型；只使用 Pillow 普通裁剪。
- [ ] 运行测试，确认清晰图进入参考、模糊图进入降级状态。

### Task 3: 接入抖音生成和导出

**Files:**
- Modify: `batch_workflow.py`
- Modify: `image_workflows.py`
- Test: `test_batch_workflow.py`

- [ ] 写失败测试：抖音直链任务没有原始 SKU 图但有截图时，生成 manifest 包含每个可用 SKU 裁剪图；低清条目使用主图并保留 `low_visual_confidence`。
- [ ] 运行该测试，确认当前只会使用采集包中的 SKU 图而失败。
- [ ] 在采集 manifest 读取后调用截图解析，合并真实 SKU 字段；对 `DirectLinkBatchItem` 复用 SKU metadata 和 generation source 逻辑，不影响替换模式。
- [ ] 扩展导出行的参考来源、置信度和降级原因字段，保留已有 12 列兼容性。
- [ ] 运行抖音相关测试和完整 Python 回归。

### Task 4: 文档与验证

**Files:**
- Modify: `操作说明书.md`

- [ ] 增加 Excel 行格式和低清截图行为说明。
- [ ] 运行 `python -m unittest discover -v` 和 `npm run check`。
- [ ] 检查 git diff，不加入任何 API、OSS 或浏览器密钥。
