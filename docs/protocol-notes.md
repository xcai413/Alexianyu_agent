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
- `/reg` 必须等待相同 `headers.mid` 的服务端响应;响应 code 缺省按 200,明确非 200 则
  注册失败。客户端回 ACK 后再发送 `ackDiff`,不能以固定 sleep 代替注册确认。
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

## 9. 卖家已售订单读取(已完成真实账号校准)

入口:`xianyu-agent order sync --account <id>`。该命令只发送订单列表读取请求并写入本地
SQLite 镜像；不会发货、退款、评价、修改订单或触发自动化。

当前实现的待校准契约:

| 项目 | 当前值 | 证据状态 |
|------|--------|----------|
| API | `mtop.taobao.idle.trade.merchant.sold.get` | 2026-08-21 真实账号成功读取 |
| 请求数据 | `pageNumber` / `rowsPerPage` / `queryCode=ALL` | 2026-08-21 真实账号成功读取 |
| 响应分页 | `data.module.items` / `totalCount` / `nextPage` | 44 条订单，2 页成功读取 |
| 每笔订单 | `commonData.orderId` / `itemId` / `orderStatus`；`priceVO.buyNum` | 44 条订单成功标准化并入库 |
| 商品标题 | `itemVO.title` | 2026-10-10 核对本地参考 `utils/xianyu_seller_api.py:parse_sold_order` 与对应测试；真实完整列表 76 条／3 页与历史精确查询 3 条均成功提取／落库，后台 79 条标题一致 |
| 历史订单精确读取 | `orderIds` 为逗号分隔字符串 | 参考 `XianyuSellerAPI.get_sold_orders`；2026-10-10 对完整列表未返回的 3 条历史订单只读查询成功，响应 `items` 为 3 条但 `totalCount` 为 0，不能据后者否定已返回记录 |

本地保存时仅白名单化保留订单号、商品 ID、商品标题、状态、下单时间、买家 ID/昵称、金额和数量。标题只接受非空字符串，缺失标题不覆盖已知值；`itemVO` 仅保存标题，不保留其他附加字段。响应中
可能存在的电话、收货地址等字段不会写入 `orders.raw_payload`。本次校准仅证明订单列表读取和
本地镜像；不代表 IM Token、WebSocket 订阅或真实发货已验证。

### 9.1 订单实时详情与收货信息（2026-10-10）

- 复用已验证的sold.get，以`orderIds`单个字符串精确读取；仅匹配实际返回的目标订单，不用`totalCount`判断是否存在。增加运营字段白名单投影：买家头像／昵称／ID、收件人／电话／地址／隐私标识、商品图片／ID／标题、成交额／单价／件数／运费／优惠／实收／退款额、下单／付款／发货／完成时间、退款与评价状态及地址修改申请。字段依据本地参考`utils/xianyu_seller_api.py:parse_sold_order`及真实目标响应的键存在性，不猜测评价码含义。
- 联系方式保持平台返回的原文和脱敏形式，仅在已登录管理员的实时详情中展示；不扩展订单镜像的raw_payload白名单，不把收货信息写入数据库、日志或浏览器存储。详情API返回`Cache-Control: no-store, private`。
- 现有发货页、物流、退款记录客户端的签名曾使用带有效期后缀的整段mtop token；修正为与已验证订单列表相同的token seed。真实只读查询随后成功取得规格／收货信息和物流轨迹；当前订单的退款记录仍读取失败，不能视作不存在退款。
- 发货页保留全部规格，物流保留全部包裹与节点，并保留既有第一规格／第一包裹的兼容字段。缺失的序列号／上传报告要求返回null；字符串false正确解析为false，不用缺失值断言“不需要”。
- `order details <订单> -a <账号>`与管理员`GET /orders/{order_id}/details`共用只读聚合；四种来源独立返回成功／失败，不返回平台原始载荷或错误正文。页面仅读取当前选中订单，失败部分可刷新，不自动重试或执行平台订单动作。该轮不证明退款记录成功、平台写动作或无人值守验收。

### 9.2 商品完整图库与账号头像（2026-10-10 真实只读校准）

