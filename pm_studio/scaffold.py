"""`python -m pm_studio init`: scaffolds a target repo for PM Studio.

Creates pm_studio_local/ (config + local instruction stubs), the seed root documents
(PROJECT_INDEX.md, PROJECT_STATUS.md), and the .gitignore block for runtime state.
Strictly non-destructive: anything that already exists is left untouched, so running
init in an already-configured (or migrated) repo is always safe.
"""

from pathlib import Path

from .config import (
    CONFIG,
    CONFIG_FILE_NAME,
    DEV_INSTRUCTIONS_NAME,
    KNOWLEDGE_DIR_NAME,
    LOCAL_DIR_NAME,
    PM_INSTRUCTIONS_NAME,
)

CONFIG_TEMPLATE = """\
# PM Studio deployment config - project identity for THIS repo.
# Everything here is optional; missing values fall back to package defaults.
# Docs: https://github.com/fromerosk/pm-studio (docs/CONFIGURATION.md)

[project]
name = "{project_name}"
# Name of the default session pinned to the primary checkout on main.
default_session_name = "Main"
# Directory (relative to the repo root) holding workspace/ runtime state.
# Migrated deployments keep their historical "pm_agent" here.
workspace_root = "pm_studio"
# Markdown lines describing where product sources live at the repo root -
# injected verbatim into the PM system prompt so every session starts oriented.
layout = '''
- (describe each product/source directory here, one line each, e.g.:)
- `web/` - the customer-facing web app
- `packages/` - shared libraries
'''

[server]
host = "127.0.0.1"
port = 8000

# The product taxonomy for the roadmap board: id = "Display Label".
# Declaration order is display order. Delete or leave empty for a single-product
# repo with unpinned sessions only.
#
# A product can be a SUB-PRODUCT of another by giving it a table with a `parent`
# instead of a plain label. It is a full product either way - its own board, its
# own sessions, its own id in every URL - and the parent's PM sees its roadmap at
# full detail and can write to it. Nest as deep as your org actually is: a
# sub-product can have sub-products of its own.
[products]
# web = "Web App"
# platform = "Platform"
# auth = {{ label = "Auth & Identity", parent = "web" }}
# billing = {{ label = "Billing", parent = "web" }}
# sso = {{ label = "SSO", parent = "auth" }}

# A product can also carry METADATA - who owns it, who builds it, where it is in
# its life, and a line of description. All optional, all context rather than
# policy: the PM's prompt names them, the board badges a non-ga stage, nothing
# authorizes or bills against them. `stage` is one of: discovery, development,
# ga (the default), sunset - anything else refuses to boot.
# [products.checkout]
# label = "Checkout"
# description = "Guest and member checkout, up to payment capture"
# owner = "jane.doe"
# team = "Payments Engineering"
# stage = "development"

# The SYSTEM taxonomy: the bounded pieces of technology your changes live in - a
# service, an app, a module. A system is not a product. A product is the
# business-facing thing (a line of business, or an umbrella over the technology
# serving one); a system is code. The two are many-to-many: a product touches
# several systems, and a system serves several products.
#
# Declaring this table turns on attribution: every NEW change must name the one
# system it is contained within, which is what makes its blast radius knowable.
# Leave it out entirely and nothing changes - no attribution, no Systems tab.
#
# Systems own no roadmap. Work that belongs to a system rather than a product -
# infra, performance, upgrades - is an initiative (see the portfolio page).
# [systems]
# claims = "Claims Processor"
#
# [systems.rides]
# label = "Rides & Logistics"
# path = "services/rides"              # source folder, repo-root-relative
# repo = "github.com/org/rides"        # its own repo, if it has one
# guidance = "docs/rides/GUIDANCE.md"  # declared now, acted on later
# pipelines = ["rides-ci"]             # declared now, acted on later
#
# Then say which systems each product touches. Every id must be declared above:
# [products.checkout]
# label = "Checkout"
# systems = ["claims", "rides"]

# Optional model allow-list override (id = "Label"); the reserved key `default`
# names the model new sessions start on. Omit the whole table to use package
# defaults (Opus/Sonnet/Haiku).
# [models]
# default = "claude-opus-4-8"
# "claude-opus-4-8" = "Opus"
# sonnet = "Sonnet"

# Operating mode. The default is "personal": one trusted user, no accounts, no
# login - exactly what you get by leaving this commented out. Switch to
# "enterprise" for user accounts, email invites and roles; the first visit then
# walks you through creating the owner account, and no existing data is migrated.
# [enterprise]
# mode = "enterprise"

# Optional outbound mail for enterprise invites. With no [smtp] table, every
# invite instead produces a copyable link - no mail server needed. Prefer
# password_env (the NAME of an env var) so the secret stays out of this file.
# [smtp]
# host = "smtp.example.com"
# port = 587
# from_address = "pm-studio@example.com"
# username = "pm-studio"
# password_env = "PM_STUDIO_SMTP_PASSWORD"
"""

