"""Per-deployment configuration: everything about the host project that PM Studio
must not hardcode, loaded from `pm_studio_local/` in the target repo.

The split is deliberate and is the package's core contract:
- The PACKAGE ships the behavior - prompts, lifecycle, endpoints, UI - identical for
  every deployment, and is never edited locally (it is maintained upstream only).
- The TARGET REPO ships `pm_studio_local/`: project identity + append-only local
  instructions (enterprise rules, private domain knowledge). Local content can ADD to
  the shipped prompts but can never replace or reorder them - that's what keeps the
  experience the same across every system using PM Studio.

Layout in the target repo (all optional - missing pieces fall back to defaults):

    pm_studio_local/
      config.toml           # [project], [server], [products], [models], [enterprise],
                            #   [smtp], [costing], [[trackers]]
      PM_INSTRUCTIONS.md    # appended to every PM system prompt
      DEV_INSTRUCTIONS.md   # appended to every dev-agent dispatch
      knowledge/*.md        # local reference docs; the PM is pointed at them

The repo root is the process working directory (`python -m pm_studio` is run from the
target repo's root), NOT this package's install location.

One file outside `pm_studio_local/` is also read: an optional `.env` at the repo root,
merged into the environment before any `*_env` name below is resolved (see
_load_env_file). It stays outside because it holds the actual secrets and the host repo
git-ignores it, while `pm_studio_local/` is committed.
"""

import json
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pathlib import Path
from typing import NoReturn

LOCAL_DIR_NAME = "pm_studio_local"
CONFIG_FILE_NAME = "config.toml"
PM_INSTRUCTIONS_NAME = "PM_INSTRUCTIONS.md"
DEV_INSTRUCTIONS_NAME = "DEV_INSTRUCTIONS.md"
KNOWLEDGE_DIR_NAME = "knowledge"

# Optional per-repo environment file, at the repo ROOT rather than inside
# pm_studio_local/: it carries the credentials themselves, so it belongs in the file the
# host repo already git-ignores, not in the directory it commits.
ENV_FILE_NAME = ".env"

DEFAULT_WORKSPACE_ROOT = "pm_studio"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_SESSION_NAME = "Main"

# The two operating modes. `personal` is the historical single-trusted-user tool and
# stays the default forever: an existing deployment that upgrades the package must not
# suddenly demand a login. `enterprise` turns on accounts, invites and roles.
MODE_PERSONAL = "personal"
MODE_ENTERPRISE = "enterprise"
MODES = (MODE_PERSONAL, MODE_ENTERPRISE)

DEFAULT_SMTP_PORT = 587

# DEPRECATED, kept only so existing config files keep loading: nothing schedules a
# tracker pull any more. Syncs are started by hand ("Sync now" on the board, or POST
# /trackers/sync) - an unattended pass that rewrites the board while nobody is watching
# is how a second or stale instance double-imports tickets, and is not worth the few
# minutes of freshness it buys.
DEFAULT_SYNC_INTERVAL_MINUTES = 15.0
MIN_SYNC_INTERVAL_MINUTES = 1.0

# Cost attribution defaults. A deployment sets its own in [costing]; rates are that
# deployment's own compensation data and never ship in this package.
DEFAULT_CAPACITY_HOURS = 40.0
DEFAULT_CURRENCY = "USD"

# Shown in the PM system prompt when the target repo hasn't described its own source
# layout yet - honest about not knowing, and points at the fix.
DEFAULT_REPO_LAYOUT = (
    "- (This project has not described its source layout in "
    f"{LOCAL_DIR_NAME}/{CONFIG_FILE_NAME} yet - explore the repo root to learn where "
    "sources live, and suggest the stakeholder fill in the `layout` setting so future "
    "sessions start oriented.)"
)


@dataclass(frozen=True)
class SmtpConfig:
    """Outbound mail for enterprise invites. Entirely optional: with no [smtp] table
    PM Studio never tries to send anything and the invite flow falls back to a
    copyable link, so no deployment is forced to stand up a mail server."""

    host: str
    port: int
    from_address: str
    username: str = ""
    password: str = ""
    use_tls: bool = True

    @property
    def is_usable(self) -> bool:
        return bool(self.host and self.from_address)


@dataclass(frozen=True)
class CostingConfig:
    """Cost-attribution inputs. Optional: with no [costing] table hours are still
    distributed and reported, and only the money columns are unknown."""

    # Fallback hourly rate for anyone with no individual rate. Supporting this is the
    # point of "blended": an org that will not put individual salaries in a tool can
    # still get project cost out.
    blended_rate: float | None = None
    default_capacity_hours: float = DEFAULT_CAPACITY_HOURS
    currency: str = DEFAULT_CURRENCY
    # Relative weight per signal kind, for tuning the split.
    weights: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class TrackerRoute:
    """One import route: matching tickets become changes on this product, attributed to
    this system.

    Two match shapes, for the two shapes a tracker project comes in. A project that
    serves MANY products routes by `component` (Jira component / ADO area path); a
    project that IS one product routes by `project` - every ticket in it, component or
    not. A route may set either or both (both = this component within that project).
    Component routes always win over project routes: the more specific claim is the
    truer one, and a project-wide default must not swallow the exceptions declared
    beside it.

    `system` may be empty - the import then lands unattributed on an in-scope product,
    which the existing unattributed report surfaces. An imported change you can see
    beats a refused one you can't."""

    product: str
    component: str = ""
    project: str = ""
    system: str = ""


# Default issue types a push creates when the operator names none. "Story" is the
# story-rung name in a stock Jira software project and "Epic" the rung above it; an
# instance that renamed either says so in [trackers.push].
DEFAULT_PUSH_CHANGE_TYPE = "Story"
DEFAULT_PUSH_EPIC_TYPE = "Epic"


@dataclass(frozen=True)
class PushConfig:
    """Where a PUSH creates tickets: the one project, and the type per rung.

    Push is the write half of a tracker connection - a change planned here becomes a
    real ticket there - and it is opt-in per tracker for two reasons. A sync token is
    often deliberately read-only, so assuming write access would turn one button into a
    confusing 403; and creating an issue on someone's board is an outward-facing act
    that a deployment should have to declare, not inherit. With no [trackers.push]
    table this is None on the TrackerConfig and every push affordance is absent - the
    same all-or-nothing shape `imports` has.

    `project` is required and must be one the tracker already syncs: a push into a
    project the catalog never pulls would create a ticket that immediately reads as
    unresolved on the card it was pushed from. It is the DEFAULT, not a cage - the push
    dialog offers the tracker's other synced projects too (see server.py), because the
    project a change belongs in is a per-change fact and config can only state the
    common case.
    """

    project: str
    change_type: str = DEFAULT_PUSH_CHANGE_TYPE
    epic_type: str = DEFAULT_PUSH_EPIC_TYPE


@dataclass(frozen=True)
class TrackerConfig:
    """One external issue tracker PM Studio pulls ticket metadata from.

    A deployment declares as many as it has, each as its own `[[trackers]]` block, so a
    shop running both a Jira instance and an ADO project can link roadmap changes into
    either. `projects` is the set of boards/projects to pull from - it bounds the sync
    (nobody wants every issue in the instance) and it is also what lets a bare key like
    `PROJ-123` be resolved to the tracker that owns `PROJ`.

    The token is NEVER stored here from a file by preference: see _parse_trackers, which
    reads `token_env` (the NAME of an environment variable) and only falls back to an
    inline `token` with a loud warning. pm_studio_local/ is normally committed.
    """

    id: str
    provider: str  # "jira" | "ado"
    label: str
    # Jira: the instance root, e.g. https://acme.atlassian.net
    # ADO:  derived from `organization` unless given explicitly.
    base_url: str
    projects: tuple[str, ...]
    # Jira Cloud authenticates as email + API token; ADO as an empty user + PAT. Both the
    # username and the token can be supplied by env var (`username_env`/`email_env` and
    # `token_env`), which is what lets a deployment publish its pm_studio_local/ safely.
    username: str = ""
    token: str = ""
    # ADO only: the dev.azure.com organization name.
    organization: str = ""
    # DEPRECATED and unread: there is no background sync (see
    # DEFAULT_SYNC_INTERVAL_MINUTES). Still parsed so a config that declares it loads,
    # and reported once at startup so it can be deleted.
    sync_interval_minutes: float = DEFAULT_SYNC_INTERVAL_MINUTES
    # IMPORT: which ticket types become changes, and how their components route onto
    # products. Both empty = the historical behavior byte for byte (catalog sync and
    # manual linking only). Declaring exactly one of the two is fatal at load - it
    # would be a config that looks like it imports and silently doesn't.
    import_types: tuple[str, ...] = ()
    routes: tuple[TrackerRoute, ...] = ()
    # Bound on the catalog pull: only tickets CHANGED on/after this date (YYYY-MM-DD)
    # are synced. For a tracker with years of history where only the recent slice is
    # planning material - the catalog, the link picker and every import pass then see
    # just that slice. Linked tickets older than the bound stay resolvable: the sync
    # fetches any linked-but-missing ticket individually (refresh_missing). Empty = no
    # bound, the historical behavior.
    since: str = ""
    # EXCLUSION: components (Jira) / area paths (ADO) whose tickets this deployment
    # declares OUT of every automatic pass - not imported as changes, not imported or
    # linked as projects, not used for assignment. For the slice of a tracker that is
    # tracked authoritatively somewhere else (the classic case: the same work mirrored
    # in a second tracker). A ticket is excluded when one of its components matches,
    # or when anything up its parent chain is excluded - children routinely carry no
    # components of their own. Manual linking stays possible: exclusion governs what
    # the sync does on its own, never what a human does on purpose.
    exclude_components: tuple[str, ...] = ()
    # CLOSING: whether an epic the tracker calls done closes the project linked to it.
    # False (the default) makes the pass report-only - it counts what it WOULD close and
    # says so on the epic report, writing nothing. That default is deliberate: enabling
    # it hands a tracker transition the power to close local planning work, and a
    # deployment should see the list once before it does. Close-only in either mode -
    # nothing here ever reopens a project a human closed, however the epic moves.
    close_done_epics: bool = False
    # PUSH: where this tracker creates tickets for work planned here. None (no
    # [trackers.push] table) = read-only, the historical behavior, and every push
    # affordance is absent rather than disabled. See PushConfig.
    push: PushConfig | None = None

    @property
    def imports(self) -> bool:
        return bool(self.import_types and self.routes)

    @property
    def can_push(self) -> bool:
        """Whether a push may be attempted. Both halves are required: a declared push
        target is worthless without a credential and a base_url to send it to, and
        answering True there would put a button on the board that can only fail."""
        return self.push is not None and self.is_usable

    @property
    def is_usable(self) -> bool:
        """A tracker with no credential or no projects is declared but cannot sync. It is
        still listed in the UI, with this as the reason - silently skipping it would look
        like the sync was working."""
        return bool(self.base_url and self.token and self.projects)

    @property
    def unusable_reason(self) -> str:
        if not self.base_url:
            return "no base_url (or organization, for ADO)"
        if not self.token:
            return "no API token - set `token_env` to the name of an environment variable"
        if not self.projects:
            return "no `projects` listed, so there is no board to pull from"
        return ""


# A product's lifecycle stage. Closed vocabulary, unknown value fatal at load - the
# same contract as [enterprise] mode: a typo must not silently mean something the
# operator did not write. "ga" is the steady state and what an undeclared stage means.
PRODUCT_STAGES = ("discovery", "development", "ga", "sunset")


@dataclass(frozen=True)
class ProductMeta:
    """Operator-declared facts about one product: who owns it, who builds it, where it
    is in its life. Context, not policy - nothing authorizes or bills against these.

    Lives in config rather than a store or UI on purpose: this is the same kind of
    slowly-changing, operator-owned fact as the taxonomy itself, and a second editable
    home for it would drift from the first. All fields optional; a product with no
    metadata simply has no entry (see Config.product_meta)."""

    description: str = ""
    owner: str = ""
    team: str = ""
    stage: str = "ga"

    @property
    def is_empty(self) -> bool:
        return not (self.description or self.owner or self.team) and self.stage == "ga"


