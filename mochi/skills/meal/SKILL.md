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
Record a meal with LLM-estimated nutrition breakdown. Estimate calories and macros from the user's description or food photo, then call this tool. Don't ask for confirmation.

**After logging**: Always tell the user the breakdown — total calories AND per-item details (calories, protein, carbs, fat). This is the main value of the feature. Example reply: "记下了～早餐284kcal，蛋白2个(34kcal, 蛋白质7g) + 蛋挞1个(250kcal, 碳水24g 脂肪16g)"

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| meal_type | string | yes | `breakfast` / `lunch` / `dinner` / `snack` |
| items | string | yes | JSON array of food items: `[{"name":"麻婆豆腐","calories":250,"protein_g":15,"carbs_g":8,"fat_g":18}]` |
| total_calories | integer | yes | Estimated total calories for this meal |
| total_protein_g | number | | Total protein grams |
| total_carbs_g | number | | Total carbs grams |
| total_fat_g | number | | Total fat grams |
| source | string | | `text` / `photo` / `voice`. Default: `text` |
| date | string | | YYYY-MM-DD. Default: today |

### query_meals (L0)
Query meal/nutrition history with daily macro summaries.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| days | integer | | Look-back days. Default 1 (today). Use 7 for weekly. |
| date | string | | Specific date YYYY-MM-DD. Overrides days. |

### delete_meal (L1, extended)
Delete a meal record by date and meal type. Use when the user says a meal was logged wrong or wants to remove it.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| meal_type | string | yes | `breakfast` / `lunch` / `dinner` / `snack` |
| date | string | | YYYY-MM-DD. Default: today |

## Usage Rules

**Editing a meal**: There is no edit_meal tool. To modify a logged meal, `delete_meal` the old record then `log_meal` the corrected version.

## Meal Estimation Guidelines

Portion baselines (standard single serving):
- Rice: ~150kcal/bowl, ~80kcal for a small portion
- Stir-fried dishes: 150-300kcal (include cooking oil +30-80kcal)
- Hotpot solo: 400-650kcal for the entire meal (already includes staple — do NOT add rice on top)
- BBQ solo: 400-600kcal total
- Shared dishes (large plate / pizza): estimate the user's share based on party size
- Takeout: +10-20% vs home cooking
- When unsure about portion size, ask the user rather than guessing
