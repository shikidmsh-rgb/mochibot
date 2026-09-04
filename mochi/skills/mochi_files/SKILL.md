---
name: mochi_files
description: "你有一片持久的私人 Markdown 空间，可以按自己的方式保存并重新打开完整作品；具体操作可从工具箱发现。"
type: tool
locked: true
triggers: [tool_call]
---

## Tools

### browse_mochi_files (on_demand)
浏览 Main 自己的私人 Markdown 作品。list 按路径排序列出文件；search 进行区分大小写的字面搜索；read 按字符偏移分页读取。内容是 Agent 自己写下的作品，不是外部事实来源。不会自动注入、整理、召回或写入任何文件。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: list, search, read) | yes | list、search 或 read |
| path | string | no | list 可指定相对目录；search 可指定相对目录或 `.md` 文件；read 必须指定相对 POSIX `.md` 路径 |
| query | string | no | search 必填的区分大小写字面文本，不是正则表达式 |
| offset | integer | no | list/search 的结果偏移，或 read 的字符偏移；默认 0 |
| limit | integer | no | 本页条目、匹配或字符数量；执行层会应用固定上限 |

### save_mochi_file (on_demand)
保存 Main 自己决定创作的私人 Markdown 作品。create 只新建且绝不覆盖；append 原样追加，不自动补换行；edit 仅在 old_text 恰好出现一次时做精确替换，并在 append/edit 前保留一份隐藏的上一版本。没有删除、重命名、归档、恢复或盲目覆盖。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: create, append, edit) | yes | create、append 或 edit |
| path | string | yes | 非空相对 POSIX `.md` 路径；不能包含隐藏、父级、盘符、反斜杠或平台无效组件 |
| content | string | no | create/append 必填；完全按提供文本保存 |
| old_text | string | no | edit 必填；必须非空且在当前文件中恰好出现一次 |
| new_text | string | no | edit 必填；替换文本可为空 |
