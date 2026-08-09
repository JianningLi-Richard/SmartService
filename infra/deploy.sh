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

echo "Deploying infra/main.bicep..."
az deployment group create \
  --resource-group "$RESOURCE_GROUP" \
  --template-file "$SCRIPT_DIR/main.bicep" \
  --parameters "$SCRIPT_DIR/main.parameters.json" \
  --parameters location="$LOCATION"

echo "Done. Outputs:"
az deployment group show \
  --resource-group "$RESOURCE_GROUP" \
  --name main \
  --query properties.outputs