PM_INSTRUCTIONS_TEMPLATE = """\
# Local PM instructions

Everything in this file is appended to the PM agent's system prompt for every
session in this repo. Use it for deployment-specific rules and knowledge that the
shared PM Studio package must not contain: company restrictions, compliance rules,
domain conventions, standing stakeholder decisions.

These instructions ADD to the shared PM behavior; they cannot replace it.

<!-- Replace this comment with your rules, e.g.:
- Never dispatch work that touches `infra/` without an explicit stakeholder go-ahead.
- All customer-facing copy must be reviewed against docs/BRAND_VOICE.md.
-->
"""

DEV_INSTRUCTIONS_TEMPLATE = """\
# Local dev-agent instructions

Everything in this file is appended to every dev-agent dispatch (and merge-conflict
resolution) in this repo. Use it for build/test conventions and hard restrictions
dev agents must always follow, independent of what any single task says.

<!-- Replace this comment with your rules, e.g.:
- Run `make test` before considering any task done.
- Never touch files under `secrets/` or commit credentials of any kind.
-->
"""

PROJECT_INDEX_TEMPLATE = """\
# Project Index (read this first)

This is the map of every document in this project. PM agent: read this file at the
start of any conversation where you have no prior turns in context (a fresh session),
before you say anything to the stakeholder about scope, status, or next steps. It tells
you what exists and where - follow the links to whichever doc answers your question,
rather than guessing or telling the stakeholder you're starting from scratch.

## Start here
- **[PROJECT_STATUS.md](PROJECT_STATUS.md)** - the current, durable summary of what's
  built, what's decided, what's open. Always read in full on a fresh session. Never
  wiped when the live spec/chat gets reset.

## Repo layout (product sources live at the root)
- (list each product/source directory here with a one-line description)
- **[{local_dir}/]({local_dir}/)** - PM Studio deployment config and local
  instructions for this repo.
- **[docs/](docs/)** - durable reference docs (list each with a one-line hook).

## Live working docs (reset between phases - may be blank right now)
- **[{workspace_rel}/current/SPEC.md]({workspace_rel}/current/SPEC.md)** - the
  spec for what's being actively built right now. Absence does not mean no history -
  check PROJECT_STATUS.md first.

## Product roadmap board (live, structured - not a markdown doc)
- Board at `/roadmap` on the running PM Studio server; data in
  `{workspace_rel}/roadmap/<product>.json` (server-owned, not git-tracked).
"""

PROJECT_STATUS_TEMPLATE = """\
# Project Status (durable — read this first)

No project history yet. This is a brand-new project - proceed to discovery.
"""

# Runtime-state entries `init` ensures are present in the target repo's .gitignore.
# Kept as a list rather than one text block so an EXISTING .gitignore can have only the
# lines it is missing appended - a deployment that ran `init` before some of these files
# existed must still end up ignoring them (see _ensure_gitignore).
GITIGNORE_ENTRIES: tuple[str, ...] = (
    "{workspace_rel}/current/chat_history.json",
    "{workspace_rel}/current/pm_session_id.txt",
    "{workspace_rel}/current/tasks/",
    "{workspace_rel}/sessions/",
    "{workspace_rel}/sessions.json",
    "{workspace_rel}/roadmap/",
    "{workspace_rel}/portfolio.json",
    "{workspace_rel}/accounts.json",
    "{workspace_rel}/costing.json",
    "{workspace_rel}/audit.jsonl",
    "{workspace_rel}/activity.jsonl",
    "{workspace_rel}/trackers.json",
    # Which process owns this workspace right now (workspace_lock.py). A pid is true
    # of one machine for one run; committing it would hand every checkout a lock
    # naming a process that was never theirs.
    "{workspace_rel}/server.lock",
    "{workspace_rel}/people.json",
    # The local-env supervisor's record of provisioned resources (local_env.py).
    "{workspace_rel}/local-env/",
    # What agents did with their MCP servers on this machine (mcp_activity.py).
    "{workspace_rel}/mcp-activity.jsonl",
    # The stores write `<name>.tmp` then atomically replace; a snapshot landing inside
    # that window would otherwise catch one.
    "{workspace_rel}/*.tmp",
    # Not runtime state: the env file config.toml's `token_env`/`password_env` names point
    # at (config._load_env_file). It is the one place a deployment is told to put an actual
    # credential, so `init` ignores it before the operator writes one there. `git add -A`
    # honours .gitignore, which is also what keeps it out of every session snapshot.
    ".env",
)

