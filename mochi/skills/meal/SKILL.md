---
name: meal
description: "饮食记录 — 记录饮食、查询历史、删除记录"
type: tool
expose_as_tool: true
tier: lite
multi_turn: true
diary_status_order: 40
writes:
  diary: [diary]
  db: [health_log]
---

# Meal Skill

Tool-only mode: `log_meal` (record meals with nutrition estimation) + `query_meals` (query meal history with daily summaries) + `delete_meal` (remove incorrect records).

## Tools

### log_meal (L1)
用户提到吃了什么或发食物照片时调用，由你估算热量和宏量营养素。如："午饭吃了麻辣烫"、附食物图片。

**记录后**：务必告知用户营养明细——总热量 + 每个食物的热量、蛋白质、碳水、脂肪。这是这个功能的核心价值。示例回复："记下了～早餐284kcal，蛋白2个(34kcal, 蛋白质7g) + 豆豆1个(250kcal, 碳水24g 脂肪16g)"

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| meal_type | string | yes | `breakfast` / `lunch` / `dinner` / `snack` |
| items | string | yes | JSON 食物数组：`[{"name":"麻婆豆腐","calories":250,"protein_g":15,"carbs_g":8,"fat_g":18}]` |
| total_calories | integer | yes | 本餐估算总热量 |
| total_protein_g | number | | 总蛋白质克数 |
| total_carbs_g | number | | 总碳水克数 |
| total_fat_g | number | | 总脂肪克数 |
| source | string | | `text` / `photo` / `voice`，默认 `text` |
| date | string | | YYYY-MM-DD，默认今天 |

### query_meals (L0)
用户询问最近吃了什么、热量摄入、营养趋势时调用。如："今天吃了多少卡"、"这周吃得健康吗"。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| days | integer | | 回看天数，默认 1（今天）。查一周用 7。 |
| date | string | | 指定日期 YYYY-MM-DD，会覆盖 days。 |

### delete_meal (L1, extended)
按日期和餐型删除饮食记录。用于用户说记错了或想删掉的情况。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| meal_type | string | yes | `breakfast` / `lunch` / `dinner` / `snack` |
| date | string | | YYYY-MM-DD，默认今天 |

## Usage Rules

**修改已记录的餐食**：没有 edit_meal 工具。先 `delete_meal` 删除旧记录，再 `log_meal` 记录正确版本。
