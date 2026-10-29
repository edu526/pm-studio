# Configuration — `pm_studio_local/`

Everything project-specific lives in the **target repo** under `pm_studio_local/`,
never in the installed package. Every file is optional: with nothing present, PM
Studio runs fully generic (no products, default port, generic prompts). All of it is
read once at server startup — restart after editing.

The core rule: local content is **append-only**. It fills identity slots and adds
instructions after the shared prompts; it cannot replace, reorder, or relax the
shared behavior. Deployments that need more than this can offer upstream a feature
request — never a local patch of package source.

```
pm_studio_local/
  config.toml           # identity + server + products + models
  PM_INSTRUCTIONS.md    # appended to every PM system prompt
  DEV_INSTRUCTIONS.md   # appended to every dev-agent dispatch
  knowledge/            # private reference docs, listed to the PM
```

Run `python -m pm_studio init` to scaffold all of it with commented templates
(non-destructive: existing files are never touched).

One optional file sits **outside** that directory: `.env` at the repo root, holding the
secrets `config.toml` refers to by name. See [`.env`](#env) below.

## `.env`

`config.toml` is committed, so credentials are named there, never written: `token_env`,
`email_env` and `password_env` hold the **name** of an environment variable. That
variable has to exist in the server process, and at startup PM Studio fills it from an
optional `.env` at your repo root:

```bash
# <repo root>/.env  - git-ignore this file
PM_STUDIO_JIRA_TOKEN=your-api-token
PM_STUDIO_JIRA_EMAIL=pm@acme.com
```

`python -m pm_studio` then picks the token up with no wrapper script and no `export`
first. Worth knowing about it:

- **Absent is normal.** No `.env`, no output, no behaviour change — export the variables
  however you already do (shell profile, CI, systemd, a run script) and nothing here
  applies.
- **The environment always wins.** A variable already set in the process is left exactly
  as it is; `.env` only fills the gaps. A real `export` is a deliberate act for that one
  run, and a stale file must never quietly override it. (Set-but-empty counts as unset,
  matching how every other credential read here treats `""`.)
- **It reports names, never values.** A loaded file prints which variables it supplied,
  so a missing credential is diagnosable from the startup log without putting a token in
  it.
- **The format is small on purpose:** `KEY=value` one per line, `#` comment lines, blank
  lines, an optional `export ` prefix, and quoted values taken literally (`X="  spaced "`
  keeps its spaces). No `$VAR` interpolation, no backslash escapes, no multi-line values
  — a file needing those wants a shell, which is what `export` is for. Lines that are not
  `KEY=value` are skipped with a warning naming the line number, rather than silently
  leaving the variable unset.
- **The whole file is loaded**, not just the keys PM Studio reads, and the dev agents it
  spawns inherit the process environment. If your `.env` also carries unrelated settings
  that would change how a subprocess behaves, keep those elsewhere or export only what
  you want with a run script.

## `config.toml`

```toml
[project]
# Display name of this deployment (defaults to the repo directory name).
name = "Acme Intranet"
# Name of the default session pinned to the primary checkout on main.
# Migrated systems typically keep whatever their default session was named.
default_session_name = "Main"
# Directory (relative to the repo root) holding `workspace/` runtime state.
# Fresh installs: "pm_studio". Migrated systems keep "pm_agent" so live session
# worktrees and registered git-worktree paths stay valid in place.
workspace_root = "pm_studio"
# Markdown lines describing where product sources live at the repo root -
# injected verbatim into the PM system prompt so every session starts oriented.
layout = '''
- `web/` - the customer-facing web app
- `packages/` - shared libraries consumed by the apps
'''

[server]
host = "127.0.0.1"   # keep it local - see the security model in the README
port = 8000

# Product taxonomy for the roadmap board: id = "Display Label".
# Declaration order is display order (customer-facing first reads best). Omit for
# a single-product repo: sessions then run unpinned, with no per-product boards.
#
# A product with a `parent` is a SUB-PRODUCT of it (see "Hierarchical products"
# below). Both spellings below mean the same thing; TOML wants every plain
# `key = "value"` line to come before the first [products.x] sub-table.
[products]
web = "Web App"
platform = "Platform"
auth = { label = "Auth & Identity", parent = "web" }

[products.billing]
label = "Billing"
parent = "web"

# Optional model allow-list override (id = "Label"). The reserved key `default`
# names the model new sessions start on; the reserved key `quick` (optional, one of
# the ids listed) the model QUICK dev tasks run on instead of their session's. Omit
# the table entirely to use the package defaults. Useful when a deployment is
# restricted to specific model ids.
[models]
default = "claude-opus-4-8"
quick = "sonnet"
"claude-opus-4-8" = "Opus"
sonnet = "Sonnet"
haiku = "Haiku"

# Operating mode. Omit this table entirely for the default, `personal` - the
# historical single-trusted-user tool with no accounts and no login.
# `enterprise` turns on user accounts, email invites and roles. Turning it on
# changes who can reach the dev agents, so it is always opt-in.
# `enabled = true` is accepted as shorthand for mode = "enterprise".
# A value that is neither `personal` nor `enterprise` is a hard startup error -
# a typo here must never silently leave an instance open.
[enterprise]
mode = "personal"

# Optional outbound mail for enterprise invites. Omit the whole table and each
# invite instead gives the admin a copyable link, which needs no mail server.
# Prefer `password_env` (the NAME of an environment variable) over `password`
# so the secret never has to sit in a file you might commit.
[smtp]
host = "smtp.example.com"
port = 587
from_address = "pm-studio@example.com"
username = "pm-studio"
password_env = "PM_STUDIO_SMTP_PASSWORD"
use_tls = true

# Optional external issue trackers. One [[trackers]] block per connection - a shop
# running both a Jira instance and an ADO project declares both. Omit the whole
# thing and the roadmap board looks exactly as it did before ticket linking
# existed. `projects` is required: it bounds what the sync pulls, and it is what
# lets a bare key like PROJ-123 be attributed to the right tracker.
# Each sync also pulls the projects' RELEASES (Jira versions; ADO iterations) into
# the same cache - no extra configuration, read back on GET /trackers/releases.
# Data only for now: nothing on the board consumes them yet.
# ALWAYS use `token_env` (the NAME of an environment variable) rather than
# `token` - pm_studio_local/ is normally committed, so an inline token is a token
# in your git history. PM Studio warns loudly if you use `token`. Put the value in
# the git-ignored `.env` at your repo root (see the ".env" section) or export it.
[[trackers]]
id = "jira"                      # stable: every linked change stores this id
provider = "jira"
label = "Acme Jira"
base_url = "https://acme.atlassian.net"
projects = ["PROJ", "PLAT"]
email = "pm@acme.com"            # Jira Cloud authenticates as email + API token
token_env = "PM_STUDIO_JIRA_TOKEN"
# sync_interval_minutes is DEPRECATED and ignored - syncs are manual ("Sync now").

# Optional: lets PM Studio CREATE tickets in this tracker (see "Pushing tickets").
# Omit and the connection stays read-only, exactly as it was before push existed.
[trackers.push]
project = "PROJ"                 # where new tickets are created; must be in `projects`
change_type = "Story"            # default "Story" — a roadmap change becomes one of these
epic_type = "Epic"               # default "Epic" — a project becomes one of these

[[trackers]]
id = "ado"
provider = "ado"
label = "Acme ADO"
organization = "acme"            # base_url is derived: https://dev.azure.com/acme
projects = ["Platform"]
token_env = "PM_STUDIO_ADO_PAT"  # an ADO personal access token
```

## Tracker import routing

A tracker can go beyond syncing a catalog: it can **create changes** from its own
tickets, routed onto products by component.

```toml
[[trackers]]
provider = "jira"
# ... connection keys ...
import_types = ["Epic", "Story", "Bug"]   # which ticket types become changes

[[trackers.routes]]
component = "Checkout Web"   # Jira component / ADO area path
product = "checkout"         # must be a declared product — fatal if not
system = "claims"            # optional; must be one that product touches — fatal if not

[[trackers.routes]]
component = "Search"
product = "search"

[[trackers.routes]]
project = "SHOP"             # the project-IS-the-product shape: every ticket in
product = "checkout"         # SHOP routes here, component or not
```

A route matches by `component`, by `project`, or by both (that component within that
project). **Component routes win over project routes** — the specific claim is the
truer one, so a project-wide default never swallows the exceptions declared beside it.
A `project` route must name a project the tracker actually syncs; anything else is a
dead route and refuses to boot.

On **ADO**, an area path is a tree, so a route on an area claims its **whole subtree**:
`component = "Portal"` covers `Portal\Authorizations\Queue` too, matched on the path
separator so `Portal-Legacy` is never mistaken for a child. Among matching area routes
the deepest one wins, and the shallower route keeps the rest of its subtree. Jira
components are flat labels and always match exactly.

`import_types` and `routes` only work together — declaring exactly one refuses to boot,
because it would be a config that looks like it imports and silently doesn't. With
neither, tracker behavior is exactly what it always was.

Two more knobs bound what the sync takes in:

```toml
since = "2025-01-01"   # only tickets CHANGED on/after this date are synced
```

`since` bounds the **catalog pull itself** — by last activity, not creation, so an old
epic still being worked stays current while abandoned history ages out. Linked tickets
older than the bound remain resolvable (the sync fetches any linked-but-missing ticket
individually). Task-level tickets are never imported as changes of their own; when a
parent ticket imports, its tasks ride along as a snapshot list inside the parent's
description — tasks are how a story gets done, not separate planning material, and a
board of task-changes buries the plan in execution detail. The tracker keeps the live
task list; the badge links straight to it.

After each sync, every catalog ticket whose type is in `import_types` and whose
component matches a route becomes a change on the routed product — `later` bucket,
status mapped from the ticket's state category, linked to its ticket in the same call.
The 1:1 link is the dedupe: re-syncs and restarts never double-import, and a manually
linked ticket is never re-imported. The import pass itself never writes to the tracker —
creating a ticket only ever happens when someone presses Push (see below).

An imported change is filed under the **project that mirrors its ticket's epic**, in
the same cycle it lands: epic-level tickets become projects of their own one rung up,
and the change joins its epic's project rather than sitting unaligned. The walk climbs
the whole parent chain, so an ADO story under a feature under an epic still finds it.

**Re-parenting a story in the tracker moves the change too** — but only out of a
project that mirrors an epic in that same tracker, because that is a slot the sync put
it in and may correct. A project created here by hand is somebody's filing and outranks
the hierarchy: the change stays, and the sync report counts it (`reparent_kept`) rather
than quietly emptying the project. Nothing ever *unfiles* a change: a parent the sync
cannot see — outside the synced projects, aged out of `since`, excluded, or an epic
nothing mirrors yet — is unknown, not "no epic", so the change keeps the project it has.

Tickets resolved as **won't-do** are never imported — not as changes and not as
projects. Jira files "Won't Do" under the Done status category, so without this the
declined work would land on the board as shipped-looking `done` changes. The check
reads the Jira resolution (and a status literally named "Won't Do"), spelled
leniently — apostrophes, hyphens, and case don't matter. Won't-do tickets are counted
in the sync report rather than silently dropped.

A ticket declined **after** it was imported leaves the board too: each sync ends with
a removal pass that deletes imported changes and projects whose ticket has since been
resolved won't-do. Only provably untouched imports are removed — the import stamp
still opening the description, no owner, the change still in `later`, the project
under no initiative and with no changes left beneath it. Anything a human touched
stays and is counted in the sync report instead. A linked ticket absent from the
catalog is unknown, not declined — nothing is removed on absence of evidence.

A ticket with **no matching route is reported, never guessed**: the board's tracker
strip counts unrouted importable tickets and names their components (the fix is a
config line, not an investigation). No catch-all product is invented. A route without
a `system` imports unattributed — into the existing unattributed report — because an
imported change you can see beats a refused one you can't.

When a slice of a tracker is tracked authoritatively **somewhere else** (the classic
case: the same work mirrored in a second tracker), declare it out entirely:

```toml
exclude_components = ["CapAdmin"]   # components / area paths owned elsewhere
```

Excluded tickets are invisible to every automatic pass — never imported as changes,
never imported or linked as projects, never used to assign a change to a project — and
the exclusion covers the **whole parent chain**, so a story or task under an excluded
epic is out too, even though children rarely carry components of their own. Excluded
is not unrouted: unrouted means "route this someday" and is reported as debt, excluded
means "never, it lives elsewhere" and is only counted. A component that is both routed
and excluded refuses to boot — one of the two lines is a leftover. Manual linking
stays possible: exclusion governs what the sync does on its own, never what a human
does on purpose.

### Closing projects the tracker has finished

A project's `closed` is normally a human act. Turn this on and an epic the tracker
calls **done** closes the project linked to it:

```toml
close_done_epics = true   # default false: count what would close, write nothing
```

**Close-only, in both modes.** Nothing here ever reopens a project — an epic that
moves back out of done leaves a project somebody closed exactly as it was, and the
disagreement is counted (`epic_open_on_closed`) instead. That asymmetry is deliberate:
a change's status was only ever a reading of its ticket, so `_mirror_ticket_status`
follows the tracker both ways, but a project's closure is a decision with a date on it
and a PM who closes ahead of an epic nobody transitioned should not be overruled every
sync.

**The default is report-only**, and the flag is worth leaving off for a sync or two:
off, the pass counts what it *would* close as `projects_closable` on the sync report
and writes nothing, so a deployment sees the list before a tracker transition is given
the power to close local planning work.

Four kinds of project are never closed, each for the reason a sibling pass shares:
an epic **absent** from the catalog (unknown, not done — it may have aged out of
`since`), a **won't-do** epic (declined is not delivered), an **excluded** epic, and
the **catch-all** (unplanned work never stops arriving).

Closing can leave open changes beneath a project, and says so — `closed_with_open_changes`
on the report. Those rows stay on the board wearing a "closed" chip rather than folding
away, which is the point: work stranded under an epic somebody closed is exactly what
the board exists to surface.

**What it will not do on its own** is close a project whose local changes have all
shipped while its epic is still open. That case is *offered*, not taken: the board
draws a "ready to close" chip and a human decides. An open epic is a live claim by the
system that actually runs the work, and — the reason this is not automated — an epic
only partly imported here reads as finished from the local roll-up. Where the epic
still has open children of its own, the row gets a warning naming the count instead of
an offer.

## Hierarchical products

A product declared with a `parent` is a **sub-product** of it:

```toml
[products]
web = "Web App"
platform = "Platform"
auth     = { label = "Auth & Identity", parent = "web" }
billing  = { label = "Billing",         parent = "web" }
sso      = { label = "SSO",             parent = "auth" }   # three levels
```

A sub-product is a full product, not a label: its own roadmap board file, its own
pinned sessions, its own id in every `/roadmap/<product>/...` URL. `parent` adds
three things and nothing else:

- **The board nests it.** The Product lens draws one section per top-level product,
  with its own project rows first and a folding sub-section per child inside it, one
  indent step per level. A parent's heading counts its own open work and, separately,
  what is open across its whole subtree — both are true at once, so neither number is
  quietly inflated. Folding a product folds everything under it.
- **A parent's PM owns its whole subtree.** A session pinned to `web` gets `web`,
  `auth`, `sso` and `billing` at full detail in its roadmap block, each under its own
  heading naming its own parent, and may create/update/schedule/triage on all of
  them. A session pinned to `auth` gets `auth` and `sso`; `web` reaches it as a
  one-line digest like any other product. Nothing outside the subtree is writable —
  that stays a suggestion (`origin_product`), enforced by the agent's own URL
  allowlist. A product in the middle is told it is both a parent and a child. (The one
  way a session reaches beyond its pinned subtree is by *adopting* another board, which
  only an initiative-scoped session can do, and only explicitly — see the README's
  "Sessions that work in an initiative".)
- **Names disambiguate.** Wherever a product is named with no section around it, it
  reads as its full path: `Web App / Auth & Identity / SSO`.

**Depth is yours to choose.** Nothing counts levels — a sub-product can have
sub-products of its own, as deep as your org actually is. The practical limit is
width, not the model: the board indents one step per level, so past four or five the
sections get narrow. An unknown or self-referential `parent`, or a **cycle**, is a
hard startup error, for the same reason a bad `[enterprise] mode` is — a child that
silently became a top-level product is a working-looking deployment with one board
too many, and the products in a cycle would hang off no top-level product at all and
vanish from the taxonomy while their boards sat on disk holding items.

Nothing stores the hierarchy: a change records the product id it always recorded.
So **re-parenting is a config edit**, not a migration — move the `parent` pointer,
restart, and the same boards appear in their new place. Deleting a `[products]`
entry does not delete its board file; items on an undeclared product stop loading
and come back if the id returns.

Adding sub-products to an existing deployment changes nothing about the products
already there: their items, boards and sessions stay exactly where they are, and a
parent simply gains children underneath it. The same goes for deepening an existing
tree later — pointing a new product at a product that already has a parent needs no
migration either.

## Product metadata

A product entry may carry facts about the product alongside its structure:

```toml
[products.checkout]
label = "Checkout"
parent = "web"
description = "Guest and member checkout flows, up to payment capture"
owner = "jane.doe"            # product decision-maker — free text
team = "Payments Engineering" # who builds it — free text
stage = "ga"                  # discovery | development | ga | sunset
```

All four are optional, and all four are **context, not policy**: a pinned PM's prompt
gains one line naming them (so it raises product decisions with the owner by name
instead of guessing), the board badges a product whose `stage` is not `ga`, and the
owner/team/description ride the section heading's tooltip. Nothing authorizes, blocks,
or bills against them — costing has its own roster, and roles live in `[enterprise]`.

`stage` is a closed vocabulary and an unknown value refuses to boot, the same contract
as `[enterprise] mode`: a typo must not silently mean the steady-state default. The
free-text fields are not validated beyond being strings.

Metadata lives here rather than in a store or UI deliberately. It is the same kind of
slowly-changing, operator-owned fact as the taxonomy itself, and it changes by config
edit — versioned by your own repo, where accountability for "who owns this product"
belongs.

## Systems

A **system** is a bounded piece of technology — a service, an app, a module — and it is
the unit a change is contained within. **A change belongs to exactly one system.** That
is what makes its blast radius knowable.

A **product** is the business-facing thing: a line of business, or an umbrella over the
technology that serves one. The two are not the same shape and neither contains the
other:

| | Product | System |
|---|---|---|
| What it is | a business-facing offering | a bounded piece of code |
| Owns a roadmap board | **yes** | no, never |
| A change relates to | exactly one (its board) | exactly one (its attribution) |
| Relationship | touches many systems | serves many products |

```toml
[products]
web = "Web App"

[products.checkout]
label   = "Checkout"
systems = ["claims", "rides"]     # which systems this product is built on

[systems]
claims = "Claims Processor"

[systems.rides]
label    = "Rides & Logistics"
path     = "services/rides"             # source folder, repo-root-relative
repo     = "github.com/org/rides"       # its own repo, when it has one
guidance = "docs/rides/GUIDANCE.md"     # declared now, acted on later
gitflow  = "docs/rides/GITFLOW.md"      # non-negotiable git workflow rules - LIVE
pipelines = ["rides-ci"]                # declared now, acted on later
```

`path`, `repo`, `guidance` and `pipelines` are all optional and, today, purely
descriptive: they are shown on the Systems page and named in the PM's prompt (so it
knows where a change's code lives before dispatching a dev task), but nothing enforces
or triggers them yet.

`gitflow` is optional too, but it is **acted on**. It points (repo-root-relative) at a
file of that system's non-negotiable git workflow rules — branching model, PR targets,
commit conventions — and the file must exist, or the server refuses to start. Two
things then happen on every dev task dispatched for the system:

- **Delivery.** The file's contents are appended to the dev agent's prompt verbatim at
  dispatch time, after (and explicitly overriding) `DEV_INSTRUCTIONS.md`, read fresh
  from the dispatching session's own worktree — an edit to the rules applies to the
  very next task, and a task whose rules file cannot be read is refused before any
  agent spend rather than run without them. The PM is told the rules travel
  automatically and never to restate or paraphrase them in a task description, so the
  only copy that reaches a dev agent is the declared one.
- **Verification.** After the task finishes, an independent compliance judge — a
  read-only agent that inspects the task's exact commit range and never sees the dev
  agent's own claims — rules `pass` / `violation` / `inconclusive` against the file.
  The verdict lands on the task record (and its card), and the PM's auto-continue turn
  is told about violations with instructions to dispatch a remediation task. A judge
  that fails is a visible `inconclusive`, never a silent pass. When the system's `path`
  is (or directly contains) its **own git repository** — a nested clone inside the
  checkout — the judge runs *inside* that repository, judging the commits the agent
  made there, or the dirty tree it left uncommitted; work spanning several of the
  system's repos gets one verdict per repo, folded worst-wins.

Once `[systems]` is declared, every dev-task dispatch must name the one system whose
code it changes (`"system"` in the POST /tasks payload) — that attribution is what
routes the rules, so it is required deployment-wide, unlike roadmap attribution below.
Prompt injection guarantees delivery, not obedience: branch protection and CI in each
repo remain the hard enforcement floor, and the judge is how the studio tells you when
they're about to be tested.

### Pull requests for reviewers (`[pull_requests]`)

Dev agents commit locally and never push. With `[pull_requests] enabled = true`, the
studio does the last step itself: when the judge rules **`pass`** on a task, every
branch the task created or moved in the system's own repositories is pushed to that
repository's `origin` and opened as a pull request. A `violation` or `inconclusive`
verdict opens nothing, and work in the deployment repo itself (the registry's
snapshot commits) is never pushed. Only systems with a `gitflow` are judged, so only
they get pull requests.

```toml
[pull_requests]
enabled = true

[pull_requests.bitbucket]            # Bitbucket Cloud: Atlassian email + API token
email_env = "BITBUCKET_EMAIL"
token_env = "BITBUCKET_API_TOKEN"

[pull_requests.ado]                  # Azure DevOps: PAT with Code (read & write)
token_env = "ADO_PAT"

[systems.rides]
gitflow    = "docs/rides/GITFLOW.md"
pr_targets = { "*-rc" = "release-candidate/next-release" }   # first glob wins; else main

[systems.rides.reviewers]            # or a plain array, for every repo of the system
"*"             = ["609b1998..."]    # repo folder under `path` -> account ids
"db-migrations" = ["712020:6418..."]
```

- **Host** comes from the repository's `origin` URL (HTTPS or SSH, Bitbucket Cloud or
  Azure DevOps). The push carries the token in an Authorization header passed through
  `GIT_CONFIG_*` environment variables, so it is never on a command line; hooks run.