# Placeholders in a local service's command, env values, url and ready_url (and in a
# resource's create/destroy). Two shapes, one token each:
#
#   {name}          a declared param, or one PM Studio fills itself (LOCAL_BUILTINS)
#   {<id>.<key>}    something another entry of the same system provides: a service's
#                   port ({api.port}) or a resource's output ({db.name})
#
# A brace preceded by `$` is left alone, so a shell's own `${VAR}` expansion can sit in a
# command without being mistaken for a parameter the operator forgot to declare. The
# group is the whole token ("api.port"); callers split on the dot.
LOCAL_PLACEHOLDER = re.compile(r"(?<!\$)\{((?:[a-z0-9-]+\.)?[A-Za-z_][A-Za-z0-9_]*)\}")
LOCAL_SERVICE_ID = re.compile(r"^[a-z0-9-]+$")
LOCAL_OUTPUT_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DEFAULT_LOCAL_READY_TIMEOUT = 180.0
DEFAULT_LOCAL_RESOURCE_TIMEOUT = 900.0
# A service's `setup` (installing its dependencies, typically) may take this long.
DEFAULT_LOCAL_SETUP_TIMEOUT = 900.0
# Where a service or resource lives: "session" (the default) runs once per chat session,
# from that session's worktree; "shared" runs once per PM Studio instance, from the
# primary checkout - for things with no per-session state, like a secrets emulator.
LOCAL_SCOPE_SESSION = "session"
LOCAL_SCOPE_SHARED = "shared"
LOCAL_SCOPES = (LOCAL_SCOPE_SESSION, LOCAL_SCOPE_SHARED)
# `port = "auto"`: PM Studio picks a free TCP port at start and exposes it as {port}.
LOCAL_AUTO_PORT = "auto"
# Placeholder names PM Studio fills itself, so no `params` entry may take them:
#   {port}      this service's own port (fixed or auto)
#   {session}   the chat session id the run belongs to (session scope only)
#   {worktree}  the absolute path of that session's worktree (the repo root for the
#               default session and for shared scope), with forward slashes
#   {repo_root} the absolute path of the PRIMARY checkout, with forward slashes, in
#               every scope. A session worktree holds only what is committed; the
#               deployment's Makefile, scripts, virtualenv and git-ignored .env live
#               only in the primary - so a session service typically runs tools from
#               there against code from its worktree:
#               `make -C {repo_root} api ROOT={worktree} PORT={port}`
LOCAL_BUILTINS = ("port", "session", "worktree", "repo_root")
# Resource expiry (see LocalResourceSpec.expires).
LOCAL_EXPIRES = ("daily",)
LOCAL_DEFAULT_EXPIRES_AT = "00:00"
LOCAL_HHMM = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")
DEFAULT_MAX_SESSION_ENVS = 2


@dataclass(frozen=True)
class LocalServiceSpec:
    """One process PM Studio can start and stop on the developer's machine for a
    system's local environment - a secrets emulator, an API, a web app, a worker.

    Deliberately technology-agnostic: a service is a shell command plus how to tell it
    is up. Node, .NET, Python and Flutter all fit the same shape, so the supervisor
    (local_env.py) never needs to know what it is running. The command goes through the
    shell exactly as the operator would type it - which is why `params` exist instead of
    free-form arguments: a caller may only fill declared `{name}` holes, and the
    supervisor validates every value against a strict allowlist before substituting it.

    Readiness is declared, never guessed: `port` (a TCP connect on localhost succeeds)
    and/or `ready_url` (an HTTP GET answers below 500). With neither, a process that is
    still alive a moment after launch counts as running.

    Every chat session gets its own copy of a "session"-scoped service, run from that
    session's worktree, so two sessions can each have the API up on their own branch at
    once - which is why `port` may be "auto" (a free port picked at start, read back
    through `{port}` and, by dependents, `{<id>.port}`). A "shared" service runs once
    for the whole instance from the primary checkout and every session reuses it.
    """

    id: str
    command: str
    label: str = ""
    # Repo-root-relative working directory for the command (resolved against the
    # session's worktree for session scope, the primary checkout for shared).
    cwd: str = "."
    # An int 1-65535, LOCAL_AUTO_PORT ("auto"), or None for no port at all. A fixed port
    # on a session-scoped service is allowed, but then only one session can run it at a
    # time: the next one finds the port taken and gets `port_in_use`.
    port: int | str | None = None
    ready_url: str = ""
    # A link the UI offers once the service is running (e.g. the web app's address).
    url: str = ""
    # Ids of services AND resources in the SAME system that must be up first.
    depends_on: tuple[str, ...] = ()
    # Declared placeholders and their defaults, as (name, default) pairs. Pairs rather
    # than a dict so the spec stays hashable like every other frozen config value.
    params: tuple[tuple[str, str], ...] = ()
    # Extra environment variables for the process, as (name, value) pairs. Values may
    # use the same placeholders as the command.
    env: tuple[tuple[str, str], ...] = ()
    ready_timeout: float = DEFAULT_LOCAL_READY_TIMEOUT
    scope: str = LOCAL_SCOPE_SESSION
    # Resources of the SAME system this service can use when they exist, without ever
    # creating them: `{db.name}` is the output while that resource is ready in the
    # service's scope at launch, and "" otherwise (so the service falls back to, say,
    # the shared DEV database). Creating one is a separate, explicit request. Contrast
    # `depends_on`, which creates the resource on demand.
    uses: tuple[str, ...] = ()
    # Optional shell command run before EVERY launch of this service, from the primary
    # checkout (repo root), to install what the command needs (npm dependencies...) so
    # nobody has to run it by hand. Runs before the session checkout's links are
    # refreshed and before dependencies are waited for, so it may only use the params,
    # {session}, {worktree} and {repo_root} - never {port} or a dependency's values,
    # which do not exist yet. A non-zero exit or exceeding `setup_timeout` seconds fails
    # the start.
    setup: str = ""
    setup_timeout: float = DEFAULT_LOCAL_SETUP_TIMEOUT

    @property
    def param_defaults(self) -> dict[str, str]:
        return dict(self.params)

    @property
    def env_vars(self) -> dict[str, str]:
        return dict(self.env)

    @property
    def auto_port(self) -> bool:
        return self.port == LOCAL_AUTO_PORT

    @property
    def fixed_port(self) -> int | None:
        return self.port if isinstance(self.port, int) else None

    @property
    def shared(self) -> bool:
        return self.scope == LOCAL_SCOPE_SHARED


@dataclass(frozen=True)
class LocalResourceSpec:
    """Something a local environment needs PROVISIONED rather than run - the canonical
    case being a per-session temporary database, so two sessions migrating their own
    branches never trample each other's schema.

    Created on demand, the first time a service that lists it in `depends_on` starts in
    its scope, then reused until it is destroyed: by an explicit request, or when the
    session that owns it ends. `create` runs from the repo root and reports what it made
    as its LAST non-empty stdout line - a JSON object of string values, e.g.
    {"name": "epicmd_it_2873"} - which dependents read as `{<id>.<key>}` ({db.name}).
    `destroy` (optional) gets the same outputs; without it the resource is forgotten
    rather than torn down, and the release report says so.
    """

    id: str
    create: str
    label: str = ""
    scope: str = LOCAL_SCOPE_SESSION
    destroy: str = ""
    timeout: float = DEFAULT_LOCAL_RESOURCE_TIMEOUT
    # Optional declaration of the keys `create` reports. When given, a `{<id>.<key>}`
    # naming any other key is refused at load and a create that omits one fails; when
    # empty, keys are only checked at runtime.
    outputs: tuple[str, ...] = ()
    # Daily expiry, for resources something else removes on a schedule (a team job that
    # drops every temporary database at midnight). `expires = "daily"` is the only kind;
    # `expires_at` is "HH:MM" in `timezone` (an IANA name; "" = this machine's local
    # time). A resource created before the most recent such boundary reads as
    # "expired": its outputs are no longer substituted and no destroy is ever run for
    # it - it is already gone.
    expires: str = ""
    expires_at: str = LOCAL_DEFAULT_EXPIRES_AT
    timezone: str = ""

    @property
    def shared(self) -> bool:
        return self.scope == LOCAL_SCOPE_SHARED


@dataclass(frozen=True)
class LocalEnvSpec:
    """A system's `[systems.<id>.local]` table: the services that make up its local
    environment, the resources they need provisioned, plus two purely descriptive facts
    a developer needs next to them (which AWS profile the services expect to be logged
    in with, and where the test users' credentials live). PM Studio shows those two; it
    never reads or acts on them.

    `link_from_primary` lists repo-root-relative paths/globs (node_modules, mostly)
    that a NON-default session's worktree borrows from the primary checkout instead of
    installing its own; see local_env's checkout preparation."""

    aws_profile: str = ""
    test_users_secret: str = ""
    services: tuple[LocalServiceSpec, ...] = ()
    resources: tuple[LocalResourceSpec, ...] = ()
    link_from_primary: tuple[str, ...] = ()

    def service(self, service_id: str) -> LocalServiceSpec | None:
        return next((s for s in self.services if s.id == service_id), None)

    def resource(self, resource_id: str) -> LocalResourceSpec | None:
        return next((r for r in self.resources if r.id == resource_id), None)

    def consumed_resources(self, service_id: str) -> list[str]:
        """Every resource whose outputs `service_id` may substitute: those it depends
        on (directly or through another service), then those it `uses`."""
        ids = [d for d in self.closure(service_id) if self.resource(d) is not None]
        spec = self.service(service_id)
        for rid in getattr(spec, "uses", ()):
            if rid not in ids:
                ids.append(rid)
        return ids

    def closure(self, service_id: str) -> list[str]:
        """Every service and resource id `service_id` depends on, directly or through
        another service, nearest first and without duplicates (config guarantees no
        cycles). What `{<id>.<key>}` may name, and what a start brings up."""
        seen: list[str] = []
        pending = list(getattr(self.service(service_id), "depends_on", ()))
        while pending:
            dep = pending.pop(0)
            if dep in seen:
                continue
            seen.append(dep)
            spec = self.service(dep)
            if spec is not None:
                pending.extend(spec.depends_on)
        return seen


@dataclass(frozen=True)
class LocalEnvSettings:
    """The top-level `[local_env]` table: instance-wide limits on local environments.

    `max_session_envs` caps how many DISTINCT (session, system) environments may have a
    service starting or running at once. Each one is a full copy of a system's stack
    (an API, a web app, their watchers) on the developer's machine, so an unbounded
    number of chat sessions each bringing theirs up is how a laptop runs out of memory.
    Shared services do not count: there is only ever one of each.

    `idle_stop_minutes` (0 = never, the default) stops a session's environment - its
    session-scoped services, in every system - once that session has made no local-env
    call (start, stop, wait, logs, status...) for that many minutes and has no dev task
    running. Shared services are never idle-stopped, and nothing provisioned is lost:
    resources (a temporary database) and the prepared session checkout are kept, so the
    next start is only process startup. The stopped services' status says why. The
    point is the limit above: a chat someone walked away from should not hold one of
    the few slots until the server restarts."""

    max_session_envs: int = DEFAULT_MAX_SESSION_ENVS
    idle_stop_minutes: int = 0


@dataclass(frozen=True)
class SystemSpec:
    """One declared system: a bounded piece of technology/code, the unit a change is
    contained within. Distinct from a product - see _parse_systems for the split.

    Everything but the label is optional and, for now, purely descriptive: `path` and
    `repo` say where the code lives, and `guidance`/`pipelines` are the seams for the
    engineering guidance and CI/CD a system will carry later. Nothing enforces them yet,
    so declaring a system costs one line and a deployment can fill the rest in over time.
    """

    label: str
    # Repo-root-relative source folder, and/or the system's own repo when it has one.
    # A system can legitimately have both (a folder in this repo AND its own remote),
    # one, or neither (a system tracked here but built somewhere unrelated).
    path: str = ""
    repo: str = ""
    # Forward-looking, descriptive only: where this system's engineering guidance lives
    # and the pipelines that build it. Surfaced in the UI and the PM prompt; not acted on.
    guidance: str = ""
    pipelines: tuple[str, ...] = ()
    # Repo-root-relative path to this system's NON-NEGOTIABLE git workflow rules
    # (branching, PR targets, commit conventions...). Unlike `guidance`, this IS acted
    # on: every dev task dispatched for this system gets the file's contents appended
    # to its prompt verbatim at dispatch time, and an independent judge verifies the
    # finished work against them (see tasks.py / judge.py). Kept as a path rather than
    # inline text so the rules are versioned with the code they govern and each
    # session's worktree reads its own branch's copy.
    gitflow: str = ""
    # Optional `[systems.<id>.local]`: the services PM Studio may start and stop on the
    # developer's machine for this system (see LocalEnvSpec and local_env.py). None
    # means the system declares no local environment and the feature ignores it.
    local: LocalEnvSpec | None = None


