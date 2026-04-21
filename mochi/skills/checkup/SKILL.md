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
用户问系统状态、记忆/数据库情况时调用。如："你内存还够吗"、"系统状态怎样"。

无需参数。

## Usage Rules
- 用户问"体检""健康检查""系统状态""checkup"时调用
- 只读操作，不会修改任何数据
- 无需参数，直接调用即可
