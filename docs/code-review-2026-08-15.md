# 项目代码审查记录

审查日期：2026-08-15
审查范围：当前工作区未提交改动，重点覆盖抖音直链采集、产品图批量替换、批处理断点恢复、SKU 导出、OSS 上传和前端批处理状态。
审查方式：静态代码审查、设计文档对照、完整 Python 测试、Python 编译检查和前端 TypeScript 检查。

## 发现

### P0：真实凭据已提交到仓库

`local_settings.json` 中包含图像 API、视觉 API 和 OSS 凭据（见 `local_settings.json:3-4,10-11`）。这些值会随 Git 工作区和发布包传播。应立即吊销并轮换所有已暴露凭据；真实配置应移出版本控制，发布流程也不应默认携带长期密钥。

### P1：历史采集复用功能缺失，相关测试失败

`BatchRunner` 只在 `image_search` 模式调用历史素材复用；`direct_link` 和 `direct_replace` 会重复采集同一商品（`batch_workflow.py:2003-2005`）。单链接切换到同一商品时也会直接清空 manifest，没有执行历史 manifest 查找（`web_app.py:974-979`）。

复现失败的测试：

- `test_direct_batch_reuses_historical_collection_for_same_product_id`（`direct_link`、`direct_replace` 两个子测试）。
- `test_confirming_reference_url_reuses_historical_collection`。

### P1：抖音短链解析失败会卡死单链接采集状态

`resolve_direct_item_url()` 在 `_collect()` 的 `try/finally` 之前执行（`web_app.py:1259-1260`）。当网络解析失败或短链无法落到受支持详情页时，异常会逃出线程，清理逻辑不会运行，`STATE.collecting` 可能永久保持为 `True`，界面无法停止或重试。

### P1：手工 SKU 覆盖平台采集 SKU

`merge_manual_sku_metadata()` 直接替换整个 `sku_variants`（`batch_workflow.py:1030-1050`）；`add_manual_sku_generation_sources()` 也会丢弃已有 SKU 图片（`batch_workflow.py:1063-1084`）。当输入表格同时提供手工 SKU 和平台真实 SKU 时，真实采集数据会从 manifest、生成任务或导出结果中消失。

### P1：批处理重试不会复用已成功的生成记录

`WorkflowRunner.run()` 支持 `existing_records`，但批处理调用没有传入（`batch_workflow.py:2098-2107`）。部分生成成功后重试会重新调用视觉分析和生图 API，可能重复计费并覆盖已有图片，而不是只重试失败任务。

### P2：上传校验摘要缺少设计要求的分类统计

后端只计算 `valid`、`invalid` 和 `unsupported`（`web_app.py:1443-1460`），没有分别统计缺少商品图、缺少链接和图片/链接配对冲突。前端也只显示有效数量（`frontend/src/App.tsx:783-785`），用户无法在确认批处理前准确判断错误类型。

### P2：抖音下载依赖固定菜单索引

抖音采集器固定点击 `li.el-dropdown-menu__item` 的 `nth(1)`（`douyin_collector.py:67-69`）。扩展菜单增加项目或调整顺序后，可能点击错误下载项，造成空包或错误资料包。应按菜单文本或稳定语义定位。

## 验证结果

- `python -m unittest discover -q`：失败，272 项中 3 项失败；失败项见上文。
- `python -m compileall -q agent_flow.py batch_workflow.py store_insight_collector.py douyin_collector.py spreadsheet_inputs.py image_workflows.py oss_uploader.py web_app.py`：通过。
- `npm run check`（`frontend`）：通过。
- `git diff --check`：通过。
- 测试运行期间另有测试 fixture 未关闭图片文件句柄的 `ResourceWarning`。

本次审查未修改业务代码。
