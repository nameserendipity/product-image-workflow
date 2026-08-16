# OSS 共享商品素材库设计

## 1. 文档信息

- 项目：商品图片工作流 Excel 批处理版
- 设计日期：2026-08-15
- 目标版本：共享素材库 MVP
- 运行方式：多个本地客户端通过同一个阿里云 OSS 私有 Bucket 共享商品素材

## 2. 目标

团队成员提交淘宝或天猫商品链接后，程序在启动浏览器和图片生成前查询 OSS。已存在完整共享素材时复用现有结果；其他用户正在生成同一商品时阻止重复任务；未命中时由一个客户端生成并发布完整素材。

MVP 不增加数据库、独立网站或云端业务服务。OSS 保存索引、锁和文件，本地程序负责业务判断、界面、打包、校验和下载。

## 3. 已确认范围

### 3.1 支持范围

- 共享查重只支持淘宝和天猫商品链接。
- 淘宝和天猫使用 `{platform}-{product_id}` 作为精确商品键。
- 不同推广参数、分享参数和 SKU 参数不改变商品键。
- 默认自动启用共享流程，不要求用户手动打开共享开关。
- 完整标准任务成功后自动发布到 OSS。
- “参考对标商品创作”的单链接任务和 `direct_link` Excel 批处理使用相同的查询、锁和复用规则。

### 3.2 不在本期实现

- 京东、抖音和快手不接入共享素材库。
- 不处理商品 ID 不同但实际为同款商品的自动或人工合并。
- 不使用标题、图片、感知哈希或视觉模型判断同款。
- 不保存同一商品的多套风格版本。
- 不提供在线管理后台、审批、计费或团队权限系统。
- 不自动上传旧版本已经保存在本地的历史素材。
- `direct_replace` 产品图批量替换、`image_search` 商品图搜同款和单链接“使用我方产品图”不参与共享查重或 Catalog 发布，因为淘宝/天猫链接不是最终生成商品的身份。

上述能力可在后续版本单独设计，不影响本期精确商品键查重。

## 4. 关键行为规则

### 4.1 自动共享

淘宝或天猫链接提交后，程序自动解析商品身份并查询共享库。共享查询发生在浏览器采集、采集器启动和图片生成 API 调用之前。

### 4.2 OSS 故障回退

以下 OSS 异常不得导致本地任务失败：

- 网络超时或连接失败；
- 认证或权限错误；
- Catalog、Manifest 或锁查询发生服务异常；
- 上传或锁续期发生异常。

遇到上述异常时，程序记录不包含密钥的警告并继续本地采集、生成和导出。回退任务不得发布 Catalog；本地结果仍可正常使用。

### 4.3 锁竞争不属于 OSS 故障

如果锁对象已经存在且仍然有效，表示其他用户正在处理同一商品。当前客户端必须显示“其他用户正在生成”，不得启动共享任务，也不得自动回退为本地重复生成。

### 4.4 已发布素材不可覆盖

状态为 `completed` 的 Catalog 和正式 Package 不允许普通任务覆盖。Catalog 损坏或文件缺失时显示“共享素材不可用”，当前任务可以仅在本地执行，但不得自动替换已有共享对象。

## 5. 完整标准任务

只有完整标准任务可以发布 Catalog。完整标准任务必须满足：

1. 商品身份能够解析为淘宝或天猫的稳定商品 ID。
2. 使用“参考对标商品创作”和系统默认生成配置。
3. 主图、SKU 图和详情图三条工作流全部执行。
4. 主图默认生成 10 张且全部成功。
5. SKU 图按现有默认逻辑生成，通常为 3 至 8 张，计划项全部成功。
6. 详情图按现有默认逻辑生成，通常为 6 至 15 张，计划项全部成功。
7. 商品 Excel 成功导出。
8. 预览图、分类 ZIP、完整 ZIP 和 Manifest 均成功生成并通过本地校验。
9. OSS 正式对象全部上传成功并可读取。

以下任务只保存在本地或沿用普通 OSS 上传，不进入共享 Catalog：

- 只执行部分图片类型；
- 任一计划生成项失败或缺失；
- 用户临时修改默认数量；
- 商品 Excel、打包或校验失败；
- 商品身份无法可靠解析；
- OSS 上传未全部完成。

## 6. 总体架构

```text
本地程序
  |- ProductIdentityResolver
  |- SharedLibraryClient
  |- SharedPackageBuilder
  |- SharedLibraryCache
  |- 现有单链接和批处理工作流
  `- 共享素材库页面

