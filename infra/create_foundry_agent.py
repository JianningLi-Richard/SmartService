"""One-time script: registers the Routing Agent on Azure AI Foundry.

Run after `infra/deploy.sh` has created the AI Foundry project and model
deployment. Reuses SYSTEM_INSTRUCTIONS and LOOKUP_TOOL_DEF from
backend/shared/agent.py directly, so the agent's brief never drifts from
the code that talks to it.

Pins azure-ai-projects==1.0.0 / azure-ai-agents==1.1.0 deliberately: this is
the version with the threads/messages/runs (Assistants-style) Agents API
that backend/shared/agent.py's _call_foundry sketch is written against.
Newer major versions (2.x) replaced that API with an unrelated
versions/sessions/code model -- installing latest here would create an
agent shape backend's code can't call.

Usage:
    python3 -m venv .venv && .venv/bin/pip install azure-ai-projects==1.0.0 azure-ai-agents==1.1.0 azure-identity
    .venv/bin/python infra/create_foundry_agent.py <ai-foundry-endpoint> <model-deployment-name>

Prints the created agent ID -- set it as AI_FOUNDRY_AGENT_ID on the Function App:
    az functionapp config appsettings set --name <func> -g <rg> --settings AI_FOUNDRY_AGENT_ID=<id>
    az functionapp restart --name <func> -g <rg>

Requires being logged in (`az login`) with a role that can manage agents on
the project (Azure AI Developer or higher) -- the same role the Function
App's managed identity is granted in main.bicep.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from azure.ai.agents.models import FunctionDefinition, FunctionToolDefinition
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

from shared.agent import SYSTEM_INSTRUCTIONS, LOOKUP_TOOL_DEF


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <ai-foundry-endpoint> <model-deployment-name>")
        sys.exit(1)
    endpoint, model = sys.argv[1], sys.argv[2]

    lookup_tool = FunctionToolDefinition(function=FunctionDefinition(**LOOKUP_TOOL_DEF["function"]))

    client = AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())
    agent = client.agents.create_agent(
        model=model,
        name="smartservice-routing-agent",
        instructions=SYSTEM_INSTRUCTIONS,
        tools=[lookup_tool],
    )
    print(f"Created agent: {agent.id}")


if __name__ == "__main__":
    main()
