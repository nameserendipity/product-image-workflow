# 快手商品参数补全设计

## 1. 问题

当前快手采集器能够获取主图、详情图和视频，但在
`collect_kuaishou_payload()` 中直接写入空的 `product_parameters`。因此参数在进入
批处理之前就已经缺失，Excel 导出器只能生成空的“商品参数”工作表。

完整生图流程已经会对采集图片执行视觉分析，并生成
`generated/product-dossier.json`。该文件包含商品身份、可见标签、容量、包装结构、
颜色、材质外观和 SKU 文字等可追溯证据，但目前没有被用于补全商品参数。

## 2. 目标

- 快手完整工作流导出的 Excel“商品参数”页不得为空。
- 优先使用与当前快手商品 ID 明确绑定的平台结构化参数。
- 平台参数不存在时，复用已有视觉分析结果，不新增一次视觉 API 请求。
- 平台原值与图片识别值必须使用不同的来源标记。
- 不根据图片推测成分、功效、认证、产地或其他不可验证事实。
- 补全后的参数写回当前任务的采集 manifest，后续补图和重新导出继续复用。

## 3. 范围

本次仅修改快手 `direct_link` 完整工作流中的参数采集、参数兜底和 Excel 导出。
淘宝、天猫、抖音、京东的现有参数逻辑保持不变；图片生成数量、提示词、OSS 上传、
SKU 截图规则和共享素材库规则均不调整。

`collect_only` 任务没有视觉分析阶段，因此只保存能够从平台接口确认的参数；
“最终参数页不得为空”的约束在完整工作流导出 Excel 时执行。

## 4. 方案

采用“平台接口优先，已有视觉分析兜底”的双层策略。

### 4.1 平台结构化参数

快手响应解析继续以请求 URL 中的 `product_id` 为身份锚点。只有以下两种响应可以
提供参数：

1. 普通 JSON 子树自身包含与请求一致的 `goodsId`、`productId` 或 `itemId`。
2. `componentized` 响应的 `idToolbar`、`idBottomBar` 或 `idFullList` 中已经确认同一
   商品 ID，此时才允许检查同一组件映射中的参数组件。

只检查名称可识别为商品参数的容器，例如 `productParameters`、`goodsParams`、
`attributes`、`specifications`、`propertyList`，以及名称中包含 `param`、`spec` 或
`attribute` 的组件。参数行只接受下列成对字段：

- 名称：`name`、`attrName`、`propertyName`、`specName`、`key` 或 `label`。
- 值：`value`、`attrValue`、`propertyValue`、`specValue` 或 `text`。

名称和值均规范化为空白压缩后的字符串。空名称、空值、URL、对象值和无法归属到
当前商品 ID 的参数全部丢弃；同名参数按首次出现顺序去重。

平台参数保存为：

```json
{
  "name": "净含量",
  "value": "800ml",
  "source": "platform_api",
  "handling": "快手平台原值"
}
```

只要存在至少一条有效平台参数，`parameter_status` 为 `complete`，视觉结果不得覆盖
平台参数。

### 4.2 视觉分析兜底

当快手 manifest 的 `product_parameters` 为空时，在 `WorkflowRunner` 已生成
`product-dossier.json` 之后、Excel 导出之前，从下列现有证据提取参数：

- `dossier.anchor_identity.object`：商品类型或包装形态。
- `dossier.anchor_identity.visible_product_labeling`：可见品名、标签和规格容量。
- `dossier.anchor_identity.brand_or_mark`：仅在不包含 `unclear`、`unknown`、
  “不清晰”或“无法确认”时输出品牌。
- 与 `anchor_identity.source_index` 对应的 `observations` 记录：提取可见颜色，不使用
  其他 SKU 或非锚点商品的颜色。
- `dossier.confirmed_components`：包装结构和可见组件。
- `dossier.materials_and_textures`：只输出“外观材质”，并保留 `-like`、
  “看起来像”等视觉限定，不转换为确定材质事实。
- manifest 中的 `sku_variants`：去重后的规格、颜色和净含量文字。