阿里云 OSS 私有 Bucket
  `- {prefix}/shared-library/
      |- catalog/
      |- locks/
      |- packages/
      `- staging/
```

现有 `oss_uploader.py` 继续承担普通生成图片上传。共享素材库使用私有对象键和新的共享库客户端，不依赖永久公网 URL。

## 7. 模块边界

### 7.1 `product_identity.py`

提供 `ProductIdentityResolver`，负责：

- 识别淘宝和天猫链接；
- 从直接链接或有限 HTTP 跳转后的地址提取商品 ID；
- 移除推广、分享和 SKU 参数；
- 返回原始链接、规范化链接、平台、商品 ID 和商品键；
- 拒绝不支持的平台和没有稳定商品 ID 的链接进入共享流程。

建议返回不可变数据对象：

```python
@dataclass(frozen=True, slots=True)
class ProductIdentity:
    platform: Literal["taobao", "tmall"]
    product_id: str
    product_key: str
    source_url: str
    canonical_url: str
```

### 7.2 `shared_library_client.py`

提供 `SharedLibraryClient`，负责：

- 读取和分页列出 Catalog；
- 读取并验证 Manifest；
- 创建、读取、续期和释放任务锁；
- 上传 staging 和正式对象；
- 发布 Manifest 和 Catalog；
- 分片或断点下载 ZIP；
- 读取对象大小、ETag 和最后修改时间；
- 将 SDK 异常转换为不泄露凭据的业务错误。

业务层不得直接操作 OSS Bucket。

### 7.3 `shared_package_builder.py`

提供 `SharedPackageBuilder`，负责：

- 检查任务是否满足完整标准；
- 生成 `main.zip`、`sku.zip` 和 `detail.zip`；
- 自动生成商品 Excel；
- 生成 300 KB 至 800 KB 的预览图；
- 生成 `complete-package.zip`；
- 计算关键文件大小与 SHA-256；
- 生成 Manifest 和 Catalog 文档。

### 7.4 `shared_library_cache.py`

提供 `SharedLibraryCache`，负责：

- 缓存 Catalog 摘要、ETag 和最后修改时间；
- 保存稳定的本机 `client_id`；
- 保存已下载包的对象键、SHA-256 和本地目录；
- 支持素材库页面先显示本地缓存，再增量刷新 OSS。

缓存只用于显示加速。生成前、锁判断和下载前必须重新查询 OSS。

## 8. OSS 对象结构

```text
{prefix}/shared-library/
|- catalog/
|  `- {product_key}.json
|- locks/
|  `- {product_key}.json
|- packages/
|  `- {platform}/
|     `- {product_id}/
|        |- manifest.json
|        |- preview.jpg
|        |- main.zip
|        |- sku.zip
|        |- detail.zip
|        |- complete-package.zip
|        `- result.xlsx
`- staging/
   `- {task_id}/
```

`staging/{task_id}` 保证不同客户端的上传互不覆盖。Catalog 始终最后发布，未发布 Catalog 的 staging 对象不得出现在素材列表中。

## 9. Catalog、Manifest 与锁

### 9.1 Catalog

Catalog 是商品的小型发现索引，不保存图片内容。每个商品只有一个 Catalog。

```json
{
  "schema_version": 1,
  "product_key": "taobao-123456789",
  "platform": "taobao",
  "product_id": "123456789",
  "source_url": "原始链接",
  "canonical_url": "规范化链接",
  "status": "completed",
  "preview_object": ".../preview.jpg",
  "package_object": ".../complete-package.zip",
  "manifest_object": ".../manifest.json",
  "main_count": 10,
  "sku_count": 8,
  "detail_count": 12,
  "package_size": 148234221,
  "package_sha256": "完整 ZIP 的 SHA-256",
  "created_at": "ISO-8601 时间",
  "created_by": "稳定客户端标识"
}
```

### 9.2 Manifest

Manifest 记录：

- 商品身份和来源链接；
- 主图、SKU 图、详情图、Excel、视频和其他可用文件；
- 每个关键文件的对象键、大小和 SHA-256；
- 采集器版本、工作流版本和共享结构版本；
- 完整 ZIP 的对象键、大小和 SHA-256；
- 创建时间、客户端和最终状态。

### 9.3 任务锁

```json
{
  "schema_version": 1,
  "product_key": "taobao-123456789",
  "task_id": "随机 UUID",
  "client_id": "稳定客户端标识",
  "created_at": "ISO-8601 时间",
  "expires_at": "ISO-8601 时间"
}
```

