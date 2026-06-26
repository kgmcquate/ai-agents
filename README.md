# ai-agents

A collection of reusable GitHub Actions that use Claude to automate software project management.

## Ticket Manager Agent

Runs on a schedule and manages your GitHub Issues through a Projects v2 kanban board. It classifies incoming tickets, asks clarifying questions, detects conflicts, decomposes large tasks, and keeps the board moving without human triage overhead.

### What it does

Every 15 minutes (configurable), the agent:

1. **Classifies new tickets** — assigns `type:`, `risk:`, `size:`, and `area:` labels using Claude
2. **Routes tickets** based on classification:
   - `risk:high` → Blocked (human decides whether to approve or implement themselves)
   - Missing information → Needs Info (posts a question as a comment)
   - `size:large` → decomposes into sub-tickets, parks parent in Blocked
   - Everything else → Ready for Agent
3. **Decomposes large tickets** — creates 2–6 sub-issues, each with enough context to implement independently, and adds a task list to the parent
4. **Unblocks conflict tickets** — when an in-progress ticket completes, re-checks any Blocked tickets with overlapping `area:` tags
5. **Monitors Needs Info tickets** — when a human replies, uses Claude to assess whether the answer is sufficient and moves the ticket back to Ready for Agent

### Board layout

| Column | Description |
|--------|-------------|
| New | Newly opened issues, not yet classified |
| Ready for Agent | Classified and ready for a worker agent to pick up |
| Needs Info | Agent has posted a question; waiting on a human reply |
| Blocked | Conflict with in-progress work, high risk, or pending decomposition |
| In Progress | A worker agent is actively implementing this |
| Ready for Review | PR opened; awaiting human code review |
| Complete | Merged and done |

### Tag taxonomy

| Prefix | Values | Purpose |
|--------|--------|---------|
| `type:` | `bug` `feature` `chore` `docs` `unknown` | Kind of work |
| `risk:` | `low` `medium` `high` | Blast radius if it goes wrong |
| `size:` | `small` `medium` `large` | Effort estimate |
| `area:` | project-specific (see below) | Codebase region — used for conflict detection |
| `blocked:` | `conflict` `risk` `decomposed` `dependency` | Reason a ticket is Blocked |

**Conflict detection:** two tickets conflict if they share any `area:` tag while one is In Progress. The `area:` values are defined per-repo via the `areas` workflow input.

---

## Onboarding a repo

### Prerequisites

- A GitHub PAT with `repo` and `project` scopes, stored as `GH_PAT` in the target repo's secrets
- An `ANTHROPIC_API_KEY` secret in the target repo

### Step 1 — Set up the project board

Copy [`examples/setup-project-caller.yml`](examples/setup-project-caller.yml) to `.github/workflows/setup-project.yml` in your repo, then run it:

```
GitHub → Actions → Setup Ticket Manager Project → Run workflow
```

Enter a title for the board (e.g. `"My Repo Tasks"`). The workflow will:
- Create the Projects v2 board if it doesn't exist
- Configure the Status columns with the correct names and colors
- Link the repo to the project
- Print the `project_number` to use in the next step

**One manual step:** in the project's **Settings → Workflows**, enable **"Item added to project"** and set the Status to `New`. This ensures newly opened issues appear in the New column automatically. This setting isn't exposed via the API.

### Step 2 — Enable the ticket manager

Copy [`examples/ticket-manager-caller.yml`](examples/ticket-manager-caller.yml) to `.github/workflows/ticket-manager.yml` in your repo and set `project_number`:

```yaml
jobs:
  run:
    uses: kevinmcquate/ai-agents/.github/workflows/ticket-manager.yml@main
    with:
      project_number: 3          # from step 1 output
      areas: "api,auth,frontend" # optional — customize for your codebase
    secrets:
      GH_PAT: ${{ secrets.GH_PAT }}
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

The `areas` input defaults to `api,auth,frontend,backend,database,infra,config`. Trim it to the areas that make sense for your project — the classifier will only assign tags from this list.

### Step 3 — Adjust the schedule (optional)

The caller workflow controls the polling frequency. The default is every 15 minutes:

```yaml
on:
  schedule:
    - cron: '*/15 * * * *'
```

Note: GitHub may delay scheduled runs by up to 15 minutes during high load.

---

## Repository structure

```
scripts/
  ticket_manager.py   # Core agent logic
  setup_project.py    # One-time board setup

.github/workflows/
  ticket-manager.yml  # Reusable workflow (workflow_call)
  setup-project.yml   # Reusable setup workflow (workflow_call + workflow_dispatch)

examples/
  ticket-manager-caller.yml   # Template to copy into target repos
  setup-project-caller.yml    # Template for one-time board setup
```
