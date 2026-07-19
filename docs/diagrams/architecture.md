# Architecture

Check Point MCP servers as stdio children of a Claude-on-Foundry
security-ops agent -- runnable locally or as a Foundry Hosted Agent. Azure
edition of the AWS repo's `architecture.svg`; the biggest structural delta is
the missing middle tier: there is **no gateway** -- the `@chkp` servers are
child processes of the agent itself, and Entra ID replaces the whole
Cognito/JWT/SigV4 chain.

```mermaid
flowchart LR
  YOU["You / SOC analyst<br/>plain-English question"]
  APP["Your app / SDK<br/>OpenAI Responses client · Entra ID"]

  subgraph LOCAL["Local runtime -- chkpmcpaz chat (default)"]
    LOOP_L["Agent loop -- chkpmcpaz.agent<br/>streaming · prompt caching · telemetry"]
    CHILD_L["@chkp MCP servers<br/>stdio npx children · pinned versions"]
    LOOP_L ---|"tools/list · tools/call"| CHILD_L
  end

  subgraph AZ["Azure -- rg-chkpmcp (eastus2 or swedencentral)"]
    subgraph FOUNDRY["Foundry account chkpmcp-foundry-{token}"]
      CLAUDE["Claude deployments<br/>claude-sonnet-4-6 (primary)<br/>claude-haiku-4-5 (fallback)<br/>/anthropic route"]
      CS["Guardrail screen (optional)<br/>default: Content Safety Prompt Shields<br/>opt-in: Check Point AI Guardrail / Lakera<br/>--guardrail-provider lakera"]
      subgraph PROJ["Project chkpmcp-project"]
        HA["Hosted agent chkpmcp-agent<br/>Responses 2.0.0 · port 8088<br/>cpu 1 · 2Gi · linux/amd64"]
        CHILD_H["@chkp MCP servers<br/>stdio npx children"]
        HA ---|"same loop"| CHILD_H
      end
    end
    KV["Key Vault kv-chkpmcp-{token}<br/>one secret per server<br/>chkpmcp-{server}"]
    ACR["ACR acrchkpmcp{token}<br/>chkp-agent:v1<br/>(az acr build, remote)"]
    MON["Log Analytics log-chkpmcp-{token}<br/>App Insights appi-chkpmcp-{token}"]
  end

  EST["Check Point estate<br/>Smart-1 Cloud · Management server · gateways<br/>your real security data"]

  YOU -->|"python3 -m chkpmcpaz chat"| LOOP_L
  YOU -->|"chat --runtime hosted"| HA
  APP -->|"Responses endpoint · Entra bearer"| HA
  LOOP_L -->|"AnthropicFoundry<br/>scope ai.azure.com"| CLAUDE
  HA -->|"AnthropicFoundry<br/>agent identity"| CLAUDE
  LOOP_L -.->|"screen_prompt (--guardrail)"| CS
  HA -.->|"screen_prompt (CHKP_GUARDRAIL=1)"| CS
  KV -->|"secret JSON -> child env at spawn"| CHILD_L
  KV -->|"Key Vault Secrets User"| CHILD_H
  CHILD_L -->|"Mgmt API / HTTPS"| EST
  CHILD_H -->|"Mgmt API / HTTPS"| EST
  ACR -.->|"image pull -- project identity, AcrPull"| HA
  HA -.->|"traces + logs"| MON

  classDef cp fill:#fdf4fa,stroke:#ee0c5d,stroke-width:2px,color:#41273c;
  classDef node fill:#ffffff,stroke:#d9ddeb,color:#1c2036;
  classDef zone fill:#f7f8fd,stroke:#e2e5f1,color:#565d78;
  class LOOP_L,HA cp;
  class YOU,APP,CHILD_L,CHILD_H,CLAUDE,CS,KV,ACR,MON,EST node;
  class AZ,FOUNDRY,PROJ,LOCAL zone;
```

Reading notes:

- **Two runtimes, one loop.** `--runtime local` (default) runs the loop in
  your process; `--runtime hosted` invokes the identical loop packaged in the
  `chkp-agent:v1` container. Both spawn the same pinned `@chkp` children and
  talk to the same model deployments.
- **One loop, two providers.** The diagram shows the Claude production
  topology. A small provider seam (`providers.get_provider(cfg.provider)` →
  `AnthropicProvider` | `AzureOpenAIProvider`) lets the identical loop run on
  first-party **`gpt-5-mini`** instead -- the cheap test model (Azure's
  analog of Amazon Nova). On a `gpt-5-mini` stack the Claude deployments node
  is replaced by a single `gpt-5-mini` deployment reached over the OpenAI
  route, and the agent identity also holds `Cognitive Services OpenAI User`.
  See [Test cheaply without Claude](../../README.md#test-cheaply-without-claude-like-aws-nova).
- **Secrets flow one way.** Key Vault secret JSON is decoded into the child
  process environment at spawn time (plus `TELEMETRY_DISABLED=true`). Values
  never appear in logs, output, or the model context.
- **Auth is Entra ID at every hop.** Your `az login` identity locally; the
  per-agent Entra identity in the container (`Cognitive Services User` on the
  account, `Key Vault Secrets User` on the vault -- granted by the CLI after
  the agent exists).
- **Dashed lines are optional paths**: guardrail screening runs only with
  `--guardrail` / `CHKP_GUARDRAIL=1`; monitoring is always-on but passive.
- **The guardrail is optional, and the engine is your choice.** Two
  interchangeable engines sit behind one `screen_prompt` seam: **Azure AI
  Content Safety Prompt Shields** (the platform **default**,
  `CHKP_GUARDRAIL_PROVIDER=content-safety`) or **Check Point's own AI Guardrail
  (Lakera Guard)** (`--guardrail-provider lakera` /
  `CHKP_GUARDRAIL_PROVIDER=lakera`) -- one inline Guard API call that is
  identical on AWS and Azure, for one guardrail story across both clouds. Note
  that Azure `chat` has no `--guardrail-provider` flag: it reads
  `CHKP_GUARDRAIL_PROVIDER` (from env/`.env`). We do not force Lakera: customers
  already invested in Prompt Shields keep it; customers who want Check Point's
  unified-across-clouds engine opt in. The always-present `@chkp` MCP servers
  are separate -- they are the core; the guardrail is the optional screen in
  front of the model.
