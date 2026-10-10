---
name: xianyu-delivery
description: 闲鱼自动发货编排 — 判断订单该走哪条发货链、Dry Run 预览、手动补发、排查失败与结果不明。订单已付款但未发货、或需要核对发货结果时使用。
---

# 闲鱼发货编排

## 何时使用

- 订单 `paid` 但迟迟未 `delivered`(自动发货失败或未触发)。
- 需要手动补发、核对库存、排查失败原因,或处理"结果不明"的发货任务。

## 先分清两条链路

本仓库有两种发货实现,判断走哪条是第一步:

| 链路 | 触发条件 | 库存来源 | 查证入口 |
|------|----------|----------|----------|
| 旧卡密链路(Phase 4) | 没有启用的发货策略 | `cards` 卡密组 | `xianyu-agent card list --account <id>` |
| 发货引擎(Phase 6,优先) | 存在**启用的**发货策略 | 库存池(`text`/`data`/`image`/`api`) | `xianyu-agent delivery policy list` |

`XIANYU_DELIVERY_ENGINE` 默认 `auto`:有启用策略走 Phase 6,没有才回落旧链路。排查前先确认
当前生效的是哪条,否则会在错误的表里找记录。

## 排查流程

1. **看订单**:`xianyu-agent order list --account <id> --status paid`,再 `xianyu-agent order show <id>`。
   订单行里的 `delivery_content` 只有旧链路会写;Phase 6 的发货内容在发货任务里,不要因为它是空
   就判定"没发货"——以 `delivered_at` 与任务状态为准。
2. **看发货任务**:`xianyu-agent delivery job list --account <id>`,再
   `xianyu-agent delivery job show <job_id>` 看状态、尝试记录与错误。
   - `uncertain`:**结果不明**。卡密可能已经发给买家,禁止直接重试;先核对平台聊天与库存,
     再用 `xianyu-agent delivery job resolve <job_id> --delivered/--not-delivered` 收尾。
   - `needs_confirmation` / `blocked`:守卫或策略拦住,按 reason 处理。
   - `failed_retryable`:明确没发出去,可以 `xianyu-agent delivery job retry <job_id>`。
3. **Dry Run 复现判断**:`xianyu-agent delivery job plan --account <id> --order-id <oid>`,
   它会说明这次发货会命中哪条策略、从哪个池取几份、是否需要平台确认,且不产生任何副作用。
4. **确认池与内容**:
   - data 池:`xianyu-agent inventory pool show <pool_id>` + `xianyu-agent inventory unit list <pool_id>`
   - api 池:用 `xianyu-agent delivery api test <配置名>` 试调用一次,只回掩码预览,不占库存
   - text / image 池:内容就在池上(`xianyu-agent inventory pool show <pool_id>`)
5. **手动补发**(仅当明确没发出去):
   - `xianyu-agent delivery job run --account <id> --order-id <oid> --send --yes`
   - 加了 `--send` 才会真的发;平台确认(虚拟发货)由策略的 `--platform-confirm` 决定。
6. **人工收尾结果不明**:任务停在 `uncertain` 时,先用 `xianyu-agent attention list` 找到它,
   核对平台后再 `resolve`;`uncertain` 绝不自动重发、也不自动退库存。

## 红线

- 买家已收货/已评价的订单**不补发**。
- 售后/退款中的订单不发货(执行器本身也会拒绝 `cancelled`/`refunded`)。
- 同一订单绝不发两次卡:以 `delivery job` 的唯一键与库存预留为准,不要凭聊天记录判断。
- 结果不明(`uncertain`)时禁止直接重发:卡密可能已经到买家手里,重发等于送两份。
- 真实发送需要显式 `--send --yes`;默认 `XIANYU_AUTOMATION_MODE=observe` 下不会执行任何外发。
