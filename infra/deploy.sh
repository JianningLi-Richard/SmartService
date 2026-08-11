#!/usr/bin/env bash
# Provisions the resource group (if missing) and deploys infra/main.bicep into it.
#
# Usage:
#   infra/deploy.sh [resource-group] [location]
#
# Defaults match infra/main.parameters.json (project "smartservice", env "demo").

set -euo pipefail

RESOURCE_GROUP="${1:-rg-smartservice-demo}"
LOCATION="${2:-canadacentral}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Resource group: $RESOURCE_GROUP ($LOCATION)"

az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output none

# main.bicep's siteConfig.appSettings and the device-keys secret are full
# overwrites on every deploy -- read back whatever is live today so a redeploy
# doesn't wipe out AI_FOUNDRY_AGENT_ID (set by create_foundry_agent.py) or
# device-keys (set by hand). Empty/missing on a first deploy is fine.
FUNC_APP_NAME="$(az functionapp list -g "$RESOURCE_GROUP" --query "[0].name" -o tsv 2>/dev/null || true)"
AI_FOUNDRY_AGENT_ID_CURRENT=""
if [ -n "$FUNC_APP_NAME" ]; then
  AI_FOUNDRY_AGENT_ID_CURRENT="$(az functionapp config appsettings list --name "$FUNC_APP_NAME" -g "$RESOURCE_GROUP" \
    --query "[?name=='AI_FOUNDRY_AGENT_ID'].value | [0]" -o tsv 2>/dev/null || true)"
fi

KEY_VAULT_NAME="$(az keyvault list -g "$RESOURCE_GROUP" --query "[0].name" -o tsv 2>/dev/null || true)"
DEVICE_KEYS_CURRENT=" "
if [ -n "$KEY_VAULT_NAME" ]; then
  DEVICE_KEYS_CURRENT="$(az keyvault secret show --vault-name "$KEY_VAULT_NAME" --name device-keys \
    --query value -o tsv 2>/dev/null || echo " ")"
fi

echo "Deploying infra/main.bicep..."
az deployment group create \
  --resource-group "$RESOURCE_GROUP" \
  --template-file "$SCRIPT_DIR/main.bicep" \
  --parameters "$SCRIPT_DIR/main.parameters.json" \
  --parameters location="$LOCATION" \
  --parameters aiFoundryAgentId="$AI_FOUNDRY_AGENT_ID_CURRENT" \
  --parameters deviceKeysValue="$DEVICE_KEYS_CURRENT"

echo "Done. Outputs:"
az deployment group show \
  --resource-group "$RESOURCE_GROUP" \
  --name main \
  --query properties.outputs