@dataclass(frozen=True)
class SignalsConfig:
    """Engineering-intelligence inputs (the `[signals]` table). Optional: with no
    table the layer still runs over git and the configured trackers with these
    defaults; every threshold and weight can be tuned per deployment, and the judge's
    own suggestions are layered on top at runtime (signals/tuning.json)."""

    # Earliest activity pulled, ISO date. History before it is never fetched.
    since: str = "2024-01-01"
    # A day someone was active counts for this many hours, split across what they
    # touched by signal weight; explicit worklogs override the split.
    capacity_hours_per_day: float = 8.0
    # IANA zone used to bucket instants into working days and hours.
    timezone: str = "America/New_York"
    # Background refresh cadence; 0 disables the loop (manual refresh only). Every
    # source is incremental after its first pull, so a short cadence is cheap.
    auto_refresh_minutes: float = 15.0
    # `git fetch --all --prune` each checkout before scanning, so work on any branch
    # anyone has pushed is in the ledger within one refresh - not only what somebody
    # pulled into this machine. Uses the checkouts' own remotes and credentials.
    git_fetch: bool = True
    # Extra repo-root-relative git checkouts to scan beyond every [systems] path.
    extra_repos: tuple[str, ...] = ()
    # Model for the intelligence judge; empty = strongest declared (opus tier).
    judge_model: str = ""
    # Run the judge automatically after each refresh.
    auto_judge: bool = False
    # Rule thresholds and signal weights, merged over the package defaults.
    thresholds: dict[str, float] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)
    # ADO projects whose pull requests are pulled (needs the ADO tracker's PAT).
    ado_pr_projects: tuple[str, ...] = ()
    # "observed": a person's hours are exactly their active days x capacity (nothing is
    # invented for days with no signal). "scaled": each person's week is brought up to
    # the full weekly capacity (5 x capacity_hours_per_day), keeping the activity split -
    # the timesheet-replacement stance finance usually wants. The page can preview either.
    allocation_mode: str = "observed"
    # Where a KNOWN ticket that is planned onto no project lands, by declaration:
    # "<tracker>:<tracker project>[:<raw type>]" -> project id. The fallback for whole
    # classes of work a team never files under an epic (support tickets, ops items).
    # Weaker than a change link or a parent epic, and reported separately as
    # via="default-project" so it never hides behind real attribution.
    default_projects: dict[str, str] = field(default_factory=dict)
    # How much to believe a source's explicit time (worklogs, CompletedWork), per source
    # id: "full" books it as hours where logged; "signal" (the default) keeps it only as
    # evidence that the ticket was worked, weighted by its size, and lets activity decide
    # the hours; "ignore" drops it. Jira worklogs written by status-dwell automation are
    # exactly the case the default protects against.
    worklog_trust: dict[str, str] = field(default_factory=dict)
    # Where commits with NO ticket key land, by repository path (repo-root-relative) or
    # system id -> project id. For repositories whose commit convention strips keys.
    # Reported as via="default-project", like default_projects.
    default_repos: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Config:
    repo_root: Path
    project_name: str
    default_session_name: str
    # Directory under repo_root that holds `workspace/` runtime state. Default
    # "pm_studio"; a system migrated from a locally-built pm_agent keeps
    # "pm_agent" here so live worktrees/registered paths stay valid in place.
    workspace_root: str
    host: str
    port: int
    # Which page GET / serves: "sessions" (the default, unchanged behavior) or
    # "overview". A deployment whose stakeholders open the tool to ASK about the work
    # points the front door at the read-first answer; the sessions list keeps a stable
    # address at /sessions-page either way, and the nav follows (see /auth/me).
    landing: str = "sessions"
    # Every product this deployment declares, id -> display label, ordered depth first so
    # each product is immediately followed by its own descendants (see _parse_products).
    # Sub-products are products in every respect - the flatness here is what keeps every
    # existing caller that just wants "is this a real product id" or "what do I call it"
    # unchanged, at any depth.
    products: dict[str, str] = field(default_factory=dict)
    # The hierarchy, held separately and only for the products that HAVE a parent:
    # child id -> parent id, at any depth (a child may itself be a parent). Empty on a
    # deployment with a flat [products] table, which is exactly what every deployment
    # before hierarchy had. Kept out of `products` so the taxonomy stays one dict to
    # iterate and this stays a lookup you opt into.
    product_parents: dict[str, str] = field(default_factory=dict)
    # Every system this deployment declares, id -> spec, in declaration order (which is
    # display order, same convention as `products`). Empty on every deployment that has
    # not declared [systems] - and an empty table is what keeps the whole system layer
    # dormant, exactly as an absent [[trackers]] keeps the tracker feature dormant.
    systems: dict[str, SystemSpec] = field(default_factory=dict)
    # The many-to-many edge, from the product's side: product id -> the systems it
    # touches, in declaration order. Held from this side only because that is the side
    # the operator declares and the side a pinned PM asks about ("what do I touch?");
    # the reverse (which products touch a system) is derived in roadmap.products_of_system
    # rather than stored, so the two can never disagree.
    product_systems: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Operator-declared facts per product (owner, team, stage, description), only for
    # the products that declared any - same opt-in-lookup pattern as product_parents,
    # so a deployment that never writes a metadata key carries an empty dict and no
    # consumer has to know the feature exists.
    product_meta: dict[str, ProductMeta] = field(default_factory=dict)
    # Ids declared as BOTH a product and a system: the explicit, temporary state of an id
    # being reclassified from product to system. Its product board keeps loading and
    # stays fully usable, so the changes on it can be re-homed at the operator's pace
    # instead of being orphaned the moment the [products] entry is deleted. Empty on any
    # deployment that is not mid-restructure.
    transitional_ids: tuple[str, ...] = ()
    # Markdown-ish lines describing where product sources live at the repo root -
    # injected verbatim into the PM system prompt's layout section.
    repo_layout: str = DEFAULT_REPO_LAYOUT
    # Optional [models] override; empty means "use the package defaults" (models.py).
    models: dict[str, str] = field(default_factory=dict)
    default_model: str | None = None
    # [models] quick: the model quick dev tasks run on (None: the session's model).
    quick_model: str | None = None
    # Append-only local prompt fragments ("" when the file is absent).
    pm_instructions: str = ""
    dev_instructions: str = ""
    # Repo-root-relative paths of local knowledge docs the PM should know exist.
    knowledge_files: tuple[str, ...] = ()
    # "personal" (default) or "enterprise" - see MODES. Personal mode keeps the
    # historical no-accounts behavior byte for byte.
    mode: str = MODE_PERSONAL
    # None unless the deployment configured [smtp].
    smtp: SmtpConfig | None = None
    costing: CostingConfig = field(default_factory=CostingConfig)
    # Empty unless the deployment declared [[trackers]]; the whole Jira/ADO feature is
    # dormant in that case and the board looks exactly as it did before.
    trackers: tuple[TrackerConfig, ...] = ()
    # Optional [signals] table - see SignalsConfig. Always present with defaults.
    signals: SignalsConfig = field(default_factory=SignalsConfig)
    # Optional [local_env] table - see LocalEnvSettings. Always present with defaults.
    local_env: LocalEnvSettings = field(default_factory=LocalEnvSettings)
    # Optional [agents.mcp.<name>] tables: the MCP servers PM Studio hands to its agents
    # (see AgentMcpServer and agent_mcp.py). The startup snapshot only - agent_mcp
    # re-reads this part of config.toml when the file changes, so a running server
    # picks up new servers without a restart.
    agent_mcp: tuple["AgentMcpServer", ...] = ()

    def tracker(self, tracker_id: str) -> TrackerConfig | None:
        return next((t for t in self.trackers if t.id == tracker_id), None)

    @property
    def is_enterprise(self) -> bool:
        return self.mode == MODE_ENTERPRISE

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def workspace_dir(self) -> Path:
        """Absolute `<workspace_root>/workspace` for the PRIMARY checkout. Session
        worktrees use workspace_rel against their own root instead."""
        return self.repo_root / self.workspace_root / "workspace"

    @property
    def workspace_rel(self) -> str:
        """Repo-root-relative workspace path, valid inside any worktree."""
        return f"{self.workspace_root}/workspace"

    @property
    def archive_rel(self) -> str:
        return f"{self.workspace_root}/workspace/archive"

    @property
    def local_dir(self) -> Path:
        return self.repo_root / LOCAL_DIR_NAME


def _read_optional(path: Path) -> str:
    if path.is_file():
        return path.read_text().strip()
    return ""


def _parse_env_file(text: str) -> tuple[list[tuple[str, str]], list[int]]:
    """Splits `.env` text into (assignments in file order, 1-based numbers of lines that
    could not be read as one).

    Deliberately small: `KEY=value` one per line, `#` comment lines, blank lines, an
    optional `export ` prefix, and a single- or double-quoted value taken literally (which
    is how a value keeps a leading space or a `#`). No `$VAR` interpolation, no backslash
    escapes, no multi-line values - those are shell features, and a deployment that needs
    them should export the variable from the shell that already implements them rather
    than have this package grow a second, subtly different shell.
    """
    pairs: list[tuple[str, str]] = []
    bad_lines: list[int] = []
    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # `export FOO=bar` is what a file meant to be `source`d looks like; the prefix is
        # noise here. (`export=bar` is a variable named export, and splits differently.)
        words = line.split(None, 1)
        if words[0] == "export":
            line = words[1] if len(words) > 1 else ""
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key or any(char.isspace() for char in key):
            bad_lines.append(number)
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        pairs.append((key, value))
    return pairs, bad_lines


def _load_env_file(path: Path) -> None:
    """Merges the target repo's `.env` into os.environ, before anything here reads it.

    `.env` is a library convention, not an OS or shell feature: nothing in the shell or
    the interpreter reads that file on its own, so a deployment keeping its tracker token
    there has no such variable in os.environ unless something puts it there. This is that
    something - it is what lets `token_env`/`email_env`/`password_env` name a variable the
    operator only ever wrote in `.env`, with no wrapper script exporting it first.

    A variable already set in the environment always WINS over the file: a real export -
    from a run script, CI, systemd, a shell profile - is a deliberate act aimed at this one
    process, while `.env` is a default sitting in the repo, and a stale file must never
    quietly beat the environment. (Set-but-empty counts as unset, because every other env
    read in this module already treats "" that way.)

    A missing file is a silent no-op; most deployments have none. A file that IS read says
    which variables it supplied, by NAME only - the values are credentials and never go
    anywhere near a log.
    """
    try:
        text = path.read_text()
    except FileNotFoundError:
        return
    except OSError as exc:
        # Unreadable is worth saying out loud (a mode-0600 file owned by someone else is
        # exactly how a token goes missing) but never fatal: the studio boots without it.
        print(f"[pm_studio] WARNING: could not read {path}: {exc}", file=sys.stderr)
        return

    pairs, bad_lines = _parse_env_file(text)
    if bad_lines:
        numbers = ", ".join(str(number) for number in bad_lines)
        print(
            f"[pm_studio] WARNING: {path} line(s) {numbers} are not `KEY=value` and were "
            "skipped. A variable written there is not set.",
            file=sys.stderr,
        )

    applied: list[str] = []
    already_set: list[str] = []
    for key, value in pairs:
        if os.environ.get(key, ""):
            already_set.append(key)
            continue
        os.environ[key] = value
        applied.append(key)

    if applied:
        print(f"[pm_studio] {path}: set {', '.join(applied)}")
    if already_set:
        print(
            f"[pm_studio] {path}: {', '.join(already_set)} already in the environment, "
            "left unchanged"
        )


def _fatal(message: str) -> NoReturn:
    """Refuses to start, loudly. Used for config that would otherwise produce a
    deployment which looks like it is working while quietly meaning something the
    operator did not write - see _parse_mode and _parse_products."""
    print(f"[pm_studio] FATAL: {message}", file=sys.stderr)
    raise SystemExit(2)


