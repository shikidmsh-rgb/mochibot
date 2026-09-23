---
name: checkup
description: "系统体检 — 一键查看 prompt 体积、数据库状态、记忆系统、运行状态"
type: tool
---

## Tools

### run_checkup (on_demand, adaptive)
汇总当前 prompt 体积、数据库、记忆和运行状态，适合了解 MochiBot 自身是否健康。

无需参数。

## Capability Context

- checkup 是无参数的只读快照，不修改数据库、记忆或运行配置。
- 输出反映检查发生时的系统状态，可以作为解释异常或健康情况的事实来源。
