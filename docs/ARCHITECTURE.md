# Architecture Specification

> The engineering-intelligence layer (`pm_studio/signals/`, the `/intelligence` page)
> is specified separately in [INTELLIGENCE.md](INTELLIGENCE.md): a ledger of every
> commit, ticket transition, worklog and agent turn, attributed up the work model below
> and judged by an independent read-only agent. It reads the stores described here and
> writes none of them.

Complete technical spec of PM Studio (~6,100 lines of Python + 11 static HTML pages):
the package `pm_studio/` with modules `config.py`, `models.py`, `gitsnapshot.py`,
`roadmap.py`, `portfolio.py`, `tasks.py`, `judge.py`, `agent.py`, `sessions.py`, `accounts.py`,
`authz.py`, `costing.py`, `mailer.py`, `server.py`, `scaffold.py`, `__main__.py`, and
`static/` (11 pages). Dependencies: `fastapi`, `uvicorn`, the `claude` CLI on PATH,
`git` — no database, and no third-party crypto or ORM: everything is JSON files + git,
with password hashing and tokens from the standard library.

The enterprise layer (`accounts.py`, `authz.py`, `mailer.py`, `costing.py`) is inert
unless `[enterprise] mode = "enterprise"` is set, and the work model (`portfolio.py`)
is additive — a deployment that ignores both behaves exactly as the sections below
describe.

Deployment model: the package is installed (pinned) into any git repo — the "target
repo" — and run from that repo's root. Everything project-specific lives in the target
repo under `pm_studio_local/` (see CONFIGURATION.md); `<workspace_root>` below is the
config's `workspace_root` (default `pm_studio`, i.e. runtime state under
`pm_studio/workspace/` in the target repo). The package itself is identical for every
deployment and is never edited locally.

## -1. `config.py` — per-deployment configuration

`load_config(repo_root=cwd)` reads `pm_studio_local/config.toml` plus the local
instruction fragments into a frozen `Config` (see CONFIGURATION.md for the file
format). Loaded once at import as `config.CONFIG` — one process serves one repo.
Everything below that used to be hardcoded (products, port, workspace paths, repo
layout text, default session name, model list) resolves through it. Local prompt
fragments are **append-only**: they are added after the shared prompts and can never
replace them.

## 0. Core concepts

- **Stakeholder** — the one human. Talks to PMs via web chat; owns product decisions.
- **PM session** — one persistent PM conversation bound to one git checkout. The
  **default session** (id `"default"`) is pinned to the primary checkout on `main`.
  Every other session gets its own **git worktree + branch** (`session/<8hex>`) off
  main, created at `<workspace_root>/workspace/sessions/<8hex>`.
- **PM agent** — a resumable headless Claude conversation (`claude -p ... --resume`)
  with a restrictive tool allowlist. It plans, writes specs/docs, and dispatches dev
  tasks over HTTP (curl) to the server it is itself hosted by.
- **Dev task** — a fire-and-forget headless Claude run with full permissions
  (`--permission-mode bypassPermissions`), cwd = the session's worktree root. It builds
  product code. Its completion auto-re-invokes the PM.
- **Workspace** — `<workspace_root>/workspace/` inside each checkout: PM bookkeeping only
  (`current/` live spec+chat+tasks, `archive/`, `sessions/` worktrees, `sessions.json`
  registry, `roadmap/` board data). Product code never goes there.

## 1. `models.py` — model allow-list

```python
MODELS: dict[str, str] = { "<model-id>": "<Label>", ... }   # e.g. opus/sonnet/haiku tiers
DEFAULT_MODEL = "<strongest model id>"
def validate_model(model) -> str   # raises ValueError on unknown
def list_models() -> list[{"id","label"}]
```
Single source of truth shared by session validation/persistence, the live runtime, and
`GET /models`. Default the PM to the strongest allowed model; dev tasks use the
session's model too. (Reference used Opus as default with Sonnet/Haiku selectable.)

## 2. `gitsnapshot.py` — snapshot commits

```python
ARCHIVE_PATH = "<workspace_root>/workspace/archive"
def snapshot(message: str, repo_root: Path) -> None
```
`git add -A`; if `git diff --cached --quiet` says nothing staged, return; else
`git commit -m <message>`. **Never raises** — print a stderr note and continue; a git
hiccup must never break a PM turn or dev task. Snapshots are repo-wide; `.gitignore`
(see SAMPLES.md) is what keeps runtime state out of them.

## 3. `roadmap.py` — product roadmap store

- `PRODUCTS: dict[str, str]` — ordered product taxonomy (ids → display labels),
  customer-facing products first. **Project-specific — from the `[products]` table in
  `pm_studio_local/config.toml`** (declaration order = display order, each parent
  laid out depth first so each product is followed by its own descendants).
- `PRODUCT_PARENTS: dict[str, str]` — the hierarchy, child id → parent id, for children
  only; empty on a flat taxonomy. Any depth: a child may itself be a parent. Validated
  fatally at config load (`config._parse_products`) for unknown, self-referential and
  **cyclic** parents — a cycle would leave its products reachable from no top-level
  product, so they would drop out of the taxonomy while their boards sat on disk. Held
  apart from `PRODUCTS` so the taxonomy stays one dict to iterate and every "is this a
  real product id" check is unchanged.