def _parse_systems(raw: dict, config_path: Path) -> dict[str, SystemSpec]:
    """Reads [systems] into id -> SystemSpec, in declaration order.

    A SYSTEM is a bounded piece of technology/code - a service, an app, a module - and
    it is the unit a change is contained within: a change belongs to exactly one system,
    which is what makes its blast radius knowable. A PRODUCT is the business-facing
    thing: a line of business, or an umbrella over the technology that serves one. A
    product touches many systems and a system is touched by many products, which is why
    the edge between them is declared per product (`systems = [...]`) rather than being
    a level of either tree.

    The two tables are deliberately separate rather than one renamed into the other:
    products keep owning the roadmap boards (roadmaps are product- and initiative-first;
    a system never has a roadmap of its own), and every deployment that has not declared
    [systems] behaves exactly as it did before this table existed.

    Both entry spellings from [products] work here too - a bare label, or a table:

        [systems]
        claims = "Claims Processor"

        [systems.rides]
        label = "Rides & Logistics"
        path = "services/rides"
        repo = "github.com/org/rides"

    A table entry may also carry a `local` sub-table - the services PM Studio can start
    and stop on the developer's machine for that system (see _parse_local_env):

        [systems.rides.local]
        aws_profile = "rides-dev"

        [[systems.rides.local.services]]
        id = "api"
        command = "npm run start:api"
        port = 3001
    """
    declared = raw.get("systems", {})
    if not isinstance(declared, dict):
        _fatal(f"{config_path} has [systems] as a {type(declared).__name__}; it must be a table")

    systems: dict[str, SystemSpec] = {}
    for key, value in declared.items():
        system_id = str(key)
        if isinstance(value, str):
            systems[system_id] = SystemSpec(label=value or system_id)
            continue
        if not isinstance(value, dict):
            _fatal(
                f"{config_path} has [systems] entry {system_id!r} as a "
                f"{type(value).__name__}; a system is either a label string "
                '(claims = "Claims Processor") or a table with `label` and optional '
                "`path`, `repo`, `guidance`, `gitflow`, `pipelines` and `local`"
            )
        # Systems do not nest. `parent` is meaningful in the sibling [products] table, so
        # an operator could reasonably expect it here - refuse loudly rather than accept
        # the line and silently drop the relationship they thought they declared.
        if "parent" in value:
            _fatal(
                f"{config_path} has system {system_id!r} with a `parent`, but systems do "
                "not nest. If it is contained by another system, declare it as its own "
                "system; to say which products touch it, list it in that product's "
                "`systems = [...]`."
            )
        pipelines = value.get("pipelines", ())
        if isinstance(pipelines, str) or not isinstance(pipelines, (list, tuple)):
            _fatal(
                f"{config_path} has system {system_id!r} with `pipelines` as a "
                f"{type(pipelines).__name__}; it must be an array, e.g. "
                'pipelines = ["rides-ci"]'
            )
        # A missing label is cosmetic, not structural - same call as [products] makes.
        systems[system_id] = SystemSpec(
            label=str(value.get("label", "")).strip() or system_id,
            path=str(value.get("path", "")).strip(),
            repo=str(value.get("repo", "")).strip(),
            guidance=str(value.get("guidance", "")).strip(),
            pipelines=tuple(str(p).strip() for p in pipelines if str(p).strip()),
            gitflow=str(value.get("gitflow", "")).strip(),
            local=_parse_local_env(value["local"], system_id, config_path)
            if "local" in value
            else None,
        )
    return systems


_LOCAL_ENV_KEYS = frozenset(
    {"aws_profile", "test_users_secret", "services", "resources", "link_from_primary"}
)
_LOCAL_SERVICE_KEYS = frozenset(
    {
        "id", "label", "command", "cwd", "port", "ready_url", "url",
        "depends_on", "params", "env", "ready_timeout", "scope", "uses",
        "setup", "setup_timeout",
    }
)
_LOCAL_RESOURCE_KEYS = frozenset(
    {
        "id", "label", "scope", "create", "destroy", "timeout", "outputs",
        "expires", "expires_at", "timezone",
    }
)
_LOCAL_SETTINGS_KEYS = frozenset({"max_session_envs", "idle_stop_minutes"})


def _local_scope(entry: dict, where: str, config_path: Path) -> str:
    scope = entry.get("scope", LOCAL_SCOPE_SESSION)
    if scope not in LOCAL_SCOPES:
        _fatal(
            f"{config_path} has {where} with `scope` = {scope!r}; it must be one of "
            f"{', '.join(repr(s) for s in LOCAL_SCOPES)}"
        )
    return scope


def _local_id(
    entry: dict, where: str, system_where: str, config_path: Path, seen: set[str], kind: str
) -> str:
    item_id = entry.get("id")
    if not isinstance(item_id, str) or not item_id.strip():
        _fatal(f"{config_path} has {where} with no `id`")
    item_id = item_id.strip()
    if not LOCAL_SERVICE_ID.match(item_id):
        _fatal(
            f"{config_path} has {where} {kind} {item_id!r}: ids may only use lowercase "
            "letters, digits and '-' (they appear in URLs, placeholders and log file names)"
        )
    if item_id in seen:
        # One namespace for services and resources: `depends_on` and `{<id>.<key>}`
        # name either, so the same id on both would make every reference ambiguous.
        _fatal(
            f"{config_path} has {system_where} declaring id {item_id!r} twice "
            "(services and resources share one set of ids)"
        )
    seen.add(item_id)
    return item_id


def _parse_local_env(declared, system_id: str, config_path: Path) -> LocalEnvSpec:
    """Reads one `[systems.<id>.local]` table into a LocalEnvSpec.

    Stricter than the rest of [systems] on purpose, because what it declares is not
    descriptive: every service here is a shell command PM Studio will actually run, and
    a mistake in it surfaces as a process that does the wrong thing rather than as a
    wrong label. So unknown keys are refused (a misspelt `depends_on` would otherwise
    start a service before the thing it needs), as is every structural error - duplicate
    or malformed ids, a dependency on a service or resource that does not exist or a
    cycle of them, a port that is not a port (nor "auto"), a shared service depending on
    something that only exists per session, and any `{placeholder}` that nothing would
    fill (which would otherwise reach the shell as literal braces). See
    _check_local_placeholders for exactly what each placeholder may name.

    Same posture as everything else in this module: refuse to start, loudly, rather than
    boot a local environment that silently means something the operator did not write.
    """
    where = f"system {system_id!r} `local`"
    if not isinstance(declared, dict):
        _fatal(
            f"{config_path} has {where} as a {type(declared).__name__}; it must be a "
            f"table ([systems.{system_id}.local])"
        )
    unknown = sorted(set(declared) - _LOCAL_ENV_KEYS)
    if unknown:
        _fatal(
            f"{config_path} has {where} with unknown key(s) {', '.join(unknown)}; "
            f"allowed: {', '.join(sorted(_LOCAL_ENV_KEYS))}"
        )
    raw_services = declared.get("services", [])
    if not isinstance(raw_services, list):
        _fatal(
            f"{config_path} has {where} `services` as a {type(raw_services).__name__}; "
            f"declare each one as [[systems.{system_id}.local.services]]"
        )
    raw_resources = declared.get("resources", [])
    if not isinstance(raw_resources, list):
        _fatal(
            f"{config_path} has {where} `resources` as a {type(raw_resources).__name__}; "
            f"declare each one as [[systems.{system_id}.local.resources]]"
        )

    seen: set[str] = set()
    resources: list[LocalResourceSpec] = []
    for index, entry in enumerate(raw_resources):
        r_where = f"{where} resource #{index + 1}"
        if not isinstance(entry, dict):
            _fatal(f"{config_path} has {r_where} as a {type(entry).__name__}; it must be a table")
        unknown = sorted(set(entry) - _LOCAL_RESOURCE_KEYS)
        if unknown:
            _fatal(
                f"{config_path} has {r_where} with unknown key(s) {', '.join(unknown)}; "
                f"allowed: {', '.join(sorted(_LOCAL_RESOURCE_KEYS))}"
            )
        resource_id = _local_id(entry, r_where, where, config_path, seen, "resource")
        r_where = f"{where} resource {resource_id!r}"
        commands: dict[str, str] = {}
        for key in ("create", "destroy", "label"):
            item = entry.get(key, "")
            if not isinstance(item, str):
                _fatal(f"{config_path} has {r_where} with `{key}` as a {type(item).__name__}; it must be a string")
            commands[key] = item.strip()
        if not commands["create"]:
            _fatal(f"{config_path} has {r_where} with no `create` command")
        timeout = entry.get("timeout", DEFAULT_LOCAL_RESOURCE_TIMEOUT)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            _fatal(f"{config_path} has {r_where} with `timeout` = {timeout!r}; it must be a positive number of seconds")
        outputs = entry.get("outputs", [])
        if not isinstance(outputs, list) or not all(
            isinstance(o, str) and LOCAL_OUTPUT_KEY.match(o) for o in outputs
        ):
            _fatal(
                f"{config_path} has {r_where} with `outputs` = {outputs!r}; it must be an "
                'array of key names (letters, digits, _), e.g. outputs = ["name"]'
            )
        expiry: dict[str, str] = {}
        for key, default in (("expires", ""), ("expires_at", LOCAL_DEFAULT_EXPIRES_AT), ("timezone", "")):
            item = entry.get(key, default)
            if not isinstance(item, str):
                _fatal(f"{config_path} has {r_where} with `{key}` as a {type(item).__name__}; it must be a string")
            expiry[key] = item.strip()
        if expiry["expires"] and expiry["expires"] not in LOCAL_EXPIRES:
            _fatal(
                f"{config_path} has {r_where} with `expires` = {expiry['expires']!r}; the "
                f"only kind is {', '.join(repr(e) for e in LOCAL_EXPIRES)}"
            )
        if not expiry["expires"] and ("expires_at" in entry or "timezone" in entry):
            _fatal(
                f"{config_path} has {r_where} with `expires_at`/`timezone` but no "
                '`expires`; add expires = "daily" (or drop them)'
            )
        if not LOCAL_HHMM.match(expiry["expires_at"]):
            _fatal(
                f"{config_path} has {r_where} with `expires_at` = {expiry['expires_at']!r}; "
                'it must be "HH:MM" on a 24-hour clock, e.g. "00:00"'
            )
        if expiry["timezone"]:
            try:
                ZoneInfo(expiry["timezone"])
            except (ZoneInfoNotFoundError, ValueError):
                _fatal(
                    f"{config_path} has {r_where} with `timezone` = {expiry['timezone']!r}, "
                    'which is not a known IANA time zone (e.g. "America/New_York")'
                )
        resources.append(
            LocalResourceSpec(
                id=resource_id,
                create=commands["create"],
                label=commands["label"],
                scope=_local_scope(entry, r_where, config_path),
                destroy=commands["destroy"],
                timeout=float(timeout),
                outputs=tuple(outputs),
                expires=expiry["expires"],
                expires_at=expiry["expires_at"],
                timezone=expiry["timezone"],
            )
        )

    services: list[LocalServiceSpec] = []
    for index, entry in enumerate(raw_services):
        s_where = f"{where} service #{index + 1}"
        if not isinstance(entry, dict):
            _fatal(f"{config_path} has {s_where} as a {type(entry).__name__}; it must be a table")
        unknown = sorted(set(entry) - _LOCAL_SERVICE_KEYS)
        if unknown:
            _fatal(
                f"{config_path} has {s_where} with unknown key(s) {', '.join(unknown)}; "
                f"allowed: {', '.join(sorted(_LOCAL_SERVICE_KEYS))}"
            )
        service_id = _local_id(entry, s_where, where, config_path, seen, "service")
        s_where = f"{where} service {service_id!r}"

        command = entry.get("command")
        if not isinstance(command, str) or not command.strip():
            _fatal(f"{config_path} has {s_where} with no `command`")
        command = command.strip()

        cwd = entry.get("cwd", ".")
        if not isinstance(cwd, str):
            _fatal(f"{config_path} has {s_where} with `cwd` as a {type(cwd).__name__}; it must be a string")
        cwd = cwd.strip() or "."
        if Path(cwd).is_absolute() or cwd.startswith(("/", "\\")):
            _fatal(f"{config_path} has {s_where} with an absolute `cwd` ({cwd!r}); it is repo-root-relative")

        port = entry.get("port")
        if port is not None and port != LOCAL_AUTO_PORT and (
            isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535
        ):
            _fatal(
                f"{config_path} has {s_where} with `port` = {port!r}; it must be an "
                f'integer 1-65535 or "{LOCAL_AUTO_PORT}"'
            )

        strings: dict[str, str] = {}
        for key in ("label", "ready_url", "url"):
            item = entry.get(key, "")
            if not isinstance(item, str):
                _fatal(f"{config_path} has {s_where} with `{key}` as a {type(item).__name__}; it must be a string")
            strings[key] = item.strip()
        if strings["ready_url"] and not strings["ready_url"].startswith(("http://", "https://")):
            _fatal(f"{config_path} has {s_where} with `ready_url` = {strings['ready_url']!r}; it must be an http(s) URL")

        depends_on = entry.get("depends_on", [])
        if not isinstance(depends_on, list) or not all(isinstance(d, str) for d in depends_on):
            _fatal(
                f"{config_path} has {s_where} with `depends_on` = {depends_on!r}; it must be "
                'an array of service or resource ids, e.g. depends_on = ["secrets"]'
            )
        uses = entry.get("uses", [])
        if not isinstance(uses, list) or not all(isinstance(u, str) for u in uses):
            _fatal(
                f"{config_path} has {s_where} with `uses` = {uses!r}; it must be an array "
                'of resource ids, e.g. uses = ["db"]'
            )

        tables: dict[str, tuple[tuple[str, str], ...]] = {}
        for key in ("params", "env"):
            table = entry.get(key, {})
            if not isinstance(table, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in table.items()
            ):
                _fatal(
                    f"{config_path} has {s_where} with `{key}` = {table!r}; it must be a "
                    f'table of strings, e.g. {key} = {{ NAME = "value" }}'
                )
            tables[key] = tuple((str(k), v) for k, v in table.items())
        for name, _ in tables["params"]:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                _fatal(f"{config_path} has {s_where} with param name {name!r}; use letters, digits and '_'")
            if name in LOCAL_BUILTINS:
                _fatal(
                    f"{config_path} has {s_where} declaring param {name!r}, a name PM Studio "
                    f"fills itself ({', '.join('{' + b + '}' for b in LOCAL_BUILTINS)}); "
                    "pick another name"
                )

        timeout = entry.get("ready_timeout", DEFAULT_LOCAL_READY_TIMEOUT)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            _fatal(f"{config_path} has {s_where} with `ready_timeout` = {timeout!r}; it must be a positive number of seconds")

        scope = _local_scope(entry, s_where, config_path)
        setup = entry.get("setup", "")
        if not isinstance(setup, str):
            _fatal(f"{config_path} has {s_where} with `setup` as a {type(setup).__name__}; it must be a string")
        setup = setup.strip()
        setup_timeout = entry.get("setup_timeout", DEFAULT_LOCAL_SETUP_TIMEOUT)
        if isinstance(setup_timeout, bool) or not isinstance(setup_timeout, (int, float)) or setup_timeout <= 0:
            _fatal(
                f"{config_path} has {s_where} with `setup_timeout` = {setup_timeout!r}; it must "
                "be a positive number of seconds"
            )
        # `setup` runs before the service has a port and before its dependencies are
        # used, so only what exists at that point may be substituted into it.
        setup_allowed = {name for name, _ in tables["params"]} | {"worktree", "repo_root"}
        if scope != LOCAL_SCOPE_SHARED:
            setup_allowed.add("session")
        setup_problems: list[str] = []
        for token in LOCAL_PLACEHOLDER.findall(setup):
            if "." in token or token == "port":
                setup_problems.append(
                    f"{{{token}}} does not exist yet when `setup` runs (it runs before the "
                    "service is given a port and before its dependencies are used); only "
                    "params, {session}, {worktree} and {repo_root} are available to `setup`"
                )
            elif token == "session" and scope == LOCAL_SCOPE_SHARED:
                setup_problems.append("{session} is not available: the service is shared, so it belongs to no session")
            elif token not in setup_allowed:
                setup_problems.append(
                    f"{{{token}}} is not declared in `params` (give each a default, even an empty one)"
                )
        if setup_problems:
            _fatal(
                f"{config_path} has {s_where} `setup` with unresolvable placeholder(s): "
                + "; ".join(setup_problems)
            )

        services.append(
            LocalServiceSpec(
                id=service_id,
                command=command,
                label=strings["label"],
                cwd=cwd,
                port=port,
                ready_url=strings["ready_url"],
                url=strings["url"],
                depends_on=tuple(d.strip() for d in depends_on),
                params=tables["params"],
                env=tables["env"],
                ready_timeout=float(timeout),
                scope=scope,
                uses=tuple(u.strip() for u in uses),
                setup=setup,
                setup_timeout=float(setup_timeout),
            )
        )

    # Dependencies are checked once every id is known, so declaration order is free.
    by_id = {s.id: s for s in services}
    resource_by_id = {r.id: r for r in resources}
    for service in services:
        for dep in service.depends_on:
            if dep not in by_id and dep not in resource_by_id:
                declared_ids = ", ".join([*by_id, *resource_by_id]) or "none"
                _fatal(
                    f"{config_path} has {where} service {service.id!r} depending on "
                    f"{dep!r}, which is not a service or resource of this system "
                    f"(declared: {declared_ids})"
                )
            if dep == service.id:
                _fatal(f"{config_path} has {where} service {service.id!r} depending on itself")
            target = by_id.get(dep) or resource_by_id[dep]
            # A shared service runs once for every session, so it cannot depend on
            # something there is one of PER session - which session's would it get?
            if service.shared and not target.shared:
                _fatal(
                    f"{config_path} has {where} shared service {service.id!r} depending "
                    f"on {dep!r}, which is session-scoped; a shared service may only depend "
                    'on other shared entries (set scope = "shared" on it too, or make '
                    f"{service.id!r} session-scoped)"
                )
        for used in service.uses:
            if used not in resource_by_id:
                _fatal(
                    f"{config_path} has {where} service {service.id!r} with `uses` = "
                    f"{used!r}, which is not a resource of this system (declared: "
                    f"{', '.join(resource_by_id) or 'none'})"
                )
            if used in service.depends_on:
                _fatal(
                    f"{config_path} has {where} service {service.id!r} listing {used!r} in "
                    "both `depends_on` (create it on start) and `uses` (never create it); "
                    "pick one"
                )
            if service.shared and not resource_by_id[used].shared:
                _fatal(
                    f"{config_path} has {where} shared service {service.id!r} using "
                    f"{used!r}, which is session-scoped; a shared service may only use "
                    "shared resources"
                )
    # Cycle check: a depth-first walk that remembers the path it is on. Resources have
    # no dependencies of their own, so only services can form one.
    done: set[str] = set()

    def visit(service_id: str, path: list[str]) -> None:
        if service_id in path:
            cycle = " -> ".join(path[path.index(service_id):] + [service_id])
            _fatal(f"{config_path} has {where} with a dependency cycle: {cycle}")
        if service_id in done or service_id not in by_id:
            return
        for dep in by_id[service_id].depends_on:
            visit(dep, path + [service_id])
        done.add(service_id)

    for service in services:
        visit(service.id, [])

    link_from_primary = declared.get("link_from_primary", [])
    if not isinstance(link_from_primary, list) or not all(
        isinstance(p, str) and p.strip() for p in link_from_primary
    ):
        _fatal(
            f"{config_path} has {where} `link_from_primary` = {link_from_primary!r}; it "
            'must be an array of repo-root-relative paths or globs, e.g. '
            'link_from_primary = ["src/app/node_modules"]'
        )
    for item in link_from_primary:
        if Path(item.strip()).is_absolute() or item.strip().startswith(("/", "\\")):
            _fatal(f"{config_path} has {where} `link_from_primary` entry {item!r}; it must be repo-root-relative")

    def text(key: str) -> str:
        item = declared.get(key, "")
        if not isinstance(item, str):
            _fatal(f"{config_path} has {where} `{key}` as a {type(item).__name__}; it must be a string")
        return item.strip()

    local = LocalEnvSpec(
        aws_profile=text("aws_profile"),
        test_users_secret=text("test_users_secret"),
        services=tuple(services),
        resources=tuple(resources),
        link_from_primary=tuple(p.strip() for p in link_from_primary),
    )
    _check_local_placeholders(local, where, config_path)
    return local


