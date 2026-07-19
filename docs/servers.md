# Server catalog

The 15 published `@chkp/*-mcp` MCP servers this project hosts (of the 18 in
the upstream repo), with the **exact pinned versions** (catalog of
2026-07-15). The pins make runs reproducible: every spawn is
`npx -y @chkp/<server>-mcp@<version>` -- package resolution never floats.

Each server runs as a **stdio child process** of the agent (locally, or
inside the hosted-agent container). Its Key Vault secret is decoded to JSON
and injected into the child's environment **at spawn time**, along with
`TELEMETRY_DISABLED=true`. Tools are namespaced
`<server-no-hyphens>___<tool>`, e.g. `quantummanagement___show_hosts`.

## The catalog

| Server | Pinned package | Credential shape (secret env-var keys) | Default 9 | `all` (11) |
|---|---|---|---|---|
| quantum-management | `@chkp/quantum-management-mcp@1.4.7` | management: `MANAGEMENT_HOST`, `MANAGEMENT_PORT` (443), `API_KEY` | ✅ | ✅ |
| management-logs | `@chkp/management-logs-mcp@1.4.6` | management | ✅ | ✅ |
| threat-prevention | `@chkp/threat-prevention-mcp@1.5.4` | management | ✅ | ✅ |
| https-inspection | `@chkp/https-inspection-mcp@1.4.6` | management | ✅ | ✅ |
| policy-insights | `@chkp/policy-insights-mcp@0.3.5` | management | ✅ | ✅ |
| quantum-gw-cli | `@chkp/quantum-gw-cli-mcp@1.4.8` | management -- authenticates to **Management**, NOT Gaia | ✅ | ✅ |
| reputation-service | `@chkp/reputation-service-mcp@1.3.1` | `API_KEY` (ThreatCloud) | ✅ | ✅ |
| threat-emulation | `@chkp/threat-emulation-mcp@1.3.1` | `API_KEY` (ThreatCloud) | ✅ | ✅ |
| documentation | `@chkp/documentation-mcp@1.4.6` | `CLIENT_ID`, `SECRET_KEY` (Infinity Portal) + startup args `--region US` (set automatically) | ✅ | ✅ |
| cloudguard-waf | `@chkp/cloudguard-waf-mcp@0.1.0` | `WAF_CLIENT_ID`, `WAF_ACCESS_KEY`, `WAF_REGION` (eu-west-1) | — | ✅ |
| spark-management | `@chkp/spark-management-mcp@1.4.8` | `CLIENT_ID`, `SECRET_KEY`, `INFINITY_PORTAL_URL` (https://portal.checkpoint.com) | — | ✅ |
| argos-erm | `@chkp/argos-erm-mcp@0.5.4` | `ARGOS_API_KEY`, `ARGOS_CUSTOMER_ID` -- needs real creds to even list tools | — | excluded |
| harmony-sase | `@chkp/harmony-sase-mcp@1.3.1` | `API_KEY`, `MANAGEMENT_HOST`, `ORIGIN` | — | excluded |
| workforce-ai | `@chkp/workforce-ai-mcp@1.1.0` | `CP_CI_CLIENT_ID`, `CP_CI_ACCESS_KEY`, `CP_CI_GATEWAY` (https://cloudinfra-gw-us.portal.checkpoint.com) | — | excluded |
| quantum-gaia | `@chkp/quantum-gaia-mcp@1.3.5` | none in-process; **agent-side** shape gaia: `GAIA_GATEWAY_IP`, `GAIA_PORT` (443), `GAIA_USER`, `GAIA_PASSWORD` | — | excluded |

Selection rules (`parse_servers`): no `--servers` → the default 9; `all` →
the catalog minus the `exclude_from_all` entries (11: it omits `argos-erm`,
`harmony-sase`, `workforce-ai`, `quantum-gaia`); any server remains
deployable **explicitly by name**. Unknown names exit 2 listing the valid
catalog.

## The secret model

**Every credentialed server gets its own Key Vault secret** named
`<prefix>-<server>` (default prefix `chkpmcp`, e.g.
`chkpmcp-quantum-management`). The body is a JSON object of that shape's
env-var keys. Nothing is shared -- `quantum-management` and `management-logs`
can point at *different* management servers if you want. The
management-shaped servers reuse the same field *names*
(`MANAGEMENT_HOST`/`API_KEY`), each in its own secret.

`deploy` writes placeholders only (`PLACEHOLDER_NOT_A_REAL_KEY`); real values
enter via `creds apply` (or `deploy --creds`). An existing secret whose body
is non-placeholder is never overwritten by a re-deploy. See
[credentials and go-live](scenarios/creds-and-golive.md).

Smart-1 Cloud note: the upstream management-shaped packages also accept
Smart-1 Cloud-style values (`S1C_URL` + `API_KEY`). The credentials file is
free-form `KEY=VALUE` per section, so you can use whichever field names your
package version expects -- the exact input names are controlled by the
upstream `@chkp` package, not by this repo.

## quantum-gaia and elicitation

`quantum-gaia` authenticates **interactively**: mid-call, the server issues an
MCP `elicitation/create` request asking for gateway ip/port/user/password.

- On AWS, the AgentCore Gateway does **not** relay elicitation, so Gaia calls
  hang through the gateway and the server is effectively local-only there.
- Here, the servers are **stdio child processes -- and stdio DOES relay
  elicitation.** The agent-side answerer (`chkpmcpaz.gaia`) satisfies the
  prompt automatically from the `<prefix>-quantum-gaia` Key Vault secret, or
  from `GAIA_GATEWAY_IP`/`GAIA_PORT`/`GAIA_USER`/`GAIA_PASSWORD` env vars
  (checked first). Requested field names are matched case-insensitively via
  aliases (`gateway_ip`/`ip`/`address`/`host`, `port`, `user`/`username`,
  `password`).
- When required fields cannot be satisfied (placeholder secret, missing env),
  the answerer **declines instead of hanging** -- the tool call fails cleanly
  and the agent reports it could not retrieve the data.

`quantum-gaia` is still excluded from the default set and from `all` to
mirror the AWS catalog; deploy it explicitly:

```
python3 -m chkpmcpaz deploy --servers "quantum-management quantum-gaia"
```

## Startup notes

- `documentation` needs a `--region` startup flag; the catalog sets
  `--region US` automatically via the server spec's args.
- `argos-erm` cannot enumerate its tools without real Argos credentials --
  with placeholders the child starts and fails cleanly; the pool logs
  `✗ argos-erm failed to start: <reason>` and continues with the rest.
- A single child failing to start never kills the pool: the remaining
  servers' tools are still merged and served to the model.
