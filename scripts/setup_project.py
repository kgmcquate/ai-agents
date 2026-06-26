#!/usr/bin/env python3
"""
One-time setup: creates (or verifies) the GitHub Projects v2 board for the ticket manager.
Run via the setup-project workflow (workflow_dispatch), or locally with the right env vars.
Prints the project_number to plug into your ticket-manager-caller.yml when done.
"""

import os
import sys
import argparse
import requests

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPO_OWNER   = os.environ["REPO_OWNER"]
REPO_NAME    = os.environ["REPO_NAME"]

REST_BASE = "https://api.github.com"
GQL_URL   = "https://api.github.com/graphql"

_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

# Ordered list of Status column names and their UI colors.
REQUIRED_COLUMNS = [
    ("New",              "GRAY"),
    ("Ready for Agent",  "BLUE"),
    ("Needs Info",       "YELLOW"),
    ("Blocked",          "RED"),
    ("In Progress",      "ORANGE"),
    ("Ready for Review", "PURPLE"),
    ("Complete",         "GREEN"),
]


def rest(method: str, path: str, **kwargs):
    r = requests.request(method, f"{REST_BASE}{path}", headers=_HEADERS, **kwargs)
    r.raise_for_status()
    return r.json() if r.text else {}


def graphql(query: str, variables: dict = None) -> dict:
    r = requests.post(
        GQL_URL,
        headers={**_HEADERS, "Content-Type": "application/json"},
        json={"query": query, "variables": variables or {}},
    )
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL error: {data['errors']}")
    return data["data"]


# ── Project helpers ───────────────────────────────────────────────────────────

def get_owner_id() -> str:
    data = graphql(
        "query($login: String!) { repositoryOwner(login: $login) { id } }",
        {"login": REPO_OWNER},
    )
    return data["repositoryOwner"]["id"]


def get_repo_id() -> str:
    data = graphql(
        "query($owner: String!, $repo: String!) { repository(owner: $owner, name: $repo) { id } }",
        {"owner": REPO_OWNER, "repo": REPO_NAME},
    )
    return data["repository"]["id"]


def find_project(title: str) -> dict | None:
    data = graphql(
        """
        query($login: String!) {
          repositoryOwner(login: $login) {
            ... on User         { projectsV2(first: 50) { nodes { id number title } } }
            ... on Organization { projectsV2(first: 50) { nodes { id number title } } }
          }
        }
        """,
        {"login": REPO_OWNER},
    )
    nodes = data["repositoryOwner"]["projectsV2"]["nodes"]
    return next((p for p in nodes if p["title"] == title), None)


def create_project(owner_id: str, title: str) -> dict:
    data = graphql(
        """
        mutation($ownerId: ID!, $title: String!) {
          createProjectV2(input: { ownerId: $ownerId, title: $title }) {
            projectV2 { id number title }
          }
        }
        """,
        {"ownerId": owner_id, "title": title},
    )
    return data["createProjectV2"]["projectV2"]


def get_status_field(project_id: str) -> dict | None:
    data = graphql(
        """
        query($projectId: ID!) {
          node(id: $projectId) {
            ... on ProjectV2 {
              fields(first: 20) {
                nodes {
                  ... on ProjectV2SingleSelectField { id name options { id name } }
                }
              }
            }
          }
        }
        """,
        {"projectId": project_id},
    )
    fields = data["node"]["fields"]["nodes"]
    return next((f for f in fields if isinstance(f, dict) and f.get("name") == "Status"), None)


def configure_status_columns(project_id: str, field_id: str):
    options = [{"name": name, "color": color, "description": ""} for name, color in REQUIRED_COLUMNS]
    graphql(
        """
        mutation($projectId: ID!, $fieldId: ID!, $options: [ProjectV2SingleSelectFieldOptionInput!]!) {
          updateProjectV2Field(input: {
            projectId: $projectId
            fieldId: $fieldId
            name: "Status"
            singleSelectOptions: $options
          }) {
            projectV2Field {
              ... on ProjectV2SingleSelectField { options { name } }
            }
          }
        }
        """,
        {"projectId": project_id, "fieldId": field_id, "options": options},
    )


def link_repo(project_id: str, repo_id: str):
    try:
        graphql(
            """
            mutation($projectId: ID!, $repositoryId: ID!) {
              linkProjectV2ToRepository(input: { projectId: $projectId, repositoryId: $repositoryId }) {
                repository { name }
              }
            }
            """,
            {"projectId": project_id, "repositoryId": repo_id},
        )
    except RuntimeError as e:
        print(f"  Warning: could not link repo automatically: {e}")
        print("  Link it manually: project Settings → Linked repositories.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", required=True, help="Project board title")
    args = parser.parse_args()

    print(f"Setting up '{args.title}' for {REPO_OWNER}/{REPO_NAME}...\n")

    # Find or create
    project = find_project(args.title)
    if project:
        print(f"  Found existing project #{project['number']}: {project['title']}")
    else:
        print("  Project not found — creating...")
        owner_id = get_owner_id()
        project  = create_project(owner_id, args.title)
        print(f"  Created project #{project['number']}")

    project_id     = project["id"]
    project_number = project["number"]

    # Configure Status columns
    status_field = get_status_field(project_id)
    if not status_field:
        print("\nERROR: No 'Status' single-select field found on this project.")
        print("New projects should have one by default. Check the board and re-run.")
        sys.exit(1)

    current = {opt["name"] for opt in status_field["options"]}
    required = {name for name, _ in REQUIRED_COLUMNS}

    if current == required:
        print("  Status columns already match — no changes needed.")
    else:
        missing  = required - current
        extra    = current - required
        if missing:
            print(f"  Adding columns:  {sorted(missing)}")
        if extra:
            print(f"  Removing columns: {sorted(extra)}")
        configure_status_columns(project_id, status_field["id"])
        print("  Status columns configured.")

    # Link repo
    print(f"  Linking {REPO_NAME} to project...")
    repo_id = get_repo_id()
    link_repo(project_id, repo_id)

    print(f"""
Setup complete.

1. Add this to your ticket-manager-caller.yml:

       with:
         project_number: {project_number}

2. In the project Settings → Workflows, enable:
     ✓ Item added to project  →  set Status to: New
   This ensures new issues land in the New column automatically.
""")


if __name__ == "__main__":
    main()