- Tree helpers, all of which answer the pre-hierarchy answer on a flat taxonomy:
  `parent_of` and `children_of` (one hop), `top_level_products`, `product_label`,
  `ancestors_of` (nearest parent first), `product_path_label` (the full path — "Web App /
  Auth & Identity / SSO"), and `subtree_products(p)` — `p` then its descendants at every
  depth. `subtree_products` is the **unit of ownership**: it decides what a pinned session
  sees at full depth, what the awareness digest must leave out, and which boards
  `agent.py` grants PATCH on. Both recursive walks carry a `seen` guard even though config
  refuses cycles: they run inside a PM's turn and the board's render, where a hang is a
  worse failure than a short answer. Nothing stores the hierarchy — a change carries only
  its product id — so re-parenting, at any depth, is a config edit, never a migration.
- `SYSTEMS: dict[str, SystemSpec]` — the ordered **system** taxonomy from the `[systems]`
  table: the bounded pieces of technology a change is contained within (`label`, plus
  optional `path`, `repo`, `guidance`, `gitflow`, `pipelines`). Distinct from a product in kind, not
  in level: a product is business-facing and owns a board; a system is code and owns
  none. `PRODUCT_SYSTEMS: dict[str, tuple[str, ...]]` is the declared many-to-many edge
  from the product's side; `products_of_system()` derives the reverse rather than storing
  it, so the two can never disagree. `systems_declared()` is the single switch every
  caller asks — an empty table keeps the whole layer dormant and every behaviour below
  pre-system. `TRANSITIONAL_IDS` holds ids declared as **both** a product and a system,
  the explicit temporary state of reclassifying one into the other (its product board
  stays live so its changes can be re-homed; see CONFIGURATION.md § Systems).
- `requires_system(product)` scopes attribution **per product**: a product is in scope
  once it declares what it touches, and untouched until then. This is what makes the
  layer adoptable one product at a time — declaring `[systems]` is deployment-wide, but a
  taxonomy is not migrated in one sitting, and requiring attribution everywhere the
  moment the first system exists would force every board to attribute to whichever
  systems happened to be declared yet: wrong data instead of missing data.
  `products_missing_systems()` lists those still outside, so "not yet declared" stays
  visible rather than becoming a silent permanent exemption.
- `validate_system(product, system, *, required)` is the one place attribution is
  decided: no `[systems]` means the only valid value is none at all; a system is required
  only where `requires_system` holds; and a system that *is* named must be declared and
  touched by the product — except on an out-of-scope product, which accepts any declared
  system (permissive, not refused). There is deliberately **no** `""`-clears convention,
  unlike `owner`/`project_id`: a change cannot go back to having no system.
  `unattributed_report()` counts the in-scope changes that still lack one — reported,
  never blocked, the same call `portfolio.unaligned_report()` makes — and reports
  out-of-scope ones separately as `not_yet_in_scope`.
- `RoadmapItem` dataclass: `id` (8-hex), `product`, `system: str|None`, `title`, `description`,
  `bucket` ("now"|"next"|"later"), `status` ("pending"|"in_progress"|"done"),
  `origin_product`, `triaged: bool`, `created_at`, `updated_at`,
  `shipped_at: float|None` (set when status flips to done, cleared otherwise),
  `owner: str|None` — free-text name of the person/team doing the work when it is
  **external** (not built through this system's dev agents). Additive with default
  None so pre-owner JSON loads unchanged. An owned item is tracked on the board and
  flagged to PMs as `[EXTERNAL - owned by <owner>: track it, never dispatch dev work
  for it]` in the deep context (and `(external: <owner>)` in the shallow digest);
  PATCH `owner: ""` clears it back to built-here, `owner: null`/absent = no change.
- **The schedule: `start_at` / `target_at`, both `str|None`, both `"YYYY-MM-DD"`.**
  Stored as calendar-date **strings**, unlike `created_at`/`updated_at`/`shipped_at`,
  which are epoch floats — and that split is deliberate, not an inconsistency to tidy
  away. Those three are *instants*; a start or a target is a *day somebody committed to*.
  As an epoch, a target of `2026-09-30` is really midnight in some zone and renders as
  the 29th for every reader west of it. (The board's JS reads them with
  `new Date(y, m-1, d)` for the same reason — `new Date("2026-09-30")` parses as UTC.)
  Both are independently optional and all four combinations are legal: a target with no
  start is a milestone, a start with no target is open-ended work, and **neither is the
  normal state** — this board plans in horizons first, and dates are the sharper tool you
  reach for when a change has a real commitment behind it.
  `parse_date()` normalises on write (`""` clears, `null`/absent = no change, anything
  else must match `^\d{4}-\d{2}-\d{2}$` *and* be a real date — the regex is not redundant
  with `date.fromisoformat`, which since 3.11 also accepts `20260930` and full
  datetimes). `_check_order()` runs against the **resulting** pair, so a PATCH setting
  only `start_at` cannot invert an order the item already had, and both dates are
  resolved *before* any field is written so a rejected schedule leaves the item
  completely untouched rather than half-applied. Both surface as HTTP 400 with the
  reason.
- **`is_overdue` is derived, never stored** — `target_at` in the past and `status !=
  "done"`. Hence the `to_dict()` / `to_public_dict()` split (same convention as
  `portfolio.py`'s Initiative/Project): `to_dict()` is the stored shape and round-trips
  through `from_dict`; `to_public_dict()` adds `is_overdue` and is what every read, every
  API response and every websocket `roadmap_item_upserted` payload hands out. A stored
  flag would be wrong by morning — and would break `from_dict`. Shipping ends overdue but
  **keeps** `target_at`, which is what lets the timeline draw the slip.
- `RoadmapStore`: **server-owned** state — one JSON file per product under
  `<workspace_root>/workspace/roadmap/`, git-ignored, read/written ONLY by the always-running
  server process, never per-worktree. Rationale: every session is its own worktree and
  would otherwise see a stale copy; routing all reads/writes through one in-process
  store (reached from PMs via `curl`) means every session sees the same board.
  Thread-lock around mutations; subscriber/broadcast pattern for websocket fan-out.
- **Triage semantics (critical):** `triaged` is *derived on create*, never taken from
  caller input: `triaged = (origin_product == product)`. A cross-product suggestion
  always lands untriaged, whatever the payload says — a PM cannot stamp its own
  suggestion as pre-accepted on someone else's board.
- `move(item_id, to_product, triaged=False, ...)` — reassigns in place, keeping
  id/created_at. Sets `origin_product` to the product it moved FROM; lands untriaged by
  default (PM-initiated moves still need the destination's review; the board UI passes
  `triaged=True` for stakeholder-initiated moves). Emits **two** events — a delete on
  the old product and an upsert on the new — so clients need no new message type.
- Context builders for PM turn injection:
  - `describe_own_product(product)` — full depth, open items only (status != done),
    with `[UNTRIAGED suggestion from <origin> - accept or drop it]` flags and the
    schedule as `[starts <date>, target <date>, <n>d left]`, or
    `[OVERDUE - target was <date>, <n>d ago]`. The day count is spelled out rather than
    left as a bare date the model has to difference against today itself. Covers the
    product's whole **subtree** at any depth — a parent's session gets every descendant
    board at the same depth under its own heading (`<Label> (sub-product of <its own
    parent>, id \`x\`)`, so a grandchild names the board it actually hangs off), since a
    parent PM owns the family; a leaf gets exactly what it always got.
  - `describe_other_products(exclude, exclude_item_ids=None)` — one line per product:
    `- <Path label>: [bucket/status] Title; [bucket/status] Title; ...`, open items only.
    Excludes the **whole subtree** of `exclude`, not just that one product, so nothing
    covered at full depth comes back a second time as a one-liner. `exclude` accepts a
    list, for a session owning several unrelated boards. `exclude_item_ids` applies the
    same rule one level down, to individual changes — an initiative's changes are read at
    full depth wherever they live, including on boards the session does not own, and
    without this each would reappear as a one-liner immediately below; a board with
    nothing left to report drops out entirely. Carries `(OVERDUE)` but not the dates
    themselves: this view is awareness, and another product's slipping date is worth
    knowing where its exact start is noise.
  - `describe_initiative(heading, groups, ticket_lookup)` — the **other lens**, for an
    initiative-scoped session: full depth grouped `Project → Change`, every change naming
    its own board (`[on <Path label>'s board]`), because in this view consecutive changes
    routinely sit on different ones. `groups` is assembled by the caller
    (`server._initiative_context`) since it joins portfolio knowledge with roadmap
    knowledge and neither store may reach into the other.
- `owned_subtrees(products)` — the union of several products' subtrees in display order,
  each named once, unknown ids dropped. The generalization of `subtree_products` for a
  session that owns more than one root (§6).

## 3b. `portfolio.py` — the work model above the roadmap

Optional layer that gives roadmap items a strategy chain. A deployment that ignores it
behaves exactly as before: every field is additive and defaults to "unset".

```
Goals  ⇄  Initiative  →  Project  →  Change
                                       ├─ belongs to exactly ONE Product  (its board)
                                       └─ belongs to exactly ONE System   (its code)
```

- A **Change** is the existing `RoadmapItem` — no new concept. It gains a single
  `project_id: str|None`, additive with default None so pre-existing boards load
  unchanged.
- A **Project** belongs to exactly one Initiative. `initiative_id = None` is the
  **unaligned** state: permitted (nobody is blocked mid-work) but reported.
- An **Initiative** has many Projects and may serve **several Goals** — the only
  many-to-many relationship in the model.
- **Products are not a level.** Product hangs off the Change. That is what makes the
  cross-cutting relationships free: an initiative spans products because its projects'
  changes each carry their own product, and a product appears in many initiatives for
  the same reason. No association records exist or are needed. It is also what lets a
  *session* be scoped to an initiative rather than pinned to a product (§6): the set of
  products an initiative touches is **derived** from its changes, so it can be discovered
  as the work goes.
- **Systems are not a level either**, and for the same reason: `system` hangs off the
  Change, additive with default None. A **system** is the bounded piece of code a change
  is contained within; a **product** is the business-facing thing built on several of
  them. The `product ⇄ system` edge is therefore many-to-many and needs no association
  records — it is declared once per product (`systems = [...]`) and the reverse is
  derived (`roadmap.products_of_system`). Systems own **no roadmap**: a change's home is
  its product's board, and system-shaped work (infra, performance) is an *initiative*.
  See `docs/CONFIGURATION.md` § Systems.
- Goals and Initiatives each carry a `status` of `"open"|"closed"` plus a `closed_at`
  stamp. Projects carry a third, pre-delivery state: `"ideation"|"open"|"closed"`.
  Ideation is a **declared phase, not an activity signal** — it says "the absence of
  changes here is expected, not neglect", so the board stops reading an idea being
  researched as a dead project. Its liveness comes from the session-activity log
  instead (`costing.project_activity`): session turns attributed to the project are
  the evidence someone is working the idea, since changes don't exist yet by
  definition. Graduating to `open` is an explicit act, and the moment a project earns
  its epic (`pending_upload_report` exempts ideation). Initiatives are never *stored*
  as in-ideation — `initiative_in_ideation` derives it (true when every non-catch-all,
  non-closed project under it is in ideation; vacuously true with none yet; never for
  maintenance or closed initiatives), so one project graduating flips the initiative
  by itself and the two can never drift. Products and Systems are persistent (they
  come from config); everything in the chain is temporary.

### Why single-parent below the initiative

It makes cost attribution unambiguous. `Change → Project → Initiative` is a unique
path, so those totals are exact and additive. **The additive tree stops at
Initiative**: because an initiative can serve several goals, the same spend
legitimately contributes to more than one goal, so goal-level figures overlap and must
never be summed into a grand total. The API reflects this — `rollup_path(project_id)`
returns only project + initiative, and `goal_ids_for_initiative()` is a deliberately
separate call so goals cannot be folded in by accident.

**Systems carry the same warning as goals.** `Change → System` is single-parent, so a
per-system count or total is exact. But a system is touched by several products, so
system figures grouped by product overlap and must never be summed across them — the
same system would be counted once per product. `roadmap.system_rollup()` returns
per-system rows only, and does no product-level aggregation, for exactly this reason.

### The maintenance scaffold

Two rules interact: goals and initiatives are never auto-created, but the catch-all
project needs a parent. So `ensure_maintenance_scaffold()` (exposed as
`POST /portfolio/bootstrap`, and prompted for on the portfolio page when no catch-all
exists) declares the trio once, explicitly, with deployment-chosen names. From then on
a change created with no `project_id` lands in the catch-all instead of floating. The
catch-all project and its maintenance initiative cannot be closed or deleted.

### Per-initiative catch-alls

The global catch-all hangs off the **maintenance** initiative, so it is the wrong home for
an initiative-scoped session that has no project of its own: falling through to it would
bill that session's every turn against maintenance, silently, in the one report that exists
to answer "what did this initiative cost". So `ensure_initiative_catch_all(initiative_id)`
creates one catch-all per initiative on demand (when a session is pinned to it — see
`POST /sessions/{id}/initiative`), marked by `Project.catch_all_for_initiative` rather than
by title, so a rename cannot orphan the attribution.

Attribution stays a **real project** rather than being recorded one level up at the
initiative, because the additive tree's exactness rests on `Change → Project → Initiative`
being a unique path; recording above Project would leave the by-project rollup with a hole
the by-initiative totals don't have.

Two guards follow: an initiative's own catch-all cannot be deleted on its own (it goes with
the initiative), and it does **not** count as a reason to refuse deleting that initiative —
otherwise every initiative that ever hosted a scoped session would be undeletable, blocked
by a project nobody created. A declared project still refuses, as before.

`PortfolioStore` mirrors `RoadmapStore`'s conventions: one server-owned JSON file at
`<workspace_root>/workspace/portfolio.json`, single lock around mutations, subscriber
fan-out. Its events ride the roadmap websocket, since anything watching the board
cares when a project is re-parented. Writes go through a temp file and `replace()`.

### The two pivots

One dataset, two lenses:

- **By product** — `GET /roadmap/data`, the original shape, unchanged. Now also carries
  an additive `portfolio` block (initiatives, projects, catch-all id) so the board page
  can re-group locally as websocket events arrive instead of refetching on every change
  made anywhere.
- **By initiative** — `GET /roadmap/by-initiative`, returning
  `groups: [{initiative, projects: [{project, changes}]}]`, produced by
  `PortfolioStore.group_changes_by_initiative(changes)`. The changes are passed *in* so
  the function is pure with respect to the roadmap store and testable on its own.

The invariant is that switching lens never loses or duplicates a change. The
per-product board can always show everything, since every change has a product; the
initiative lens has two ways to fall off the tree, and both are caught:

- a project with `initiative_id = None` → appears under a group with `initiative: null`
- a change with `project_id = None`, **or pointing at a deleted project** → appears
  under that same group with `project: null`

Both land in **one** trailing group, not two, so the UI renders a single "Unaligned"
heading. An initiative with no changes still appears — "nothing is happening on this"
is information, not a reason to hide it.

The board page mirrors this grouping in JS for live updates; `portfolio_changed` events
trigger a refetch, since a re-parented project changes how the board groups without
changing any item.

### Referential guards

Deleting is refused rather than silently cascading: a goal an initiative still serves,
an initiative that still has projects, a project that still has changes. The change
count is passed *in* by the server (`roadmap_store.count_by_project`) so this module
never reaches across into the roadmap store. An initiative a live (non-archived) session
is still scoped to is refused too — checked in `server.py`, since the portfolio has no
business knowing sessions exist, and the sessions are **named** in the error, because a
session losing its scope mid-conversation is something the person clicking delete should
decide knowingly.

### Session scope report

`_session_scope_report()` (on `GET /portfolio/data`, rendered as a banner on the portfolio
page) reports sessions whose scope has drifted. Never blocking — the same choice the model
makes about an unaligned project — but never silent either, because each of these makes a
cost figure quietly mean something other than what it says:

- `missing` — scoped to an initiative that no longer exists.
- `closed` — scoped to one that was closed while the session ran on.
- `mismatch` — has **both** a project and an initiative, and the project belongs to a
  different initiative. Attribution follows the **project** (the more specific statement,
  and the only one the additive rollup can use), so the initiative pin is the part that is
  lying.

## 3c. `trackers.py` — external issue trackers (Jira / Azure DevOps)

**Import routing** (optional, per tracker): `import_types` + `[[trackers.routes]]`
(component → product, optional system) turn catalog tickets into linked changes at the
tail of every sync (`server._import_routed_tickets`). Routes are validated fatally at
load against the product/system taxonomies. Idempotent via the 1:1 link guard —
`linked_ticket_refs` up front, `TicketAlreadyLinked` under the store lock as the race
backstop. Unrouted importable tickets are counted per component and reported on
`GET /trackers` and the board's tracker strip, never guessed onto a catch-all. Tickets
carry `components` (Jira components / ADO area path) in the catalog for this; a cache
written before that field existed loads as "no components".


Owns everything *about* a ticket; `roadmap.py` owns only the link to it.

**The 1:1 link.** A `RoadmapItem` carries `tracker_id` + `ticket_key`, both defaulting to
`None` so pre-existing roadmap JSON loads with no migration. One item holds at most one
ticket by construction; the other direction is enforced in `RoadmapStore.link_ticket`,
which scans for a conflicting holder and raises `TicketAlreadyLinked` (→ HTTP 409) naming
it. The check and the write happen under **one** lock hold, so two concurrent links to the
same ticket cannot both pass. Keys are compared through `normalize_key` (Jira upper-cased,
ADO ids left alone) — imported by `roadmap.py` rather than reimplemented, because the
guarantee is only as good as the definition of "same ticket".

**Why the type is not denormalised onto the item.** The type is the tracker's fact. Copying
it would make every sync rewrite every linked item, and one missed write would leave a card
claiming "Bug" for something converted to a Story months ago. So the item stores the
reference, this store holds one entry per ticket, and `server.py`'s `_with_ticket` joins
them at read time — including on websocket events, or a live edit would blank a card's badge
until the next reload.

**Type normalisation.** Tracker type names map onto `CANONICAL_TYPES` (epic, feature, story,
task, bug, spike, subtask, other), which is what picks the badge colour — so ADO's *Product
Backlog Item* and Jira's *Story* colour alike. `raw_type` keeps whatever the tracker said and
is what the UI actually labels, so a renamed or custom type is never relabelled into a lie;
an unrecognised one lands on `other` (neutral colour) rather than being guessed at. The
palette itself lives in roadmap.html, keyed on these slugs, where light/dark are expressible.

**Three properties worth preserving:**

- **The network is one seam.** Every HTTP call goes through the module-level `_request`,
  which the tests replace wholesale — the suite pins the exact URLs, methods, auth headers
  and pagination we send without anyone needing a live Jira.
- **A sync never raises at the caller.** Each tracker's outcome lands in its own
  `SyncStatus`. On failure the previous catalog is deliberately **kept** and the error is
  surfaced in the board header: a stale type beats a card that suddenly claims no ticket.
  Due-ness keys off the last *attempt*, so a failing tracker keeps retrying.
- **Tokens never leave the module.** They go into an `Authorization` header and nowhere
  else. `_scrub` is applied to every error before it is stored, and `describe()` builds its
  payload field by field (never `asdict`) so adding a config field can't leak one.

**Provider shapes.** Jira tries `/rest/api/3/search/jql` (token-paginated, current Cloud)
and falls back to `/rest/api/{3,2}/search` (offset-paginated) on 404/410 only — any other
status surfaces rather than walking the chain. ADO needs two calls by design: WIQL for ids
(single quotes doubled, so a project named `Bob's Team` can't break the literal), then
hydration in batches of 200, a server limit rather than a preference.

**Sync trigger.** Manual only: `POST /trackers/sync` (**Sync now** in the board header).
There is no background poller - an unattended pass rewriting the board on a timer is how
a second or stale instance double-imports tickets, and `sync_interval_minutes` is now a
dead config key. One pass at a time, process-wide: `server._sync_run_lock` refuses an
overlapping run and `TrackerStore.syncing()` is held across *every* pass (fetch, import,
assignment, removal, mirrors), not just the fetch. Across processes, `workspace_lock`
keeps there from being a second server at all. After a catalog pull, linked tickets
outside the configured `projects` are fetched individually, so a one-off dependency on
another team's board resolves instead of rendering unresolved forever.

**Releases (data only, so far).** Each sync also pulls the tracker's release catalog:
Jira project *versions* (the Releases page; paginated `/project/{key}/version` on Cloud,
falling back to the plain `/versions` array on Server/DC) and ADO *iterations* (the
classification-node tree, flattened, root skipped — Boards has no first-class release,
and the classic Release *pipelines* are deployment executions, deliberately out of
scope). Cached as `Release` entries in the same `trackers.json`, served read-only on
`GET /trackers/releases`, counted on `describe()`. Releases fail independently of the
ticket pull (`SyncStatus.release_error`), with the same keep-on-failure posture. Nothing
on the board consumes them yet.

**Push — the one write** (optional, per tracker, Jira only): `[trackers.push]`
(`config.PushConfig`) declares a project and the issue type per rung. `can_push` is target
**and** usable credential, so a declared-but-unauthenticable tracker renders no control
rather than a failing one; a target naming an unsynced project, or a table on an ADO
tracker, is fatal at load. `JiraClient.create_issue` → `TrackerStore.push_ticket` →
`server.push_roadmap_item` / `push_project_epic`, which create, read the issue back into
the catalog and link it in one request. Three properties:

- **It writes once.** Create and link, never update — nothing transitions, retitles or
  deletes a ticket afterwards, which is what keeps "a ticket's state is the tracker's
  fact" true everywhere else in this section.
- **A created key is never lost.** After a successful create nothing may raise without
  naming the key: a read-back failure falls back to a synthesized catalog entry built only
  from what we know (empty state, never a guessed one), and a link failure returns 409
  *with* the key. An orphan ticket nothing points at is the one outcome worse than a
  failed push.
- **The parent is best-effort and reported.** `fields.parent` links a story to its epic on
  Cloud; older instances answer it with a 400 (they want a per-instance Epic Link custom
  field), so the create is retried once unparented and `parent_skipped` says so on the
  response. Only a 400 mentioning `parent` earns the retry — auth and reachability
  failures surface as themselves.

The epic parent comes from the change's **project** link (`_parent_epic_for`), which is why
projects are pushable too: push the epic, then every change under it lands beneath it and
the tracker gets the plan's own shape. Cross-tracker parenting answers None — Jira cannot
express it.

Pushing needs `manage_roadmap` and is audited (`roadmap.ticket_pushed`,
`portfolio.epic_pushed`). The PM prompt splits on `can_push` (`PUSH_GUIDANCE` /
`NO_PUSH_GUIDANCE`): with no pushable tracker it is told upload is unavailable, exactly as
before; with one it is told the control exists **and told not to press it** — the roadmap
POST allowlist does reach the endpoint, so the prompt is the only thing standing between an
agent and a real ticket on someone else's board.

**Read-only otherwise.** Apart from that create, nothing writes to Jira or ADO. Inbound,
the split is per field: `_mirror_ticket_status` makes a linked change's **status** follow
its ticket's state every sync (both directions — a reopened ticket reopens its change,
and `shipped_at` was only ever the import clock anyway), while **bucket** stays this
deployment's plan and is never touched by any pass. Absent-from-catalog, won't-do and
excluded tickets are skipped, so a short sync cannot reopen the board and a declined
ticket is never relabelled as delivered. It is the one pass that does not gate on
`TrackerConfig.imports` — that flag governs manufacturing changes, not keeping linked
ones current, so a catalog-only tracker's hand-linked changes are mirrored too. The PM prompt states the split explicitly.

One rung up, `_mirror_epic_status` does the same for **projects**, and deliberately not
symmetrically: a done epic closes its project, and nothing reopens one. A change's status
was only ever a reading of its ticket; a project's `closed` is a decision carrying a
`closed_at`, sometimes taken ahead of an epic nobody transitioned, so an epic that leaves
done is *reported* (`epic_open_on_closed`) rather than acted on. Gated per tracker by
`close_done_epics`, **default off** — off it counts `projects_closable` and writes nothing,
so a deployment sees the list once before handing a tracker transition that power. Same
four skips as the change mirror plus the catch-all, which `update_project` refuses outright.
Closing can strand open changes and reports the count; those rows stay visible wearing a
"closed" chip, which is what `isHistoryRow` was built to surface.

The pass that would have been wrong to automate is `_close_candidates`, which only
*offers*. A project whose every local change is settled while its epic is still open is a
"ready to close" chip on the board, not a write — because all-imported-done is not
all-done: an epic partly routed onto this deployment reads as finished from the local
roll-up while its unimported children still run. The report names those children's count
(`reason: "blocked"`) rather than offering anything, which is the one fact the board cannot
work out for itself — the page holds no catalog.

**Declined is settled, not outstanding.** `_outstanding_changes` (server) and
`isOutstanding` (board) both discount a change whose ticket is won't-do. The status mirror
refuses to mark those done and the removal pass only deletes untouched ones, so a declined
change a human had filed into now/next keeps `pending` for good — counted as open work it
pinned its project on the board permanently, closed and fully declined and unable to ever
become history. `Ticket.to_dict` carries `is_wont_do` onto the wire so the board reads the
one definition instead of spelling a second one in JavaScript.

The cache at `workspace/trackers.json` is in `SENSITIVE_WORKSPACE_FILES`: it holds no
credential of ours but caches another system's ticket titles, and never committing it costs
exactly one re-sync.

## 3d. `people.py` — who is doing the work

The directory of people, the load view derived from it, and the read-time join that puts an
assignee on every change. Data at `workspace/people.json`.

**Two fields on the ticket, one person in the directory.** A synced `Ticket` carries
`assignee` (the display name), `assignee_key` (Jira `accountId`, ADO `uniqueName`, falling
back to the name when an instance discloses neither) and `assignee_email` where one is
disclosed — all additive, so a catalog written before they existed loads and reports nobody.
`PeopleStore.reconcile` folds every distinct identity a sync saw into one `Person` per human,
matching most-certain-first: the identity already recorded, then the same email, then the
same normalized display name **from a different tracker**. Never within one tracker — two
accounts on one Jira sharing a display name are two people, and merging them would attribute
one person's work to the other. Nobody is ever removed: an assignee who drops out of the sync
window has left the query, not the history. The pass runs first in `_run_tracker_sync`, so a
change the import creates can name its assignee the same cycle.

**A person is not an account.** Most assignees never sign in. `Person.account_id` optionally
links one to an `accounts.User`, which is what lets the costing roster's capacity and rate
apply to the same human; without it a person is simply a name to attribute work to. This is
why the People page and `GET /people/directory` work in personal mode, where the roster half
of that page does not exist.

**Two possible assignees, one answer, both reported.** A change's own `assignee` (a person
id) is this deployment's decision; a linked ticket's assignee is the tracker's.
`effective_assignee` resolves them at read time — local wins, and a disagreement comes back
as `conflict: true` rather than being hidden, because a local reassignment the tracker has
not been told about is the thing somebody has to go and say out loud. `server._with_ticket`
applies it, so the two joins a card needs cannot come apart. Clearing `assignee` on a linked
change hands the answer back to the ticket rather than to nobody.

**Nothing here writes to a tracker.** Assignment is local planning only — the single write
pm_studio makes to Jira/ADO is still `push_ticket`. The PM prompt says so and is told never
to claim a person has been notified.

**Load is derived, never stored.** `workload()` counts *open* changes per person, split by
horizon, with overdue/in-flight counts and the products, systems and tracker areas the work
sits in — because "who has room" and "who knows this area" are two questions and an
assignment needs both. Shipped work is reported separately, never as load. Unassigned work
comes back as its own row (`UNASSIGNED`), which on most boards is the largest one. The
board's own load table is computed client-side from the sections it is already drawing, so an
optimistic assignment cannot leave a stale figure sitting above the card that just moved;
`GET /people/directory` serves the server-side version for the People page.

`workspace/people.json` is in `SENSITIVE_WORKSPACE_FILES`, and is the sharper case of why
that list exists: `trackers.json` caches other people's work, this holds their identity —
real names and email addresses reconciled out of another organisation's tracker. Unstaged
from every snapshot unconditionally, and listed in the scaffolded `.gitignore` as well.

**Repairs.** Reconciliation cannot always be right, so `merge` (two entries, one human) and
`split_identity` (one entry, two humans) exist, both on the People page and both needing
`manage_roadmap` — this is roadmap housekeeping, not access control. A merge re-points local
assignments **before** folding the entries, so a failure between the steps leaves changes
naming the survivor rather than an id that no longer exists. Deleting is refused for anyone a
tracker knows (they return next sync) or anyone still holding work; `inactive` is the honest
retirement, and keeps their name resolving on everything they did.

## 4. `tasks.py` — dev-task registry (one per session)

```python
DEV_AGENT_TIMEOUT_SECONDS = 1800
class TaskRegistry:
    def __init__(session_id, workspace_dir, repo_root, git_lock, model=DEFAULT_MODEL)
```
- Task records are JSON files at `<workspace_dir>/tasks/<id>.json`:
  `{id: 8hex, kind: "dev"|"merge_resolution", description, system, status:
  "running"|"done"|"error"|"stopped", started_at, finished_at, result, head_before, head_after,
  system_repos_before?, system_repos_after?, judge?}` — the `system_repos_*` maps
  (`{rel: {head, dirty}}`) exist only for systems with their own nested repositories
  (see `_system_repo_dirs`).
- `validate_dispatch_system(system)` (module-level, called by the dispatch endpoint) —
  once `[systems]` is declared every dev task must name a declared system (400 naming
  the valid ids otherwise; with no `[systems]`, naming one is refused and omitting it
  is the pre-system dispatch, byte for byte). Attribution is what routes a system's
  `gitflow` rules into the dev agent's prompt, so it is enforced deployment-wide.
- `start_task(description, system="")` — records `head_before` (`git rev-parse HEAD`
  under `git_lock`), writes a `running` record, spawns a **daemon thread**, returns
  immediately. The thread runs `_execute`, then (under `git_lock`) snapshots
  (`Dev task <id> (<status>): <description[:72]>`) and records `head_after` — the
  task's exact commit range — runs the compliance judge if there is anything to judge,
  and only then writes the final record: the `done` notification (which triggers the
  PM's auto-continue) must already carry the verdict.
- `_execute(description, system="")` runs:
  `claude -p <prompt> --output-format json --permission-mode bypassPermissions
  --model <self.model>` with `cwd=repo_root` (the worktree root — product sources live
  there; historically cwd=workspace_dir made agents create code inside the bookkeeping
  folder). The prompt is the description plus, appended at dispatch time (never stored
  in the record), `DEV_INSTRUCTIONS.md` and then — last, so they win on conflict — the
  system's `gitflow` rules, read fresh from this worktree's copy; an unreadable rules
  file refuses the task before any agent spend. Timeout 1800 s. Parse stdout JSON;
  `result = data["result"]`; `is_error = data.get("is_error") or returncode != 0`.
  Handle timeout / CLI-not-found / empty output / non-JSON output as error results with
  the literal evidence (stderr excerpt), never a guess.
- `_judge(...)` — nothing to judge (no system, no `gitflow`, nothing observably
  changed) means no `judge` key at all; rules declared but no usable evidence is a
  visible `inconclusive`, never a silent skip. Otherwise delegates to
  `judge.run_judge`: an independent read-only agent (`--allowedTools` limited to
  Read/Grep/Glob and git inspection subcommands, **no** bypassPermissions) that
  inspects the task's evidence against the rules file — composed entirely server-side,
  the dev agent's own claims never shown — and returns
  `{verdict: "pass"|"violation"|"inconclusive", violations: [{rule, evidence}], summary,
  model, agent_usage}`. Every judge failure (timeout, bad output shape, uncited
  violation) maps to `inconclusive` with the reason: fail loud, never fail open. Runs on
  the Opus tier when the deployment declares one, else the registry's model
  (`judge.judge_model`) - a wrong verdict is expensive in both directions, so the
  verdict gets the strongest model available.
- **Nested repositories.** `_system_repo_dirs(system)` finds the system's OWN repos
  inside the checkout: its `path` when that is itself a git repo, else the path's
  immediate children that are (a bounded, predictable rule — never a deep scan). When
  the system has them, THEY are where the rules apply: each repo the task touched gets
  its own judge run **inside that repo** (`cwd` = the nested repo, so the read-only git
  allowlist works unchanged) — a commit range when its HEAD moved, the dirty working
  tree when the agent left work uncommitted (the deployment snapshot cannot sweep
  nested work into a commit, so that state would otherwise be invisible; the record's
  `dirty` flags exist for exactly this). Multiple verdicts fold worst-wins via
  `judge.merge_verdicts` (evidence prefixed `[rel]`, spends summed). The deployment
  repo is deliberately NOT judged alongside — its only change is the registry's
  gitlink-bump snapshot, and judging bookkeeping against a system's branching rules
  manufactures false violations — but stays the evidence when no nested repo was
  touched. The nested evidence prompt drops the snapshot-commit caveat (no snapshots
  exist there; keeping it would hand the agent an exemption to hide behind). A session
  worktree materializes gitlinks as empty directories, so discovery returns nothing
  there and judging falls back to the root range — nested-repo systems are effectively
  a default-session (primary checkout) affair.
- `run_conflict_resolution(description)` — same `_execute`, but **synchronous**, id
  prefixed `merge-`, `kind="merge_resolution"`, and **no snapshot commit** (the merge
  flow verifies and commits or aborts explicitly).
- `_reconcile_stale_tasks()` on construction (i.e. server startup): any record still
  `running` on disk cannot actually be running (its thread died with the old process) —
  flip to `error`, `finished_at=now`, result = "Interrupted: the PM Studio server
  restarted while this task was still running… If this was a merge-conflict resolution,
  check `git status` by hand."
- **Stopping.** `start_task` keeps each task's thread, and `_execute` (with
  `stoppable_id`) passes `on_start` to `run_agent`, so the registry holds the running
  `claude` process per task id. `stop_task(id, who)` / `stop_all(who)` record who asked,
  kill the process tree (`agent.kill_process_tree`; a stop that comes before the process
  exists kills it from `on_start`) and wait up to `STOP_WAIT_SECONDS` (15) for the thread
  to write the final record: status `stopped`, result `Stopped by {who} after
  {duration}.`, the snapshot as usual, **no judge**. Merge resolutions are not stoppable
  from outside (the merge flow owns them). `_reconcile_stale_tasks` only touches
  `running`, so a `stopped` record is final like `done`/`error`.
- Subscriber pattern: `subscribe(cb)`; every `_write_task` notifies. The server wires
  this to websocket broadcast + PM auto-continue (done/error only — never `stopped`).
- `model` is a plain attribute read fresh per dispatch, so live model changes apply to
  the next task.

## 5. `agent.py` — the PM agent

Constants: `PM_TIMEOUT_SECONDS = 1800`, `MAX_AUTO_CONTINUE_STREAK = 6`,
`MAX_ATTACHMENTS_PER_TURN = 6`, `MAX_ATTACHMENT_BYTES = 10 MB`, image mime→extension map
(png/jpeg/gif/webp).

### Paths (per session)
- `repo_root` = the session's worktree path.
- `workspace_dir` = `<repo_root>/<workspace_root>/workspace/current/`; inside it: `SPEC.md`,
  `chat_history.json`, `pending_messages.json` (the durable chat queue — see below),
  `pm_session_id.txt` (the resumable Claude CLI session id — distinct from the app-level
  session id), `uploads/`.
- `archive_dir` = `<repo_root>/<workspace_root>/workspace/archive/<session_id>/`.
- `PROJECT_STATUS.md` and `PROJECT_INDEX.md` at the **repo root** — deliberately outside
  `workspace/current/` so a reset can never wipe them; root-level so the stakeholder
  finds them without digging.

### System prompt & allowlist
Built per session from the templates in PROMPTS.md, with these session-scoped URLs baked
in: `tasks_base_url = http://<host>:<port>/tasks/<session_id>`,
`session_meta_url = http://<host>:<port>/sessions/<session_id>/meta`,
`scope_url = http://<host>:<port>/sessions/<session_id>/scope`. The allowlist is
the security model — literal prefix matching means a PM structurally cannot touch
another session or a board outside its own ownership:

```
Bash(curl -s -X POST {tasks_base_url}*)
Bash(curl -s {tasks_base_url}*)
Bash(curl -s -X POST {session_meta_url}*)
# initiative-scoped sessions only:
Bash(curl -s -X POST {scope_url}*)                  # adopt/release a board for ITSELF
# sessions that own a board, OR are initiative-scoped:
Bash(curl -s -X POST {ROADMAP_BASE_URL}/*)          # POST broad: cross-product handoff is intended
# one entry per OWNED board (Session.owned_products()):
Bash(curl -s -X PATCH {ROADMAP_BASE_URL}/{owned}/*)
Write Read WebSearch WebFetch
```

Note the POST grant: an initiative-scoped session that owns nothing yet still gets it,
because suggesting work on another board is its only way to reach one, and the prompt
promises exactly that. The default session (no product, no initiative) gets neither POST
nor PATCH, as before.

`_refresh_scope()` builds the prompt and the allowlist **together**, from one
`owned_products()` call, and `set_scope(initiative_id, adopted_products)` re-runs it on a
live agent. They are two statements of one rule: building them apart is how a PM ends up
told it owns a board it cannot write to, or handed one it was never told about.

An initiative-scoped session's prompt gets `INITIATIVE_GUIDANCE_TEMPLATE` prepended,
including one line that changes as it widens — `NO_BOARDS_OWNED_YET` (stated as the
correct starting state, not a limit to route around) or `BOARDS_OWNED_TEMPLATE` listing
what it has adopted. The initiative's own title, description and goals are deliberately
**not** baked into the prompt: they arrive in the per-turn context block, which is rebuilt
from the store every turn, so a rename cannot go stale here.

### Turn execution
`_run_pm_turn(text)` runs:
`claude -p --output-format stream-json --verbose --system-prompt-file <sys> --allowedTools
<allowlist> --model <model> [--resume <claude_session_id>]` with `text` on stdin,
`cwd=repo_root` (NOT workspace_dir — relative paths must resolve against real sources),
through `_stream_claude`, which reads the JSON lines as they arrive. Every assistant text
block and tool call becomes a `{"type":"pm_progress","kind":"text"|"tool","text":...}`
event (`progress_events`; tool calls get a label built from what they are for, never the
raw command, which carries the agent token; an MCP call's label adds a sanitized argument
summary - SQL, URL, element, file name, never typed text or secrets). Tool calls carry
their `id` (the tool_use_id), and a `ProgressTracker` per turn - also used by dev tasks,
whose record's `progress` steps get updated in place - matches each later `tool_result`
to its step and emits `{"type":"pm_progress","kind":"result","id","ok","outcome",
"duration_ms"}`: the outcome is the error's first line on failure, an MCP result's shape
only ("12 rows", "ok") on success, never its contents. Each MCP call is also appended to
`<workspace>/mcp-activity.jsonl` (`mcp_activity.py`). The final `{"type":"result"}` line has the
fields `--output-format json` used to print: save its `session_id` for resume and return
`{"type":"pm_reply","text":result}` or `{"type":"pm_error","message":...}` (with literal
stderr/exit evidence on failure).

`_run_turn(...)` wraps it: sets `turn_active=True` + fires `on_activity_change` (in a
try/finally so it always clears), yields `{"type":"pm_working"}` first, takes `pm_lock`
(serializes manual turns vs. auto-continuations — two concurrent `--resume` calls on one
session id would race), runs the turn on a worker thread and yields its `pm_progress`
events live (never persisted), appends the reply to `chat_history.json`, snapshots under
`git_lock` (`PM turn: <text[:72]>` / `PM auto-continue after dev task <id> (<status>)`),
yields the final event. The chat folds the progress into a "Worked through N steps" row.

The roadmap context block (`_with_roadmap_context`) is sent in full only when the
conversation has none to build on — a fresh or reset conversation, the first turn after a
restart, the turn after a failed one — and every `ROADMAP_FULL_EVERY` (8) turns; otherwise
a turn carries only the changed lines (`roadmap_delta`) or a one-line "unchanged". A
resumed conversation keeps every block it was ever sent, so re-sending the whole roadmap
each turn made every turn slower and costlier than the last.

`claude -p --resume` keeps the system prompt a conversation **started** with and ignores
the `--system-prompt-file` of later turns. So `_with_current_instructions` re-states the
full current prompt at the top of a resumed turn whenever it differs from the last one
this process sent (always on the first turn after a restart, which also minted a new
agent token — without this every curl in a resumed conversation would 401), marked as
replacing the original. Scope, mode and MCP changes reach a running conversation the same
way. A failed turn clears the record, so the next one re-states again.

### Stopping a turn
`_stream_claude(..., on_start=)` hands the process to `on_start` right after `Popen`, and
starts it in its own process group (`local_env._group_kwargs`), so
`kill_process_tree` (`local_env._kill_tree`: `taskkill /PID <pid> /T /F` on Windows,
`killpg` SIGTERM→SIGKILL elsewhere) reaches the MCP servers and commands it spawned — the
timeout kill uses it too. A PM turn's `on_start` is `_on_turn_start`, which keeps the
process in `_turn_proc`. `stop_turn(who)` (only while `turn_active`) sets `_stop_reason =
"Stopped by <who>."` and kills the tree; a turn with no process yet is killed by
`_on_turn_start` the moment it gets one. `_run_pm_turn` then returns
`{"type":"pm_error","message":_stop_reason}`, which `_run_turn` records like any failed
turn; the reason is cleared there and in `_run_turn`'s `finally`, so a late stop never
carries into the next turn. `stop_turn` waits (≤ `STOP_WAIT_SECONDS`) for the turn to
record itself. Shutdown (`stop_running_turns`) tree-kills every `claude` process
`_stream_claude` is running — PM turns and dev tasks — after `stop_all` has marked each
session's running dev tasks stopped: the process groups are out of Ctrl+C's reach.

### Transcript
`chat_history.json` — a list of `{role, text, ts, attachments?}` with roles
`user | pm | error | system` and epoch-seconds `ts` stamped by `_append_history` as
each entry is written (this is what lets the chat page interleave chat + dev-task
cards into one chronological timeline on reload). Keep `ts` additive/backward-
compatible: legacy entries without it must still load, treated as untimestamped.
`last_message_role()` normalizes pm/error → "assistant" for the waiting-state
computation.

### Pending queue
`pending_messages.json` — a list of `{id, text, attachments[], ts}`: messages accepted
but not yet run. `enqueue_pending(text, attachments)` writes one the moment it arrives,
**decoding its images to `uploads/` there** rather than when the turn starts, so a queued
attachment survives a restart like its text does; `handle_user_message` therefore takes
attachment *filenames*, not base64. `drop_pending(id)` removes it as its turn begins.
Guarded by `pending_lock`, not `pm_lock`: it is written from the event loop thread
(message arriving) and a worker thread (turn starting).

**A message is in exactly one of the two files**, with one deliberate exception: `_run_turn`
writes the transcript entry *first*, stamped with the queue record's `id`, and drops the
queue record *second*. A reader caught between them sees it twice and dedupes on that id —
the alternative ordering would show it zero times, which is the failure that actually loses
a message. `reset()` clears the queue file along with the transcript: a message that never
ran belongs to the conversation being cleared.

### Two entry points
- `handle_user_message(text, attachments, other_sessions_context, roadmap_context,
  pending_id)` — resets `_auto_continue_streak = 0`; `attachments` are filenames already
  written under `uploads/` by `enqueue_pending` (base64 → files, skipping bad/oversized
  ones silently), pointed at from a "[The stakeholder attached N image(s)… use your Read
  tool…]" block; prepends the two context blocks (see PROMPTS.md §4 — the blocks go only
  into the prompt, never into persisted history: per-turn plumbing, not conversation);
  `pending_id` is the queue record this turn came from.
