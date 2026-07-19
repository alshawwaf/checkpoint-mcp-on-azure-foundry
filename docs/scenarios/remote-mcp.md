# Scenario: Remote MCP tier — share the `@chkp` tools with a second consumer

By default this project runs the `@chkp` MCP servers as **stdio children** of the
agent (see [architecture](../aws-vs-azure.md)). That is the fastest, cheapest
topology — but only *this* agent can use the tools. When you need a **second
consumer** — a Foundry **portal-built agent**, Copilot Studio, Claude Desktop, an
`n8n`/LangGraph node, or any other MCP client — deploy the opt-in **remote MCP
tier**: each `@chkp` server *also* runs as its own **Azure Container App** over
streamable-HTTP, behind **Entra Easy Auth**.

This is the Azure analogue of the AWS **AgentCore Gateway**: one shared,
authenticated MCP endpoint per server, consumable by anything that can present
an Entra token — not just the agent in this repo.

> The stdio path is unchanged and stays the default. `--remote-mcp` adds the
> remote tier **alongside** it; nothing about the local/hosted agent changes.

## When to use it

| You want to… | Use |
|---|---|
| Run the built-in agent (`chat`, hosted, bridge) | the default stdio path — do nothing |
| Let a **Foundry portal agent** or **another MCP client** call the `@chkp` tools | `deploy --remote-mcp` |
| Show the "one shared, governed MCP endpoint" story (AgentCore-Gateway parity) | `deploy --remote-mcp` |

## Prerequisites

- The base stack is deployed (`python3 -m chkpmcpaz deploy` — Foundry account,
  ACR, Key Vault, monitoring). The remote tier reuses the **same agent image**
  and the **same per-server Key Vault secrets**.
- `az` logged in to the subscription that owns the stack.

## Deploy the tier

```bash
# Alongside a normal deploy:
python3 -m chkpmcpaz deploy --remote-mcp

# Or add it to an existing stack (rebuilds the image, then stands up the tier):
python3 -m chkpmcpaz deploy --remote-mcp --servers "quantum-management threat-prevention documentation"
```

What it provisions (idempotent; names come from `chkpmcpaz/config.py`):

1. An **Entra app registration** (`<prefix>-mcp-gateway`) — the Easy Auth
   *audience* every endpoint requires a token for (the analogue of the AWS
   gateway's Cognito resource server). v2 access tokens, identifier-uri
   `api://<clientId>`.
2. A shared **user-assigned managed identity** (`id-<prefix>-mcp`) granted
   **AcrPull** (pull the agent image) and **Key Vault Secrets User** (read each
   server's credential secret) — no secrets ever live in the containers.
3. A **Container Apps environment** (`cae-<prefix>`) wired to the stack's Log
   Analytics workspace.
4. **One Container App per selected server** (`<prefix>-mcp-<server>`), running
   `python -m chkpmcpaz.remote_server` (fetch the Key Vault secret via the
   managed identity, then exec `npx -y @chkp/<server>-mcp@<pin> --transport
   http`). Ingress `:8000`, **Entra Easy Auth** (`Return401` for anything
   without a valid token), and **scale-to-zero** (`minReplicas: 0`) so idle cost
   is ~0.

The deploy summary prints the endpoint catalog:

```
  Remote MCP: 3 @chkp endpoint(s) behind Entra Easy Auth (audience api://<clientId>)
                quantum-management: https://chkpmcp-mcp-quantum-management.<region>.azurecontainerapps.io/mcp
                threat-prevention:  https://chkpmcp-mcp-threat-prevention.<region>.azurecontainerapps.io/mcp
                documentation:      https://chkpmcp-mcp-documentation.<region>.azurecontainerapps.io/mcp
```

`python3 -m chkpmcpaz status` shows the tier too (`◈ Remote MCP — N endpoint(s)…`).

## Consume it

**From this repo's agent** (proves the same tools over the remote transport):

```bash
CHKP_MCP_TRANSPORT=remote python3 -m chkpmcpaz chat "how many hosts are configured?"
```

The CLI reads the persisted endpoint catalog, acquires an Entra token for the
audience via `DefaultAzureCredential`, and drives the exact same agent loop —
now over streamable-HTTP instead of stdio children.

**From any other MCP client** — point it at an endpoint URL and present an Entra
bearer token for the audience:

```bash
# A token for the gateway audience (any principal in the tenant):
TOKEN=$(az account get-access-token --resource api://<clientId> --query accessToken -o tsv)

# Then connect your MCP client to the streamable-HTTP endpoint with:
#   Authorization: Bearer $TOKEN
#   URL: https://<prefix>-mcp-<server>.<region>.azurecontainerapps.io/mcp
```

**From a Foundry portal agent** — add the endpoints to the project **Toolbox**
(portal → project → Tools/Connections → add MCP endpoint) so portal-built agents
share the same governed tools. The deploy summary lists the URLs to paste.

## Best practices this demonstrates

- **Every endpoint authenticated** (org policy): Entra Easy Auth in front of RBAC;
  no anonymous path to the tools.
- **No secrets in containers**: credentials come from Key Vault through a managed
  identity at container start.
- **Least privilege**: the shared identity holds exactly AcrPull + Key Vault
  Secrets User, scoped to this stack's registry and vault.
- **Cost discipline**: scale-to-zero — the tier bills only while a consumer is
  actively calling tools.
- **Observability**: Container App logs flow to the stack's Log Analytics.

## Teardown

`destroy` removes the tier with the rest of the stack: the Container Apps,
environment, and identity go with the resource group (`azd down --force
--purge`), and the **Entra app registration** (a tenant object, not in the RG) is
deleted explicitly.

```bash
python3 -m chkpmcpaz destroy
```

To keep the base stack but drop just the remote tier, re-run `deploy` without
`--remote-mcp` after deleting the Container Apps in the portal, or run a full
`destroy` and redeploy without the flag.