- 商品详情 API `mtop.taobao.idle.pc.detail` 1.0：先用既有结构校准入口核对，再在内存中确认 `data.itemDO.imageInfos` 为列表，每项图片地址为 `url` 字符串。测试账号的 9 件在售商品均成功返回图库；浏览器逐件确认所有返回的图库图片加载成功，首件 5 张。只解析这一已观察路径，不据本轮图片证据扩展标题、价格等未校准字段。
- 商品详情与卖家资料客户端均修正签名为 `_m_h5_tk` 的下划线前 seed，附有效期后缀的签名断言已固化。资料 API `mtop.idle.web.user.page.nav` 的 `data.module.base.avatar`／昵称路径真实读取成功，头像在真实后台加载成功。
- `ItemDetailFacts.images` 缺失／错误形状为 `None`，显式空列表为 `()`；正常列表取全部非空字符串 URL 并去重。既有 CLI 详情同步复用这些事实写入图库；Web `GET /items/{account_id}/{item_id}/images?platform=true` 则只读当前选中商品，不写商品镜像，平台失败保留本地主图／详情图／规格图。复用持久化风控闸和账号节流；页面不批量读取全部商品。
- 管理员 `GET /accounts/{account_id}/profile` 返回头像和昵称，不更新昵称或写操作审计；与平台图库读取均禁止缓存。未登录不调用平台、跨账号商品查询按本地归属隔离，失败不返回平台原始正文。图片仅允许无用户名／密码的 HTTP(S) URL，加载失败显示占位，原图入口不发送 Referer。

## 10. 买家侧自动化接口(2026-09-20 核对参考项目)

本节的接口事实来自对两个既定参考项目的只读核对(未复制源码,仅取接口名、参数与业务结论,
再在本仓库独立实现):

- `xcai413/Ydisks-Xianyu-Helper`(Apache-2.0):`internal/xianyu/mtop/account_tasks.go`、
  `docs/wiki/在线聊天与账号自动任务.md`。
- `xcai413/xianyu-super-butler`(AGPL-3.0):`utils/item_polish.py`、
  `utils/xianyu_seller_api.py`、`app/reply_server.py`。

### 10.1 商品擦亮(两个参考一致,可实现)

| 项目 | 值 |
|------|-----|
| API | `mtop.taobao.idle.item.polish` |
| 版本 | `2.0`(H5);传 `1.0` 会被判「接口不存在」 |
| URL | `https://h5api.m.goofish.com/h5/mtop.taobao.idle.item.polish/2.0/` |
| 请求数据 | `{"itemId": "<商品 id>"}` |
| Referer / Origin | `https://www.goofish.com/` |
| 签名 | H5 `_m_h5_tk` 签名,与其他 H5 接口一致 |
| 幂等信号 | 当天已擦亮返回业务错误:`IDLEITEM_POLISH_AGAIN`、「已经擦亮」、`POLISH_DUPLICATE`、「一天只能擦亮一次」 |
| 备用接口 | `mtop.idle.item.polish` `1.0`(仅 Helper 使用,作为主接口失败后的兜底) |

业务结论(Helper 文档):每日擦亮是**账号级**开关,按北京时间每天最多执行一次,默认 `03:00`;
逐个商品调用,单个商品失败要记录原因并允许重试,不能因为按钮返回就认为全部成功。

### 10.2 索要小红花(单一来源,需真实校准)

| 项目 | 值 |
|------|-----|
| API | `mtop.taobao.idlemessage.red.flower` |
| 版本 | `1.0`(参考实现使用默认版本) |
| 请求数据 | `{"orderId": "<订单 id>"}` |
| 特殊参数 | 不带 `valueType` |
| Referer / Origin | `https://seller.goofish.com/` |

业务结论:交易完结后即可调用,与评价状态无关;会给买家发出一条消息,属于真实外发;
参考实现按账号开关控制,且对同一订单重复触发会连续打扰买家,因此必须按订单幂等。

证据强度:只有 super-butler 一处来源,尚未在真实账号校准;实现后必须由用户先做单订单验证。