def _check_local_placeholders(local: LocalEnvSpec, where: str, config_path: Path) -> None:
    """Every `{placeholder}` must be one something will fill, decided here rather than
    at launch so a typo is a startup error instead of a service that fails (or worse,
    runs with literal braces in its arguments) the first time someone starts it.

    What each one may name:

      service command / env values / url / ready_url:
        {name}         a declared `params` entry
        {port}         this service's own port (it must declare one, fixed or "auto")
        {session}      the session id - session-scoped services only; a shared service
                       belongs to no session
        {worktree}     the checkout it runs from
        {repo_root}    the primary checkout (everywhere, every scope)
        {<id>.port}    a service this one depends on, directly or transitively, that
                       declares a port
        {<id>.<key>}   a resource this one depends on, directly or transitively, or
                       `uses` (and, when the resource declares `outputs`, one of those
                       keys); for a used resource that is not ready it is ""
      resource create:  {session} (session scope only), {worktree}, {repo_root}
      resource destroy: {session} (session scope only), {repo_root}, {<own id>.<key>}
    """
    services = {s.id: s for s in local.services}
    resources = {r.id: r for r in local.resources}

    def dotted(token: str, reachable: list[str], what: str) -> str | None:
        """None when `token` (an `<id>.<key>`) resolves, else why not."""
        target_id, key = token.split(".", 1)
        if target_id not in reachable:
            return (
                f"{{{token}}} names {target_id!r}, which {what} neither depends on "
                "(directly or through another service) nor `uses`"
            )
        if target_id in services:
            if key != "port":
                return f"{{{token}}}: a service only provides {{{target_id}.port}}"
            if services[target_id].port is None:
                return f"{{{token}}}: service {target_id!r} declares no `port`"
            return None
        declared_outputs = resources[target_id].outputs
        if declared_outputs and key not in declared_outputs:
            return (
                f"{{{token}}}: resource {target_id!r} declares outputs "
                f"{', '.join(declared_outputs)}, not {key!r}"
            )
        return None

    def check(texts: dict[str, str], allowed_plain: set[str], reachable: list[str], what: str) -> None:
        problems: list[str] = []
        for field_name, value in texts.items():
            for token in LOCAL_PLACEHOLDER.findall(value):
                if "." in token:
                    reason = dotted(token, reachable, what)
                elif token in allowed_plain:
                    reason = None
                elif token in LOCAL_BUILTINS:
                    reason = {
                        "port": f"{{port}} needs {what} to declare a `port`",
                        "session": f"{{session}} is not available to {what}: it is shared, so it belongs to no session",
                    }.get(token, f"{{{token}}} is not available to {what}")
                else:
                    reason = (
                        f"{{{token}}} is not declared in `params` (give each a default, "
                        "even an empty one)"
                    )
                if reason:
                    problems.append(f"{field_name}: {reason}")
        if problems:
            _fatal(f"{config_path} has {where} {what} with unresolvable placeholder(s): " + "; ".join(problems))

    for service in local.services:
        plain = {name for name, _ in service.params} | {"worktree", "repo_root"}
        if service.port is not None:
            plain.add("port")
        if not service.shared:
            plain.add("session")
        texts = {"command": service.command, "url": service.url, "ready_url": service.ready_url}
        texts.update({f"env.{name}": value for name, value in service.env})
        check(texts, plain, [*local.closure(service.id), *service.uses], f"service {service.id!r}")

    for resource in local.resources:
        plain = {"worktree", "repo_root"} if resource.shared else {"worktree", "repo_root", "session"}
        check({"create": resource.create}, plain, [], f"resource {resource.id!r}")
        # destroy may run after the worktree is gone (session release), so no {worktree}.
        plain = {"repo_root"} if resource.shared else {"repo_root", "session"}
        check({"destroy": resource.destroy}, plain, [resource.id], f"resource {resource.id!r}")


AGENT_MCP_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
AGENT_MCP_TRANSPORTS = ("stdio", "http", "sse")
_AGENTS_KEYS = frozenset({"mcp"})
_AGENT_MCP_KEYS = frozenset({"command", "args", "type", "url", "headers", "env", "pm", "probe"})
_AGENT_MCP_PROBE_KEYS = frozenset({"tool", "args"})
AGENT_MCP_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


class AgentConfigError(ValueError):
    """An invalid [agents] table. Fatal at startup (_parse_agents); at a hot reload
    (agent_mcp.py) it is recorded and the last good server list is kept."""