- **Reviewers.** A pull request created through Bitbucket's API does not get the
  repository's default reviewers, so the studio reads `effective-default-reviewers`
  and sends them, plus the system's `reviewers` for that repository, minus the PR's
  author (the token's owner), whom Bitbucket refuses. On Azure DevOps, branch-policy
  reviewers are added by the service; `reviewers` are identity ids added on top.
  Both hosts take ids, not emails: on Bitbucket a `{uuid}` or an Atlassian account id
  (the same id Jira shows).
- **The Bitbucket token** needs `read:user`, `read:repository`, `write:repository`,
  `read:pullrequest` and `write:pullrequest`.
- **An open pull request** from the same branch to the same target is not duplicated:
  the push updates it.
- **Failures never fail the task.** Each branch gets a record on the task
  (`pull_requests`: opened / updated / failed, with the link or the scrubbed error),
  shown on the task card and handed to the PM, who passes the links on.

**The edge is many-to-many, so systems totals never sum across products.** A system
touched by three products would be counted three times. This is the same overlap rule
goals already carry — see `docs/ARCHITECTURE.md`.

**Systems have no roadmap, deliberately.** Roadmaps are product-first and
initiative-first. Work that belongs to a system rather than to any product — infra,
performance, an upgrade — is an **initiative**, which is exactly what initiatives are
for. Grouping a system's own changes is a filter on the board, not a board of its own.

### Turning it on, and what changes

**Declaring `[systems]` shows the tab; declaring a product's `systems = [...]` is what
puts that product in scope.** With no `[systems]` table at all, a deployment behaves
exactly as it did before this feature existed: no attribution, no Systems tab, nothing
new on the board.

Attribution is then scoped **per product**, not per deployment — which is what makes the
layer adoptable one product at a time. A product that declares no systems requires
nothing, and its board looks exactly as it did before. Bring products in scope as you
work out their systems; the Systems page lists the ones still outside.

That scoping is deliberate, not laziness. A deployment with two dozen products and
thousands of existing changes would otherwise, the moment its first system was declared,
require every board to attribute to whichever systems happened to exist yet — which is
worse than not attributing at all, because it manufactures wrong data instead of leaving
data missing.

For a product that **is** in scope:

- Every **new** change must name its system — from the board's create form, or
  `"system": "<id>"` in a PM's `POST`. A create without one is a 400 listing the valid
  ids.
- Changes that predate it carry no system. That is an **inconsistency to close, not a
  supported state**: the board shows a `no system` chip and a banner, the PM's context
  block marks each one `[NO SYSTEM]`, and the Systems page lists them with a control to
  attribute each. It is never a hard block — refusing to load a board would be worse
  than showing the work left to do.
- A change's system can be corrected, never removed: `"system": ""` is refused rather
  than treated as a reset.

An **out-of-scope** product (no `systems` declared) requires nothing, and its system-less
changes are counted separately from the debt above — they measure missing *config*, not
owed attribution, and no amount of attributing would bring that number down. Such a
product will still *accept* an explicit system if you give it one: permissive rather than
refused, since attributing a change while its product's edge is undeclared is useful and
harmless.

### Reclassifying a product that was really a system

If an id in `[products]` turns out to be a system, **an id may be declared in both
tables at once** — that is the explicit, temporary reclassification state, not an error:

```toml
[products]
claims = "Claims Processor"   # still here, so its board keeps loading

[systems]
claims = "Claims Processor"   # and now also a system
```

Its product board stays live and fully usable, and the Systems page flags it
`reclassifying` and offers each of its changes a "re-home to product X, attribute to
system Y" control (one `move_to_product` + `system` PATCH). When its board is empty,
delete the `[products]` entry.

Do it in that order. Deleting the `[products]` entry first would orphan the board:
board files are loaded only for **declared product ids**, so its items would silently
stop loading — the same behavior described for any deleted product above, which is
harmless for an empty board and data loss for a full one.

### Local environments (`[systems.<id>.local]`)

A system may declare the services PM Studio can **start and stop on the developer's
machine** — a secrets emulator, an API, a web app — and the **resources** they need
provisioned, such as a temporary database. Any technology works: a service is a shell
command plus how to tell it is up.

**One environment per chat session.** Each session works on its own branch in its own
worktree, so each gets its own copy of the system's `session`-scoped services, run from
that worktree (typically on auto-assigned ports so copies never collide), and its own
copy of each `session`-scoped resource. A few things have no per-session state — a
stateless secrets emulator — and are declared `shared`: one copy for the whole PM Studio
instance, run from the primary checkout, reused by every session. **Everything is on
demand**: nothing starts, nothing is provisioned and no session checkout is prepared
until someone asks.

```toml
[local_env]
max_session_envs = 2       # optional (default 2): see "Limit" below
idle_stop_minutes = 30     # optional (default 0 = never): see "Idle stop" below

[systems.epicride.local]
aws_profile = "epicride-dev"                           # descriptive: shown in the UI/API;
                                                       # ${NAME} / ${NAME:-default} read the environment (.env)
test_users_secret = "alivi-os/epicride/dev/test-users"  # descriptive: shown in the UI/API
# Optional: repo-root-relative paths/globs a NON-default session's worktree borrows
# from the primary checkout instead of installing its own (see "Session checkouts").
link_from_primary = ["src/epicride/frontend/node_modules", "src/epicride/backend/src/**/node_modules"]

[[systems.epicride.local.resources]]
id = "db"                                     # required; shares one id space with services
label = "Temporary database (epicmd_it_*)"    # optional
scope = "session"                             # optional: "session" (default) or "shared"
create = "make epicride-db-create"            # required; runs from the repo root
destroy = "make epicride-db-drop NAME={db.name}"  # optional
timeout = 900                                 # optional seconds for create/destroy (default 900)
outputs = ["name"]                            # optional: the keys `create` reports
expires = "daily"                             # optional: something else drops it every day...
expires_at = "00:00"                          # ...at this "HH:MM" (default "00:00")...
timezone = "America/New_York"                 # ...in this IANA zone (default: this machine's local time)

[[systems.epicride.local.services]]
id = "secrets"                      # required, unique in the system, [a-z0-9-]+
label = "Lambda secrets extension"  # optional
scope = "shared"                    # optional: "session" (default) or "shared"
command = "make epicride-secrets"   # required; run through the shell
port = 2773                         # optional readiness: TCP connect on localhost succeeds

[[systems.epicride.local.services]]
id = "api"
command = "npm run start -- --port {port} --apis {apis}"
cwd = "src/epicride/backend"        # optional, relative to the worktree it runs from (default ".")
port = "auto"                       # an int, or "auto": a free port picked at start
ready_url = "http://localhost:{port}/health"  # optional readiness: HTTP GET answers < 500
url = "http://localhost:{port}"     # optional link the UI shows once running
depends_on = ["secrets"]            # optional: services AND resources of the SAME system (created on demand)
uses = ["db"]                       # optional: resources used IF they exist (never created by a start)
params = { apis = "" }              # optional: caller-fillable {name} placeholders + defaults
env = { DB_NAME = "{db.name}", SECRETS_PORT = "{secrets.port}" }  # optional extra env vars
ready_timeout = 180                 # optional seconds before "failed"
setup = "make -C {repo_root} epicride-backend-install APIS={apis}"  # optional: run before EVERY launch (see "Setup")
setup_timeout = 900                 # optional seconds the setup may take (default 900)
```

**Ports.** `port` is a fixed integer (typical for `shared` services), `"auto"` (PM
Studio picks a free TCP port when the service launches and exposes it as `{port}`), or
absent. A fixed port on a `session` service is allowed, but then only one session can
run it at a time: the next one gets `port_in_use` (409) naming the session that holds it.

**Placeholders** work in `command`, `env` values, `url` and `ready_url`:

| Placeholder | Value |
|---|---|
| `{name}` | a declared `params` entry (the caller may override it) |
| `{port}` | this service's own port, fixed or auto (it must declare `port`) |
| `{<service>.port}` | the port of a service this one `depends_on`, directly or transitively — the shared run, or this session's |
| `{<resource>.<key>}` | an output of a resource this one depends on (directly or transitively) or `uses` (one of its `outputs`, when declared); `""` for a used resource that is not ready |
| `{session}` | the session id (`session`-scoped entries only) |
| `{worktree}` | absolute path of the checkout it runs from, with forward slashes — the session's worktree, or the repo root for the default session and `shared` scope |
| `{repo_root}` | absolute path of the **primary** checkout, with forward slashes — in every scope |

`{repo_root}` exists because a session worktree only contains what is committed to the
deployment repo: the deployment's `Makefile`, `scripts/`, virtualenv and git-ignored
`.env` live only in the primary checkout. So a session service typically runs tools from
the primary against product code from its worktree:

```toml
command = "make -C {repo_root} epicride-local ROOT={worktree} PORT={port}"
```

A resource's `create` may use `{session}` (session scope), `{worktree}` and
`{repo_root}`; its `destroy` may use `{session}`, `{repo_root}` and its own outputs
(`{db.name}`) — not `{worktree}`, which may be gone by then. `params` may not be named
`port`, `session`, `worktree` or `repo_root`.

**Setup.** A service may declare `setup`: a shell command PM Studio runs **before every
launch** of that service, so whatever the command needs installed (its npm
dependencies, say) is installed without anyone running anything by hand. Make it
idempotent and quick when there is nothing to do — it runs on every start and restart.
Shared services run theirs too. The order of a start is:

1. **`setup`** — from the **primary checkout** (the repo root, whatever the scope),
   through the shell, in its own process group, with the environment the service gets
   (its `env` entries included, except those that need `{port}` or a dependency's value).
2. **Session checkout** — prepared the first time (see "Session checkouts"), and after a
   setup **refreshed** every time, so what the setup just installed in the primary
   checkout (`node_modules`) is linked into the session's worktree.
3. **Dependencies** — services and resources it `depends_on`, waited for.
4. **`command`** — the service itself.

`setup` may use the service's `params`, `{session}` (session scope), `{worktree}` and
`{repo_root}` — **not** `{port}`, `{<service>.port}` or `{<resource>.<key>}`, which do
not exist yet when it runs (fatal at startup). While it runs the service is `starting`
with the message "Running setup..."; its output goes to the service's logs, after a
`[pm-studio] ... setup in <repo root>: <command>` header, each line prefixed with
`[setup]` and a space. A non-zero exit fails the start (`failed`, "Setup failed (exit
N): *its last output line*") and running longer than `setup_timeout` seconds kills it and fails the start
("Setup did not finish within Ns; stopped it."); either way the command is never
launched, and the next start runs the setup again. Stopping, restarting or idle-stopping
the service, ending its session or shutting the server down while its setup runs kills
the setup's process tree, and nothing is launched. Two launches whose setups render to
the same command (two sessions starting the same service) never run it at once: the
second waits for the first ("waiting for the same setup, already running, to finish").

Every mistake here is fatal at startup — an unknown key, a missing `id`/`command`/
`create`, a duplicate or malformed id (services and resources share one id space), a
dependency on an unknown id or a cycle, a port that is neither 1-65535 nor `"auto"`, an
unknown `scope`, a `shared` service depending on (or using) anything session-scoped, a
`uses` entry that is not a resource of the system or is also in `depends_on`, an
`expires` other than `"daily"`, an `expires_at` that is not `"HH:MM"`, an unknown
`timezone`, `expires_at`/`timezone` without `expires`, non-string `params`/`env`/`setup`,
a `ready_timeout`/`setup_timeout`/`timeout` that is not a positive number of seconds, and
**any placeholder nothing would fill** (an undeclared param,
`{port}` on a service with no port, `{api.port}` when `api` is not a dependency or has no
port, `{session}` in a shared entry, an undeclared output, `{port}` or a dependency's
value in `setup`...). `${VAR}` is left to the
shell. With neither `port` nor `ready_url`, a process still alive one second after
launch counts as running.

The **server owns the processes**: they outlive dev tasks and PM turns and are all
stopped when the server stops. Each runs in its own process group, and stopping one kills
its whole tree (`taskkill /T` on Windows, the process group on POSIX); stopping a service
leaves its dependencies running. Output is kept in memory (last 1000 lines) and appended
to `<temp>/pm-studio-local-env/<session|shared>-<system>-<service>.log` — never inside
the repo.

Callers only fill declared `{name}` holes, and every value must match
`^[A-Za-z0-9 ,._:/@=-]*$` (max 256 characters) — the command runs through the shell, so
anything else is a 400. **Generated values pass the same allowlist** before they are
substituted: ports, session ids, `{worktree}`/`{repo_root}` paths (max 1024) and resource outputs. A
fixed port that something else already listens on is refused with state `port_in_use`
(409) rather than started twice.

#### Resources

A resource comes to exist in one of two ways, and is then **reused** by every later
start in its scope (the session's, or the shared one) and across server restarts:

- **On demand**, through `depends_on`: the first time a service that depends on it
  starts in that scope, it is created, and the service waits for it.
- **Only when asked**, through `uses`: starting the service never creates it. Its
  `{db.<key>}` placeholders are the outputs when the resource is `ready` in the
  service's scope at launch, and `""` otherwise — the service's cue to fall back (for
  EpicRide: to the shared DEV database, which is the default). It is created by an
  explicit `POST /local-env/{system}/resources/{resource}/create?session=`; a service
  already running keeps what it launched with, and its status says `restart_needed`
  until it is restarted.

`create` runs from the repo root with a `timeout`;
its stdout and stderr go to the same logs as services (`GET .../{resource}/logs`), and
its **last non-empty stdout line must be a JSON object of string values**, e.g.
`{"name":"epicmd_it_2873"}`. Each value must pass the allowlist above (a value with shell
metacharacters fails the resource). A non-zero exit, a timeout, a last line that is not
such an object, or a missing declared output marks it `failed` — and the dependent
services `failed` with "dependency db is failed"; the next start retries.

States: `absent`, `creating`, `ready`, `failed`, `destroying`, `expired`. Outputs are persisted in
`<workspace_root>/workspace/local-env/resources.json` (primary checkout). That
directory is on the snapshot's never-commit list and in `init`'s `.gitignore` block:
the names are not secrets, but they describe databases on *this* machine.

`destroy` runs **only** on explicit request (`POST .../resources/{resource}/destroy`,
refused with 409 while a running service uses it) or when the session ends. **Shutdown
never destroys resources** — re-creating a database on every restart is what reuse
exists to avoid. A resource without `destroy` is forgotten rather than torn down, and
the release report names it with its outputs so you can clean up by hand. A failed
destroy keeps the outputs (state `failed`) so it can be retried.

**Daily expiry.** A resource declared `expires = "daily"` is one something else removes
on a schedule — a team job drops every temporary database at midnight. Once a ready
resource's `created_at` is before the most recent `expires_at` boundary (computed on the
calendar in `timezone`, so DST moves the instant, not the wall-clock time), it reads as
`expired`, with the message "dropped at <boundary> by the nightly job; create it again":

- its outputs are no longer substituted — a service that `uses` it gets `""` on its
  next launch (and shows `restart_needed` meanwhile), a service that `depends_on` it
  creates a new one on its next start;
- an explicit create is allowed again (no 409);
- **no `destroy` is ever run for it** — it is already gone; destroying or releasing it
  just forgets it (the release report lists it under `expired`);
- the old outputs stay in `resources.json` until the next create overwrites them.

While ready, the status carries `expires_at_iso`: the next expiry instant.

#### Session checkouts

A new session's worktree is a plain `git worktree` of the main repo: empty submodule
folders, no `node_modules`. So before the **first** session-scoped service or resource
of a system starts in a **non-default** session, PM Studio prepares that worktree once
(`session_checkout.ensure_system_checkout`): git worktrees for the submodules under the
system's `path`, and links from the primary checkout for each `link_from_primary` entry.
A system with no submodules under its `path` (or no `path`) is not an error: its code is
already in the worktree, so only the links are made and the notes say so. Preparation
fails only when submodules exist and none of them could be prepared.
Its notes, or the error, show in the status's `checkout` member (`not_needed`,
`pending`, `preparing`, `ready`, `failed`); a failure fails the start and the next start
retries. The default session and `shared` entries run from the primary checkout and
never need it.

#### Limit

`[local_env] max_session_envs` (default 2, minimum 1) caps how many **distinct
(session, system)** environments may have a service starting or running at once — each
is a full copy of a stack on your machine. Shared services do not count.

Before refusing, a start (`start`, `start_all`, `restart`) that would exceed the limit
**makes room**: among the OTHER sessions holding a session environment, those with no
dev task running and no local-env call for at least 60 seconds qualify, and the least
recently active one has its session-scoped services stopped (exactly as an idle stop,
below; its services' `message` reads "Stopped to make room for session *name* (limit of
*N* session environments reached) after *M* min without use; start it again when needed.").
When no session qualifies — every other one is busy or was active within the last
minute — the start is a **409** whose message names the running ones, as before; stop
one (or wait for its session to end) first. A request refused for another reason (bad
params, unknown service) evicts nobody.

#### Idle stop

`[local_env] idle_stop_minutes` (default `0` = never; an integer `>= 0`, anything else
is fatal at startup) stops a session's environment once that session has been **idle**
that long. Activity is any local-env call made on behalf of the session — start, stop,
restart, wait, logs, status (the Systems page polling the session it shows counts too)
or create — and a session with a **dev task running** is never idle: it counts as
activity, so the countdown starts when the task ends. What stops is the session's
`session`-scoped services in every system; **shared services are never idle-stopped**.
Nothing provisioned is lost: resources (the temporary database) and the prepared
session checkout are kept, so starting again is only process startup. Each stopped
service's `message` says why ("Stopped after 30 min without use; start it again when
needed."), and the server logs one line per stopped session environment. A background
thread checks every 30 seconds; with `0` there is no thread at all.

#### When a session ends

Terminating, deleting or cleaning up a session **releases its environment first**: its
services are stopped synchronously (they run from the worktree about to be removed),
then its resources are destroyed in the background (or forgotten, without `destroy` or
once expired),
and its checkout preparation is forgotten. Shared services and resources are untouched.
The release is audited as `local_env.released`. A terminate that then stops on a merge
conflict leaves the session active; its environment simply comes back on the next start
(with a fresh resource).

#### API

Every route takes an optional **`?session=<id>`** (default `default`): which chat
session's environment it acts on. An unknown session (or one with no worktree left) is
a 404.

| Route | Capability | |
|---|---|---|
| `GET /local-env?session=` | view | `{"session": id, "systems": [...]}`, only systems that declare `local` |
| `GET /local-env/overview` | view | every active run and held resource across sessions (below) |
| `POST /local-env/{system}/start?session=` | manage_local_env | all services in dependency order; body `{"params": {"<service>": {...}}}` (optional) |
| `POST /local-env/{system}/stop?session=` | manage_local_env | the session's services, dependents first; shared ones only if no other session has this system up |
| `POST /local-env/{system}/{service}/start?session=` | manage_local_env | dependencies (services and resources) first; body `{"params": {...}}` (optional) |
| `POST /local-env/{system}/{service}/stop?session=` | manage_local_env | stopping a shared service stops it for everyone |
| `POST /local-env/{system}/{service}/restart?session=` | manage_local_env | reuses the last params unless given |
| `GET /local-env/{system}/{service}/logs?lines=200&session=` | view | `{"lines": [...]}`; a resource id gives its create/destroy output |
| `GET /local-env/{system}/{service}/wait?timeout=180&session=` | view | blocks until the service is no longer `starting` (or `timeout` seconds pass, capped at 600); answers its status plus `waited_seconds` and `timed_out` |
| `POST /local-env/{system}/resources/{resource}/create?session=` | manage_local_env | explicit creation (the only way a `uses`-only resource exists); answers at once with `creating`; 409 when already creating, ready (and not expired) or destroying |
| `POST /local-env/{system}/resources/{resource}/destroy?session=` | manage_local_env | answers at once with `destroying`; 409 while a running service depends on or uses it, or while creating |

Each system in `GET /local-env` carries `session`, `checkout`, `services` and
`resources`. A service: `id`, `label`, `scope`, `session` (`shared` or the session id),
`state`, `pid`, `port` (resolved — the auto port once launched), `auto_port`, `url` and
`ready_url` (resolved; `""` while a placeholder has no value yet), `command`, `setup`
(as declared; `""` when none),
`depends_on`, `uses`, `resources_used` (every resource whose outputs it may substitute:
those it depends on, directly or transitively, then those it uses), `restart_needed`
(starting/running, and one of those resources changed — created, destroyed, expired or
re-created with other outputs — since its launch), `params`, `started_at`, `exit_code`,
`message`, `log_path`. A resource: `id`, `label`, `scope`, `session`, `state`, `outputs`
(empty while expired), `message`, `created_at`, `expires`, `expires_at_iso` (next expiry
while ready, else null), `has_destroy`, `log_path`.

`GET /local-env/overview` answers `{"runs": [{session, session_name, system,
system_label, service, scope, state, port, pid, url}], "resources": [{session,
session_name, system, resource, scope, state, outputs, message}], "session_envs":
[{session, session_name, system}], "active_session_envs": n, "max_session_envs": m}`
— runs only while `starting`/`running`, resources unless `absent`.

Starting never waits for readiness: it answers at once with state `starting`, and you
poll `GET /local-env` — or block on `GET .../{service}/wait`, which answers as soon as
the service is `running`, `failed`, `exited`, `port_in_use` or `stopped`, with
`"timed_out": true` (state still `starting`) when the timeout passed first; a service
not starting answers at once. A service's state is one of `stopped`, `starting` (including
while its setup runs and while it waits for its checkout, dependencies or resources — see
`message`), `running`,
`failed` (exited, or not ready in time, before becoming ready; or a dependency failed),
`exited` (died after becoming ready, with `exit_code`) or `port_in_use`, with a
`message` explaining it. 404 is an unknown system, service, resource or session; 409 is
a taken port, the session limit, or a resource that is busy.