GITIGNORE_HEADER = "# PM Studio runtime/bookkeeping state (per-session, not product content)"

# Appended above the credential-bearing entries so the reason is visible in the file
# itself, where an operator tidying their .gitignore will actually read it.
GITIGNORE_SENSITIVE_NOTE = (
    "# Keep these ignored: .env holds the API tokens config.toml names via token_env,\n"
    "# accounts.json holds password hashes and live login tokens,\n"
    "# costing.json holds pay rates, audit/activity name who did what, and\n"
    "# trackers.json caches ticket titles pulled from your Jira/ADO, and\n"
    "# people.json holds the names and addresses of whoever those tickets are assigned to."
)


def missing_gitignore_entries(existing: str, workspace_rel: str) -> list[str]:
    """Which required entries this .gitignore doesn't have yet.

    Compared line by line (trimmed) rather than by substring, so a longer path that
    merely *contains* a required one doesn't count as covering it.
    """
    present = {line.strip() for line in existing.splitlines()}
    required = [entry.format(workspace_rel=workspace_rel) for entry in GITIGNORE_ENTRIES]
    return [entry for entry in required if entry not in present]


def _ensure_gitignore(root: Path, created: list[str]) -> None:
    """Makes sure every runtime-state entry is ignored, appending only what is missing.

    The previous version appended one fixed block and skipped entirely if the workspace
    root was mentioned at all. That meant a repo which ran `init` before accounts.json
    and costing.json existed could never pick them up - `init` would report "nothing to
    do" while password hashes sat committable. Now the check is per entry.
    """
    gitignore = root / ".gitignore"
    existing = gitignore.read_text() if gitignore.exists() else ""
    missing = missing_gitignore_entries(existing, CONFIG.workspace_rel)
    if not missing:
        return

    sensitive = {
        entry.format(workspace_rel=CONFIG.workspace_rel)
        for entry in GITIGNORE_ENTRIES
        if any(name in entry for name in (".env", "accounts", "costing", "audit", "activity"))
    }
    lines = ["", GITIGNORE_HEADER]
    # Ordinary state first, then the credential-bearing group under its own note.
    lines += [entry for entry in missing if entry not in sensitive]
    if any(entry in sensitive for entry in missing):
        lines += ["", GITIGNORE_SENSITIVE_NOTE]
        lines += [entry for entry in missing if entry in sensitive]

    text = existing
    if text and not text.endswith("\n"):
        text += "\n"
    gitignore.write_text(text + "\n".join(lines) + "\n")
    created.append(
        f".gitignore ({len(missing)} runtime-state "
        f"{'entry' if len(missing) == 1 else 'entries'} appended)"
    )


def _write_if_absent(path: Path, content: str, created: list[str], root: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    created.append(str(path.relative_to(root)))


def run_init(root: Path) -> None:
    created: list[str] = []
    local_dir = root / LOCAL_DIR_NAME

    _write_if_absent(
        local_dir / CONFIG_FILE_NAME,
        CONFIG_TEMPLATE.format(project_name=root.name),
        created,
        root,
    )
    _write_if_absent(local_dir / PM_INSTRUCTIONS_NAME, PM_INSTRUCTIONS_TEMPLATE, created, root)
    _write_if_absent(local_dir / DEV_INSTRUCTIONS_NAME, DEV_INSTRUCTIONS_TEMPLATE, created, root)
    knowledge_dir = local_dir / KNOWLEDGE_DIR_NAME
    if not knowledge_dir.exists():
        knowledge_dir.mkdir(parents=True)
        created.append(f"{LOCAL_DIR_NAME}/{KNOWLEDGE_DIR_NAME}/")

    seed_format = {"local_dir": LOCAL_DIR_NAME, "workspace_rel": CONFIG.workspace_rel}
    _write_if_absent(
        root / "PROJECT_INDEX.md", PROJECT_INDEX_TEMPLATE.format(**seed_format), created, root
    )
    _write_if_absent(root / "PROJECT_STATUS.md", PROJECT_STATUS_TEMPLATE, created, root)

    _ensure_gitignore(root, created)

    if created:
        print("PM Studio init - created:")
        for entry in created:
            print(f"  {entry}")
    else:
        print("PM Studio init - nothing to do, everything already in place.")
    print(
        f"\nNext: fill in {LOCAL_DIR_NAME}/{CONFIG_FILE_NAME} (products, layout), "
        f"then run `python -m pm_studio` from the repo root."
    )