锁默认有效期为两小时。创建锁时使用 OSS 禁止覆盖能力，保证同一商品只有一个客户端成功。长任务定期续期；释放、续期或接管过期锁时必须校验当前任务身份和对象版本，避免删除或刷新其他客户端的锁。

## 10. 核心流程

### 10.1 单链接任务

1. 用户提交淘宝或天猫链接。
2. 解析 `ProductIdentity`。
3. 查询正式 Catalog 和 Manifest。
4. 命中完整素材时阻止采集，显示预览和复用操作。
5. Catalog 未命中但有效锁存在时显示“其他用户正在生成”。
6. 标准完整任务在 Catalog 和有效锁都不存在时抢占任务锁。
7. OSS 异常时切换到仅本地模式并继续现有流程。
8. 执行现有采集、生成和 Excel 导出。
9. 完整性检查通过且仍持有有效锁时打包并发布。
10. 发布成功或失败后释放锁；无法安全释放时等待锁自然过期。

### 10.2 发布顺序

1. 本地生成全部正式文件和校验摘要。
2. 上传到 `staging/{task_id}/`。
3. 校验 staging 对象。
4. 写入正式 Package 对象。
5. 上传正式 `manifest.json`。
6. 再次确认锁归属。
7. 最后上传 `catalog/{product_key}.json`。
8. 释放锁并尽力清理 staging。

任何步骤失败都不发布 Catalog。

### 10.3 一键复用

1. 下载前重新读取 Catalog 和 Manifest。
2. 本地存在相同 SHA-256 的完整包时直接复用。
3. 否则下载到 `.part` 文件，并从已有字节继续。
4. 完成后校验文件大小和 SHA-256。
5. 校验通过后原子重命名为正式 ZIP。
6. 解压到 `outputs/reused/{product_key}/`。
7. 保存下载记录并打开本地目录。

分类下载使用相同流程，但只处理 `main.zip`、`sku.zip` 或 `detail.zip`。

### 10.4 Excel 批处理

`direct_link` 参考对标商品创作模式在每一行开始前执行同样的身份解析和 OSS 查询：

- 命中完整素材时跳过浏览器和生成 API，自动下载并导出当前批次的本地结果与商品 Excel；
- 有效锁存在时该行报告“其他用户正在生成”，不得重复生成；
- OSS 异常时该行进入本地回退并维持现有批处理顺序与停止行为；
- 未命中时执行现有流程，只有完整标准结果才发布共享素材。

`direct_replace` 产品图批量替换和 `image_search` 商品图搜同款继续执行现有本地生成与普通 OSS 上传，不查询共享 Catalog、不抢占共享锁，也不发布共享素材。界面明确提示“共享素材库仅适用于参考对标商品创作”。

## 11. 本地 HTTP 接口

### 11.1 `GET /api/shared-library`

查询参数：

- `platform`：空、`taobao` 或 `tmall`；
- `query`：完整商品链接或商品 ID；
- `cursor`：OSS 分页游标。

返回素材卡片、下一页游标、缓存刷新时间和 OSS 可用状态。

### 11.2 `POST /api/shared-library/reuse`

请求包含 `product_key` 和 `package_kind`。`package_kind` 为 `complete`、`main`、`sku` 或 `detail`。响应返回下载状态、校验状态和本地目录。

### 11.3 `POST /api/shared-library/open-folder`

只允许打开 `outputs/reused/` 下已经记录的目录，禁止任意路径打开。

### 11.4 `/api/status` 扩展

当前商品增加以下共享状态：

- `idle`；
- `checking`；
- `available`；
- `locked`；
- `local_fallback`；
- `publishing`；
- `published`；
- `publish_failed`；
- `corrupt`。

## 12. 前端设计

侧边栏新增第三个一级入口“共享素材库”。页面包含：

- 链接或商品 ID 搜索框；
- 淘宝、天猫平台筛选；
- 预览图、商品 ID、图片数量、创建时间和包大小；
- 一键复用、下载主图、下载 SKU、下载详情图和打开目录操作；
- 查询中、其他用户处理中、下载中、校验中、已复用、共享库不可用和素材损坏状态。

素材列表只加载小型预览图。ZIP 仅在用户执行下载操作时请求。固定尺寸的卡片媒体区和操作区避免加载状态导致布局跳动。

## 13. 并发与一致性