**Agents can drive it too.** Every `claude` subprocess (PM turns, dev tasks, judges)
gets `PM_STUDIO_URL` and `PM_STUDIO_AGENT_HEADER` (`"<header>: <per-process agent
token>"`) in its environment, PM turns and dev tasks also get `PM_STUDIO_SESSION` (their
chat session's id), and the agent principal holds `manage_local_env`:

```sh
curl -s -X POST "$PM_STUDIO_URL/local-env/epicride/api/start?session=$PM_STUDIO_SESSION" -H "$PM_STUDIO_AGENT_HEADER"
curl -s "$PM_STUDIO_URL/local-env/epicride/api/wait?timeout=180&session=$PM_STUDIO_SESSION" -H "$PM_STUDIO_AGENT_HEADER"
curl -s "$PM_STUDIO_URL/local-env?session=$PM_STUDIO_SESSION" -H "$PM_STUDIO_AGENT_HEADER"
```

A dev task dispatched for a system that declares `local` is told this in its prompt,
including to read ports and URLs from the status rather than assume them.

## Operating modes

`personal` (the default) is the tool as it has always been: one trusted user, no
accounts, everything bound to localhost.

`enterprise` adds a roster on top of exactly the same core loop. Nothing about
sessions, dispatch, the roadmap or git behavior changes — what changes is that
requests need an identity.

Turning it on:

1. Set `mode = "enterprise"` under `[enterprise]` and restart the server.
2. Open the server. With no accounts yet, every page redirects to `/setup`, which
   creates the **owner** account — full access, manages the roster. This is the
   personal-to-enterprise conversion step, and it migrates no data: sessions, chat
   history, tasks and roadmap files stay exactly where they are.
3. Invite people from `/people` (admins only). Each invite is mailed if `[smtp]` is
   configured, and always yields a copyable `/accept-invite?token=…` link. Invites
   expire after 7 days, are single-use, and re-inviting someone supersedes their old
   link.

### Roles

| Capability | admin | pm | reviewer | viewer |
| --- | :-: | :-: | :-: | :-: |
| View sessions and the whole roadmap | ✅ | ✅ | ✅ | ✅ |
| Work in PM sessions (send turns, retitle, reset) | ✅ | ✅ | — | — |
| Create / merge / sync / end sessions | ✅ | ✅ | — | — |
| **Dispatch dev agents** | ✅ | ✅ | — | — |
| Change the roadmap | ✅ | ✅ | — | — |
| Start / stop local environments | ✅ | ✅ | — | — |
| Manage people (invites, roles, disable) | ✅ | — | — | — |

Reads are open to every role on purpose: transparency is the deployment default, so
there is no per-user filtering of the board. What a role changes is what you may *do*,
not what you may see.

**`dispatch_dev_task` is a code-execution boundary, not a UI affordance.** Dev agents
run with bypassed permissions inside the repo, so granting `pm` is granting the ability
to run arbitrary code on the host. It is enforced on the HTTP path and on the chat
websocket, and every dispatch is recorded in the audit log with the actor who asked
for it.

`reviewer` is read-only today. The role exists so it can be assigned now, but the
reviewer workflow itself is not implemented yet.

The matrix lives in one table in `pm_studio/authz.py` — that is the single place to
read or change what a role can do.

### Time &amp; cost attribution

Optional `[costing]` table:

```toml
[costing]
blended_rate = 120.0            # fallback hourly rate for anyone with no individual one
default_capacity_hours = 40.0   # default declared capacity per person per week
currency = "USD"                # label only; no conversion is done
weights = { pm_turn = 1.0, dev_task = 3.0, review = 1.0 }   # signal weights
```

**This is a distribution mechanism, not a stopwatch.** Nothing times anybody's screen.
Each person's *declared capacity* is split across projects in proportion to the activity
signals they generated, so a week always reconciles:

> Dana, 2026-W31: 28.4h Signup rewrite, 11.6h Billing — 40h total

That reconciliation is the point. Capacity is the input and signals only decide the
proportions, so the total is always a real week and the denominator isn't arguable.
Because it is explicitly an approximation, an admin can **override** any person's week
(whole-week, so the total stays meaningful) and the derived figures are kept alongside
the override — an estimate is never the only record.

**Two cost streams, never conflated:**

- **Labour** — distributed hours × rate (individual, else blended). An *estimate*.
- **Agent** — token/API spend, read from the Claude CLI's own JSON output. *Measured*.

An agent grinding for twenty minutes while nobody is at the keyboard is machine time,
not somebody's afternoon — so an auto-continuation's tokens are counted while its labour
weight is deliberately zero.

Rates and capacities live in `<workspace_root>/workspace/costing.json` (written `0600`,
never git-tracked); the activity log is append-only at `workspace/activity.jsonl`.
**Rates are the deployment's own compensation data** and never ship in the package. Rate
changes are audited, but the rate value itself is deliberately kept out of the audit
detail, since every admin can read that log.

Attribution needs a project, and sessions are pinned to a *product* (which is not a
level in the work model), so a session can be pointed at a **Project** — its PM turns
and dev tasks then count towards it. A session with no project falls back to the
catch-all, the same way an unparented change does.

Cost is visible to `admin` only (`view_cost`), and is additive **up to initiative**;
goal-level figures overlap and are never summed. See `/costing`.

### Audit log

Consequential actions (dispatches, session lifecycle changes, roadmap writes, role and
invite changes) append one JSON line to
`<workspace_root>/workspace/audit.jsonl`, recording who did it. Admins can read it at
`GET /audit`. It only ever grows and is never rewritten, so a crash cannot corrupt
earlier entries. Nothing is written in personal mode — with one trusted user, the git
snapshot history already answers the question.

State lives in `<workspace_root>/workspace/accounts.json`, written `0600`. It is kept
out of git by two independent mechanisms, because one was not enough: `init` lists it in
`.gitignore`, **and** every snapshot commit unstages it unconditionally (see
`gitsnapshot.SENSITIVE_WORKSPACE_FILES`). A PM turn snapshots the whole repo with
`git add -A`, so a deployment whose `.gitignore` predates this file would otherwise have
committed password hashes on its next turn. If one of these files is already tracked,
the server says so on every snapshot with the `git rm --cached` command to run. Passwords are
PBKDF2-HMAC-SHA256; login tokens and invite tokens are stored only as hashes, which
also means an invite link cannot be re-read later — revoke and re-invite instead.

**The PM agents keep working.** They reach the server over `curl` and have no browser
cookie, so the process mints a per-run agent token and splices it into the prompts'
curl examples. It is never persisted, holds the `pm` role (never `admin`, so it can't
reach the roster or cost data), and grants nothing beyond the endpoints an agent's
Bash allowlist already matched in personal mode.

## External trackers (Jira / Azure DevOps)

Declare one `[[trackers]]` block per connection (see the sample above). With none
declared the whole feature is dormant: no extra threads, no ticket controls on the
board, nothing.

> **Tracker config belongs in your repo, not in this package.** This package is public and
> its own `pm_studio_local/` is the config for developing PM Studio itself, so it declares
> no tracker. Yours names your Jira site, your project keys and — via the synced catalog —
> your ticket titles. That is your organisation's internal structure, and it stays in the
> repo you control.
>
> Two consequences worth internalising before you point this at a real instance:
>
> - `pm_studio_local/config.toml` is **committed**. Use `token_env` and `email_env` so no
>   credential is in it — put the values in the root `.env`, which is not — and remember
>   that `base_url` and `projects` alone disclose which
>   vendor instance you run and how your work is organised. If your repo is public, that is
>   published.
> - A sync writes `<workspace>/trackers.json` — a cache of ticket titles from the synced
>   instance — and `<workspace>/people.json`, the directory of whoever those tickets are
>   assigned to, by real name and email address. Both land in whichever checkout the server
>   runs from. Both are git-ignored and unstaged from every snapshot, but do not run a sync
>   from a checkout that is not yours to fill with that data.
>
> `tests/test_no_deployment_data.py` enforces both halves of this for the package itself:
> no tracked file may name a real tracker host, the dogfood config may declare no tracker,
> and no synced catalog may exist anywhere in the checkout. To exercise the sync while
> developing, point a throwaway config at a local fake tracker; the suite covers the client
> behaviour with no network at all.

**What a link is.** A roadmap change may be linked to exactly **one** ticket, and a
ticket to exactly one change — 1:1 in both directions. The change stores only
`tracker_id` + `ticket_key`; the ticket's type, title, state and URL come from the
synced catalog, so a sync updates one place instead of rewriting every linked change.
Attempting to link a ticket another change already holds returns **409** naming that
change. Keys are compared case-insensitively for Jira, so `proj-1` cannot sneak in
alongside `PROJ-1`.

**Linking.** On the board, each card gets a *Link ticket…* control opening a picker
over the synced catalog (already-linked tickets are shown but greyed out). You can
also paste a full ticket URL or a bare key. The PM agent can do the same from a
session pinned to that product:

```bash
curl -s -X PATCH http://127.0.0.1:8000/roadmap/<product>/items/<item_id> -H "Content-Type: application/json" -d '{"ticket": "PROJ-123"}'
```

`"ticket": ""` unlinks. A key that does not exist in the tracker is refused with a
**404** rather than stored, so a typo can't sit on a card forever looking like a sync
outage.

**Syncing.** Pulled when you ask for it - **Sync now** in the board header, or
`POST /trackers/sync`. Nothing syncs on a timer, and a pass already running makes a
second request a no-op. Jira uses
`/rest/api/3/search/jql` and falls back to the older offset-paginated
`/rest/api/{3,2}/search` for Server/DC; ADO uses WIQL then batched work-item hydration.
Linked tickets that fall outside `projects` are fetched individually, so a one-off
dependency on another team's board still resolves.

**When a tracker is down** its previous catalog is deliberately *kept* — a card showing
last week's type is more useful than one that suddenly claims the ticket is gone. The
failure and its reason appear in the board header, and the tracker keeps retrying. A
link whose ticket isn't in the catalog renders as a dashed **NOT SYNCED** badge rather
than disappearing.

**Ticket types and colour.** Each tracker's own type names are normalised onto a small
vocabulary — epic, feature, story, task, bug, spike, subtask, other — which is what
picks the badge colour, so ADO's *Product Backlog Item* and Jira's *Story* look alike.
The badge always shows the tracker's **own** name for the type, so a team that renamed
Story to Deliverable sees "Deliverable", and an unrecognised custom type gets a neutral
colour rather than a wrong one. Colour is never the only signal.

**A ticket is never updated.** The sync is read-only, and so is everything after it: PM
Studio never changes a ticket's type, state, title or anything else in Jira or ADO. The
single exception is **creating** a ticket, which only happens when someone presses Push
— see below.

The reverse direction is not symmetric. A linked change's **`status` follows its
ticket's state on every sync**, both ways: a ticket closed after it was imported closes
its change, and one reopened upstream reopens it. Status is a claim about execution, and
for work that has a ticket the tracker is the system that knows — a change reading "in
progress" against a closed ticket is a stale snapshot, not a plan. **`bucket` is never
touched:** now/next/later is the plan and stays yours, so a closed ticket sitting in NOW
is your signal to clear the column, not licence for the sync to reshuffle it. This
applies to **every** linked change, including one linked by hand from a tracker that
imports nothing (no `import_types`, no routes): `import_types` governs whether a tracker
manufactures changes, never whether existing ones stay current. Three
tickets are left alone — one absent from the catalog (unknown, not "not done", so a
short sync cannot reopen the board), one resolved **won't-do** (declined is not
delivered, and its Done category would otherwise relabel abandoned work as shipped —
the removal pass below owns that case), and one **excluded**. The sync report counts
what moved as `status_refreshed`.