- `handle_task_completion(task, ...)` — increments the streak; builds the auto-continue
  prompt (normal or at-cap variant, PROMPTS.md §3); history entry is a **system** role
  label `Dev task <id> finished (<status>): <description[:100]>`.

### Reset & archive
- `archive_current(reason)` — copy (never delete) SPEC.md, chat_history.json, uploads/
  into `archive/<session_id>/<UTC yyyymmddThhmmssZ>_<reason>/`.
- `reset()` — under `git_lock`: archive with reason "reset", delete SPEC/chat/pending
  queue/session pointer/uploads, `claude_session_id = None`, snapshot ("PM session <id> reset (prior
  spec/chat archived)"). PROJECT_STATUS/INDEX/docs untouched by construction.
- `_ensure_project_status_seed()` on init — create PROJECT_STATUS.md with an honest
  "No project history yet… proceed to discovery." if missing, so the system prompt's
  read instruction never 404s.

## 6. `sessions.py` — session registry & lifecycle

### Data
`Session` dataclass: `id`, `name`, `branch`, `worktree_path: str|None`,
`base_branch: str|None`, `created_at`, `status`, `is_default`,
`merge_result: dict|None`, `sync_result: dict|None` (separate — a failed sync never
changes status), `product: str|None`, `archived: bool` (visibility flag, independent of
lifecycle), `model` (defaults so old records load), `title: str|None`,
`goal: str|None` (PM-maintained, nullable so old records fall back to `name`),
`project_id: str|None` (which Project its activity is attributed to),
`initiative_id: str|None` and `adopted_products: list[str]` (see below).

