# OSS 本机配置

程序只会上传生成成功的主图、SKU 图和详情图。采集的对标素材不会上传。

## 1. 设置 Windows 用户环境变量

将下面两行中的占位值替换为新建且未泄露的 RAM AccessKey。请在 PowerShell 中逐行执行：

```powershell
[Environment]::SetEnvironmentVariable('PRODUCT_WORKFLOW_OSS_ACCESS_KEY_ID', 'YOUR_ACCESS_KEY_ID', 'User')
[Environment]::SetEnvironmentVariable('PRODUCT_WORKFLOW_OSS_ACCESS_KEY_SECRET', 'YOUR_ACCESS_KEY_SECRET', 'User')
```

不要将密钥写入 `local_settings.json`，不要把它发送到聊天、截图或 Git。

## 2. 重启程序

完全退出“商品图片工作台”，再重新启动服务。新服务才会读取刚写入的 Windows 用户环境变量。

## 3. 验证结果

完成一次图片生成后：

- 本地生成文件仍位于“生成结果”文件夹。
- 批处理导出的 Excel 中，“生成图路径”列会是以 `https://transform-image.oss-cn-shenzhen.aliyuncs.com/product-workflow/` 开头的公网链接。
- 采集图路径仍是本地文件链接，这是预期行为。

OSS Bucket、区域和前缀配置位于 `local_settings.json` 的 `oss` 对象中；默认值对应深圳区域的 `transform-image` Bucket。
