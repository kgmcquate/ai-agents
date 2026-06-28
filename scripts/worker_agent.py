#!/usr/bin/env python3
"""Worker Agent — claims tickets from 'Ready for Agent' and implements them with Claude Code."""

import os
import re
import subprocess
import time
import random

from anthropic import Anthropic, APIStatusError
from github_client import (
    REPO_OWNER, REPO_NAME,
    ensure_labels, add_issue_label, remove_label,
    add_comment, get_comments,
    get_project_meta, get_items_in_column, move_item_to_column,
)

WORKER_ID       = os.environ.get("WORKER_ID", "1")
TARGET_REPO_DIR = os.environ.get("TARGET_REPO_DIR", ".")
ACTIONS_RUN_URL = os.environ.get("ACTIONS_RUN_URL", "")
TEST_COMMAND    = os.environ.get("TEST_COMMAND", "")

_AGENT_MARKER = "<!-- worker-agent -->"

# ── Size → agent budget mapping ───────────────────────────────────────────────

_SIZE_CONFIG = {
    "size:small":  {"max_turns": 25, "loc_budget": 200},
    "size:medium": {"max_turns": 40, "loc_budget": 400},
    "size:large":  {"max_turns": 60, "loc_budget": 800},
}
_DEFAULT_SIZE_CONFIG = {"max_turns": 30, "loc_budget": 300}


def _size_config(labels: list[str]) -> dict:
    for label in labels:
        if label in _SIZE_CONFIG:
            return _SIZE_CONFIG[label]
    return _DEFAULT_SIZE_CONFIG


# ── Git helpers ───────────────────────────────────────────────────────────────

def _git(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        cwd=TARGET_REPO_DIR,
        check=check,
        text=True,
        capture_output=True,
    )


def _create_branch(ticket: dict) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", ticket["title"].lower())[:40].strip("-")
    branch = f"agent/issue-{ticket['issue_number']}-{slug}"
    _git(["checkout", "-b", branch])
    _git(["push", "-u", "origin", branch])
    return branch


def _loc_changed() -> int:
    """Return total lines added + removed since branching from the default branch."""
    result = _git(["diff", "origin/HEAD", "--shortstat"], check=False)
    if not result.stdout:
        return 0
    # "3 files changed, 42 insertions(+), 7 deletions(-)"
    numbers = re.findall(r"(\d+) insertion|(\d+) deletion", result.stdout)
    return sum(int(a or b) for a, b in numbers)


def _has_new_commits() -> bool:
    result = _git(["log", "origin/HEAD..HEAD", "--oneline"], check=False)
    return bool(result.stdout.strip())


# ── Ticket claiming ───────────────────────────────────────────────────────────

def _claim(item: dict, meta: dict, col: dict) -> bool:
    """
    Move the ticket to In Progress and label it. Returns False if the move fails,
    which is the signal that another worker already claimed it.
    """
    if "agent:working" in item["labels"]:
        return False
    try:
        move_item_to_column(item["item_id"], col["in_progress"], meta)
    except Exception as e:
        print(f"  Could not claim #{item['issue_number']}: {e}")
        return False
    ensure_labels(["agent:working"])
    add_issue_label(item["issue_number"], "agent:working")
    return True


# ── gh CLI helpers ────────────────────────────────────────────────────────────

def _gh(args: list[str], check: bool = False) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["gh"] + args,
        cwd=TARGET_REPO_DIR,
        text=True,
        capture_output=True,
        env={**os.environ, "GH_TOKEN": os.environ["GH_PROJECTS_TOKEN"]},
    )
    if result.returncode != 0:
        print(f"  gh {args[0]} {args[1]} failed (exit {result.returncode}):", flush=True)
        print(f"  stderr: {result.stderr.strip()}", flush=True)
        if check:
            raise RuntimeError(f"gh command failed: {result.stderr.strip()}")
    return result


def _find_pr_url(branch: str) -> str:
    """Return the URL of the PR the agent opened for this branch, or '' if none exists."""
    result = _gh([
        "pr", "list",
        "--repo", f"{REPO_OWNER}/{REPO_NAME}",
        "--head", branch,
        "--json", "url",
        "--jq", ".[0].url",
    ])
    return result.stdout.strip()


def _mark_pr_ready(pr_url: str):
    """Promote a draft PR to ready-for-review."""
    _gh(["pr", "ready", "--repo", f"{REPO_OWNER}/{REPO_NAME}", pr_url])


# ── Anthropic API pre-flight ──────────────────────────────────────────────────

def _check_api_credits() -> bool:
    """Make a minimal API call to verify credits are available before running the agent."""
    try:
        Anthropic().messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1,
            messages=[{"role": "user", "content": "hi"}],
        )
        return True
    except APIStatusError as e:
        print(f"  Anthropic API error ({e.status_code}): {e.message}", flush=True)
        return False
    except Exception as e:
        print(f"  Anthropic API unreachable: {e}", flush=True)
        return False


# ── Agent prompt ──────────────────────────────────────────────────────────────

