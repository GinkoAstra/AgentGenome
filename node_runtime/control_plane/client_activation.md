---
name: agentgenome
description: Use the connected AgentGenome service to discover, create, publish, run, and observe reusable Graph SOPs from this conversation.
---

Use the existing `agentgenome` MCP connection. Keep the current conversation's model and native file tools. This is a user client, not a Worker or a new Agent loop.

1. Call `get_service_info` and `list_definitions` using the discovered MCP tool schemas. Report the connected service and available Graph definitions. A connection identifier is not necessarily the native conversation identifier.
2. For an existing Graph, read its frozen definition and input contract before creating a run. For a new or edited SOP, read `references/graph-sop.md`, edit local YAML with native file tools, then explicitly validate and publish through MCP.
3. Upload local file contents using the tool's actual input schema. Never assume the remote service can read a local path. Native Pi may expose a local `definition_path` convenience; do not assume other clients do.
4. Create the run through MCP with a caller-owned request ID, then observe state, trace, accepted outputs, and the report through the same service. A Worker response or runtime completion is not independent business acceptance.
5. For an uncertain write result, retain the original request ID and arguments. Inspect existing state before any explicit retry; never silently create a new request to repeat an unknown write.
6. If the service is unavailable, report it and the separate deployment requirement. Do not automatically start a Host, change Worker models, load executable programs from Graph YAML, or open the service database.
7. Closing this conversation must not stop the service or its runs. Only change run intent when the user requests it.

Keep explanations about the user's current stage and the information needed next. Do not implement the SOP's business work in this conversation in place of the registered Graph execution.