**Credentials and cached data.** Tokens are read from the environment via `token_env`
(filled from the git-ignored root `.env` if you keep it there), are only ever sent in an
`Authorization` header, and are scrubbed from every error
message before it is stored or served — `GET /trackers` cannot return one. The catalog
cache at `<workspace>/trackers.json` holds ticket titles from your tracker, so it is
treated like the other credential-bearing state: gitignored *and* unstaged from every
snapshot unconditionally (see `gitsnapshot.SENSITIVE_WORKSPACE_FILES`). Deleting it
costs one sync.

`<workspace>/people.json` is on that same list and is the sharper case: a sync reads each
ticket's **assignee** and keeps one entry per person — display name, email where the
instance discloses one, and the identity each tracker knows them by — so that "what is Dana
working on" is one question across Jira and ADO rather than one per tool. Deleting it costs
one sync too, but unlike the catalog it also loses any assignment made in PM Studio, since
those name a person by id. Nothing in PM Studio ever writes an assignee back to a tracker:
assigning here is local planning, and where it disagrees with the ticket the board says so
on the card rather than quietly overriding either answer.

## Pushing tickets to a tracker

The write half of a tracker connection: a change you planned in PM Studio becomes a real
ticket on the board, linked to the change in the same step. It is the inverse of import
routing — import takes a ticket that exists and makes a change, push takes a change and
makes the ticket.

