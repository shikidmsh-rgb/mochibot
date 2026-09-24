---
name: meal
description: "饮食记录 — 记录饮食、查询历史、删除记录"
type: tool
multi_turn: true
diary_status_order: 40
---

# Meal Skill

Tool-only mode: `log_meal` (record meals with nutrition estimation) + `query_meals` (query meal history with daily summaries) + `delete_meal` (remove incorrect records).

## Tools

### log_meal (routed)
记录一餐的食物、估算热量和宏量营养素，适用于文字描述或食物照片。总量由代码累加逐项估算，作为后续交流的真实依据。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| meal_type | string (enum: breakfast, lunch, dinner, snack) | yes | 餐次 |
| items | array (items: object {name:string, calories:integer, protein_g:number, carbs_g:number, fat_g:number}) | yes | 已吃食物及逐项营养估算；非空数组，每项五个字段齐全，name 非空，营养数值非负且有限 |
| source | string (enum: text, photo, voice) | | text / photo / voice，默认 text |
| date | string | | YYYY-MM-DD，默认今天 |

### query_meals (routed)
读取近期餐食、记录 ID、热量摄入和营养趋势，可按日期或回看天数汇总。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| days | integer | | 回看天数，正整数，默认 1（今天）。查一周用 7。 |
| date | string | | 指定日期 YYYY-MM-DD，会覆盖 days。 |

### delete_meal (on_demand)
按查询结果中的记录 ID 删除一条明确饮食记录。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| meal_id | integer | yes | query_meals 返回的记录 ID |

## Capability Context

- `log_meal` 的营养数字是估算，不是实测值。
- 早餐、午餐、晚餐按日期和餐次更新该餐记录；零食记录可有多条。更正同一天的同一正餐可重新 log_meal；要移除一条记录可使用 delete_meal。
- `delete_meal` 仅删除当前用户指定 ID 的一条饮食记录，不按日期或餐型批量删除。