### Scope: two orthogonal axes

A session's scope is two independent things, and keeping them apart is the whole design:

| | field | means |
|---|---|---|
| **Authority** | `product` + `adopted_products` | which boards this session may **write** to — enforced by the allowlist, not by convention |
| **Scope** | `initiative_id` | what the session is **about**, and where its cost lands |

All four combinations are meaningful, none is a special case:

- **product only** — a single product's PM. Unchanged from before this existed.
- **initiative only** — work that deliberately spans several integrated products. Which
  ones is *discovered as the session goes*, not declared up front.
- **both** — this board's share of that initiative.
- **neither** — the default session's shallow awareness of everything.

`Session.owned_products()` is the single definition of authority:
`owned_subtrees([product, *adopted_products])` — a union of subtrees, so adopting a parent
adopts its children exactly as pinning one always has. It feeds **both** the roadmap
context a turn is given and the allowlist that enforces it, so the two cannot disagree
about what a session owns.

**Breadth is not authority.** An initiative-scoped session sees its whole initiative at
full depth from turn one but starts able to write **nowhere**. It widens only by adopting
a board explicitly — `POST /sessions/{id}/scope {"adopt_product": ...}`, which the PM may
call for itself (the URL carries its own session id, so it can never widen another
session's authority) and which the stakeholder sees happen in the conversation.
`release_product` hands a board back. Adoption is an **event**, never passive discovery,
precisely because it rebuilds the PATCH allowlist.

`set_initiative` / `adopt_product` / `release_product` push the new scope into the live
`PMAgent` via `set_scope` (see §5) rather than rebuilding the runtime, which would drop
the PM's turn lock and its dev-task registry. Effective from the **next** turn: the CLI
subprocess for a turn already in flight was launched with the old `--allowedTools`, and
the prompt says so outright, or the PM tries its PATCH immediately and reports itself
blocked.

Validation of `initiative_id` and `project_id` lives in `server.py`, not here — the
session registry has no business knowing the portfolio store exists. A session can outlive
the initiative it names; every reader treats an id that no longer resolves as *unscoped*
rather than an error, and `_session_scope_report` surfaces it.

`Status = active | merging | merged | conflict | archived`.

Persistence: `<workspace_root>/workspace/sessions.json` (git-ignored), whole-file JSON keyed by
id. On load: bootstrap the default session if absent (id "default", branch "main",
worktree = primary checkout, `is_default=True`); flip stale `merging` → `active` (its
background thread died with the old process); backfill migrations for fields added
later.

### Runtime
`SessionRuntime` per session with a worktree: one `threading.Lock` (`git_lock`) shared
by that session's `PMAgent` and `TaskRegistry` so their commits into the same worktree
never interleave (and, for the default session, so merges into main serialize with its
own commits). Wire `pm_agent.on_activity_change` → broadcast a `session_updated` event.

### Cross-session visibility
`describe_other_active_sessions(exclude_id, tasks_per_session=3)` — for every OTHER
`active` session: `- "<title or name>" (<id>) — <goal>: <status>: "<task desc[:100]>"; …`
(up to 3 most-recent tasks; or "active, no dev tasks yet"). Returns `""` when nothing
else is active so callers skip the block entirely rather than injecting noise.

### Live activity (derived, never persisted)
`activity_of(id)` → `{"state","reason","detail"}` with priority:
1. any task `running` in the registry → `working`/`dev_task` (detail = the running
   *dev* task's description; a merge-resolver counts as working but detail stays None);
2. `pm_agent.turn_active` → `working`/`thinking`;
3. last transcript role == assistant → `waiting` (PM answered; stakeholder's move);
4. else `idle`. Also `idle` for any lifecycle status outside active/conflict.

### Lifecycle operations
- `create(name, product, model)` — validate; `git worktree add -b session/<8hex>
  <workspace_root>/workspace/sessions/<8hex> main` from the primary checkout; default name =
  product label or "Session <id>"; build runtime; broadcast `session_created`.
- `merge(id)` — refuse for default or status outside active/conflict; set `merging`,
  broadcast, run `_do_merge` on a background thread. `_do_merge_inner`:
  1. Soft precondition: refuse if the DEFAULT session has a running dev task (cheap
     race guard for a single local user).
  2. `archive_current("merge")` — the session's final spec/chat must not depend on its
     worktree surviving.
  3. Under the session's git_lock: snapshot ("Pre-merge snapshot for session <id>").
  4. Under the DEFAULT session's git_lock: snapshot main ("Pre-merge safety snapshot…"),
     then `_run_merge` (below). Outcome → status `merged` (+`merge_result`) or
     `conflict`; broadcast `session_merged`.
- `_run_merge(worktree_root, branch_to_merge, commit_message, conflict_label,
  conflict_runtime)` — direction-agnostic (session→main and main→session):
  `git merge --no-ff <branch> -m <msg>`; on conflict, collect
  `git diff --name-only --diff-filter=U`, dispatch a **synchronous** resolver task via
  `conflict_runtime.task_registry.run_conflict_resolution` (text in PROMPTS.md §5), then
  **independently verify** (never trust the resolver's self-report):
  `_merge_still_broken` = any porcelain unmerged codes (UU/AA/DD/AU/UA/UD/DU) OR any
  listed file with a line **starting** `<<<<<<<` or `>>>>>>>` (not bare `=======` —
  too common in legitimate content). Broken or resolver-error → `git merge --abort`,
  outcome `failed`. Else `git commit --no-edit` (abort on failure too) → outcome
  `auto_resolved` (deliberate product call: no manual confirm step). Clean merge →
  `clean`. Never raises for a conflict; always returns an outcome dict
  `{outcome, message?, conflicted_files?, resolver_task_id?, merged_at/synced_at}`.
- `sync(id)` — merge main INTO the session's worktree (anti-drift). Same `_run_merge`
  with `conflict_runtime` = the session's own; failure only sets `sync_result`, never
  status. Refuse if that session has a running dev task.
- `terminate(id)` — "merge, then don't stop": run the exact merge flow; only if it
  reaches `merged`, remove worktree (safe delete) + branch, `status=archived`,
  `archived=True`, drop runtime. On conflict it stops there — **there is no path that
  skips the merge**, so a terminated session's work is never lost to a force-deleted
  unmerged branch.
- `cleanup(id)` — post-merge worktree removal (refuse unless status == merged).
  `delete(id)` — force removal (never for default). Both → archived.
- Before `archive`, `terminate`, `cleanup` and `delete`, the server stops the session
  (`server._stop_for_lifecycle` → `_stop_session`: queued messages, PM turn, dev tasks,
  local services), so nothing of a session keeps running once it is ended or hidden.
- Every worktree removal (`_remove_worktree`) first calls
  `session_checkout.release_system_checkouts(REPO_ROOT, worktree, force=force)` (see
  below); a checkout it had to keep turns cleanup/terminate into a `ValueError` naming
  it, while `delete` (force) discards it like the rest of the session.

### Product-code checkouts in sessions (`session_checkout.py`)
A session worktree materializes submodule gitlinks as empty directories, so by default
nested-repo systems are a primary-checkout affair (§4). `ensure_system_checkout(repo_root,
worktree, system_path, link_paths=())` fills them on demand, idempotently and without a
network clone:
- Submodules = the superproject `.gitmodules` paths equal to / under `system_path`, plus
  their own nested submodules (recursively) when initialized in the primary checkout.
- Each one initialized in the primary becomes a **linked worktree of the primary
  submodule repo**: `git -C <primary>/<P> worktree add --detach <session>/<P> <sha>`,
  where `<sha>` is the gitlink the *session's* HEAD pins (`git ls-tree HEAD <P>`; for a
  nested one, its parent's session checkout). Missing locally → `git fetch origin <sha>`
  → else the primary's HEAD, with a note. Objects/refs are shared, so a feature branch the
  dev agent creates there is visible from the primary at once; the superproject sees the
  submodule clean until its HEAD actually moves. Uninitialized / non-empty targets are
  notes; `CheckoutError` only when nothing could be prepared.
- `link_paths` (repo-relative paths/globs, `**` never descends into `node_modules`/`.git`)
  share heavy dependency dirs from the primary: Windows junctions (`_winapi.CreateJunction`,
  fallback `mklink /J`; no admin needed), POSIX symlinks. Existing paths are never
  overwritten. Created links are listed in `<session git-dir>/pm-studio-links.json`.
- `release_system_checkouts(repo_root, worktree, force=False)` removes those links (rmdir
  on the junction — never `rmtree`, the target is the primary's), then each submodule
  worktree listed by `git worktree list --porcelain` under the session, deepest first,
  via `git worktree remove --force`, restores the empty gitlink dir and `git worktree
  prune`s. Required anyway: git refuses to remove a worktree containing populated
  submodules. Without `force`, a checkout with uncommitted changes or commits reachable
  from no branch/remote is kept and reported with a note starting `KEPT_PREFIX`.
- `set_meta(id, title, goal)` — trim; empty/whitespace = "no change" (a PM can't blank
  its own identity by accident); clamp title ≤ 60, goal ≤ 200; persist + broadcast.
- `set_model(id, model)` — validate, persist, AND update the live runtime
  (`pm_agent.set_model`, `task_registry.model`) so it applies to the very next
  turn/task with no restart.
- All lifecycle mutations guarded by one `_registry_lock`; subscriber/broadcast pattern
  identical to TaskRegistry's.

## 7. `server.py` — FastAPI app + websockets

Singletons: `SessionManager`, `RoadmapStore`. Capture the asyncio loop in lifespan; all
background-thread → websocket hops via `loop.call_soon_threadsafe(asyncio.create_task, …)`.

### Endpoints
| Route | Behavior |
|---|---|
| `GET /` | sessions.html |
| `GET /sessions` | list of `_public_session` dicts (session + derived `activity`) |
| `POST /sessions` | create `{name?, product?, initiative_id?, project_id?, model?}`; an initiative-scoped session with no product is named after its initiative, and its catch-all project is created up front |
| `GET /sessions/{id}` | one session |
| `POST /sessions/{id}/model` | live model switch |
| `POST /sessions/{id}/meta` | PM-maintained title/goal; returns `_public_session` |
| `POST /sessions/{id}/project` | attribution target; `""` clears |
| `POST /sessions/{id}/initiative` | scope to an initiative; `""` clears. Ensures that initiative's catch-all project exists, so the attribution read on the signal path stays a read |
| `POST /sessions/{id}/scope` | `{"adopt_product"}` or `{"release_product"}` (exactly one). Refused for a session with no initiative — a product-pinned session's boards are the stakeholder's to set. Called by the PM itself as well as the UI |
| `GET /models` | the allow-list |
| `POST /sessions/{id}/merge` / `/sync` / `/terminate` / `/cleanup` / `/archive` / `/unarchive`, `DELETE /sessions/{id}` | lifecycle (background threads for merge/sync/terminate) |
| `GET /chat/{id}`, `GET /dashboard/{id}`, `GET /roadmap` | static pages |
| `GET /history/{id}` | chat_history.json |
| `GET /chat/{id}/pending` | messages accepted but not yet started — the chat page appends these below the transcript on load, deduped against it by `id` |
| `GET /chat/{id}/uploads/{filename}` | attachment files (basename-sanitized) |
| `POST /chat/{id}/reset` | archive + clear |
| `POST /chat/{id}/stop` | `run_session`. Stops the PM turn in flight (it ends as `pm_error` "Stopped by …", transcribed) and discards the queued messages (memory + `pending_messages.json`; open pages get `pm_queue_discarded {ids}`). `{session, pm_turn, discarded}` |
| `POST /tasks/{id}/{tid}/stop` | `dispatch_dev_task`. Stops one running dev task → status `stopped`; `{session, task}`. 404 unknown task, 409 not running / merge resolution |
| `POST /sessions/{id}/stop` | `run_session`. `_stop_session`: discard queue, stop PM turn, `stop_all` dev tasks, `local_env.stop_session` (services only — resources and checkout kept). `{session, pm_turn, tasks, services, discarded}`; empty when idle. 404 unknown session. Archive / terminate / cleanup / delete run the same stop first (`_stop_for_lifecycle`), before `_release_local_env` |
| `POST /tasks/{id}` `{"task": "...", "system": "..."}` | dispatch dev task (immediate return); `system` required once `[systems]` is declared — it routes the system's `gitflow` rules into the dev agent's prompt — and refused when it isn't (400 naming the valid ids either way) |
| `GET /tasks/{id}`, `GET /tasks/{id}/{tid}` | list / one |
| `GET /roadmap/data` | `{products, product_parents, systems, product_systems, unattributed, items-by-product, people}`. Every change (and project) carries `assigned` — who is on it, from the local assignment or the linked ticket, with `conflict` when they disagree. The load table is NOT in this payload: the board derives it from the sections it draws, so an optimistic assignment cannot leave a stale figure above the card that just moved |
| `GET /people/directory` | the directory, the server-side load table, and (enterprise only) the accounts a person can be linked to. Open to any reader, in both modes |
| `POST /people/directory`, `PATCH /people/directory/{id}`, `DELETE /people/directory/{id}` | add somebody the trackers do not know, rename/retire/link them to an account, remove a local entry. `manage_roadmap` |
| `POST /people/directory/{id}/merge` `{"from"}`, `POST /people/directory/{id}/split` `{"tracker_id","key"}` | the two reconciliation repairs (see §3d). Merge re-points local assignments first and reports `changes_moved` |
| `GET /systems`, `GET /systems/data` | the system catalogue page and its dataset: one row per declared system (label, path, repo, guidance, gitflow, pipelines, the products touching it, its change counts, whether it is mid-reclassification), plus the restructure gap. `{"declared": false}` when no `[systems]` table exists |
| `GET/POST /roadmap/{product}/items`, `PATCH/DELETE /roadmap/{product}/items/{item_id}` | board CRUD; PATCH/DELETE verify the URL product OWNS the item (this is what makes own-product-scoped allowlists safe); PATCH with `move_to_product` triggers the move flow; `start_at`/`target_at` accept `"YYYY-MM-DD"` or `""` to clear, and a malformed or inverted pair is a 400 naming the reason with nothing applied; `assignee` takes a person id, `""` unassigns, and an id the directory does not know is a 400 rather than a card that reads "assigned to somebody unknown" forever |
| `POST /roadmap/{product}/items/{item_id}/push`, `POST /portfolio/projects/{id}/push` | create the ticket/epic for work planned here and link it, in one call. Optional `tracker_id`/`project`/`issue_type` override `[trackers.push]`; an empty body is the one-click path. 409 if already linked (naming the ticket) or if a created ticket could not be linked (naming its key); 400 if no tracker can push or the target is ambiguous; 502 carrying the tracker's own refusal. Response adds `push: {key, url, parent_key, parent_skipped}` |
| WS `/ws/chat/{id}`, `/ws/tasks/{id}`, `/ws/sessions`, `/ws/roadmap` | push channels |

Optional: zero-segment aliases to the default session (`/tasks`, `/history`, `/ws/chat`,
`/ws/tasks`, `/dashboard`) for backward compat.

### Chat websocket flow
On message `{text, attachments[]}`: the receive loop **only enqueues** — it never waits
on the turn, so the stakeholder can keep typing and keep sending while the PM is
thinking. First `pm_agent.enqueue_pending(...)` (in an executor — it touches disk) writes
the message to the session's `pending_messages.json`, so from that instant it outlives
this connection; then `_enqueue_pm_turn` parks `{pending, websocket, send_lock, user}` on
the in-memory `_chat_queues[id]` and starts `_drain_pm_queue(id)` if no worker is running.
**Every** accepted message is acked with `{"type":"pm_queued","id":…,"ahead":n}` — `ahead`
> 0 means it is parked behind earlier ones, and the page needs the `id` either way to
label its own bubble. `pm_working` carries `queued_id` so the page can promote a queued
bubble when its turn starts. One worker per session drains the queue **in order, one turn
at a time** (it exits the moment the queue is dry, with no `await`
between the empty check and deregistering, so an enqueue can never hand work to a worker
on its way out — it starts a fresh one). Per turn: build `other_ctx =
describe_other_active_sessions(id)` and `roadmap_ctx = _roadmap_context_for(id)` — own
product deep + others digest for pinned sessions; digest-of-all for unpinned; and for an
initiative-scoped session the **initiative first** at full depth (its changes cut across
boards, so leading with one product's roadmap would bury the actual scope), then one
full-depth block per owned root, then the digest of everything not already shown — run
`handle_user_message` in an executor, stream events back through an asyncio queue to the
connection that sent the message. A sender that disconnected mid-queue just loses the
events; the turn still runs and is still written to the transcript.

`_chat_queued_ids[id]` tracks which queue records this process is holding. On **connect**,
`_resume_pending` re-queues every record in the session's queue file that is *not* in that
set: those are leftovers a restart stranded, which nothing would otherwise ever run. A
plain page reload skips them all (the worker never stopped), so nothing runs twice.
Deliberately on connect, not at startup — a stranded turn runs when someone is there to
read it, not unattended at boot. `POST /chat/{id}/reset` calls `_discard_queued_turns`
first (hopping onto the loop, since the endpoint runs on a worker thread): a message that
hasn't started belongs to the conversation being cleared, not the fresh one.

**Per-connection send lock**: two producers can push to one socket (the queue worker,
and an auto-continuation broadcast firing at an arbitrary time) and concurrent
`send_text` isn't safe — every send on both paths takes the lock.

### Task completion fan-out
`TaskRegistry.subscribe` → on every task write: broadcast `task_update` on
`/ws/tasks/{id}`; broadcast an enriched `session_updated` on `/ws/sessions` (activity
changed); and **only for `kind=="dev"` reaching done/error** (never `stopped` — someone
stopped it on purpose; its spend is still recorded), spawn a thread running
`handle_task_completion`, broadcasting each resulting event to every open chat socket
for that session with `"auto": true` (a merge-resolver finishing must NOT make the PM
narrate an unrelated merge).

## 8. `__main__.py` + `scaffold.py`
`python -m pm_studio` (or the `pm-studio` script): runs
`uvicorn.run("pm_studio.server:app", host=CONFIG.host, port=CONFIG.port)` plus a
thread that opens the browser to the root URL after ~1 s. `python -m pm_studio init`
runs `scaffold.run_init`: non-destructively creates `pm_studio_local/` (config +
instruction stubs + `knowledge/`), the seed `PROJECT_INDEX.md`/`PROJECT_STATUS.md`,
and appends the runtime-state `.gitignore` block. `version` prints the package
version.

## 9. Static UI (vanilla HTML/JS/CSS, one file per page)

### Shared navigation (`nav.css` + `nav.js`, served at `/static/nav.*`)

The only assets more than one page loads. Every page mounts them the same way — the
stylesheet and a **non-deferred** `<script>` in `<head>`, plus one element in the body:

```html
<div id="pm-nav" data-page="roadmap"></div>
```

`nav.js` renders two rows, and the split is the design:

1. **The bar** — brand, then **the only place destinations are listed**:
   `Portfolio → Roadmap → Sessions │ Time & cost · People`, current one marked by
   `aria-current="page"` plus weight *and* a tinted chip, so "you are here" survives
   greyscale. The arrows between the first three *are* the work model — intent narrowing
   into work — drawn where the links already are. On the right, in enterprise mode, the
   acting user, their role and **Sign out** — on every page, not just the sessions list.
   `Time & cost` is filtered by capability (`view_cost`). `People` is shown to everyone,
   in both modes: it carries the directory of who is doing the work, which a tracker sync
   fills in whether or not anybody can sign in — the page hides its enterprise-only roster
   half rather than the tab hiding the page. Hiding a tab is orientation, never protection
   — §7 enforces every capability.
2. **The context row** — what the page you are on *holds*, plus a slot it fills itself.
   An ordinary page gets a label (not a link): its name and one-line descriptor. The
   pages nested inside a session (`chat`, `dashboard`) get a breadcrumb —
   `Sessions › <session title>` — and the session's sub-tabs, **Chat** / **Dev
   lifecycle**. Every page also gets `#pm-nav-slot`, right-aligned, resolved through
   `PMNav.ready`; roadmap.html puts its grouping and view switches there rather than
   growing a third band of chrome. `.pmnav-seg` is the one control shape offered for it
   — a segmented switch whose state is `aria-pressed`, with no parallel "active" class
   to keep in sync.

   This row used to be a flow map that re-listed row 1's three links directly beneath
   them. Two lit copies of the same destination read as two menus. **Nothing added here
   may re-list what the bar already shows.**

Two constraints that are easy to break:

- **The script must not be deferred.** Pages run inline scripts at the end of `<body>`,
  which execute before any deferred script would. Loading it plainly from `<head>` means
  `window.PMNav` already exists for them. It exposes `PMNav.auth` and `PMNav.session`
  (both resolve to `null` instead of rejecting), so a page load makes exactly one
  `/auth/me` and at most one `/sessions/{id}` however many consumers there are —
  sessions.html reads `auth` to hide the create form, chat.html reads `session` for its
  metadata.
- **Every rule in `nav.css` is scoped to `#pm-nav`,** and the block after the tokens
  resets the inherited properties the component cares about. Pages style bare elements —
  portfolio.html puts a border and padding on every `li` — and those rules would
  otherwise reshape the nav's own markup. The nav also defines its own `--pmnav-*`
  palette rather than borrowing page tokens, whose names differ per page (chat.html uses
  `--bg`/`--fg`/`--muted`, the board pages `--page`/`--ink-primary`).

Page `<header>`s therefore carry only what the page *is* and controls that act on it —
no per-page link sets, which is what let them drift out of sync and leave
`/dashboard/{id}` with no inbound link from anywhere at all. A page whose controls fit
`#pm-nav-slot` can drop its header entirely; roadmap.html does, which is worth ~90 px of
vertical space on a board that wants it.

- **sessions.html** (`/`) — session cards: title (fallback name → "Untitled session"),
  muted one-line goal, product tag, model selector (POST /model), lifecycle status
  badge, and the LIVE activity signal: pulsing-dot "Working" pill (with the running dev
  task's description when reason=dev_task), a loud amber left-border + tinted
  background + "⏳ Waiting for you" badge for waiting (with a reduced-motion guard),
  ordering waiting → working → rest. A card also shows its **initiative** (accented, above
  the product tag — for a cross-product session that is the primary scope and the boards
  are the derived thing), falling back to the raw id plus "no longer exists" rather than
  going blank, since that is exactly the case worth seeing; and its boards as
  `Pinned · Other (adopted)`, so authority arriving by a different route is visible at a
  glance. Create form: optional name ("leave blank to auto-title"), product picker,
  **initiative picker** (hidden until the deployment has open initiatives, so an instance
  not using the work model gets no permanently empty control), model picker. Per-card
  actions: open chat, merge, sync, terminate, cleanup/delete, archive toggle (+ "show
  archived" filter). Live via `/ws/sessions`; initial `GET /sessions`, plus
  `GET /portfolio/data` for initiative titles. Changing scope after creation is API-only
  (`/initiative`, `/scope`), matching how `project_id` already worked.
- **chat.html** (`/chat/{id}`) — ONE chronological timeline, everything reading
  oldest→newest with newest at bottom (no second list sorted the other way — that was
  a real stakeholder complaint that forced a redesign). Chat messages (user / pm /
  error / system) interleave by `ts` with **inline dev-task cards** anchored at their
  chronological position in the stream, visually distinct from chat bubbles (status-
  colored left accent + elbow connector). Card anatomy: an always-visible top row
  (status badge with running-pulse/done/error + relative time / "took Xm Ys"), the
  **description collapsed** behind a `<details>` "Task" disclosure whose summary is a
  one-line ~90-char truncated preview (verbose task specs otherwise fill the screen),
  and a separate collapsed Result disclosure. Cards **update in place** via a
  `taskId → cardElement` map upserted on every `/ws/tasks/{id}` message — never emit
  throwaway "task started/done/failed" status rows; a newly started task appends its
  card at the current bottom. Hydration on reload: merge `GET /history` (ordered by
  `ts`; array-order fallback for legacy entries without `ts`) with `GET /tasks`
  (ordered by `started_at`); never crash on missing timestamps. Auto-scroll to bottom
  only when the user is already near the bottom (don't yank them while reading
  history); jump to bottom on first hydration. Session lifecycle controls live in a
  compact **collapsible strip under the header** (collapsed: branch + status badge;
  expanded: Merge to main / Clean up worktree buttons and `merge_result` details, kept
  live via `/ws/sessions`, expanded state surviving re-renders) — they are
  session-level, not per-task, so they don't belong in the stream. Composer with image
  attach (base64), "PM is working…" indicator on `pm_working`, reset button (confirm
  first); auto-continuation events arrive marked `auto:true`. The composer is **never
  disabled while the PM is thinking** — sending is always allowed and the server queues
  the message onto the session; it locks only when the server reports the session is
  gone. A message still waiting is the stakeholder's own bubble, dimmed and tagged
  "Queued": drawn from `GET /chat/{id}/pending` on load, or marked live when its
  `pm_queued` ack reports `ahead > 0` (acks pair off with sent bubbles in order), and
  un-tagged on the `pm_working` carrying its `queued_id`. Markdown-ish rendering
  of PM replies is nice-to-have.
- **dashboard.html** (`/dashboard/{id}`) — the session's task list with statuses,
  durations, results; live via `/ws/tasks/{id}`.
- **roadmap.html** (`/roadmap`) — **two independent axes**, both persisted in
  `localStorage` and both mounted into the nav's controls slot rather than a page header
  of their own (the page has no `<h1>`; the lit tab and the context row already say what
  it is):
  - *Group by* — `product` (one section per product board), `initiative` (one section per
    initiative, computed client-side to mirror `portfolio.group_changes_by_initiative` so it
    re-groups on a websocket event instead of refetching), or `person` (one section per
    person, heaviest open load first, with unassigned work trailing as its own section
    whatever its size). Any of them yields a group of the same shape —
    `{key, title, flags, changes, omit, addTo}` — and every view renders it.
    `addTo` is null when a section has no unambiguous board to add to — an initiative
    spanning two products shows no "+" rather than guessing one.

    The person lens only draws people who hold work: the directory holds everybody either
    tracker ever mentioned, and a board of mostly empty headings answers nothing. Who has
    *room* is the **load table** above it — open counts by horizon, overdue, and the
    products and tracker areas each person's work sits in, with zero-load active people
    added back because they are the answer to that question. It is derived from the very
    sections below it (`loadRows`), never fetched, so an optimistic assignment cannot leave
    a stale number above the card that just moved, and it narrows with the search filter
    like every other count on the page.
  - *View* — `board` (Now/Next/Later lanes) or `timeline` (a Gantt).

  A card is **one line collapsed**: a status dot, the title, and a middot-separated meta
  line (product / project / external owner / assignee / ticket badge). The assignee chip
  says which source knows — a bare name for a local assignment, "(from tracker)" when
  nobody here overrode the ticket, and the warning treatment naming both when they
  disagree — and is omitted inside a person section, which the heading already is.
  Clicking the card reveals the description and every control — status, horizon,
  move-to-product, assignee, owner, ticket link, delete. Assignment is a picker over the
  directory rather than free text, because a typed name would be an id nothing resolves;
  display names come from Jira/ADO, so they reach the DOM through `textContent` only,
  never a template string. Those controls used to be on every card at all times, four selects and three
  buttons deep, which is what made the old board ~140 px per change. Open-card state
  lives in a JS `Set`, not the DOM, because a websocket event re-renders everything.

  The **timeline** shares one axis across every group: one quarter of history plus the
  three horizons (Now = this quarter, Next = the next, Later = the two after), fifteen
  months, bars positioned as percentages of real timestamps. A bar carries two
  independent facts through two independent modifiers — colour (`--bar`: pending / in
  progress / shipped, or **overdue**, which overrides) and fill (**solid** when the span
  comes from dates the change actually carries, **hatched** when it is derived from the
  horizon and claims nothing more). A diamond marks `target_at`; on a shipped change it
  stays put, so the gap between the bar's end and the diamond *is* the slip, and it turns
  red when the date was missed. A dotted tail is time spent waiting on the board before
  the span opens.

  How the span is derived matters, and it is four cases, not one (see `barFor`): both
  dates → exactly that span; **target only → today → target**, *not* the horizon, because
  a change bucketed `next` but due in three weeks would otherwise get a bar starting after
  it was meant to finish; start only → start out to the end of its horizon; neither → the
  hatched horizon band. An overdue bar is extended to today so the overrun past its
  diamond is the length of the bar.

  The axis header is deliberately **not** `position: sticky`: the horizontal
  `overflow-x: auto` wrapper is the nearest scrollport in both axes, so a sticky header
  there renders `top` pixels down over the first rows and never sticks.

  Cross-product suggestions still surface as an untriaged strip with accept/dismiss, and
  done items still collapse into a "recently shipped" disclosure. Live via `/ws/roadmap`;
  a stakeholder can add and edit items directly.
- **people.html** (`/people`) — two sections, gated separately. **Who is doing the work**:
  the directory with each person's load beside them and their tracker identities under their
  name, plus the merge/split repairs and the optional link to a sign-in account. Reachable in
  both modes by any reader; the writes need `manage_roadmap`. **The roster** (invite, team,
  open invites) is enterprise-only and shown only to `manage_users`, hidden rather than
  disabled — a viewer has no business reading a list of invite tokens.

## 10. Concurrency model (summary)

| Lock | Guards |
|---|---|
| per-session `git_lock` | all commits into that worktree (PM turns, dev-task snapshots, merges into it) |
| per-session `pm_lock` | PM turn execution + history append (manual vs auto-continue) |
| `SessionManager._registry_lock` | registry mutations (create/cleanup/delete/meta/model/archive) |
| `RoadmapStore._lock` | item mutations |
| `PeopleStore._lock` | directory mutations (reconcile, merge, split, status) |
| per-chat-ws asyncio send lock | concurrent sends on one websocket |

Threads: dev tasks, merge/sync/terminate, and auto-continue each run on daemon threads;
everything crossing into asyncio goes through `call_soon_threadsafe`. Every background
flow is wrapped so an exception marks an outcome (conflict/failed/error event) instead
of crashing the server or wedging a session in `merging`.

## 11. Timeouts & limits
PM turn 1800 s; dev task 1800 s; auto-continue streak cap 6; attachments 6 × 10 MB;
title 60 chars; goal 200 chars; task/session/item ids `uuid4().hex[:8]`.
