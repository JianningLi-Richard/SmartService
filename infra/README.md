# Infra — deployment lead

Provisions everything in [docs/api-contract.md](../docs/api-contract.md) section 3,
including the AI Foundry project and Routing Agent.

## What gets created

| Resource | Purpose |
|---|---|
| Storage account | `requests` / `sessions` tables + Functions runtime storage |
| Function App (Linux, Python 3.12, Consumption) | the backend |
| Key Vault (RBAC) | `storage-connection-string`, `speech-key`, `ai-foundry-key` (unused placeholder — see below), `device-keys` (placeholder) — Function App reads them via Key Vault references, no redeploy needed when a value changes |
| Log Analytics + Application Insights | telemetry, plus an availability test on `GET /api/health` |
| Azure AI Speech (F0, free tier) | TTS |
| Azure AI Foundry (`AIServices` account + project, eastus2) | Routing Agent — model deployment `gpt-5-mini`; Function App calls it via managed identity, no key |
| Static Web App (Free tier) | dashboard |

## One-time setup

```bash
az login
infra/deploy.sh                                    # creates rg-smartservice-demo, deploys main.bicep
infra/setup-federated-identity.sh <org>/<repo>      # lets GitHub Actions deploy without stored secrets
```

`setup-federated-identity.sh` prints three values — add them as repo secrets
(Settings → Secrets and variables → Actions):

```
AZURE_CLIENT_ID
AZURE_TENANT_ID
AZURE_SUBSCRIPTION_ID
```

Then set two more things once `infra/deploy.sh` has run:

- Repo **variable** `AZURE_FUNCTIONAPP_NAME` = the `functionAppName` output (backend deploy workflow needs it).
- Secret `AZURE_STATIC_WEB_APPS_API_TOKEN` = `az staticwebapp secrets list --name <staticWebAppName> --query properties.apiKey -o tsv` (dashboard deploy workflow needs it).

After that, `.github/workflows/deploy-{infra,backend,dashboard}.yml` deploy on every push to
`main` that touches their respective folder, or via manual `workflow_dispatch`.

## Filling in secrets after the fact

```bash
az keyvault secret set --vault-name <keyVaultName> --name device-keys --value "pi-3f-01:<key>,pi-1f-02:<key>"
```

(`ai-foundry-key` stays an unused empty placeholder — see below, the agent uses the
Function App's managed identity instead of a key.)

## AI Foundry agent

`infra/deploy.sh` provisions the AI Foundry project and a `gpt-5-mini` model deployment
and points `AI_FOUNDRY_ENDPOINT` at it, but creating the actual **agent** (an object on
the Foundry service with its own ID) isn't a Bicep resource — it's a one-time SDK call.
Run it once after `deploy.sh`:

```bash
python3 -m venv .venv && .venv/bin/pip install azure-ai-projects==1.0.0 azure-ai-agents==1.1.0 azure-identity
.venv/bin/python infra/create_foundry_agent.py <aiFoundryEndpoint output> gpt-5-mini
```

It reads `SYSTEM_INSTRUCTIONS` and `LOOKUP_TOOL_DEF` straight from
`backend/shared/agent.py` so the registered agent never drifts from what the code expects,
and prints an agent ID (`asst_...`). Set it and restart:

```bash
az functionapp config appsettings set --name <functionAppName> -g rg-smartservice-demo --settings AI_FOUNDRY_AGENT_ID=<id>
az functionapp restart --name <functionAppName> -g rg-smartservice-demo
```

**Pin those exact SDK versions.** `azure-ai-projects` 2.x replaced the threads/messages/runs
(Assistants-style) Agents API with an unrelated versions/sessions/code model — installing
latest gives you an agent shape `backend/shared/agent.py::_call_foundry` cannot call.
`backend/requirements.txt` includes the same pins used by the Function App.

Re-run `create_foundry_agent.py` any time `SYSTEM_INSTRUCTIONS` or `LOOKUP_TOOL_DEF` change in
code — it creates a new agent (not an update), so update `AI_FOUNDRY_AGENT_ID` again after.

## Known gap: dashboard sign-in

README's deployment checklist calls for "authenticated sign-in on the dashboard" so the
API can read `x-ms-client-principal-name` on acknowledge/complete. That header comes from
Azure App Service/Functions **Easy Auth**, which needs an AAD app registration (interactive,
not scripted here) — and `function_app.py` currently sends
`Access-Control-Allow-Origin: *`, which blocks the browser from sending auth cookies
cross-origin. Wiring auth end-to-end means: enable Easy Auth (AAD) on the Function App with
`unauthenticatedClientAction: AllowAnonymous` (device calls must keep working), and change
the CORS origin from `*` to the Static Web App's hostname with credentials allowed — that
second part is a `function_app.py` change, owned by backend. Flag it in team chat before
touching it.

## Cost

Consumption Function App, Storage LRS, Speech F0 (free), Static Web App Free tier, App
Insights pay-as-you-go — all within Azure for Students credit for a project this size.
