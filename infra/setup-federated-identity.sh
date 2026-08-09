#!/usr/bin/env bash
# One-time setup: creates a user-assigned managed identity that GitHub Actions can use
# to deploy without a stored secret (OIDC federated credentials), scoped to Contributor
# on the target resource group.
#
# Uses a managed identity rather than an Azure AD app registration/service principal —
# managed identities are ARM resources, not Microsoft Graph objects, so creating one only
# needs Contributor on the resource group. Use this on tenants (e.g. school/edu) where
# `az ad app create` fails with "Insufficient privileges to complete the operation."
#
# Usage:
#   infra/setup-federated-identity.sh <github-org>/<github-repo> [resource-group]
#
# After it runs, add the three printed values as GitHub repo secrets:
#   AZURE_CLIENT_ID, AZURE_TENANT_ID, AZURE_SUBSCRIPTION_ID
# (no client secret is needed or created).

set -euo pipefail

REPO="${1:?Usage: infra/setup-federated-identity.sh <org>/<repo> [resource-group]}"
RESOURCE_GROUP="${2:-rg-smartservice-demo}"
IDENTITY_NAME="smartservice-github-actions"
LOCATION="canadacentral"

SUBSCRIPTION_ID="$(az account show --query id -o tsv)"
TENANT_ID="$(az account show --query tenantId -o tsv)"

echo "Ensuring resource group $RESOURCE_GROUP exists..."
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output none

echo "Creating user-assigned managed identity $IDENTITY_NAME..."
az identity create \
  --name "$IDENTITY_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --output none

CLIENT_ID="$(az identity show --name "$IDENTITY_NAME" --resource-group "$RESOURCE_GROUP" --query clientId -o tsv)"
PRINCIPAL_ID="$(az identity show --name "$IDENTITY_NAME" --resource-group "$RESOURCE_GROUP" --query principalId -o tsv)"

echo "Granting Contributor on $RESOURCE_GROUP..."
az role assignment create \
  --assignee-object-id "$PRINCIPAL_ID" \
  --assignee-principal-type ServicePrincipal \
  --role Contributor \
  --scope "/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RESOURCE_GROUP" \
  --output none

# Plain arrays instead of associative arrays — macOS ships bash 3.2, no declare -A.
CRED_NAMES=(github-main github-pull-request)
CRED_SUBJECTS=("repo:${REPO}:ref:refs/heads/main" "repo:${REPO}:pull_request")
for i in "${!CRED_NAMES[@]}"; do
  NAME="${CRED_NAMES[$i]}"
  SUBJECT="${CRED_SUBJECTS[$i]}"
  echo "Adding federated credential ($NAME)..."
  az identity federated-credential create \
    --name "$NAME" \
    --identity-name "$IDENTITY_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --issuer "https://token.actions.githubusercontent.com" \
    --subject "$SUBJECT" \
    --audiences "api://AzureADTokenExchange" \
    --output none 2>/dev/null || echo "  (already exists, skipping)"
done

cat <<EOF

Done. Add these as GitHub repo secrets (Settings > Secrets and variables > Actions):

  AZURE_CLIENT_ID       $CLIENT_ID
  AZURE_TENANT_ID       $TENANT_ID
  AZURE_SUBSCRIPTION_ID $SUBSCRIPTION_ID

  gh secret set AZURE_CLIENT_ID -b"$CLIENT_ID"
  gh secret set AZURE_TENANT_ID -b"$TENANT_ID"
  gh secret set AZURE_SUBSCRIPTION_ID -b"$SUBSCRIPTION_ID"
EOF
