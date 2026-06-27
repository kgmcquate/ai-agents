#!/usr/bin/env python3
"""Ticket Manager Agent — classifies and routes GitHub issues on a Projects v2 kanban board."""

import os
import requests
from anthropic import Anthropic

# ── Environment ───────────────────────────────────────────────────────────────

GITHUB_TOKEN = os.environ["GH_PROJECTS_TOKEN"]
REPO_OWNER   = os.environ["REPO_OWNER"]
REPO_NAME    = os.environ["REPO_NAME"]

anthropic = Anthropic()

# ── GitHub REST API ───────────────────────────────────────────────────────────

REST_BASE = "https://api.github.com"
GQL_URL   = "https://api.github.com/graphql"

_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


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


# ── Label helpers ─────────────────────────────────────────────────────────────

_LABEL_COLORS = {
    "type:":       "0075ca",  # blue
    "risk:low":    "0e8a16",  # green
    "risk:medium": "fbca04",  # yellow
    "risk:high":   "d73a4a",  # red
    "size:":       "cfd3d7",  # light gray
    "area:":       "7057ff",  # purple
    "blocked:":    "b60205",  # dark red
}

_existing_labels: set[str] | None = None


def _color_for(name: str) -> str:
    for prefix, color in _LABEL_COLORS.items():
        if name.startswith(prefix):
            return color
    return "ededed"


def _load_existing_labels():
    global _existing_labels
    if _existing_labels is not None:
        return
    labels = rest("GET", f"/repos/{REPO_OWNER}/{REPO_NAME}/labels", params={"per_page": 100})
    _existing_labels = {l["name"] for l in labels}


def ensure_labels(names: list[str]):
    _load_existing_labels()
    for name in names:
        if name not in _existing_labels:
            rest("POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/labels",
                 json={"name": name, "color": _color_for(name)})
            _existing_labels.add(name)


def set_issue_labels(issue_number: int, labels: list[str]):
    rest("PUT", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{issue_number}/labels",
         json={"labels": labels})


