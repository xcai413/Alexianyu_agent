---
name: xianyu-delivery
description: 闲鱼自动发货编排 — 确认订单、查卡密库存、补发、排查发货失败。订单已付款但未发货时使用。
---

# 闲鱼发货编排

## 何时使用

- 订单 `paid` 但迟迟未 `delivered`(自动发货失败或未触发)。
- 需要手动补发、核对库存、排查失败原因。

## 流程

1. **查订单**:`xianyu-agent order list --account <id> --status paid`,再 `xianyu-agent order show <id>`。
2. **看失败原因**:`delivery_fail_reason` 字段:
   - `无可用卡密` → 补货:`xianyu-agent card restock <card_id> --content "..."` 或 `card add` 新卡
   - `发送器返回失败` → 网络/连接问题,稍后重试或人工补发
   - 空 → 订单可能还没被事件触发,检查 Worker 是否在线
3. **检查库存**:`xianyu-agent card list --account <id>`,确认 `remaining > 0`。
4. **补发**:
   - 有库存:手动触发 `xianyu-agent card consume <card_id>`,把取出的卡密发给买家(经聊天)
   - 无库存:先补货再发
5. **记录**:确认发出后,确保订单 `delivery_content` 有值(Phase 4 自动流程已写,人工补发需手动登记)。

## 红线

- 买家已收货/已评价的订单**不补发**。
- 售后/退款中的订单不发货。
- 同一订单绝不发两次卡(查 `card_consumptions` 有无记录)。