def _build_prompt(ticket: dict, branch: str, cfg: dict) -> str:
    labels_str = ", ".join(ticket["labels"]) or "none"
    return f"""You are a software engineering agent implementing a GitHub issue for the repository \
{REPO_OWNER}/{REPO_NAME}.

## Issue

Number: #{ticket['issue_number']}
Title:  {ticket['title']}
Labels: {labels_str}

Description:
{ticket['body'] or '(no description provided)'}

## Workflow — follow these steps in order

1. **Read CLAUDE.md** (if present) for this repo's coding standards, test patterns, and conventions.
   If it is absent, infer conventions from the existing code.

2. **Understand the issue** before writing any code. Read relevant files.

3. **Write tests first** (TDD). The tests should fail at this point — no implementation yet.

4. **Commit and push the tests**:
   ```
   git add -A && git commit -m "test: #{ticket['issue_number']} add failing tests"
   git push origin {branch}
   ```

5. **Open a draft PR** so progress is visible as you work:
   ```
   gh pr create --draft \
     --repo {REPO_OWNER}/{REPO_NAME} \
     --title "#{ticket['issue_number']}: {ticket['title'][:60]}" \
     --body "Closes #{ticket['issue_number']}\n\nImplemented by Worker Agent {WORKER_ID}.\n\n[View run logs]({ACTIONS_RUN_URL})"
   ```

6. **Post a comment** summarising your test plan:
   ```
   gh issue comment {ticket['issue_number']} --repo {REPO_OWNER}/{REPO_NAME} \
--body "{_AGENT_MARKER}
**Tests written** — proceeding with implementation.

<brief summary of what the tests cover>"
   ```

7. **Check your LOC budget** — run `git diff origin/{branch} --shortstat`.
   If lines changed already exceed **{cfg['loc_budget']}**, stop and call for help (see below).

8. **Implement** the code until all tests pass. Run the test suite frequently.

9. **Update documentation** for any public functions, classes, or API endpoints you add or modify.

10. **Run the full test suite** one final time and confirm it is green.

11. **Commit and push everything**:
    ```
    git add -A && git commit -m "feat: #{ticket['issue_number']} {ticket['title'][:60]}"
    git push origin {branch}
    ```

## Calling for help

If at any point you are stuck, uncertain about the approach, or your changes are growing
unexpectedly large, post a help comment and then stop:

```
gh issue comment {ticket['issue_number']} --repo {REPO_OWNER}/{REPO_NAME} \
--body "{_AGENT_MARKER}
**Needs human help** — <explain what you are stuck on and what you have tried so far>"
```

Situations that require calling for help:
- Lines changed would exceed **{cfg['loc_budget']}** (your budget for this ticket size)
- Tests still failing after several fix attempts
- Scope is much larger than the ticket description suggested
- You are unsure which approach is correct and both are non-trivial

## Constraints

- You are on branch `{branch}`. Push after every commit so progress is visible in the draft PR.
- Do NOT open or close a PR — the workflow handles it.
- Do NOT move the ticket on the project board — the workflow handles it.
- Keep commits clean: one commit for tests, one for implementation (plus any fixups).
- Security: do not introduce hardcoded secrets, SQL injection, XSS, or SSRF vulnerabilities.
  If you spot an existing vulnerability in code you are touching, fix it.
"""


# ── Run Claude Code ───────────────────────────────────────────────────────────

def _run_claude(prompt: str, max_turns: int) -> bool:
    """Invoke Claude Code non-interactively. Returns True on exit code 0."""
    result = subprocess.run(
        [
            "claude",
            "-p",
            "--dangerously-skip-permissions",
            "--max-turns", str(max_turns),
        ],
        input=prompt,
        cwd=TARGET_REPO_DIR,
        text=True,
        env={**os.environ},
    )
    return result.returncode == 0


# ── Post-run verification ─────────────────────────────────────────────────────

def _agent_asked_for_help(issue_number: int) -> bool:
    comments = get_comments(issue_number)
    return any(
        _AGENT_MARKER in c.get("body", "") and "Needs human help" in c.get("body", "")
        for c in comments
    )


def _run_tests() -> tuple[bool, str]:
    """Run the repo's test suite if TEST_COMMAND is set. Returns (passed, output)."""
    if not TEST_COMMAND:
        return True, ""
    result = subprocess.run(
        TEST_COMMAND,
        shell=True,
        cwd=TARGET_REPO_DIR,
        text=True,
        capture_output=True,
    )
    return result.returncode == 0, (result.stdout + result.stderr)[-3000:]


# ── Outcome handlers ──────────────────────────────────────────────────────────

def _handle_success(ticket: dict, branch: str, pr_url: str, meta: dict, col: dict):
    # Safety-net push in case the agent forgot to push its final commit.
    _git(["push", "origin", branch])
    _mark_pr_ready(pr_url)

    move_item_to_column(ticket["item_id"], col["ready_for_review"], meta)
    remove_label(ticket["issue_number"], "agent:working")
    ensure_labels(["agent:done"])
    add_issue_label(ticket["issue_number"], "agent:done")

    add_comment(ticket["issue_number"], (
        f"{_AGENT_MARKER}\n"
        f"**Implementation complete.** PR is ready for review: {pr_url}\n\n"
        f"Moved to **Ready for Review**."
    ))
    print(f"  Worker {WORKER_ID}: Done — {pr_url}", flush=True)