@dataclass(frozen=True)
class AgentMcpServer:
    """One `[agents.mcp.<name>]` table: an MCP server PM Studio gives its agents.

    The single place a deployment declares agent MCP servers: every launch passes
    `--strict-mcp-config`, so whatever else an agent's Claude Code config holds (its
    isolated ~/.claude.json, a project .mcp.json) is ignored. Values are kept exactly
    as written - `${VAR}` / `${VAR:-default}` references included - and resolved from
    the environment only when an agent is launched (agent_mcp.py), so a credential in
    the git-ignored .env never sits in a frozen config value or a log.

    Who gets it: dev agents get every server, the PM agent only those with `pm = true`,
    judges none.
    """

    name: str
    # "stdio" (command + args) or "http"/"sse" (url + headers).
    transport: str
    command: str = ""
    args: tuple[str, ...] = ()
    url: str = ""
    headers: tuple[tuple[str, str], ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    pm: bool = False
    # Optional `probe = { tool = "query", args = { sql = "select 1" } }`: a harmless call
    # the connection test (POST /agents/mcp/{name}/test) makes after the handshake.
    probe: "AgentMcpProbe | None" = None


@dataclass(frozen=True)
class AgentMcpProbe:
    """`probe` of an [agents.mcp.<name>] table. `args_json` is the args table as JSON
    (kept as text so the server spec stays hashable); string values may use ${VAR}."""

    tool: str
    args_json: str = "{}"

    @property
    def args(self) -> dict:
        return json.loads(self.args_json)


def _parse_probe(value, where: str) -> AgentMcpProbe:
    example = 'probe = { tool = "query", args = { sql = "select 1" } }'
    if not isinstance(value, dict):
        raise AgentConfigError(f"{where} `probe` = {value!r}; it must be a table, e.g. {example}")
    unknown = sorted(set(value) - _AGENT_MCP_PROBE_KEYS)
    if unknown:
        raise AgentConfigError(
            f"{where} `probe` has unknown key(s) {', '.join(unknown)}; allowed: tool, args"
        )
    tool = value.get("tool")
    if not isinstance(tool, str) or not AGENT_MCP_TOOL_NAME.match(tool):
        raise AgentConfigError(
            f"{where} `probe.tool` = {tool!r}; it must be the name of one of the server's "
            f"tools (letters, digits, '_', '.', '-'), e.g. {example}"
        )
    args = value.get("args", {})
    if not isinstance(args, dict):
        raise AgentConfigError(f"{where} `probe.args` = {args!r}; it must be a table, e.g. {example}")
    try:
        args_json = json.dumps(args, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise AgentConfigError(
            f"{where} `probe.args` holds a value JSON cannot carry (dates/times are not allowed): {exc}"
        ) from exc
    return AgentMcpProbe(tool=tool, args_json=args_json)


def _string_table(value, where: str, key: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise AgentConfigError(
            f"{where} `{key}` = {value!r}; it must be a table of strings, e.g. "
            f'{key} = {{ NAME = "value" }}'
        )
    return tuple((k, v) for k, v in value.items())


def parse_agent_mcp(raw: dict) -> tuple[AgentMcpServer, ...]:
    """The `[agents]` table of an already-parsed config.toml -> its MCP servers, in
    declaration order. Raises AgentConfigError on any mistake: unknown keys anywhere,
    a bad name, wrong types, both `command` and `url`, or neither. Strict because what
    it declares is launched for agents that run unattended."""
    agents = raw.get("agents", {})
    if not isinstance(agents, dict):
        raise AgentConfigError(f"[agents] is a {type(agents).__name__}; it must be a table")
    unknown = sorted(set(agents) - _AGENTS_KEYS)
    if unknown:
        raise AgentConfigError(
            f"[agents] has unknown key(s) {', '.join(unknown)}; allowed: "
            f"{', '.join(sorted(_AGENTS_KEYS))} (declare servers as [agents.mcp.<name>])"
        )
    declared = agents.get("mcp", {})
    if not isinstance(declared, dict):
        raise AgentConfigError(
            f"[agents] `mcp` is a {type(declared).__name__}; declare each server as a "
            "table, [agents.mcp.<name>]"
        )
    servers: list[AgentMcpServer] = []
    for name, entry in declared.items():
        where = f"[agents.mcp.{name}]"
        if not AGENT_MCP_NAME.match(name):
            raise AgentConfigError(f"{where}: server names may only use letters, digits, '_' and '-'")
        if not isinstance(entry, dict):
            raise AgentConfigError(f"{where} is a {type(entry).__name__}; it must be a table")
        unknown = sorted(set(entry) - _AGENT_MCP_KEYS)
        if unknown:
            raise AgentConfigError(
                f"{where} has unknown key(s) {', '.join(unknown)}; allowed: "
                f"{', '.join(sorted(_AGENT_MCP_KEYS))}"
            )
        command, url = entry.get("command"), entry.get("url")
        if command is not None and url is not None:
            raise AgentConfigError(f"{where} has both `command` and `url`; a server is one or the other")
        if command is None and url is None:
            raise AgentConfigError(f"{where} has neither `command` (stdio) nor `url` (http/sse)")
        pm = entry.get("pm", False)
        if not isinstance(pm, bool):
            raise AgentConfigError(f"{where} `pm` = {pm!r}; it must be true or false")
        env = _string_table(entry.get("env", {}), where, "env")
        probe = _parse_probe(entry["probe"], where) if "probe" in entry else None
        transport = entry.get("type")
        if command is not None:
            if not isinstance(command, str) or not command.strip():
                raise AgentConfigError(f"{where} `command` must be a non-empty string")
            if transport not in (None, "stdio"):
                raise AgentConfigError(f"{where} has `command` with type = {transport!r}; a command is a stdio server")
            args = entry.get("args", [])
            if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
                raise AgentConfigError(f"{where} `args` = {args!r}; it must be an array of strings")
            if "headers" in entry:
                raise AgentConfigError(f"{where} has `headers` on a stdio server; headers are for url servers")
            servers.append(AgentMcpServer(
                name=name, transport="stdio", command=command.strip(), args=tuple(args), env=env, pm=pm,
                probe=probe,
            ))
            continue
        if not isinstance(url, str) or not url.strip():
            raise AgentConfigError(f"{where} `url` must be a non-empty string")
        transport = "http" if transport is None else transport
        if transport not in ("http", "sse"):
            raise AgentConfigError(f'{where} type = {transport!r}; a url server is "http" or "sse"')
        if "args" in entry:
            raise AgentConfigError(f"{where} has `args` on a url server; args are for command servers")
        servers.append(AgentMcpServer(
            name=name, transport=transport, url=url.strip(),
            headers=_string_table(entry.get("headers", {}), where, "headers"), env=env, pm=pm,
            probe=probe,
        ))
    return tuple(servers)


def _parse_agents(raw: dict, config_path: Path) -> tuple[AgentMcpServer, ...]:
    """The startup read of [agents]: same parser as the hot reload, but fatal."""
    try:
        return parse_agent_mcp(raw)
    except AgentConfigError as exc:
        _fatal(f"{config_path} {exc}")


def _parse_local_env_settings(raw: dict, config_path: Path) -> LocalEnvSettings:
    """Reads the optional top-level [local_env] table (LocalEnvSettings). Strict like
    the per-system `local` tables: it bounds processes PM Studio actually runs."""
    table = raw.get("local_env", {})
    if not isinstance(table, dict):
        _fatal(f"{config_path} has [local_env] as a {type(table).__name__}; it must be a table")
    allowed = _LOCAL_SETTINGS_KEYS
    unknown = sorted(set(table) - allowed)
    if unknown:
        _fatal(
            f"{config_path} has [local_env] with unknown key(s) {', '.join(unknown)}; "
            f"allowed: {', '.join(sorted(allowed))}"
        )
    limit = table.get("max_session_envs", DEFAULT_MAX_SESSION_ENVS)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        _fatal(
            f"{config_path} has [local_env] `max_session_envs` = {limit!r}; it must be "
            "an integer >= 1"
        )
    idle = table.get("idle_stop_minutes", 0)
    if isinstance(idle, bool) or not isinstance(idle, int) or idle < 0:
        _fatal(
            f"{config_path} has [local_env] `idle_stop_minutes` = {idle!r}; it must be "
            "an integer >= 0 (0 = never)"
        )
    return LocalEnvSettings(max_session_envs=limit, idle_stop_minutes=idle)


def _parse_products(
    raw: dict, config_path: Path, systems: dict[str, SystemSpec] | None = None
) -> tuple[dict[str, str], dict[str, str], dict[str, tuple[str, ...]], dict[str, ProductMeta]]:
    """Reads [products] into (id -> label, child id -> parent id, id -> systems touched,
    id -> metadata for the products that declared any).

    Two spellings, both first class. A flat table of plain strings is what every
    deployment had before hierarchy existed, and those lines keep working byte for byte:

        [products]
        web = "Web App"                                  # a top-level product
        auth = { label = "Auth & Identity", parent = "web" }   # a child of web

        [products.billing]                               # the same child, spelled out
        label = "Billing"
        parent = "web"

    (TOML wants every bare `key = "value"` line in [products] before the first
    [products.x] sub-table, which is the only reason the inline form is shown first -
    the two mean the same thing.)

    A child is a full product in every other respect: its own board file, its own
    sessions, its own items, its own id in every URL. `parent` is purely an organizing
    pointer, which is what makes re-parenting one later a config edit rather than a data
    migration - no stored change carries the hierarchy, only the product id it always
    carried.

    Nesting goes as deep as a deployment declares - a child can itself be a parent:

        [products]
        web  = "Web App"
        auth = { label = "Auth & Identity", parent = "web" }
        sso  = { label = "SSO", parent = "auth" }          # three levels

    Nothing in the model counts levels; every consumer walks the pointer (see
    roadmap.subtree_products), so depth is the operator's decision about their own
    org, not this package's opinion. The board indents one step per level, and past
    four or five that gets narrow - a practical limit, not an enforced one.

    A bad `parent` is fatal for the same reason a bad [enterprise] mode is: a child that
    silently became a top-level product is a working-looking deployment with one board
    too many, and nothing anywhere points at the typo. A CYCLE is fatal for a harder
    reason - the products in it are reachable from no root at all, so they would simply
    vanish from the taxonomy while their boards sat on disk holding items.

    A product also declares WHICH SYSTEMS IT TOUCHES (see _parse_systems), which is the
    many-to-many edge between the two taxonomies:

        [products.checkout]
        label   = "Checkout"
        systems = ["claims", "rides"]      # every id must be a declared system

    `parent` and `systems` answer different questions and never substitute for each
    other: `parent` says which product contains this one, `systems` says which
    technology this product is built out of. Neither implies the other - a child product
    touches whatever it touches, independently of its parent.

    A product may also carry METADATA - facts about it rather than structure:

        [products.checkout]
        label = "Checkout"
        description = "Guest and member checkout, up to payment capture"
        owner = "jane.doe"            # product decision-maker, free text
        team = "Payments Engineering" # who builds it, free text
        stage = "ga"                  # discovery | development | ga | sunset

    All optional and all context, not policy: the PM prompt names them and the board
    badges a non-ga stage, but nothing authorizes, blocks, or bills against them. An
    unknown `stage` is fatal (see PRODUCT_STAGES); the free-text fields are not
    validated beyond being strings.
    """
    declared = raw.get("products", {})
    if not isinstance(declared, dict):
        _fatal(f"{config_path} has [products] as a {type(declared).__name__}; it must be a table")

    known_systems = systems or {}
    labels: dict[str, str] = {}
    parents: dict[str, str] = {}
    touches: dict[str, tuple[str, ...]] = {}
    meta: dict[str, ProductMeta] = {}
    for key, value in declared.items():
        product_id = str(key)
        if isinstance(value, str):
            # The historical form: the whole value is the display label.
            labels[product_id] = value
            continue
        if not isinstance(value, dict):
            _fatal(
                f'{config_path} has [products] entry {product_id!r} as a '
                f"{type(value).__name__}; a product is either a label string "
                '(web = "Web App") or a table with `label` and optional `parent` '
                "and `systems`"
            )
        # A missing label is cosmetic, not structural - falling back to the id shows the
        # mistake on the board immediately without refusing to boot over a display string.
        labels[product_id] = str(value.get("label", "")).strip() or product_id
        parent = str(value.get("parent", "")).strip()
        if parent:
            parents[product_id] = parent
        stage = str(value.get("stage", "")).strip() or "ga"
        if stage not in PRODUCT_STAGES:
            _fatal(
                f"{config_path} has product {product_id!r} with stage {stage!r}; it must "
                f"be one of {', '.join(PRODUCT_STAGES)}. A typo here would silently read "
                "as the steady state, so it is refused instead."
            )
        entry_meta = ProductMeta(
            description=str(value.get("description", "")).strip(),
            owner=str(value.get("owner", "")).strip(),
            team=str(value.get("team", "")).strip(),
            stage=stage,
        )
        if not entry_meta.is_empty:
            meta[product_id] = entry_meta
        if "systems" in value:
            touched = value["systems"]
            if isinstance(touched, str) or not isinstance(touched, (list, tuple)):
                _fatal(
                    f"{config_path} has product {product_id!r} with `systems` as a "
                    f"{type(touched).__name__}; it must be an array of declared system "
                    'ids, e.g. systems = ["claims", "rides"]'
                )
            # Order is the operator's, duplicates are a harmless typo rather than a
            # structural error - dedupe and keep the first mention's position.
            touched_ids: dict[str, None] = {}
            for entry in touched:
                system_id = str(entry).strip()
                if not system_id:
                    continue
                if system_id not in known_systems:
                    _fatal(
                        f"{config_path} has product {product_id!r} touching system "
                        f"{system_id!r}, which is not declared in [systems] (declared: "
                        f"{', '.join(known_systems) or 'none'})"
                    )
                touched_ids[system_id] = None
            touches[product_id] = tuple(touched_ids)

    for child, parent in parents.items():
        if parent == child:
            _fatal(f"{config_path} has product {child!r} declared as its own parent")
        if parent not in labels:
            # Pointing a product's `parent` at a system is the predictable confusion once
            # both tables exist, so name the fix instead of only the fact.
            if parent in known_systems:
                _fatal(
                    f"{config_path} has product {child!r} with parent {parent!r}, which "
                    "is a system, not a product. A product's `parent` is the product "
                    "that contains it; to say this product is built on that system, add "
                    f'it to {child!r}\'s `systems = ["{parent}"]` instead.'
                )
            _fatal(
                f"{config_path} has product {child!r} with parent {parent!r}, which is "
                f"not a declared product (declared: {', '.join(labels) or 'none'})"
            )

    # Every chain has to terminate at a top-level product. Walking up from each child is
    # what proves it: with a cycle, the products in it have no root above them, so the
    # depth-first ordering below would never reach them and they would silently drop out
    # of the taxonomy - the board short a section, `create` rejecting a product id the
    # config plainly declares.
    for child in parents:
        seen = {child}
        cursor = parents[child]
        while True:
            if cursor in seen:
                _fatal(
                    f"{config_path} has a cycle in [products]: "
                    f"{' -> '.join([*seen, cursor])}. Every product's `parent` chain must "
                    "end at a top-level product."
                )
            seen.add(cursor)
            if cursor not in parents:
                break
            cursor = parents[cursor]

    # Declaration order, re-laid depth first so each product is immediately followed by
    # its own descendants. Every consumer treats iteration order as display order (the
    # board's sections, the session picker), so the tree is ordered once, here, rather
    # than re-derived by each of them.
    ordered: dict[str, str] = {}

    def emit(product_id: str) -> None:
        ordered[product_id] = labels[product_id]
        for child, parent in parents.items():
            # `parents` is in declaration order, so siblings come out in the order the
            # operator wrote them.
            if parent == product_id:
                emit(child)

    for product_id in labels:
        if product_id not in parents:
            emit(product_id)
    # Guaranteed by the cycle check above; asserted because a product missing here is
    # invisible everywhere downstream rather than loudly broken.
    assert len(ordered) == len(labels), "product ordering dropped a declared product"
    return ordered, parents, touches, meta


def _parse_landing(server: dict, config_path: Path) -> str:
    """`[server] landing`: which page the front door serves. Validated fatally like
    every other taxonomy-shaped value - a typo here would silently land every visitor
    on the wrong page, which is exactly the class of quiet misconfiguration load-time
    validation exists to refuse."""
    landing = str(server.get("landing", "")).strip() or "sessions"
    if landing not in ("sessions", "overview"):
        _fatal(
            f"{config_path} sets [server] landing = {landing!r}; "
            'valid values are "sessions" (default) and "overview".'
        )
    return landing


def _parse_mode(raw: dict, config_path: Path) -> str:
    """Reads [enterprise] mode / enabled. A typo here decides whether the whole
    instance requires authentication, so an unrecognized value is fatal rather than
    silently falling back to the permissive default."""
    enterprise = raw.get("enterprise", {})
    mode = str(enterprise.get("mode", "")).strip().lower()
    if not mode:
        # Convenience form: `enabled = true` is the same as `mode = "enterprise"`.
        mode = MODE_ENTERPRISE if bool(enterprise.get("enabled", False)) else MODE_PERSONAL
    if mode not in MODES:
        print(
            f"[pm_studio] FATAL: {config_path} has [enterprise] mode = {mode!r}; "
            f"must be one of: {', '.join(MODES)}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return mode


def _parse_costing(raw: dict) -> CostingConfig:
    """Reads the optional [costing] table."""
    costing = raw.get("costing")
    if not isinstance(costing, dict):
        return CostingConfig()
    blended = costing.get("blended_rate")
    weights = costing.get("weights")
    return CostingConfig(
        blended_rate=float(blended) if blended is not None else None,
        default_capacity_hours=float(
            costing.get("default_capacity_hours", DEFAULT_CAPACITY_HOURS)
        ),
        currency=str(costing.get("currency", DEFAULT_CURRENCY)).strip() or DEFAULT_CURRENCY,
        weights={str(k): float(v) for k, v in (weights or {}).items()},
    )


def _parse_signals(raw: dict) -> SignalsConfig:
    """Reads the optional [signals] table. Lenient on purpose: a bad number falls back
    to its default rather than refusing to start, because this layer is read-only
    intelligence over data the rest of the tool owns."""
    table = raw.get("signals")
    if not isinstance(table, dict):
        return SignalsConfig()

    def num(key: str, default: float) -> float:
        try:
            return float(table.get(key, default))
        except (TypeError, ValueError):
            return default

    def mapping(key: str) -> dict[str, float]:
        value = table.get(key)
        if not isinstance(value, dict):
            return {}
        out: dict[str, float] = {}
        for k, v in value.items():
            try:
                out[str(k)] = float(v)
            except (TypeError, ValueError):
                continue
        return out

    def strings(key: str) -> tuple[str, ...]:
        value = table.get(key)
        if not isinstance(value, list):
            return ()
        return tuple(str(v).strip() for v in value if str(v).strip())

    since = str(table.get("since", "2024-01-01")).strip() or "2024-01-01"
    return SignalsConfig(
        since=since,
        capacity_hours_per_day=num("capacity_hours_per_day", 8.0),
        timezone=str(table.get("timezone", "America/New_York")).strip() or "America/New_York",
        auto_refresh_minutes=num("auto_refresh_minutes", 15.0),
        git_fetch=bool(table.get("git_fetch", True)),
        extra_repos=strings("extra_repos"),
        judge_model=str(table.get("judge_model", "")).strip(),
        auto_judge=bool(table.get("auto_judge", False)),
        thresholds=mapping("thresholds"),
        weights=mapping("weights"),
        ado_pr_projects=strings("ado_pr_projects"),
        allocation_mode=str(table.get("allocation_mode", "observed")).strip() if str(table.get("allocation_mode", "observed")).strip() in ("observed", "scaled") else "observed",
        default_projects={str(k): str(v) for k, v in (table.get("default_projects") or {}).items()} if isinstance(table.get("default_projects"), dict) else {},
        worklog_trust={str(k): str(v) for k, v in (table.get("worklog_trust") or {}).items() if str(v) in ("full", "signal", "ignore")} if isinstance(table.get("worklog_trust"), dict) else {},
        default_repos={str(k): str(v) for k, v in (table.get("default_repos") or {}).items()} if isinstance(table.get("default_repos"), dict) else {},
    )


def _parse_smtp(raw: dict) -> SmtpConfig | None:
    """Reads the optional [smtp] table. The password may be given inline, but
    `password_env` (the name of an environment variable to read it from) is preferred
    so the secret never has to sit in a file."""
    smtp = raw.get("smtp")
    if not isinstance(smtp, dict) or not smtp:
        return None
    password = str(smtp.get("password", ""))
    password_env = str(smtp.get("password_env", "")).strip()
    if password_env:
        password = os.environ.get(password_env, "")
    elif password:
        # CONFIGURATION.md tells operators to commit pm_studio_local/ (session worktrees
        # need it), so an inline password is a password in git. Warn rather than refuse:
        # a deployment that has already done it should still boot, but nobody should be
        # able to do it without being told.
        print(
            "[pm_studio] WARNING: [smtp] password is set inline in "
            f"{LOCAL_DIR_NAME}/{CONFIG_FILE_NAME}, which is normally committed to your "
            "repo - that puts the password in git history. Use `password_env = "
            '"YOUR_ENV_VAR_NAME"` instead and unset `password`.',
            file=sys.stderr,
        )
    return SmtpConfig(
        host=str(smtp.get("host", "")).strip(),
        port=int(smtp.get("port", DEFAULT_SMTP_PORT)),
        from_address=str(smtp.get("from_address", "")).strip(),
        username=str(smtp.get("username", "")).strip(),
        password=password,
        use_tls=bool(smtp.get("use_tls", True)),
    )


TRACKER_PROVIDERS = ("jira", "ado")


def _parse_trackers(
    raw: dict,
    config_path: Path,
    products: dict[str, str] | None = None,
    product_systems: dict[str, tuple[str, ...]] | None = None,
    systems: dict[str, SystemSpec] | None = None,
) -> tuple[TrackerConfig, ...]:
    """Reads the optional `[[trackers]]` array-of-tables.

    Malformed entries are dropped with a warning rather than being fatal: an unreachable
    tracker must never stop the studio from booting, because the roadmap board is useful
    without it. A bad *provider*, though, is loud - it is a typo that would otherwise look
    like "sync silently does nothing".

    IMPORT ROUTES are the exception to drop-with-warning: they are validated fatally,
    against the product and system taxonomies passed in. A route that quietly skipped a
    misspelled product would import that component's tickets nowhere while the config
    plainly says otherwise - the exact failure mode _fatal exists for:

        [[trackers]]
        # ... connection keys ...
        import_types = ["Epic", "Story", "Bug"]   # which ticket types become changes

        [[trackers.routes]]
        component = "Checkout Web"   # Jira component / ADO area path
        product = "checkout"         # must be a declared product
        system = "claims"            # optional; must be one that product touches

    `import_types` and `routes` only work together, so declaring exactly one of them
    is fatal too, rather than a config that looks like it imports and silently doesn't.
    """
    entries = raw.get("trackers")
    if not isinstance(entries, list):
        return ()

    trackers: list[TrackerConfig] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        where = f"[[trackers]] #{index + 1}"

        provider = str(entry.get("provider", "")).strip().lower()
        if provider not in TRACKER_PROVIDERS:
            print(
                f"[pm_studio] WARNING: {where} in {config_path} has provider "
                f"{provider!r}; expected one of {', '.join(TRACKER_PROVIDERS)}. Skipping it.",
                file=sys.stderr,
            )
            continue

        organization = str(entry.get("organization", "")).strip().strip("/")
        base_url = str(entry.get("base_url", "")).strip().rstrip("/")
        if provider == "ado" and not base_url and organization:
            base_url = f"https://dev.azure.com/{organization}"

        # A stable id is what links on roadmap items point at, so it must not drift.
        # Defaulting it to the provider keeps a single-tracker config to a few lines.
        tracker_id = str(entry.get("id", "")).strip() or provider
        if tracker_id in seen:
            print(
                f"[pm_studio] WARNING: {where} in {config_path} reuses id "
                f"{tracker_id!r}; ids identify a tracker on every linked item, so this "
                "one is skipped. Give each tracker its own `id`.",
                file=sys.stderr,
            )
            continue
        seen.add(tracker_id)

        token = str(entry.get("token", ""))
        token_env = str(entry.get("token_env", "")).strip()
        if token_env:
            token = os.environ.get(token_env, "")
            if not token:
                print(
                    f"[pm_studio] WARNING: {where} in {config_path} sets token_env = "
                    f"{token_env!r}, but that environment variable is empty or unset, so "
                    f"tracker {tracker_id!r} cannot sync until it is exported.",
                    file=sys.stderr,
                )
        elif token:
            # Same reasoning as [smtp] password above: pm_studio_local/ is normally
            # committed, so an inline token is a token in git history.
            print(
                f"[pm_studio] WARNING: {where} in {config_path} sets `token` inline, "
                "which is normally committed to your repo - that puts an API token in git "
                'history. Use `token_env = "YOUR_ENV_VAR_NAME"` instead and unset `token`.',
                file=sys.stderr,
            )

        # The account name (Jira Cloud wants the email). Not a secret the way the token is,
        # but pm_studio_local/ is committed and a deployment may publish that repo, so
        # `username_env` / `email_env` exist to keep a work address out of it too.
        username = str(entry.get("username", "") or entry.get("email", "")).strip()
        username_env = str(
            entry.get("username_env", "") or entry.get("email_env", "")
        ).strip()
        if username_env:
            username = os.environ.get(username_env, "").strip()
            if not username:
                print(
                    f"[pm_studio] WARNING: {where} in {config_path} sets username_env/"
                    f"email_env = {username_env!r}, but that environment variable is empty "
                    f"or unset, so tracker {tracker_id!r} will fail to authenticate.",
                    file=sys.stderr,
                )

        projects = entry.get("projects")
        if isinstance(projects, str):
            projects = [projects]
        project_keys = tuple(
            str(p).strip() for p in (projects or []) if str(p).strip()
        )

        try:
            interval = float(
                entry.get("sync_interval_minutes", DEFAULT_SYNC_INTERVAL_MINUTES)
            )
        except (TypeError, ValueError):
            interval = DEFAULT_SYNC_INTERVAL_MINUTES
        # Floored rather than rejected: a config asking for every 5 seconds is a mistake,
        # and hammering someone's tracker is worse than quietly slowing down.
        interval = max(MIN_SYNC_INTERVAL_MINUTES, interval)
        if entry.get("sync_interval_minutes") is not None:
            print(
                f"[config] tracker {tracker_id!r}: sync_interval_minutes is ignored - "
                f"tracker syncs are manual (\"Sync now\" / POST /trackers/sync). "
                f"Remove the line.",
                file=sys.stderr,
            )

        import_types_raw = entry.get("import_types", [])
        if isinstance(import_types_raw, str) or not isinstance(import_types_raw, (list, tuple)):
            _fatal(
                f"{config_path} {where} has `import_types` as a "
                f"{type(import_types_raw).__name__}; it must be an array of ticket "
                'types, e.g. import_types = ["Epic", "Story"]'
            )
        import_types = tuple(
            str(t).strip() for t in import_types_raw if str(t).strip()
        )

        since = str(entry.get("since", "")).strip()
        if since:
            try:
                date.fromisoformat(since)
            except ValueError:
                _fatal(
                    f"{config_path} {where} has since = {since!r}; it must be a "
                    'calendar date as "YYYY-MM-DD"'
                )

        exclude_raw = entry.get("exclude_components", [])
        if isinstance(exclude_raw, str) or not isinstance(exclude_raw, (list, tuple)):
            _fatal(
                f"{config_path} {where} has `exclude_components` as a "
                f"{type(exclude_raw).__name__}; it must be an array of component/area "
                'names, e.g. exclude_components = ["CapAdmin"]'
            )
        exclude_components = tuple(
            str(c).strip() for c in exclude_raw if str(c).strip()
        )

        close_raw = entry.get("close_done_epics", False)
        if not isinstance(close_raw, bool):
            _fatal(
                f"{config_path} {where} has `close_done_epics` as a "
                f"{type(close_raw).__name__}; it must be true or false"
            )

        routes_raw = entry.get("routes", [])
        if not isinstance(routes_raw, list):
            _fatal(
                f"{config_path} {where} has `routes` as a {type(routes_raw).__name__}; "
                "declare each route as its own [[trackers.routes]] table"
            )
        known_products = products or {}
        known_product_systems = product_systems or {}
        known_systems = systems or {}
        routes: list[TrackerRoute] = []
        seen_matchers: set[tuple[str, str]] = set()
        for r_index, route in enumerate(routes_raw):
            r_where = f"{where} route #{r_index + 1}"
            if not isinstance(route, dict):
                _fatal(
                    f"{config_path} {r_where} must be a table with `product` and a "
                    "`component` and/or `project` to match on"
                )
            component = str(route.get("component", "")).strip()
            project = str(route.get("project", "")).strip()
            product = str(route.get("product", "")).strip()
            system = str(route.get("system", "")).strip()
            if not product:
                _fatal(f"{config_path} {r_where} needs a `product`")
            if not component and not project:
                _fatal(
                    f"{config_path} {r_where} matches nothing - give it a `component`, "
                    "a `project`, or both"
                )
            if project and project not in project_keys:
                # A route scoped to a project the tracker does not sync can never fire:
                # a dead route that the config plainly declares is the silent-no-op
                # failure _fatal exists for.
                _fatal(
                    f"{config_path} {r_where} is scoped to project {project!r}, which "
                    f"this tracker does not sync (projects: "
                    f"{', '.join(project_keys) or 'none'})"
                )
            matcher = (project.casefold(), component.casefold())
            if matcher in seen_matchers:
                # Two routes with one matcher would import each ticket wherever the
                # loop happened to look first - an ambiguity, not a preference.
                _fatal(
                    f"{config_path} {r_where} repeats the matcher "
                    f"(project={project or '*'!r}, component={component or '*'!r})"
                )
            seen_matchers.add(matcher)
            if product not in known_products:
                _fatal(
                    f"{config_path} {r_where} routes to product {product!r}, which is "
                    f"not declared (declared: {', '.join(known_products) or 'none'})"
                )
            if system:
                # The same two rules validate_system enforces on create, applied at load
                # so a bad route fails at boot, not at 3am mid-sync.
                if system not in known_systems:
                    _fatal(
                        f"{config_path} {r_where} attributes to system {system!r}, which "
                        f"is not declared (declared: {', '.join(known_systems) or 'none'})"
                    )
                declared = known_product_systems.get(product, ())
                if declared and system not in declared:
                    _fatal(
                        f"{config_path} {r_where} attributes to system {system!r}, but "
                        f"product {product!r} touches: {', '.join(declared)}"
                    )
            routes.append(
                TrackerRoute(
                    product=product, component=component, project=project, system=system
                )
            )

        if bool(import_types) != bool(routes):
            missing, present = (
                ("routes", "import_types") if import_types else ("import_types", "routes")
            )
            _fatal(
                f"{config_path} {where} declares `{present}` without `{missing}`; the "
                "two only import together, so this config looks like it imports and "
                "silently would not"
            )

        # PUSH target. Validated fatally for the same reason routes are: a push table
        # naming a project this tracker does not sync, or sitting on a provider that
        # cannot push, is a config that plainly says "you can create tickets here" and
        # silently would not.
        push_raw = entry.get("push")
        push: PushConfig | None = None
        if push_raw is not None:
            if not isinstance(push_raw, dict):
                _fatal(
                    f"{config_path} {where} has `push` as a "
                    f"{type(push_raw).__name__}; declare it as a [trackers.push] table"
                )
            if provider != "jira":
                _fatal(
                    f"{config_path} {where} declares [trackers.push], but pushing is "
                    f"only implemented for Jira and this tracker is {provider!r}. "
                    "Remove the table; syncing and manual linking still work."
                )
            push_project = str(push_raw.get("project", "")).strip()
            if not push_project:
                _fatal(
                    f"{config_path} {where} [trackers.push] needs a `project` - the "
                    "project key new tickets are created in"
                )
            if push_project not in project_keys:
                _fatal(
                    f"{config_path} {where} [trackers.push] pushes into project "
                    f"{push_project!r}, which this tracker does not sync (projects: "
                    f"{', '.join(project_keys) or 'none'}). A pushed ticket the catalog "
                    "never pulls would read as unresolved on the card that created it."
                )
            push = PushConfig(
                project=push_project,
                change_type=str(push_raw.get("change_type", "")).strip()
                or DEFAULT_PUSH_CHANGE_TYPE,
                epic_type=str(push_raw.get("epic_type", "")).strip()
                or DEFAULT_PUSH_EPIC_TYPE,
            )

        excluded_set = {c.casefold() for c in exclude_components}
        for route in routes:
            if route.component and route.component.casefold() in excluded_set:
                # Routed AND excluded is a contradiction, not a preference - one of the
                # two lines is a leftover, and which one wins should never be implicit.
                _fatal(
                    f"{config_path} {where} both routes and excludes component "
                    f"{route.component!r}; delete one of the two declarations"
                )

        trackers.append(
            TrackerConfig(
                id=tracker_id,
                provider=provider,
                label=str(entry.get("label", "")).strip()
                or ("Jira" if provider == "jira" else "Azure DevOps"),
                base_url=base_url,
                projects=project_keys,
                username=username,
                token=token,
                organization=organization,
                sync_interval_minutes=interval,
                import_types=import_types,
                routes=tuple(routes),
                since=since,
                exclude_components=exclude_components,
                close_done_epics=close_raw,
                push=push,
            )
        )
    return tuple(trackers)


def load_config(repo_root: Path | None = None) -> Config:
    """Builds the Config for one target repo. Every part is optional: with no
    pm_studio_local/ at all you get a fully generic (but working) deployment."""
    root = (repo_root or Path.cwd()).resolve()
    # FIRST, before any `*_env` name is resolved below ([smtp] password, tracker tokens):
    # the file can only supply a credential if it is merged in before the read.
    _load_env_file(root / ENV_FILE_NAME)
    local_dir = root / LOCAL_DIR_NAME

    raw: dict = {}
    config_path = local_dir / CONFIG_FILE_NAME
    if config_path.is_file():
        try:
            raw = tomllib.loads(config_path.read_text())
        except tomllib.TOMLDecodeError as exc:
            # A broken config must be loud, not silently generic: a deployment that
            # thinks it has products/instructions and silently loses them would
            # behave differently with no visible cause.
            print(f"[pm_studio] FATAL: {config_path} is not valid TOML: {exc}", file=sys.stderr)
            raise SystemExit(2)

    project = raw.get("project", {})
    server = raw.get("server", {})
    # [models] maps model id -> UI label; the reserved key "default" names which id
    # new sessions start on. Empty table = use the package defaults (models.py).
    models_raw = dict(raw.get("models", {}))
    default_model = str(models_raw.pop("default", "")).strip() or None
    # Reserved key "quick": the model QUICK dev tasks (look, don't build) run on instead
    # of their session's - looking and screenshotting rarely needs the biggest model.
    quick_model = str(models_raw.pop("quick", "")).strip() or None
    if quick_model and models_raw and quick_model not in models_raw:
        _fatal(
            f"{config_path} has [models] quick = {quick_model!r}, which is not one of the "
            f"models declared there ({', '.join(models_raw)})."
        )

    # Systems first: a product declares which of them it touches, so they have to be
    # known before [products] can validate those references.
    systems = _parse_systems(raw, config_path)
    # A `gitflow` pointing at nothing would silently mean "no rules injected" on every
    # dev task for that system - the exact opposite of what the operator declared. Same
    # refuse-loudly posture as every other config error. Checked against the primary
    # checkout; worktrees branch from it, so the file exists in them too.
    for system_id, spec in systems.items():
        if spec.gitflow and not (root / spec.gitflow).is_file():
            _fatal(
                f"{config_path} declares system {system_id!r} with "
                f"gitflow = {spec.gitflow!r}, but {root / spec.gitflow} does not exist. "
                "The path is repo-root-relative and must point at the file holding that "
                "system's git workflow rules."
            )
    products, product_parents, product_systems, product_meta = _parse_products(raw, config_path, systems)
    # An id in both tables is the reclassification-in-progress state, not an error: see
    # Config.transitional_ids. Ordered by the [products] table so the report reads in the
    # order the operator sees their own board.
    transitional_ids = tuple(p for p in products if p in systems)

    knowledge_dir = local_dir / KNOWLEDGE_DIR_NAME
    knowledge_files: tuple[str, ...] = ()
    if knowledge_dir.is_dir():
        knowledge_files = tuple(
            sorted(
                str(p.relative_to(root))
                for p in knowledge_dir.rglob("*")
                if p.is_file() and not p.name.startswith(".")
            )
        )

    return Config(
        repo_root=root,
        project_name=str(project.get("name", "")).strip() or root.name,
        default_session_name=str(project.get("default_session_name", "")).strip()
        or DEFAULT_SESSION_NAME,
        workspace_root=str(project.get("workspace_root", "")).strip()
        or DEFAULT_WORKSPACE_ROOT,
        host=str(server.get("host", "")).strip() or DEFAULT_HOST,
        port=int(server.get("port", DEFAULT_PORT)),
        landing=_parse_landing(server, config_path),
        products=products,
        product_parents=product_parents,
        systems=systems,
        product_systems=product_systems,
        product_meta=product_meta,
        transitional_ids=transitional_ids,
        repo_layout=str(project.get("layout", "")).strip() or DEFAULT_REPO_LAYOUT,
        models={str(k): str(v) for k, v in models_raw.items()},
        default_model=default_model,
        quick_model=quick_model,
        pm_instructions=_read_optional(local_dir / PM_INSTRUCTIONS_NAME),
        dev_instructions=_read_optional(local_dir / DEV_INSTRUCTIONS_NAME),
        knowledge_files=knowledge_files,
        mode=_parse_mode(raw, config_path),
        smtp=_parse_smtp(raw),
        costing=_parse_costing(raw),
        signals=_parse_signals(raw),
        local_env=_parse_local_env_settings(raw, config_path),
        agent_mcp=_parse_agents(raw, config_path),
        trackers=_parse_trackers(raw, config_path, products, product_systems, systems),
    )


# Loaded once at import from the process cwd - the same "one process, one repo"
# assumption the rest of the package (module-level stores, registries) already makes.
# Tests replace this module attribute directly with a Config built around a tmp dir.
CONFIG = load_config()
