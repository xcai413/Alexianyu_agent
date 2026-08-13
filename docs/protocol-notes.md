# 闲鱼协议笔记 (Phase 1)

> 来源:公开搜索 xianyu-auto-reply、XianYuApis、GoofishEngine 项目,只整理字段命名与流程,不引用任何源代码。
> 本文档由 xianyu-agent 维护,字段值以实际抓包为准。

## 1. 三类入口

| 入口 | 用途 | 备注 |
|------|------|------|
| wss://wss-goofish.dingtalk.com/ (WebSocket) | 实时聊天消息、订单状态推送 | 长连接,需 heartbeat |
| https://h5api.m.goofish.com/h5/mtop... (mtop RPC) | 主动拉取订单、详情、用户信息 | 短连接,需带 _m_h5_tk 签名 |
| https://passport.goofish.com/... (登录) | 扫码 / cookie 校验 | Phase 1 不做自动登录,只接 cookie |

## 2. Cookie 关键字段

| 字段 | 用途 | 必备 |
|------|------|------|
| unb | 用户身份标识 | 是 |
| _m_h5_tk | mtop 接口签名 token | 是 |
| cookie2 | 反爬 | 否但建议有 |
| sgcookie | 风控 | 否 |
| t | 时间戳,部分场景用 | 否 |

### 签名生成 (mtop)

token 由 _m_h5_tk 字段拆分得到:

    token = _m_h5_tk.split('_')[0]    # 前半段作为种子
    ts    = 当前毫秒时间戳
    appKey = "34839810"                # 固定
    sign = md5(f"{token}&{ts}&{appKey}&{data}")

请求头需要带:
    x-t:        {ts}
    x-sign:     {sign}
    x-mini-wua: {H5加密后的ua}  # 复杂,Phase 1 先留空
    x-sid:      会话ID
    x-uid:      用户ID(unb)

> 实际项目里 _m_h5_tk 会随时间过期,需要定期 xianyu-agent auth refresh --account id 重拉。

## 3. WebSocket 帧结构 (mtop push)

### 连接与握手

- 地址:`wss://wss-goofish.dingtalk.com/`(无额外 path/query)
- 请求头:仅需 `Cookie: <完整 cookie 串>`(含 unb / _m_h5_tk / cookie2 等)
- 心跳帧(间隔 ~15s):

      {"lwp": "/!", "headers": {"mid": "<随机3位><毫秒时间戳> 0"}}

- 连接建立后必须先获取 IM `accessToken`,发送 `/reg`,随后发送
  `/r/SyncStatus/ackDiff`;当前实现完成这两帧后才标记 connected。
- 服务端推送需回 code=200 ACK(复用 mid/sid),否则连接可能被服务端关闭。
- 实时数据位于 `body.syncPushPackage.data[*].data`,当前支持 Base64 JSON 与 MessagePack。

消息推送帧格式 (以下结构为常见约定,实际以抓包为准):

    {
      "code": 0,
      "packetId": "<uuid>",
      "headers": {
        "appKey": "34839810",
        "mid": "...",
        "timestamp": "1700000000000"
      },
      "body": {
        "ack": "...",
        "dataTemplate": "...",
        "pushStrategy": "...",
        "time": "1700000000000",
        "userId": "<unb>",
        "version": "..."
      }
    }

body 通常是 base64 编码的 JSON,需要先解码再解析。常见字段:

| 字段 | 含义 |
|------|------|
| 1 (string) | 消息正文 (买家发的文字 / 系统通知) |
| 2 (string) | 发送者 userId |
| 3 (string) | 接收者 userId |
| 4 (string) | 消息类型,常见值: text / image / card / system |
| 5 (object) | 扩展数据 (订单详情、卡片信息等) |
| 6 (object) | 时间戳 + 消息 ID |
| 10 (string) | chatId (会话唯一标识) |
| 100 (object) | 商品信息 (订单推送时) |

> 字段编号与 body 内的 JSON 结构会根据闲鱼后端版本变化,不要硬编码。

## 4. 事件类型映射 (本项目关注)