def _handle_failure(ticket: dict, pr_url: str, meta: dict, col: dict, reason: str):
    # Push whatever partial commits exist so the draft PR shows the work done so far.
    _git(["push", "origin", "--force-with-lease"], check=False)

    move_item_to_column(ticket["item_id"], col["blocked"], meta)
    remove_label(ticket["issue_number"], "agent:working")
    ensure_labels(["blocked:agent-stuck"])
    add_issue_label(ticket["issue_number"], "blocked:agent-stuck")

    pr_line = f"Draft PR with partial work: {pr_url}\n" if pr_url else ""
    add_comment(ticket["issue_number"], (
        f"{_AGENT_MARKER}\n"
        f"**Worker agent stopped** — {reason}\n\n"
        f"{pr_line}"
        f"Moved to **Blocked**. Human review needed.\n"
        f"[View run logs]({ACTIONS_RUN_URL})"
    ))
    print(f"  Worker {WORKER_ID}: Failed — {reason}", flush=True)


# ── Entry point ───────────────────────────────────────────────────────────────

def load_config() -> dict:
    return {
        "project_number": int(os.environ["PROJECT_NUMBER"]),
        "columns": {
            "ready_for_agent":  "Ready for Agent",
            "in_progress":      "In Progress",
            "blocked":          "Blocked",
            "ready_for_review": "Ready for Review",
        },
    }


def main():
    # Stagger workers slightly so they don't race to claim the same ticket.
    time.sleep(random.uniform(0, int(WORKER_ID) * 3))

    config = load_config()
    meta   = get_project_meta(config["project_number"])
    col    = config["columns"]

    ready = get_items_in_column(col["ready_for_agent"], meta)
    if not ready:
        print(f"Worker {WORKER_ID}: No tickets ready.", flush=True)
        return

    # Try to claim the first unclaimed ticket.
    ticket = None
    for item in ready:
        if _claim(item, meta, col):
            ticket = item
            break

    if not ticket:
        print(f"Worker {WORKER_ID}: Could not claim any ticket (all taken).", flush=True)
        return

    n = ticket["issue_number"]
    print(f"Worker {WORKER_ID}: Claimed #{n}: {ticket['title']}", flush=True)

    # Create the branch and push it so the agent can commit to it immediately.
    branch = _create_branch(ticket)

    add_comment(n, (
        f"{_AGENT_MARKER}\n"
        f"Picked up by **Worker Agent {WORKER_ID}**.\n\n"
        f"- Branch: `{branch}`\n"
        f"- [View run logs]({ACTIONS_RUN_URL})\n\n"
        f"Working on: tests → draft PR → implementation → PR ready for review."
    ))

    # Determine budget from ticket size label.
    cfg = _size_config(ticket["labels"])
    print(f"  Budget: {cfg['loc_budget']} LOC, {cfg['max_turns']} turns", flush=True)

    # Verify Anthropic API is accessible before spending a turn on the agent.
    print(f"  Checking Anthropic API...", flush=True)
    if not _check_api_credits():
        _handle_failure(ticket, pr_url, meta, col,
                        "Anthropic API is not accessible — check API key and credit balance at console.anthropic.com.")
        return

    # Run the Claude Code agent.
    print(f"  Running Claude Code (max_turns={cfg['max_turns']})...", flush=True)
    agent_ok = _run_claude(_build_prompt(ticket, branch, cfg), cfg["max_turns"])

    # Retrieve the draft PR URL the agent opened (may be empty if it stopped before that step).
    pr_url = _find_pr_url(branch)
    print(f"  PR: {pr_url or '(none)'}", flush=True)

    # Evaluate outcome.
    if _agent_asked_for_help(n):
        _handle_failure(ticket, pr_url, meta, col, "Agent requested human help (see comments above).")
        return

    if not agent_ok:
        _handle_failure(ticket, pr_url, meta, col, "Claude Code exited with a non-zero status.")
        return

    if not _has_new_commits():
        _handle_failure(ticket, pr_url, meta, col, "Agent ran but produced no commits.")
        return

    loc = _loc_changed()
    if loc > cfg["loc_budget"] * 1.5:
        _handle_failure(
            ticket, pr_url, meta, col,
            f"Change size ({loc} lines) is well above the budget ({cfg['loc_budget']} lines). "
            "Please review before merging.",
        )
        return

    # Run the test suite as a final safety check.
    tests_ok, test_output = _run_tests()
    if not tests_ok:
        add_comment(n, (
            f"{_AGENT_MARKER}\n"
            f"**Test suite failed** after implementation. Output (last 3 000 chars):\n\n"
            f"```\n{test_output}\n```"
        ))
        _handle_failure(ticket, pr_url, meta, col, "Test suite failed after implementation.")
        return

    _handle_success(ticket, branch, pr_url, meta, col)


if __name__ == "__main__":
    main()
