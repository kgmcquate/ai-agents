#!/usr/bin/env python3
"""Shared GitHub REST + GraphQL + Projects v2 helpers."""

import os
import requests

GITHUB_TOKEN = os.environ["GH_PROJECTS_TOKEN"]
REPO_OWNER   = os.environ["REPO_OWNER"]
REPO_NAME    = os.environ["REPO_NAME"]

REST_BASE = "https://api.github.com"
GQL_URL   = "https://api.github.com/graphql"

_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


# ── REST / GraphQL ────────────────────────────────────────────────────────────

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


# ── Labels ────────────────────────────────────────────────────────────────────

_LABEL_COLORS = {
    "type:":       "0075ca",
    "risk:low":    "0e8a16",
    "risk:medium": "fbca04",
    "risk:high":   "d73a4a",
    "size:":       "cfd3d7",
    "area:":       "7057ff",
    "blocked:":    "b60205",
    "agent:":      "e4e669",
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
    _existing_labels = set()
    page = 1
    while True:
        batch = rest("GET", f"/repos/{REPO_OWNER}/{REPO_NAME}/labels",
                     params={"per_page": 100, "page": page})
        _existing_labels.update(l["name"] for l in batch)
        if len(batch) < 100:
            break
        page += 1


def ensure_labels(names: list[str]):
    _load_existing_labels()
    for name in names:
        if name not in _existing_labels:
            try:
                rest("POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/labels",
                     json={"name": name, "color": _color_for(name)})
            except requests.HTTPError as e:
                if e.response.status_code != 422:
                    raise
                # 422 = label already exists (created by a concurrent run or previous job)
            _existing_labels.add(name)


def set_issue_labels(issue_number: int, labels: list[str]):
    rest("PUT", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{issue_number}/labels",
         json={"labels": labels})


def add_issue_label(issue_number: int, label: str):
    """Add a single label without replacing existing ones."""
    rest("POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{issue_number}/labels",
         json={"labels": [label]})


def remove_label(issue_number: int, label: str):
    try:
        rest("DELETE", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{issue_number}/labels/{label}")
    except requests.HTTPError:
        pass


# ── Comments ──────────────────────────────────────────────────────────────────

def add_comment(issue_number: int, body: str):
    rest("POST", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{issue_number}/comments",
         json={"body": body})


def get_comments(issue_number: int) -> list[dict]:
    return rest("GET", f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{issue_number}/comments",
                params={"per_page": 100})


# ── GitHub Projects v2 ────────────────────────────────────────────────────────

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
        raise ValueError(f"Column '{column_name}' not found in project. Check your workflow config.")

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
