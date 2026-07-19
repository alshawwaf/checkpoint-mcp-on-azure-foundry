# Security

This repo is a demo/reference tool, but it follows Check Point org policy as if
it were production. The load-bearing facts:

## Secrets live in Azure Key Vault -- and nowhere else

- Check Point credentials (Management API keys, ThreatCloud keys, Infinity
  Portal keys, Gaia logins) are stored **only** as Key Vault secrets, one per
  server (`<prefix>-<server>`, e.g. `chkpmcp-quantum-management`).
- `deploy` writes **placeholder** bodies (`PLACEHOLDER_NOT_A_REAL_KEY`); real
  values enter via `creds apply` from a local, gitignored file and go straight
  to Key Vault. A re-deploy never overwrites a secret that already holds real
  values.
- Secret **values** are never printed, logged, or embedded in exceptions --
  only secret names and env-var key names appear in output and logs.
- Nothing secret is ever committed: `.gitignore` blocks `.env`, `*.pem`,
  `*.key`, `chkp-credentials*`, and `.azure/`. There are no API keys, tokens,
  or credentials hardcoded anywhere in this repository.

## Entra ID authenticates everything

- No API keys and no custom auth tier. Locally, `DefaultAzureCredential`
  resolves to your `az login` identity; inside the hosted container it
  resolves to the agent's per-agent Entra identity.
- The Claude `/anthropic` route, Content Safety Prompt Shields, and Key Vault
  are all called with Entra bearer tokens (Claude scope
  `https://ai.azure.com/.default`).
- The hosted agent's Responses endpoint is authenticated by Foundry itself --
  there is no anonymous surface. Access is least-privilege RBAC: the agent
  identity gets only `Cognitive Services User` (account scope) and
  `Key Vault Secrets User` (vault scope).

## Transport and input hygiene

- TLS verification is **never** disabled -- no `verify=False` exists anywhere
  under `chkpmcpaz/`, and a unit test enforces it.
- All user input is validated before use: `--prefix` against
  `^[a-z][a-z0-9-]{0,11}$`, server names against the catalog, session/actor
  ids sanitized to `[a-zA-Z0-9_-]`. No `shell=True`, no dynamic code
  execution.
- The guardrail is optional and the customer's choice of engine. Azure AI
  Content Safety Prompt Shields (the Azure-native engine) is the default;
  Check Point's own AI Guardrail (Lakera Guard) is a drop-in opt-in provider
  (`--guardrail-provider lakera` / `CHKP_GUARDRAIL_PROVIDER=lakera`) that is
  identical on AWS and Azure, giving one guardrail story across both clouds.
  Either engine screens input for prompt-injection and jailbreak attacks
  before any model call. On Azure, Lakera screens inline in local chat and,
  when baked at deploy time, inside the hosted container.

## Reporting

If you find a security issue in this repository, report it privately to the
repository owner or through Check Point's internal security channels -- do not
open a public issue containing exploit details, credentials, hostnames, or
customer data.
