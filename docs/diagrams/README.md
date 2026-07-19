# Diagrams

Mermaid sources -- GitHub renders these inline. They mirror the AWS repo's
`docs/img/*.svg` diagrams, redrawn for the Azure architecture. The README
embeds hand-authored SVGs (from the AWS visual family) in [../img](../img) --
not exports of these Mermaid sources; these `.md` files stay as the browsable,
inline-on-GitHub versions.

| Diagram | Mirrors (AWS repo) | Shows |
|---|---|---|
| [architecture.md](architecture.md) | `architecture.svg` | The whole stack: consumers, Foundry account/project, hosted agent, Key Vault, ACR, monitoring, Check Point estate. |
| [agent-flow.md](agent-flow.md) | `agent-flow.svg` | How one question flows: spawn → discover → reason → call → loop → grounded answer, through the provider seam (Claude or `gpt-5-mini`). |
| [where-checkpoint-fits.md](where-checkpoint-fits.md) | `where-checkpoint-fits.svg` | The Microsoft Foundry building blocks and which ones this integration uses. |
