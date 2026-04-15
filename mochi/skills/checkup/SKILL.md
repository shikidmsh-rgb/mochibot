---
name: checkup
description: "系统体检 — 一键查看 prompt 体积、数据库状态、记忆系统、运行状态"
type: tool
expose_as_tool: true
tier: lite
core: false
always_on: false
---

## Tools

### run_checkup (L0)
运行系统体检，返回 prompt 体积、数据库状态、记忆系统、运行状态的结构化报告。

无需参数。

## Usage Rules
- 用户问"体检""健康检查""系统状态""checkup"时调用
- 只读操作，不会修改任何数据
- 无需参数，直接调用即可
