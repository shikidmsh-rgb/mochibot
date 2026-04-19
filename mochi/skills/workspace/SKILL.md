---
name: workspace
description: "日记读写、markdown 文件编辑 — 写日记、查日记、编辑 data 目录下的 md 文件"
type: tool
tier: lite
expose_as_tool: true
always_on: false
core: true
writes:
  diary: [journal]
---

# Workspace Skill

日记读写 + data 目录 markdown 文件编辑。

## Tools

### write_diary (L1)
往今日日記追加一条记录。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| entry | string | yes | 日记内容 |

### read_diary (L0)
读取今天的日记，或按日期查历史归档。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| date | string | no | YYYY-MM-DD 格式。不填 = 今天 |

### edit_file (L1)
读写 data/ 目录下的 markdown 文件。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: read, write) | yes | read = 读取内容, write = 覆盖写入 |
| path | string | yes | 相对于 data/ 的文件路径（如 notes.md） |
| content | string | no | write 时的新内容。action=write 时必填 |

## Usage Rules

- 用户聊天中提到值得记录的事（心情、经历、事件），用 write_diary 写进日记
- 不要把 habit checkin、todo、meal 等已有专用 skill 的内容写进日记 — 那些 skill 会自动更新状態面板
- read_diary 不填日期 = 今天；填 YYYY-MM-DD = 查归档
- edit_file 只能操作 data/ 目录下的 .md 文件，用于编辑 soul 等自定义文件