**Opt-in, per tracker.** Add a `[trackers.push]` table:

```toml
[[trackers]]
provider = "jira"
projects = ["PROJ", "PLAT"]
# ... connection keys ...

[trackers.push]
project = "PROJ"        # required — where new tickets are created
change_type = "Story"   # default "Story"
epic_type = "Epic"      # default "Epic"
```

With no such table the tracker is **read-only** and no push control renders anywhere —
not disabled, absent. Two reasons it is declared rather than assumed: a sync token is
often deliberately read-only, so assuming write access would turn one button into a
confusing 403; and creating an issue on a shared board is an outward-facing act a
deployment should have to opt into.

`project` must be one the tracker already syncs, or PM Studio **refuses to boot** — a
ticket created in a project the catalog never pulls would read as "not synced" on the very
card that created it. A `[trackers.push]` table on an ADO tracker is fatal too: pushing is
implemented for **Jira only** so far, and silently ignoring the table would leave you
believing your Boards project was writable.

**What it does.** Pressing Push on an unlinked change creates one issue —
`change_type`, in `project`, summary from the change's title, description from the
change's own text plus one provenance line naming the product and change id — and links
it. Pressing Push epic on a project does the same at the epic rung with `epic_type`.
Both are one API call plus a read-back, so the badge shows the tracker's own type and
opening status rather than an echo of what was asked for.