### 10.3 卖家评价买家(**两个参考冲突,暂不实现**)

| 来源 | API | 版本 | 请求数据 | Referer |
|------|-----|------|----------|---------|
| Helper | `mtop.taobao.idle.rate.create` | `4.0` | `tradeID` + `feedback` | `https://seller.goofish.com/` |
| super-butler | `mtop.taobao.idle.merchant.rate.create` | `1.0` | `tradeIdList` + `rate` + `feedback` + `imageUrls` + `anonymous` | `https://seller.goofish.com/` |

待评价列表同样存在两处说法:Helper 用 `mtop.taobao.idle.merchant.rate.list` `1.0`,
super-butler 走订单列表里的 `sellerRateStatus` 字段。

结论:**接口名、版本、请求体三方分歧**,且评价提交后不可撤销。在拿到真实抓包证据之前不实现,
避免用错误接口对买家产生不可逆动作。

### 10.4 平台确认发货(单一来源,需真实校准)

虚拟商品的"发货"由两段组成:把卡密发给买家,以及在订单上确认发货。只做前一段时买家已经
拿到内容,但订单仍停在待发货,平台按未发货超时处理并影响退款判定。

| 项目 | 值 |
|------|-----|
| API | `mtop.taobao.idle.logistic.consign.dummy` |
| 版本 | `1.0` |
| URL | `https://h5api.m.goofish.com/h5/mtop.taobao.idle.logistic.consign.dummy/1.0/` |
| 请求方式 | POST 表单,`data=<签名后的 JSON>` |
| 请求数据 | `{"orderId":"...","tradeText":"...","picList":[],"newUnconsign":true}` |
| 特殊参数 | 查询参数里没有 `spm_cnt`,也没有 `valueType` |
| Referer / Origin | `https://www.goofish.com/` |
| 成功标记 | `ret` 含 `SUCCESS`(参考实现观察到的形态是 `SUCCESS::调用成功`) |

业务结论:`tradeText` / `picList` 承载"已经发给买家的发货凭证",属于可选的补充信息,
参考实现的默认调用不带凭证。凭证内容只参与本次请求,不得写入日志或任务快照。

证据强度:只有 Helper 一处来源(`internal/xianyu/mtop/consign.go`)。参考实现只记录过
`FAIL_BIZ_ORDER_STATUS_ERROR::订单状态错误` 这一种业务失败,**没有**"已发货"的同义错误样本;
因此本项目不猜测重复发货的返回形态:任何非 SUCCESS 都如实上报,由人工判断,
绝不因为一次业务错误就自动重发卡密。

### 10.5 订单改价(单一来源,需真实校准)

平台只接受买家**尚未付款**的订单改价。

| 项目 | 值 |
|------|-----|
| API | `mtop.taobao.idle.trade.user.adjust.price` |
| 版本 | `1.0` |
| URL | `https://h5api.m.goofish.com/h5/mtop.taobao.idle.trade.user.adjust.price/1.0/` |
| 请求方式 | POST 表单,`data=<签名后的 JSON>` |
| 请求数据 | `{"modifyFee": <整数分>, "newTransportFee": "0", "orderId": "..."}` |
| Referer / Origin | `https://www.goofish.com/` |
| 成功判定 | `ret` 含 SUCCESS **且** `data.success` 为 `true` |

业务结论:`modifyFee` 是**分为单位的整数**;参考实现把运费固定成 `"0"`。只看 `ret` 会把
"接口通了但改价没生效"误判成成功,所以本项目同时要求 `data.success`。

证据强度:只有 Helper 一处来源(`internal/xianyu/mtop/adjust_price.go`)。金额改动会直接影响
买家付款,真实调用前必须按 `docs/待真实验证清单.md` 做单订单校准,且不得接入批量流程。

### 10.6 聊天图片消息(单一来源,需真实校准)

图片消息与文本共用已校准的 `MessageSend/sendByReceiverScope` 信封,
只有 base64 后的 `content` 不同:

| 项目 | 值 |
|------|-----|
| 外层 lwp | `/r/MessageSend/sendByReceiverScope`(与文本一致) |
| content | `{"contentType":2,"image":{"pics":[{"height":H,"type":0,"url":URL,"width":W}]}}` |
| contentType | `2`(文本是 `1`) |
| 图片地址 | 必须是平台能拉取的 **https 公网地址** |
| 默认尺寸 | 参考实现宽高缺失时用 800x600 |

证据强度:只有 Helper 一处来源(`internal/xianyu/ws/sync.go` 的 `SendImage`,以及
`internal/server/chat_handlers.go` 的上传入口)。本项目按同一形状实现客户端,
并在发送前强制 https 校验;真实发送前必须按 `docs/待真实验证清单.md` F9 做单会话校准。

### 10.7 卖家账号资料(只读,可直接实现)

| 项目 | 值 |
|------|-----|
| API | `mtop.idle.web.user.page.nav` |
| 版本 | `1.0` |
| URL | `https://h5api.m.goofish.com/h5/mtop.idle.web.user.page.nav/1.0/` |
| 请求方式 | POST 表单,`data={}`(空 JSON) |
| 额外查询参数 | `ecode=0`、`spm_cnt=a21ybx.home.0.0`(参考实现多带的两个) |
| Referer / Origin | `https://www.goofish.com/` |
| 昵称路径 | `data.module.base.displayName`,为空回退 `displayNick` |
| 头像路径 | `data.module.base.avatar` |

这是只读接口,用来解决"本地账号昵称一直为空"的运营可见性问题;不修改任何平台状态。
证据:Helper `internal/xianyu/mtop/user_profile.go`。

### 10.8 已读上报(单一来源,需真实校准)

| 项目 | 值 |
|------|-----|
| lwp | `/r/MessageStatus/read` |
| payload | `{}`(空对象) |
| 参数 | 一个字符串列表,内容是待上报的平台消息 ID |
| 效果 | 只影响卖家侧未读标记,**不向买家发送任何内容** |

证据:Helper `internal/xianyu/ws/sync.go` 的 `MarkChatRead`。它是平台写操作,所以和发送一样
"写一次 + 按响应判定":平台拒绝按明确拒绝上报,超时或写后异常按结果不明上报,不盲目重试。
真实验收见 `docs/待真实验证清单.md` F10。

### 10.9 官方消息识别(可直接复用的事实)

Helper 记录的平台消息元数据可直接用于过滤自动化输入:

- `contentType=14`:平台通知类消息;
- `contentType=25`:确认收货后的评价提醒;
- `contentType=26`:交易卡片;
- 发送方用户 ID `1400`:闲小蜜;
- 占位内容「发来一条新消息」;
- 评价提醒兼容摘要「快给ta一个评价吧～」/「快给ta一个评价吧~」。

这些消息不得进入关键词回复、默认回复或 AI 回复链。

### 9.3 订单镜像付款／发货时间（2026-10-10 真实只读校准）

- 用户反馈自定义范围无收益后，确认本地79笔订单全部缺失paid_at／delivered_at；并非没有付款，而是卖家订单同步DTO和落库均遗漏这两个字段。真实第一页30笔的commonData.paySuccessTime有29笔可解析字符串，consignTime有27笔可解析字符串，均为上海时区YYYY-MM-DD HH:MM:SS；与实时详情现有解析路径一致。
- 卖家订单列表DTO新增paid_at／delivered_at，分别仅按paySuccessTime／consignTime解析为UTC，不用下单时间或当前时间补造。快照保存两个时间，缺失／无效值不覆盖已知值，已回到待发货的paid状态仍清除旧delivered_at。raw_payload仅额外允许这两个可解析的时间字符串，不保留电话／地址／物流公司／单号或其他未白名单字段。
- 精确历史订单读取与完整列表共用同一DTO解析，orderIds仍只指定单个订单，严格匹配返回记录，totalCount=0不会丢弃实际结果。真实完整列表76笔／3页，另3笔历史精确读取均成功；通过既有快照应用补齐79笔镜像，付款／发货统计所需时间缺失数均为0。仅平台读取及本地镜像更新，不执行平台外发或改变订单状态。