def remove_label(issue_number: int, label: str):
    try:
        rest("DELETE", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{issue_number}/labels/{label}")
    except requests.HTTPError:
        pass


def add_comment(issue_number: int, body: str):
    rest("POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{issue_number}/comments",
         json={"body": body})


def get_comments(issue_number: int) -> list[dict]:
    return rest("GET", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{issue_number}/comments",
                params={"per_page": 100})


# ── GitHub Projects v2 (GraphQL) ──────────────────────────────────────────────

_PROJECT_FIELDS_FRAGMENT = """
fragment ProjectFields on ProjectV2 {
  id
  fields(first: 20) {
    nodes {
      ... on ProjectV2SingleSelectField {
        id
        name
        options { id name }
      }
    }
  }
}
"""

_project_meta: dict | None = None


def get_project_meta(project_number: int) -> dict:
    global _project_meta
    if _project_meta:
        return _project_meta

    data = graphql(
        _PROJECT_FIELDS_FRAGMENT + """
        query($owner: String!, $number: Int!) {
          repositoryOwner(login: $owner) {
            ... on Organization { projectV2(number: $number) { ...ProjectFields } }
            ... on User         { projectV2(number: $number) { ...ProjectFields } }
          }
        }
        """,
        {"owner": REPO_OWNER, "number": project_number},
    )

    project = data["repositoryOwner"]["projectV2"]
    status_field = next(
        f for f in project["fields"]["nodes"]
        if isinstance(f, dict) and f.get("name") == "Status"
    )
    _project_meta = {
        "project_id": project["id"],
        "field_id":   status_field["id"],
        "options":    {opt["name"]: opt["id"] for opt in status_field["options"]},
    }
    return _project_meta


_ITEMS_QUERY = """
query($project_id: ID!) {
  node(id: $project_id) {
    ... on ProjectV2 {
      items(first: 100) {
        nodes {
          id
          content {
            ... on Issue {
              number
              title
              body
              labels(first: 20) { nodes { name } }
            }
          }
          fieldValues(first: 10) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                optionId
                field { ... on ProjectV2SingleSelectField { name } }
              }
            }
          }
        }
      }
    }
  }
}
"""


def get_items_in_column(column_name: str, meta: dict) -> list[dict]:
    option_id = meta["options"].get(column_name)
    if not option_id:
        raise ValueError(f"Column '{column_name}' not found in project. Check ticket-manager.yml.")

    data = graphql(_ITEMS_QUERY, {"project_id": meta["project_id"]})
    result = []
    for item in data["node"]["items"]["nodes"]:
        if not item.get("content"):
            continue
        for fv in item["fieldValues"]["nodes"]:
            if fv.get("optionId") == option_id:
                result.append({
                    "item_id":      item["id"],
                    "issue_number": item["content"]["number"],
                    "title":        item["content"]["title"],
                    "body":         item["content"].get("body") or "",
                    "labels":       [l["name"] for l in item["content"]["labels"]["nodes"]],
                })
                break
    return result


def move_item_to_column(item_id: str, column_name: str, meta: dict):
    graphql(
        """
        mutation($project_id: ID!, $item_id: ID!, $field_id: ID!, $option_id: String!) {
          updateProjectV2ItemFieldValue(input: {
            projectId: $project_id
            itemId: $item_id
            fieldId: $field_id
            value: { singleSelectOptionId: $option_id }
          }) {
            projectV2Item { id }
          }
        }
        """,
        {
            "project_id": meta["project_id"],
            "item_id":    item_id,
            "field_id":   meta["field_id"],
            "option_id":  meta["options"][column_name],
        },
    )


def create_issue(title: str, body: str) -> dict:
    return rest("POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues",
                json={"title": title, "body": body})


def add_issue_to_project(issue_node_id: str, column_name: str, meta: dict) -> str:
    """Add an issue to the project board and place it in the given column. Returns item_id."""
    result = graphql(
        """
        mutation($project_id: ID!, $content_id: ID!) {
          addProjectV2ItemById(input: { projectId: $project_id, contentId: $content_id }) {
            item { id }
          }
        }
        """,
        {"project_id": meta["project_id"], "content_id": issue_node_id},
    )
    item_id = result["addProjectV2ItemById"]["item"]["id"]
    move_item_to_column(item_id, column_name, meta)
    return item_id


# ── Claude classification ─────────────────────────────────────────────────────

_CLASSIFY_TOOL = {
    "name": "classify_ticket",
    "description": "Classify a GitHub issue for the ticket management system.",
    "input_schema": {
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": ["bug", "feature", "chore", "docs", "unknown"],
            },
            "risk": {
                "type": "string",
                "enum": ["low", "medium", "high"],
                "description": "high = significant blast radius or touches critical/auth systems.",
            },
            "size": {
                "type": "string",
                "enum": ["small", "medium", "large"],
                "description": "small=<1hr, medium=1-4hr, large=4hr+",
            },
            "areas": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 2,
                "description": "Which codebase areas this touches. Max 2. Only use values from the provided list.",
            },
            "needs_info": {
                "type": "boolean",
                "description": "True if the issue lacks enough detail to implement.",
            },
            "question": {
                "type": "string",
                "description": "Specific question to ask if needs_info is true.",
            },
            "classification_comment": {
                "type": "string",
                "description": "2-3 sentence comment summarising what was classified and why, to post on the issue.",
            },
        },
        "required": ["type", "risk", "size", "areas", "needs_info", "classification_comment"],
    },
}


def classify_ticket(title: str, body: str, valid_areas: list[str]) -> dict:
    prompt = f"""You are a ticket manager for a software project. Classify this GitHub issue.

Valid area tags for this project: {', '.join(valid_areas)}

Issue title: {title}

Issue body:
{body or '(no description provided)'}

Use the classify_ticket tool to return your classification."""

    response = anthropic.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        tools=[_CLASSIFY_TOOL],
        tool_choice={"type": "tool", "name": "classify_ticket"},
        messages=[{"role": "user", "content": prompt}],
    )
    tool_use = next(b for b in response.content if b.type == "tool_use")
    return tool_use.input