The declared target is a **default, not a cage**: the link dialog's create section lets
you pick a different synced project and type per push, because which project a given
change belongs in is a per-change fact.

**Hierarchy.** Push the project's epic first and every change under that project lands
beneath it — the pushed board then has the same shape as the plan here, instead of a flat
pile of orphan stories. If an instance will not accept a `parent` on create (older
Server/DC wants a per-instance "Epic Link" custom field), the ticket is still created and
you are **told** the epic link was skipped, rather than it failing or silently ending up
unparented.

**What it deliberately does not do:**

- **It writes once.** A push creates and links. It never transitions, retitles, closes or
  deletes anything afterwards, and a later edit here is not mirrored there. The tracker
  owns that ticket's state from the moment it exists.
- **It does not set status or dates.** Jira workflow transitions are per-instance, so a
  push that tried to match the change's status would silently no-op on an unfamiliar
  workflow. The synced state flows back the normal way instead.
- **It never pushes twice.** A change that already has a ticket is refused with a 409
  naming the ticket it has; unlink first if a second one is really intended. Catch-all
  projects (cost-attribution plumbing) and `ideation` projects are exempt, the same
  exemptions the pending-upload report makes.
- **The PM agent will not push for you.** The prompt tells it to recommend a push and let
  you click: which work is worth filing on a shared board, and when, is a product
  decision. It reports state accurately and names the changes it thinks are ready.