| 原始事件 | 本项目事件 | 触发动作 |
|----------|-----------|---------|
| 收到买家文字消息 | MessageReceived | 匹配回复规则 / AI 回复 |
| 收到订单创建推送 | OrderCreated | 入库待支付 |
| 收到订单支付推送 | OrderPaid | 触发自动发货 |
| 收到订单发货完成推送 | OrderDelivered | 更新订单状态 |
| 收到系统通知 | SystemNotice | 入 audit_logs |
| 连接断开 | ConnectionStateChanged | 触发重连 |
| 签名失败 / token 过期 | ErrorOccurred | 需要 auth refresh |

## 5. 鉴权失败常见信号

| 现象 | 原因 | 处理 |
|------|------|------|
| code != 0 且 body 含 "重新登录" | cookie 过期 | auth refresh --account id |
| 连接建立后立刻断开 | unb 不匹配 _m_h5_tk | 重新抓 cookie |
| heartbeat 反复失败 | IP 风控 / 滑块 | Phase 1 不处理,只记录 |
| mtop sign error | 签名算法不对 | 检查 signer.py 的 md5 拼接顺序 |

## 6. 已知风险与限制

- 闲鱼后端频繁变更:字段编号、签名算法会变,本项目假设一个稳定期内有效。
- 风控:高频 heartbeat / 异常消息触发滑块验证码。Phase 1 不接滑块求解器,触发后告警并暂停账号。
- AGPL-3.0 上游:GoofishEngine 与本项目不兼容。本文档只引用公开字段命名约定。

## 7. 后续 Phase 扩展点

- Phase 2:多账号并行 - 需要连接池抽象
- Phase 3:消息规则匹配 - 复用 events.MessageReceived
- Phase 4:自动发货 - 复用 events.OrderPaid
- Phase 5:Textual TUI - 订阅 events.ConnectionStateChanged 实时显示状态

## 8. 扫码登录(已实现,Phase 9)

入口:`xianyu-agent auth qr-login --account <id>`(实现:`protocol/qr_login.py`)。

### 流程与接口(字段命名参考公开项目,代码自写)

1. **取 m_h5_tk**:GET `h5api.m.goofish.com/h5/mtop.gaia.nodejs.gaia.idle.data.gw.v2.index.get/1.0/`
   → 响应 Set-Cookie 含 `m_h5_tk`;token = 下划线前半段;md5(token&t&appKey&data) 签名后 POST 同址。
2. **取登录参数**:GET `passport.goofish.com/mini_login.htm`(参数:lang/appName=xianyu/
   appEntrance=web/stie=77/rnd 等)→ 从 HTML 正则提取 `window.viewData = {...}` →
   `loginFormData` 字段,附加 `umidTag=SERVER`。
3. **生成二维码**:GET `passport.goofish.com/newlogin/qrcode/generate.do` 带 loginFormData
   → `content.success=true` 时取 `content.data.t` / `content.data.ck` / `content.data.codeContent`。
4. **轮询状态**:POST `passport.goofish.com/newlogin/qrcode/query.do`(data=t/ck+loginFormData)
   → `content.data.qrCodeStatus`:
   - `NEW` 等待扫码;`SCANED` 已扫待确认
   - `CONFIRMED` 且 `iframeRedirect=false` → 成功,响应 Set-Cookie 含 `unb` 等
   - `CONFIRMED` 且 `iframeRedirect=true` → 风控,需手机验证(`iframeRedirectUrl`)
   - `EXPIRED` 过期;其他 → 取消
5. **落库**:成功后将 Cookie 串经 Fernet 加密存 `cookies` 表。

### 状态机

`waiting → scanned → success | verification_required | expired | cancelled`;轮询间隔 0.8s,
会话 TTL 300s。

### 已知风险

- passport 接口字段可能随版本变化(与 WS 协议同理),实测为准。
- 风控(手机验证)不在自动化范围内,CLI 会打印验证 URL 并退出码 2。
- 扫码成功保存新 Cookie 时,旧 IM accessToken 会立即失效;稳定 device ID 保留,随后用
  `auth refresh --account <id>` 换取新 token。
- 真实帧校准使用 `protocol capture`;该命令不保存原始字符串值,仅保存字段结构、类型、长度
  与不可逆 SHA-256 摘要,并由账号级文件锁防止重复连接。
