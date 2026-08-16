# OSS 私有共享素材库配置

共享素材库只服务于淘宝、天猫的“参考对标商品创作”任务。Catalog、预览图、分类包和完整包均存放在私有 OSS 前缀下，浏览器只访问本地服务代理，不会拿到 OSS 签名地址。

## 1. 创建最小权限 RAM 用户

为工作台单独创建 RAM 用户和 AccessKey，不要复用个人主账号。Bucket 保持私有，建议把共享素材放在 `local_settings.json` 的 `prefix/shared-library/` 下（例如 prefix 为 `product-workflow` 时，实际前缀是 `product-workflow/shared-library/`）。

该 RAM 用户只需要对目标 Bucket 的以下对象权限：

- `ListObjects`：仅允许列出 `product-workflow/shared-library/catalog/`。
- `GetObject`：允许读取 `product-workflow/shared-library/catalog/`、`packages/` 和 `staging/`。
- `PutObject`、`CopyObject`、`DeleteObject`：允许写入和清理 `product-workflow/shared-library/locks/`、`staging/`、`packages/`、`catalog/`。

如果使用不同的 `prefix`，将上面的路径替换为 `{prefix}/shared-library/*`。不要授予整个 Bucket 的删除、公开读或公共 ACL 权限。生产环境应再按实际 RAM 策略收紧 Catalog 的列举范围。

## 2. 本地配置

复制示例配置后填写 HTTPS endpoint、Bucket 和业务前缀：

```powershell
Copy-Item local_settings.example.json local_settings.json
```

AccessKey 只放在当前 Windows 用户环境变量中，不写入 JSON、代码、日志或提交记录：

```powershell
[Environment]::SetEnvironmentVariable('PRODUCT_WORKFLOW_OSS_ACCESS_KEY_ID', 'YOUR_RAM_ACCESS_KEY_ID', 'User')
[Environment]::SetEnvironmentVariable('PRODUCT_WORKFLOW_OSS_ACCESS_KEY_SECRET', 'YOUR_RAM_ACCESS_KEY_SECRET', 'User')
```

重新打开工作台进程后再运行。`local_settings.json` 已被 Git 忽略；共享 API 也不会返回凭据、签名查询串或永久 OSS URL。

## 3. 并发和降级规则

- 两个客户端提交同一个淘宝/天猫商品时，先拿到条件锁的客户端生成并发布；另一个客户端显示“其他用户正在生成”，不会重复采集或生图。
- 不同商品使用不同锁，可以同时上传。每个任务只在自己的 staging 路径写入，Catalog 是最后的可见性边界。
- 查询、认证、续租、下载或上传遇到 OSS 基础设施故障时，任务继续生成并保存到本地，结果标记为本地降级，不发布 Catalog。
- Catalog 已存在时自动下载完整包并物化到 `outputs/reused/{product_key}/`，跳过采集和模型调用。下载会校验大小和 SHA-256，`.part` 文件可断点续传。
- 只有默认完整任务才发布：主图 10 张、SKU 3–8 张、详情 6–15 张，三类工作流均完成且 Excel 已导出。自定义数量或部分结果只保存在本地。

共享素材库不接入产品图批量替换、图片搜同款、自有商品、京东、抖音或快手任务。

## 4. 两客户端验收

使用非生产商品 ID 和独立测试前缀执行以下检查：

1. 两个客户端同时提交同一淘宝链接：一个成功拿锁并发布，另一个只得到锁占用提示。
2. 两个客户端同时提交不同商品：两边都能完成，Catalog 中各有一条记录。
3. 临时撤销 OSS 权限：单链接和参考对标批处理仍完成本地输出，Catalog 不新增记录。
4. 中断完整包下载：保留 `.part`，下一次下载从已有字节继续，并在 SHA-256 匹配后替换最终 ZIP。
5. 替换测试 ZIP 内容：校验失败时不解压、不登记本地目录。
6. 完成一套标准任务后，确认 OSS 写入顺序中 Catalog 最后出现；第二个客户端命中 Catalog 后不启动采集器和生图模型。

## 5. 2026-08-15 真实双进程验收记录（最终）

本机使用两个独立 Python 进程连接同一个 Bucket，未启动采集器或生图模型：

- 同一测试商品并发抢锁：一个进程成功拿锁，另一个得到锁占用结果，通过。
- 两个不同测试商品同时发布：两个进程先同时持有各自商品锁，再并发上传 2 MB 测试包；两个发布区间重叠并且 Catalog 均可读取，通过。
- 真实 OSS 响应头使用 SDK 映射对象而不是原生 `dict`，验收发现旧代码会把正确的 `Content-Length` 误判为 0；已增加回归测试并修复。
- 为 RAM 用户补充四个共享素材库前缀的 `DeleteObject` 后，上一轮残留测试对象已全部清理。
- 最终运行使用两个独立进程：同商品结果为 `acquired + busy`，不同商品均为 `completed`，两个发布时间区间重叠，两个 Catalog 探测状态均为 `available`。
- 最终清理结果为 `ok: true`，无残留对象；当前环境满足共享素材库的并发上传、锁释放和 staging 清理要求。
