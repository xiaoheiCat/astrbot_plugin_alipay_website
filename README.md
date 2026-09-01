# AstrBot 支付宝 AI 网页应用收款

基于支付宝 AI网页应用收款 的 Agent 收款插件

## 原理

本插件为 AstrBot Agent 注册两个内置参数固定的工具：

- `create_alipay_bill(cny, message)`：创建 `0.01`–`50.00` 元人民币订单，先发送一句话，再发送支付二维码。
- `verify_alipay_bill(out_trade_no)`：只允许查询当前会话创建的订单，并直接向支付宝复核状态。

二维码指向 AstrBot 自身的公开落地页。买家可在电脑网页或手机浏览器打开页面，再进入支付宝收银台。公开路由无需管理员登录：

- `GET /alipay?token=...`：仅保存摘要的高强度随机令牌支付落地页。
- `POST /alipay/notify`：支付宝异步通知。
- `GET /alipay/return`：支付宝同步返回页。

## 安装与依赖

项目使用 `pyproject.toml` 与 `uv.lock` 管理、锁定依赖：

```bash
uv sync --frozen
```

AstrBot 是运行宿主，不作为插件依赖重复安装。插件运行依赖为 `alipay-sdk-python`、`aiosqlite` 和 `qrcode[pil]`。

AstrBot v4.27.4 的插件安装器只识别插件根目录的 `requirements.txt`，因此仓库保留该文件作为安装器兼容清单；测试会检查它与 `pyproject.toml` 的直接依赖完全一致。版本解析与开发环境仍以 `pyproject.toml` 和 `uv.lock` 为准。通过 WebUI 安装插件时，AstrBot 会自动安装缺失依赖。

手动部署时，也可以让 uv 把兼容清单安装进 AstrBot 实际使用的 Python 环境：

```bash
uv pip install --python /path/to/AstrBot/.venv/bin/python -r requirements.txt
```

## 配置

先在 AstrBot 全局设置中配置“对外可达的回调接口地址”（`callback_api_base`），例如 `https://bot.example.com`。插件会在该地址后追加固定公开路由；生产环境强制 HTTPS。

可用 `GET /alipay` 检查公开路由：未携带令牌时应显示插件的“支付链接无效”页面，而不是 AstrBot 面板的通用 404。反向代理只需公开 `/alipay` 及其子路径，无需开放面板登录接口。

插件设置包括：

- 环境：`sandbox` 或 `production`；
- 当前环境对应的 App ID、PKCS#1 应用私钥、支付宝公钥、`seller_id`；
- 订单有效期：5–30 分钟整数，默认 15；
- 回调提醒模式：`off`、`user_message` 或 `fake_tool_call`。

托管的 AstrBot 无需也不应修改插件目录：选择 `sandbox` 时，直接在插件配置页填写沙箱应用的四项凭据；选择 `production` 时填写生产应用的四项凭据。两套凭据不要混用。插件不会读取、创建或持久化支付宝 AI 付 Skill 动态生成的 `.alipay-sandbox.json`。

`user_message` 会把以下一次性内容追加为用户消息内容：

```xml
<system_reminder>用户已完成付款，商户订单号：ORDER_NO。请使用 verify_alipay_bill 复核支付状态。</system_reminder>
```

`fake_tool_call` 则按照 AstrBot 的消息结构构造一组 ID 匹配的 assistant tool call 和 tool result。两种模式均使用数据库原子状态保证同一订单只成功注入一次；失败会保留重试。

## 存储与安全

订单保存在 `data/plugin_data/astrbot_plugin_alipay_website/orders.sqlite3`。落地页只保存令牌的 SHA-256 摘要，支付表单使用订单自身的绝对到期时间；异步通知以流式 64 KiB 上限读取，并依次校验 RSA2 签名、App ID、商户订单号、金额、`seller_id` 和交易状态。订单状态只允许单调迁移，已确认付款不会被陈旧查询或乱序通知降级。

资源参数固定在代码中，不出现在配置页面：每会话每分钟最多创建 3 单、最多同时保留 3 笔有效订单；全局每分钟最多创建 30 单、最多 500 笔有效订单和 10000 条保留记录；同一订单两次支付宝查询至少间隔 5 秒且最多查询 60 次。公开通知和同步返回页均限制输入规模、请求速率与并发，订单创建、二维码生成和支付宝网关调用也有固定并发上限。定时任务每 5 分钟处理过期订单；查询预算耗尽的过期订单会转为本地过期状态，之后仍可被真实付款通知升级。任务还会关闭等待付款的死订单，并清理 30 天前的终态订单及孤立二维码。

> 配置页的密钥遮罩只用于避免界面直接展示。请同时保护 AstrBot 配置文件、插件数据目录和服务器访问权限，且不要在日志中输出密钥或完整回调参数。