Every push is recorded in the audit log (`roadmap.ticket_pushed` /
`portfolio.epic_pushed`) and requires the `manage_roadmap` capability.

**If a push fails,** the error carries Jira's own words — which is what names an issue
type the project doesn't have, or a required field nobody configured. And if the ticket
was created but could not be linked, the error names the key: a created ticket is never
left with nothing pointing at it.

## Engineering intelligence (`[signals]`)

Optional. Turns on nothing by itself - the `/intelligence` page and its ledger run over
git and whatever `[[trackers]]` are declared, with these defaults - but this is where a
deployment sets the window, the capacity a day is worth, and the calibration. Full
design in [INTELLIGENCE.md](INTELLIGENCE.md).

```toml
[signals]
since = "2024-01-01"              # earliest activity ever pulled (git, Jira, ADO)
capacity_hours_per_day = 8.0      # what one active person-day is worth
timezone = "America/New_York"     # working days and hours are bucketed in this zone
auto_refresh_minutes = 15         # background re-pull cadence; 0 = only the page's button
git_fetch = true                  # fetch every checkout before scanning (all pushed branches count)
extra_repos = []                  # repo-root-relative checkouts beyond the [systems] paths
judge_model = ""                  # empty = the strongest declared model (opus tier)
auto_judge = false                # run the judge after every refresh
ado_pr_projects = ["Proj"]        # ADO projects whose pull requests are pulled (needs the ADO tracker)

[signals.default_projects]        # known tickets planned onto no project land here, by
"ado:Arizona:Ticket" = "6e37cea0" # "<tracker>:<tracker project>[:<raw type>]" -> project id
                                  # (reported as via = "default-project", never as a real link)

[signals.default_repos]           # commits with NO key land here, by repo path or system id
"src/dataflow" = "78aeb8c0"       # (also via = "default-project")

[signals.worklog_trust]           # explicit time per source: "full" books it as hours,
jira = "signal"                   # "signal" (default) keeps it as evidence only, "ignore" drops it
ado = "full"

[signals.thresholds]              # any key from findings.DEFAULT_THRESHOLDS
stale_in_progress_days = 10
unkeyed_commit_pct = 40

[signals.weights]                 # minutes-equivalent per signal kind, for the daily split
commit = 45
comment = 10
```

Credentials are the trackers' own (`email_env` / `token_env`); the layer adds none.
Everything it writes lives under `<workspace>/signals/`: `cache/` (each source's last
result - large, rebuildable, ignore it in git), `feedback.json`, `tuning.json`,
`aliases.json` and `judgments.jsonl` (small, worth committing), `judge/` dossiers and
the `inbox/` drop folder for calendar / email / chat exports. Thresholds edited on the
page and suggestions accepted from the judge land in `tuning.json`, layered over this
table - so the file is the calibration history, and this table the starting point.

## Agent MCP servers (`[agents.mcp.<name>]`)

The MCP servers PM Studio's agents may use are declared **here, and only here**. PM
Studio writes them into a Claude Code `--mcp-config` file for every agent launch and
adds `--strict-mcp-config`, so whatever else an agent's own Claude Code configuration
holds (its isolated `~/.claude.json` under `PM_AGENT_HOME`, a project `.mcp.json`) is
ignored.

```toml
[agents.mcp.playwright]                 # name: letters, digits, _ and -
command = "npx"                         # a stdio server: command + args
args = ["-y", "@playwright/mcp@latest", "--isolated"]

[agents.mcp.dev-db]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-postgres", "${AGENT_MCP_DEV_DB_URL}"]
env = { PGAPPNAME = "${AGENT_APP_NAME:-pm-studio}" }   # optional extra env for the server
pm = true                               # also give it to the PM agent (default false)

[agents.mcp.docs]
type = "http"                           # or "sse": a remote server, url (+ headers)
url = "https://docs.example.com/mcp"
headers = { Authorization = "Bearer ${DOCS_MCP_TOKEN}" }
```

A server is either `command` (+ `args`, stdio) or `url` (+ `headers`, `type = "http"`
— the default — or `"sse"`); both or neither, an unknown key, a wrong type or any other
key in `[agents]` is fatal at startup.

**Who gets what.**

| Agent | MCP servers |
|---|---|
| dev tasks | every server |
| PM turns | only `pm = true` servers — each also added to that turn's `--allowedTools` as `mcp__<name>` (the PM's own allowlist is otherwise fixed), and named in its prompt as look-up-only |
| judges | none (`--strict-mcp-config` with no config) |

**Variables.** `${VAR}` and `${VAR:-default}` in `command`, `args`, `env` values, `url`
and header values are resolved from the environment when an agent is launched — which
includes the repo's git-ignored `.env`, merged in at startup. Keep credentials there,
never in `config.toml`. A server with a variable that is unset and has no default is
**skipped** (one warning per server, naming the variable, never a value). Resolved values
exist only in the per-launch config file, created readable by your user only in the
system temp directory (never the repo) and deleted when the agent exits.

One variable exists per launch rather than in the environment: `PM_STUDIO_ARTIFACTS_DIR`,
the dev task's artifacts folder (see [Dev task artifacts and quick tasks](#dev-task-artifacts-and-quick-tasks)).
Give it a default so the server still starts for PM turns, which have none:

```toml
[agents.mcp.playwright]
command = "npx"
args = ["-y", "@playwright/mcp@latest", "--isolated", "--output-dir", "${PM_STUDIO_ARTIFACTS_DIR:-.playwright-mcp}"]
```

**Zero install, OS-adapted.** There is no install step: `npx -y <package>` downloads the
package on first use. On Windows, `npx`, `npm`, `pnpm`, `yarn` and `bunx` (bare names)
are run as `cmd /c npx ...`, because they are `.cmd` shims. A stdio server whose command
is not on PATH — no Node.js, so no `npx` — is **unavailable** and left out until it is.

**Changes apply without a restart.** This part of `config.toml` is re-read whenever the
file changed — at every agent launch and on `POST /agents/mcp/reload` — so a running PM
chat gets a newly added `pm = true` server on its next turn (its resumed conversation
just continues). An edit that does not parse keeps the last good list in force and
shows the error in the status. At startup the server logs one line, e.g.
`agent MCP: 2 servers (playwright, dev-db), skipped: docs (DOCS_MCP_TOKEN)`.