- 不同商品使用不同对象键，可以同时上传。
- 同一商品必须先通过锁对象竞争，只有一个任务可以成为发布者。
- 每个任务使用独立 staging 路径。
- 正式对象和 Catalog 禁止普通任务覆盖。
- Catalog 最后发布，保证列表只展示完整素材。
- 锁竞争失败是正常业务状态，不触发本地回退。
- OSS 网络、认证和服务异常才触发本地回退。

## 14. 异常处理

### 14.1 查询失败

记录警告并继续本地任务。日志和响应不得包含 AccessKey、Secret 或签名查询参数。

### 14.2 锁续期失败

本地任务可以继续完成，但立即失去共享发布资格。不得在锁归属不确定时发布 Manifest 或 Catalog。

### 14.3 生成不完整

保留全部本地产物，不进入共享发布流程。

### 14.4 上传中断

保留本地包；不发布 Catalog；尽力清理当前 staging。清理失败不影响本地任务结果。

### 14.5 Catalog 与文件不一致

标记为 `corrupt`，禁止下载和自动覆盖。用户仍可执行仅本地任务。

### 14.6 下载中断或校验失败

下载中断保留 `.part`。校验失败不得解压，损坏的正式 ZIP 必须删除或隔离，并允许重新下载。

## 15. 安全

- Bucket 保持私有，不开放完整素材匿名读取。
- 使用仅覆盖 `{prefix}/shared-library/*` 的专用 RAM 用户。
- 凭据优先从现有 Windows 用户环境变量读取，不写入 Catalog、Manifest、Excel、日志或错误响应。
- `packages/*` 和 `catalog/*` 允许读取和新建，但不向普通客户端开放删除已完成素材的权限。
- 删除权限只用于客户端拥有的锁和 staging 对象。
- 前端只接收本地代理返回的预览内容或短期访问结果，不生成永久公网素材地址。

## 16. 验收标准

### 16.1 身份与查重

- 同一淘宝或天猫商品的不同推广链接得到同一 `product_key`。
- 京东、抖音和快手不进入共享流程。
- 命中完整共享素材时不启动浏览器和图片生成 API。

### 16.2 并发

- 两个客户端同时提交同一商品时只有一个成功获得锁。
- 未获得锁的客户端在五秒内显示“其他用户正在生成”。
- 不同商品可以并行上传。
- 锁归属不确定时不会发布 Catalog。

### 16.3 故障回退

- OSS 查询、认证、网络或上传异常不阻断本地任务。
- 回退任务保留本地结果且不发布 Catalog。
- 锁竞争失败不会触发本地重复生成。

### 16.4 发布完整性

- 不完整或自定义任务不发布 Catalog。
- Catalog 出现时，Manifest、预览图、分类 ZIP、完整 ZIP 和 Excel 均可读取。
- 完整 ZIP 的大小和 SHA-256 与 Catalog、Manifest 一致。

### 16.5 下载与复用

- 用户可以搜索链接或商品 ID 并看到预览。
- 一键复用可以下载、校验、解压并打开本地目录。
- 中断下载可以从 `.part` 继续。
- 本地存在相同 SHA-256 时不重复下载。
- 校验失败的文件不会解压。

### 16.6 批处理

- 命中共享素材的行跳过采集和生成。
- 复用结果仍生成当前批次所需目录和商品 Excel。
- OSS 异常时维持原有批处理顺序、暂停和停止行为。
- 产品图批量替换和商品图搜同款不参与共享查询或发布。

### 16.7 安全

- 日志、错误响应、Catalog 和 Manifest 不出现 OSS Secret。
- 完整素材不提供永久匿名公网地址。

## 17. 测试策略

- 单元测试：身份解析、完整性判断、Catalog/Manifest 序列化、SHA-256、缓存和路径边界。
- OSS 模拟测试：禁止覆盖锁、锁续期、锁竞争、分页列表、发布顺序、断点下载和服务异常。
- 单链接集成测试：命中、锁定、未命中、故障回退、完整发布和不完整任务。
- 批处理集成测试：自动复用、当前批次 Excel 导出、故障回退和原有停止行为。
- 前端测试：筛选、搜索、分页、下载状态、损坏状态和按钮可用性。
- 回归验证：完整 Python 测试、前端 TypeScript 检查、前端生产构建和 `git diff --check`。
- 真实 OSS 手工验收：两台客户端同时抢锁、并发上传不同商品、上传中断、下载续传和私有 Bucket 权限。