_DECOMPOSE_TOOL = {
    "name": "decompose_ticket",
    "description": "Break a large ticket into smaller, independently implementable sub-tickets.",
    "input_schema": {
        "type": "object",
        "properties": {
            "sub_tickets": {
                "type": "array",
                "minItems": 2,
                "maxItems": 6,
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "body": {
                            "type": "string",
                            "description": "Full description — enough to implement without reading the parent.",
                        },
                    },
                    "required": ["title", "body"],
                },
            },
            "decomposition_comment": {
                "type": "string",
                "description": "1-2 sentences explaining how the work was split.",
            },
        },
        "required": ["sub_tickets", "decomposition_comment"],
    },
}


def decompose_ticket(title: str, body: str) -> dict:
    prompt = f"""You are a ticket manager. This ticket is too large to implement in one pass.
Break it into 2-6 smaller sub-tickets, each independently implementable.
Include enough detail in each body that an engineer can implement it without reading the parent.

Parent title: {title}

Parent body:
{body or '(no description provided)'}

Use the decompose_ticket tool."""

    response = anthropic.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        tools=[_DECOMPOSE_TOOL],
        tool_choice={"type": "tool", "name": "decompose_ticket"},
        messages=[{"role": "user", "content": prompt}],
    )
    tool_use = next(b for b in response.content if b.type == "tool_use")
    return tool_use.input


def answer_is_sufficient(question: str, replies: list[str]) -> bool:
    """Ask Claude (Haiku — fast/cheap) whether the human's replies answer the question."""
    prompt = f"""A ticket was paused with this question:
"{question}"

Human replies since then:
{chr(10).join(f'- {r}' for r in replies)}

Has the question been answered well enough to proceed with implementation? Reply YES or NO only."""

    response = anthropic.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=10,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip().upper().startswith("YES")


# ── Ticket flows ──────────────────────────────────────────────────────────────

_AGENT_MARKER = "<!-- ticket-manager -->"


def _build_labels(clf: dict) -> list[str]:
    labels = [f"type:{clf['type']}", f"risk:{clf['risk']}", f"size:{clf['size']}"]
    labels += [f"area:{a}" for a in clf.get("areas", [])]
    return labels


def process_new_tickets(config: dict, meta: dict):
    col = config["columns"]
    items = get_items_in_column(col["new"], meta)
    if not items:
        print("  No new tickets.")
        return

    valid_areas = config.get("areas", [])

    for item in items:
        n = item["issue_number"]
        print(f"  Classifying #{n}: {item['title']}")

        clf = classify_ticket(item["title"], item["body"], valid_areas)

        labels = _build_labels(clf)
        if clf["risk"] == "high":
            labels.append("blocked:risk")
        elif clf["size"] == "large" and not clf.get("needs_info"):
            labels.append("blocked:decomposed")

        ensure_labels(labels)
        set_issue_labels(n, labels)

        comment = clf["classification_comment"]
        if clf.get("needs_info") and clf.get("question"):
            comment += f"\n\n**Question:** {clf['question']}"
        add_comment(n, f"{_AGENT_MARKER}\n{comment}")

        if clf["risk"] == "high":
            move_item_to_column(item["item_id"], col["blocked"], meta)
            print(f"    → BLOCKED (risk:high)")
        elif clf.get("needs_info"):
            move_item_to_column(item["item_id"], col["needs_info"], meta)
            print(f"    → NEEDS INFO")
        elif clf["size"] == "large":
            decomp = decompose_ticket(item["title"], item["body"])
            created = []
            for st in decomp["sub_tickets"]:
                sub_issue = create_issue(
                    title=st["title"],
                    body=f"{st['body']}\n\n---\n_Part of #{n}_",
                )
                add_issue_to_project(sub_issue["node_id"], col["new"], meta)
                created.append(sub_issue)

            task_list = "\n".join(f"- [ ] #{i['number']} {i['title']}" for i in created)
            add_comment(n, f"{_AGENT_MARKER}\n{decomp['decomposition_comment']}\n\n{task_list}")
            move_item_to_column(item["item_id"], col["blocked"], meta)
            print(f"    → BLOCKED (decomposed into {len(created)} sub-tickets)")
        else:
            move_item_to_column(item["item_id"], col["ready_for_agent"], meta)
            print(f"    → READY FOR AGENT")


