---
name: local_native
mod_api: 1
description: Persist a configurable counter through the public Mod API.
type: tool
config:
  LABEL:
    type: str
    default: "native"
  STEP:
    type: int
    default: 2
  ACCESS_TOKEN:
    type: str
    default: ""
    secret: true
---

## Tools

### local_native_record (on_demand)
Record text and increment a private counter, or return a known failure.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| text | string | yes | Text to record. |
| mode | string (enum: record, reject, crash) | no | Exercise result behavior. |

## Capability Context

Stores records in this tool's private data directory; does not call a service.