| Route | Capability | |
|---|---|---|
| `GET /agents/mcp` | view | `{source, loaded_at, error, servers: [{name, transport, command, url_host, pm, status, missing_env, message}], node: {npx, path}}` — `status` is `ready`, `skipped` (see `missing_env`) or `unavailable`; never values, args or headers |
| `POST /agents/mcp/reload` | manage_local_env | forces a re-read; same payload |
| `GET /agents/auth` | view | the agents' Claude sign-in: `{isolated, config_dir, logged_in, email, auth_method, subscription, error, login_running, login_output}` (`?refresh=true` skips the 60 s cache) |
| `POST /agents/auth/login` | manage_local_env | starts `claude auth login` for the agents' config - a browser sign-in on this machine that finishes by itself; poll `GET /agents/auth` |
| `POST /agents/mcp/{name}/check` | manage_local_env | starts a stdio server once (OS-adapted, stdin held open) and reports whether it is still running after 3 s; does not speak MCP |
| `POST /agents/mcp/{name}/test` | manage_local_env | a real connection test (below): `{name, transport, ok, stage, tools, probe, probe_outcome, error, duration_ms}` |
| `GET /agents/mcp/activity?server=&limit=50` | view | the newest MCP calls agents made, newest first (`limit` up to 500), plus per-server counters: `{entries: [...], servers: {<name>: {calls, errors, consecutive_errors, last_error: {ts, tool, outcome}, last_ok_at, last_call_at, last_ok}}, max_entries}` |

**Probe.** An optional `probe` names one harmless tool call the connection test makes
after the handshake. It is parsed as strictly as the rest of the table (only `tool` and
`args`; `tool` a tool name, `args` a table of JSON-compatible values - no dates) and is
part of the hot-reloaded `[agents]` table. String values in `args` may use `${VAR}`.

```toml
[agents.mcp.dev-db]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-postgres", "${AGENT_MCP_DEV_DB_URL}"]
probe = { tool = "query", args = { sql = "select 1" } }
```

**Connection test.** `POST /agents/mcp/{name}/test` (the **Test** button on the Systems
page) starts the server exactly as agents get it - the same resolved entry, OS-adapted
command and the agents' environment, from the repo root - and speaks MCP over stdio:
`initialize`, `notifications/initialized`, `tools/list` (following `nextCursor`), then
`tools/call` for the `probe` if there is one. Everything must finish within 30 s; the
server's whole process tree is killed afterwards. `stage` says where it stopped (`start`,
`initialize`, `tools`, `probe`; on success the last one run), `tools` lists the tool
names, `probe_outcome` is the probe result's shape only (`"1 row"`, `"ok"`) and `error`
one line (the server's last stderr line when it exits, credentials in URLs and
`password=` pairs blanked). An `http` server gets the same handshake over streamable HTTP
(JSON or SSE answers, `Mcp-Session-Id` carried); `sse` servers are not supported yet
(`ok: null`). A probe call is also recorded in the activity log (source `test`).

**What agents did: step outcomes and the activity log.** Every tool step on a chat's
live progress and on a dev task's card shows its outcome when the result arrives - ✓/✗,
how long it took, and for an MCP call the arguments that are safe to show and what came
back: `Using dev-db: query (select id from rides limit 5)` → `✓ 5 rows · 0.4 s`, or
`✗ read ECONNRESET`. Only a whitelist of arguments is ever shown - SQL (`sql`/`query`/
`statement`, one line, ~140 chars), URLs (`url`/`href`, without credentials or query
values), the `element` a browser acts on, a screenshot's `filename` (base name), a key
press, a table name; typed text, values, passwords, tokens, bodies and scripts never are
(a browser_type step reads "typing into Password field"). Results are never shown or stored,
only their shape. Each MCP call is also appended to `<workspace>/mcp-activity.jsonl`
(ts, session, source `pm`/`task` + task id, server, tool, argument summary, ok, outcome,
duration_ms). The log keeps the newest 2,000 calls (it is trimmed once it passes 2,400,
so an append stays one write), is machine-local and is never committed (it is on the
snapshot's never-commit list and in the `init` .gitignore). The Systems page shows, per
server, "last call: ✓ 2 min ago" or "✗ 5 failed in a row: read ECONNRESET", and a
**Recent calls** list.

**Signing the agents in.** The agents' Claude config (`PM_AGENT_HOME`, or your own) must be
signed in, or every PM chat and dev task fails. The server checks at startup and logs
the result; the Systems page shows it next to these tools with a **Sign in** button, and the
chat checks it when it opens and offers the same button before the first message (a turn
that still fails for lack of a sign-in offers it again). The button opens the browser
sign-in on this machine and reports when it is done. From a terminal:
`python -m pm_studio.agent_env --ensure-login` (a no-op when signed in).

**Only read-only DEV servers belong here.** Agents run these servers unattended, on
every launch, with no human approving each call — dev agents even with bypassed
permissions. A server that can write (a database user with write grants, a tracker that
can transition issues, anything pointed at production) lets an agent change data nobody
asked it to.

## Dev task artifacts and quick tasks

The PM agent plans and dispatches; it never runs anything itself. Two things make that
enough when a stakeholder wants to *see* something.

**Quick tasks.** A dispatch with `"quick": true` is a look-don't-build task: the dev agent
gets a preamble telling it to bring up what it needs (a local environment, a browser),
check, capture evidence and report — no code changes, commits, pushes or PRs — and a
900 s timeout instead of 1800 s. It is the same dev agent with the same powers, gated by
`dispatch_dev_task` and audited (`[quick]` prefix) like any dispatch, and judged like any
task — the judge only runs when something changed. Research sessions cannot dispatch
quick tasks either. The PM's prompt tells it to use one whenever it would otherwise say
"I can't run or screenshot that".

**Artifacts.** Every dev task gets a folder, `<workspace>/current/tasks/<task id>/` (PM
bookkeeping, never committed), named in its prompt and exported as
`PM_STUDIO_ARTIFACTS_DIR`. Files left there are listed on the task record as
`artifacts: [{name, size}]` (at most 50), shown on the task's card in the chat — images
(`.png .jpg .jpeg .gif .webp`) as thumbnails — and named by absolute path in the PM's
completion message, so it can Read an image before reporting. Merge resolutions get no
folder.

| Route | Capability | |
|---|---|---|
| `GET /tasks/{session}/{task}/artifacts/{name}` | view | one artifact; resolved strictly inside that task's folder (no `..`, absolute paths or links out) |
| `POST /sessions/{session}/checkout/{system}` | run_session | checks the system's submodules out into the session's worktree (see below); `{system, status, notes}`, 409 when none could be |

**Code in session worktrees.** A non-default session's worktree holds submodules as empty
folders until they are checked out. A dev task now checks its system's code out *before*
it runs (and before its nested repos' starting state is recorded, so the checkout is never
mistaken for the task's work; a failed checkout is told to the agent and recorded as
`checkout_error`). The PM of such a session gets the checkout curl above in its prompt and
allowlist — scoped to its own session — so it can read code it plans against instead of
reporting empty folders as missing code.

**Stopping.** Anything running can be stopped from the chat page — the PM's turn (Stop on
its "PM is thinking..." row), a dev task (Stop on its card), or the whole session (Stop
everything, in the session strip). A stop kills the agent's whole process tree (its MCP
servers and commands included). A stopped PM turn ends as an error, `Stopped by {who}.`,
recorded in the transcript; a stopped dev task is recorded with status `stopped` (result
`Stopped by {who} after {duration}.`, not judged, and the PM is not woken up to react to
it). Messages still waiting for the PM are discarded with the turn. A session's local
services are stopped but its resources kept. Archiving, terminating, cleaning up or
deleting a session stops it first.

| Route | Capability | |
|---|---|---|
| `POST /chat/{session}/stop` | run_session | stops the PM turn in flight and discards the queued messages; `{session, pm_turn, discarded}` |
| `POST /tasks/{session}/{task}/stop` | dispatch_dev_task | stops one running dev task; `{session, task}` with the task as recorded. 404 unknown task, 409 when not running (or a merge resolution) |
| `POST /sessions/{session}/stop` | run_session | all of the above plus the session's local services; `{session, pm_turn, tasks, services, discarded}`, empty when nothing was running. 404 unknown session |

## `PM_INSTRUCTIONS.md`

Appended to the PM agent's system prompt for every session, framed as
project-specific rules that add to (never replace) the shared behavior. This is the
place for what must not live in the public package:

- enterprise restrictions ("never dispatch work touching `infra/` without an
  explicit stakeholder go-ahead")
- compliance constraints and review requirements
- domain knowledge and conventions
- standing stakeholder decisions

## `DEV_INSTRUCTIONS.md`

Appended to **every** dev-agent dispatch (including merge-conflict resolutions), at
dispatch time — the task record and UI keep showing exactly what the PM wrote.
Build/test conventions and hard restrictions dev agents must always follow:

- "Run `make test` before considering any task done."
- "Never touch files under `secrets/` or commit credentials of any kind."

## `knowledge/`

Private reference docs (any files). The PM's system prompt lists their paths and
tells it to Read the relevant one before making calls in its territory. Keep an
index line for each in your `PROJECT_INDEX.md` too if the stakeholder should find
them.

## Committing `pm_studio_local/`

Commit it to the target repo — it is project configuration, and PM sessions in other
worktrees need it. If parts are too sensitive even for the project repo, keep those
files out via your own `.gitignore` entries and accept that fresh worktrees won't
carry them until the session syncs from a checkout that has them.