def process_blocked_conflict_tickets(config: dict, meta: dict):
    """Unblock tickets whose area-tag conflict has since resolved."""
    col = config["columns"]
    blocked_items = get_items_in_column(col["blocked"], meta)
    conflict_items = [i for i in blocked_items if "blocked:conflict" in i["labels"]]
    if not conflict_items:
        print("  No conflict-blocked tickets.")
        return

    in_progress = get_items_in_column(col["in_progress"], meta)
    busy_areas = {lbl for ip in in_progress for lbl in ip["labels"] if lbl.startswith("area:")}

    for item in conflict_items:
        item_areas = {l for l in item["labels"] if l.startswith("area:")}
        if not item_areas & busy_areas:
            remove_label(item["issue_number"], "blocked:conflict")
            move_item_to_column(item["item_id"], col["ready_for_agent"], meta)
            add_comment(item["issue_number"],
                        f"{_AGENT_MARKER}\nConflict resolved — moved to **Ready for Agent**.")
            print(f"  #{item['issue_number']} unblocked.")


def process_needs_info_tickets(config: dict, meta: dict):
    """Move tickets to Ready for Agent once the human has answered the agent's question."""
    col = config["columns"]
    items = get_items_in_column(col["needs_info"], meta)
    if not items:
        print("  No needs-info tickets.")
        return

    for item in items:
        n = item["issue_number"]
        comments = get_comments(n)

        # Find the agent's most recent question
        agent_q_comments = [
            c for c in comments
            if _AGENT_MARKER in c.get("body", "") and "**Question:**" in c.get("body", "")
        ]
        if not agent_q_comments:
            continue

        last_q = agent_q_comments[-1]
        question_text = last_q["body"].split("**Question:**")[-1].strip()
        question_time = last_q["created_at"]

        # Collect human replies posted after the question
        human_replies = [
            c["body"] for c in comments
            if c["created_at"] > question_time
            and _AGENT_MARKER not in c.get("body", "")
        ]
        if not human_replies:
            continue

        if answer_is_sufficient(question_text, human_replies):
            move_item_to_column(item["item_id"], col["ready_for_agent"], meta)
            add_comment(n, f"{_AGENT_MARKER}\nThanks for the info — moved to **Ready for Agent**.")
            print(f"  #{n} answered, moved to Ready for Agent.")


# ── Entry point ───────────────────────────────────────────────────────────────

def load_config() -> dict:
    areas_raw = os.environ.get("AREAS", "api,auth,frontend,backend,database,infra,config")
    return {
        "project_number": int(os.environ["PROJECT_NUMBER"]),
        "areas": [a.strip() for a in areas_raw.split(",") if a.strip()],
        "columns": {
            "new":              "New",
            "ready_for_agent":  "Ready for Agent",
            "needs_info":       "Needs Info",
            "blocked":          "Blocked",
            "in_progress":      "In Progress",
            "ready_for_review": "Ready for Review",
            "complete":         "Complete",
        },
    }


def main():
    config = load_config()
    meta   = get_project_meta(config["project_number"])

    print("=== NEW tickets ===")
    process_new_tickets(config, meta)

    print("=== BLOCKED (conflict) tickets ===")
    process_blocked_conflict_tickets(config, meta)

    print("=== NEEDS INFO tickets ===")
    process_needs_info_tickets(config, meta)

    print("Done.")


if __name__ == "__main__":
    main()