兜底参数使用稳定字段名：`商品类型`、`可见品名/标签`、`可见规格/容量`、`包装结构`、
`外观颜色/材质`、`SKU规格`。没有证据的字段不输出，不以固定模板填充未知值。

视觉参数保存为：

```json
{
  "name": "可见规格/容量",
  "value": "800ml",
  "source": "visual_analysis",
  "handling": "图片识别，待核验"
}
```

存在视觉参数时，`parameter_status` 为 `inferred`。如果平台和视觉结果都没有任何
可靠参数，写入唯一的显式占位记录：

```json
{
  "name": "参数识别状态",
  "value": "未识别到可靠参数，需人工补充",
  "source": "manual_required",
  "handling": "待人工补充"
}
```

此时 `parameter_status` 为 `needs_review`。占位记录不被当作商品事实，只用于保证
Excel 不会静默显示空白页。

### 4.3 持久化与导出

参数补全完成后更新当前任务的 `collected/direct-manifest.json`：

- `product_parameters`：规范化后的参数数组。
- `product_parameters_text`：每行 `名称: 值`。
- `parameter_status`：`complete`、`inferred` 或 `needs_review`。
- `parameter_error`：平台缺参时保留简短说明；平台参数完整时清空。

Excel 导出器读取每条参数自身的 `handling`。旧 manifest 没有该字段时继续使用
“采集原值”，保持向后兼容。视觉兜底和人工补充状态不得显示成“采集原值”。

补图、失败重试和历史任务重新导出时，如果 manifest 已有非空参数，不重复生成或
覆盖；平台参数始终高于视觉参数。

## 5. 数据流

1. 在万象浏览器中加载快手商品页并收集与商品 ID 绑定的 JSON 响应。
2. 解析主图、详情图、视频、标题、价格和可验证的平台参数。
3. 写入初始快手 manifest；有平台参数时状态为 `complete`。
4. 完整工作流执行已有图片分析并生成 `product-dossier.json`。
5. manifest 无参数时，从 dossier 和 SKU 文字生成视觉兜底参数。
6. 将最终参数和来源写回 manifest。
7. Excel“商品参数”页按参数自身来源导出，并保证至少一行。

## 6. 错误处理

- 单个响应无法解析参数时跳过该响应，不影响图片、视频和后续视觉兜底。
- 参数容器存在但格式未知时不做递归猜测，避免把促销文案或推荐商品当成参数。
- `product-dossier.json` 缺失、损坏或没有可靠字段时写入人工补充占位记录，工作流
  继续完成并明确暴露状态。
- 已有平台参数不得因视觉分析失败、补图或重新导出而被清空。

## 7. 验收标准

- 与当前商品 ID 匹配的快手结构化参数被写入 manifest，来源为 `platform_api`。
- 其他商品、推荐区或未绑定商品 ID 的参数不会进入当前 manifest。
- 平台参数为空但 dossier 有证据时，生成来源为 `visual_analysis` 的参数。
- 平台参数与视觉证据同时存在时，只保留平台参数，不被视觉结果覆盖。
- 平台和视觉证据都为空时，Excel 显示“未识别到可靠参数，需人工补充”。
- 最终 Excel“商品参数”页至少一行，且来源列正确显示“快手平台原值”、
  “图片识别，待核验”或“待人工补充”。
- 补全参数写回 `direct-manifest.json`，重新导出结果一致。
- 现有快手媒体、SKU 截图、其他平台、图片工作流和 Excel 测试全部通过。
- 使用用户提供的真实快手商品链接跑完整工作流后，参数页不为空。

## 8. 测试策略

- `test_kuaishou_collector.py`：商品 ID 绑定、支持的参数字段、去重和跨商品隔离。
- `test_kuaishou_integration.py`：快手适配器将平台参数写入 metadata。
- `test_batch_workflow.py`：视觉兜底、平台优先、人工补充占位、manifest 持久化和
  Excel 来源列。
- 完整 Python 回归、前端 TypeScript 检查、Python 编译检查和 `git diff --check`。
- 最后使用真实快手链接验证 manifest 和 Excel，不重新生成已经成功的图片。
