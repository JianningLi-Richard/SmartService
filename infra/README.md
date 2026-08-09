# Infra — deployment lead

Provisions everything in [docs/api-contract.md](../docs/api-contract.md) section 3 except
the AI Foundry agent (backend is wiring that directly this week).

## What gets created

| Resource | Purpose |
|---|---|
| Storage account | `requests` / `sessions` tables + Functions runtime storage |
| Function App (Linux, Python 3.12, Consumption) | the backend |
| Key Vault (RBAC) | `storage-connection-string`, `speech-key`, `ai-foundry-key` (placeholder), `device-keys` (placeholder) — Function App reads them via Key Vault references, no redeploy needed when a value changes |
| Log Analytics + Application Insights | telemetry, plus an availability test on `GET /api/health` |
| Azure AI Speech (F0, free tier) | TTS |
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
az keyvault secret set --vault-name <keyVaultName> --name ai-foundry-key --value <key>
az keyvault secret set --vault-name <keyVaultName> --name device-keys --value "pi-3f-01:<key>,pi-1f-02:<key>"
```

Also set `AI_FOUNDRY_ENDPOINT` / `AI_FOUNDRY_AGENT_ID` app settings directly (they're not
secrets): `az functionapp config appsettings set --name <functionAppName> -g rg-smartservice-demo --settings AI_FOUNDRY_ENDPOINT=... AI_FOUNDRY_AGENT_ID=...`

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
