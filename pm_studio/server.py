import asyncio
import json
import re
from dataclasses import asdict
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import Body, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse, RedirectResponse

from . import agent_env as agent_env_module
from . import agent_mcp, mailer
from . import mcp_activity
from .accounts import (
    AGENT_HEADER_NAME,
    LOGIN_TTL_SECONDS,
    ROLE_LABELS,
    SESSION_COOKIE_NAME,
    AccountError,
    AccountStore,
    User,
    agent_principal,
    is_agent_token,
)
from .authz import (
    CAPABILITY_LABELS,
    AuditLog,
    Capability,
    capabilities_of,
    describe_matrix,
    role_has,
)
from .config import CONFIG, TrackerConfig
from .agent import stop_running_turns
from .local_env import LocalEnvError, LocalEnvManager, SessionContext
from .costing import (
    KIND_DEV_TASK,
    KIND_PM_TURN,
    CostingError,
    CostingStore,
    current_week,
)
from .models import list_models
from .overview import build_overview
from .plans import PlanStore, plan_vs_actual
from .people import (
    UNASSIGNED,
    PeopleError,
    PeopleStore,
    effective_assignee,
    workload,
)
from .portfolio import (
    DEFAULT_CATCH_ALL_PROJECT,
    DEFAULT_MAINTENANCE_GOAL,
    DEFAULT_MAINTENANCE_INITIATIVE,
    EpicAlreadyLinked,
    PortfolioError,
    PortfolioStore,
)
from .roadmap import (
    PRODUCT_META,
    PRODUCT_PARENTS,
    PRODUCT_SYSTEMS,
    PRODUCTS,
    SYSTEMS,
    RoadmapItem,
    RoadmapStore,
    TicketAlreadyLinked,
    parent_of,
    product_path_label,
    subtree_products,
    systems_declared,
)
from .sessions import DEFAULT_SESSION_ID, SessionManager, SessionRuntime
from .signals import api as signals_api
from .signals.service import IntelligenceService
from .tasks import ensure_system_code, validate_dispatch_system
from .trackers import (
    TYPE_EPIC,
    TYPE_SUBTASK,
    TYPE_TASK,
    Ticket,
    TrackerError,
    TrackerStore,
    canonical_type,
    normalize_key,
)

STATIC_DIR = Path(__file__).parent / "static"

# Maps each open chat connection to a lock guarding its sends. Two producers can push
# to the same connection - the normal request/response turn in _run_chat_ws, and an
# auto-continuation broadcast in _auto_continue_pm firing from a dev-task completion at
# an arbitrary time - and concurrent `send_text` calls on one websocket aren't safe, so
# every send (both paths) goes through this lock.
chat_ws_clients: dict[str, dict[WebSocket, asyncio.Lock]] = {}
# Stakeholder messages waiting for their PM turn, per session. The chat websocket's
# receive loop only ever enqueues here, so a stakeholder can keep typing and keep
# sending while the PM is mid-turn instead of being locked out until the reply lands.
# One worker per session drains its queue strictly in order (see _drain_pm_queue), so
# turns still run one at a time - the queue is where the waiting happens now, not in
# the receive loop.
_chat_queues: dict[str, asyncio.Queue] = {}
_chat_workers: dict[str, asyncio.Task] = {}
_chat_inflight: set[str] = set()  # sessions whose queued turn is running right now
# Ids of the queue records currently held in memory, per session. The durable copy of
# the queue lives in the session's pending_messages.json; this is what tells a
# reconnecting page which of those records are already being handled here and which are
# leftovers from a previous process that nothing will ever run - see _resume_pending.
_chat_queued_ids: dict[str, set[str]] = {}
task_ws_clients: dict[str, set[WebSocket]] = {}
session_ws_clients: set[WebSocket] = set()
roadmap_ws_clients: set[WebSocket] = set()
_main_loop: asyncio.AbstractEventLoop | None = None

sessions = SessionManager()
roadmap_store = RoadmapStore()
portfolio_store = PortfolioStore()
# The synced Jira/ADO ticket catalog. Constructed unconditionally; with no [[trackers]]
# configured it holds nothing and every ticket join below resolves to None, so the board
# behaves exactly as it did before the feature existed.
tracker_store = TrackerStore()
# Who is doing the work. Populated by the tracker sync rather than by hand (see
# _reconcile_people); on a deployment with no trackers it holds only whoever was added
# locally, and every assignee join below resolves to nobody - which renders as an
# unassigned board, exactly as it did before this existed.
people_store = PeopleStore()
# Constructed in both modes (it is empty and untouched in personal mode) so nothing
# has to branch on mode just to reach the store.
account_store = AccountStore()
audit_log = AuditLog()
costing_store = CostingStore(
    blended_rate=CONFIG.costing.blended_rate,
    default_capacity_hours=CONFIG.costing.default_capacity_hours,
    weights=CONFIG.costing.weights,
    currency=CONFIG.costing.currency,
)
plan_store = PlanStore(CONFIG.workspace_dir / "plans")


def _local_env_session(session_id: str) -> SessionContext | None:
    """How the local-env supervisor sees a chat session: where its worktree is. None
    for an unknown session and for one whose worktree is gone (terminated, deleted) -
    there is nothing left to run its environment from."""
    session = sessions.get(session_id)
    if session is None or not session.worktree_path:
        return None
    return SessionContext(
        id=session.id,
        name=session.name or session.id,
        worktree=Path(session.worktree_path),
        primary=session.is_default,
    )


def _local_env_session_busy(session_id: str) -> bool:
    """Whether a session has a dev task running - the one way a session uses its local
    environment without making local-env calls (the task's agent may be minutes into a
    test run against it). The supervisor neither idle-stops nor evicts a busy session's
    environment. Never raises: a session whose runtime is gone, or a task file being
    rewritten mid-read, is simply not busy."""
    try:
        runtime = sessions.get_runtime(session_id)
        if runtime is None:
            return False
        return any(t.get("status") == "running" for t in runtime.task_registry.list_tasks())
    except Exception:
        return False


# The processes of every system's `[systems.<id>.local]` environment, one environment
# per chat session (plus the shared services every session reuses). Owned by the server
# rather than by whichever task or turn asked for them, so they outlive dev tasks and PM
# turns - and stopped in the lifespan hook below, so they never outlive the server.
# Constructing it starts nothing and reads nothing; with no `local` declared it holds
# nothing. Provisioned resources (per-session databases...) are remembered in
# local-env/resources.json under the workspace so they are reused across restarts; that
# directory is on gitsnapshot's never-commit list. With `[local_env] idle_stop_minutes`
# set, the lifespan hook also runs its idle-stop thread.
local_env = LocalEnvManager(
    CONFIG.systems,
    CONFIG.repo_root,
    session_lookup=_local_env_session,
    state_path=CONFIG.workspace_dir / "local-env" / "resources.json",
    max_session_envs=CONFIG.local_env.max_session_envs,
    idle_stop_minutes=CONFIG.local_env.idle_stop_minutes,
    session_busy=_local_env_session_busy,
)


def _release_local_env(session_id: str, actor) -> None:
    """Gives back a session's local environment before its worktree goes away: stops
    its services now (a running process holds files in the worktree open, which on
    Windows makes `git worktree remove` fail) and destroys its resources in the
    background. Never blocks the lifecycle operation it precedes - a failure here is
    logged and audited, not raised."""
    try:
        report = local_env.release_session(session_id, wait=False)
    except Exception as exc:  # the session must still be able to end
        print(f"[pm_studio] local-env release for {session_id} failed: {exc}")
        return
    if any(report[k] for k in ("stopped", "destroying", "forgotten", "aborted")):
        _audit(actor, "local_env.released", session_id, json.dumps(report))


def _who(actor) -> str:
    """Who a stop is attributed to on the record ("Stopped by <who>.")."""
    return (actor.name or actor.email) if actor is not None else "the user"


def _discard_pending_messages(session_id: str, runtime) -> list[str]:
    """Throws away every message the session accepted but has not started - from the
    in-memory queue and from its queue file, or a reconnecting page would run them
    after all (see _resume_pending) - and tells open chat pages which ones went.
    Returns their ids."""
    ids = [str(record["id"]) for record in runtime.pm_agent.load_pending()]
    _discard_queued_turns(session_id)
    for message_id in ids:
        runtime.pm_agent.drop_pending(message_id)
    if ids:
        _broadcast_chat_event_threadsafe(session_id, {"type": "pm_queue_discarded", "ids": ids})
    return ids


def _stop_session(session_id: str, who: str = "the user") -> dict:
    """Stops everything a session has running: the messages queued behind its PM
    (discarded), the PM turn in flight, every running dev task, and the services of its
    local environment - stopped, not released: its resources and checkout stay for the
    next start (_release_local_env gives those back when the session ends). Each part
    is attempted even when another fails - a session must always be stoppable, and
    whatever lifecycle step follows must still run. Returns what it stopped; a session
    with nothing running gets empty lists."""
    report: dict = {"session": session_id, "pm_turn": False, "tasks": [], "services": [], "discarded": []}
    runtime = sessions.get_runtime(session_id)
    steps = []
    if runtime is not None:
        steps += [
            ("discarded", lambda: _discard_pending_messages(session_id, runtime)),
            ("pm_turn", lambda: bool(runtime.pm_agent.stop_turn(who))),
            ("tasks", lambda: [str(task["id"]) for task in runtime.task_registry.stop_all(who)]),
        ]
    steps.append(("services", lambda: [str(name) for name in local_env.stop_session(session_id, f"Stopped by {who}.")]))
    for key, step in steps:
        try:
            report[key] = step()
        except Exception as exc:
            print(f"[pm_studio] stopping {key} of session {session_id} failed: {exc}")
    return report


def _stopped_anything(report: dict) -> bool:
    return any(report[key] for key in ("pm_turn", "tasks", "services", "discarded"))


def _stop_for_lifecycle(session_id: str, actor, doing: str) -> None:
    """The stop every path that ends or hides a session runs first (archive, terminate,
    clean up, delete): nothing of a session keeps running once it is out of sight."""
    report = _stop_session(session_id, f"{_who(actor)} {doing}")
    if _stopped_anything(report):
        _audit(actor, "session.stopped", session_id, json.dumps(report))


def _signals_rate_for(person_id: str) -> tuple[float | None, str]:
    """The costing roster is keyed by account; a directory person reaches it through
    account_id. Everyone else gets the blended rate, or nothing."""
    person = people_store.get(person_id)
    account_id = getattr(person, "account_id", None) if person is not None else None
    entry = costing_store.entry_for(account_id or person_id)
    return costing_store._resolve_rate(entry)


# The engineering-intelligence layer (pm_studio.signals): the ledger of every commit,
# ticket transition, worklog and agent turn, attributed up the work model. Built from
# callables onto the stores above so the package never imports them. Read-only over
# everything the rest of this file owns.
intelligence_service = IntelligenceService(
    repo_root=CONFIG.repo_root,
    workspace_dir=CONFIG.workspace_dir,
    config=CONFIG.signals,
    trackers=list(CONFIG.trackers),
    systems=CONFIG.systems,
    product_systems=CONFIG.product_systems,
    routes=[
        {"tracker_id": t.id, "component": r.component, "project": r.project, "product": r.product, "system": r.system}
        for t in CONFIG.trackers for r in t.routes
    ],
    stores={
        "tickets": lambda: [t.to_dict() for tc in CONFIG.trackers for t in tracker_store.tickets_of(tc.id)],
        "changes": lambda: [item for items in roadmap_store.list_all().values() for item in items],
        "projects": portfolio_store.list_projects,
        "initiatives": portfolio_store.list_initiatives,
        "goals": portfolio_store.list_goals,
        "people": people_store.list_people,
        "accounts": (lambda: account_store.list_users()) if CONFIG.is_enterprise else (lambda: []),
        "releases": lambda: tracker_store.list_releases(),
        "trackers": lambda: list(CONFIG.trackers),
        "product_labels": lambda: dict(CONFIG.products),
    },
    rate_for=_signals_rate_for,
    currency=CONFIG.costing.currency,
    fallback_model=CONFIG.default_model or "",
)

# task_id -> the user who dispatched it. A dev task's token spend is only known when it
# finishes, by which time the request that started it is long gone, so the dispatcher is
# remembered here. In-process is enough: a task outliving the process is marked errored
# on restart anyway (see TaskRegistry's startup sweep).
_dev_task_dispatchers: dict[str, str] = {}

# Reachable without a session cookie. Everything else needs one when enterprise mode
# is on; in personal mode this set is irrelevant because auth is skipped entirely.
_PUBLIC_PATHS = frozenset(
    {
        "/login",
        "/setup",
        "/accept-invite",
        "/auth/login",
        "/auth/setup",
        "/auth/invite",
        "/auth/accept-invite",
        "/auth/me",
        "/favicon.ico",
    }
)


def _lookup_ticket(tracker_id: str, key: str) -> dict | None:
    ticket = tracker_store.lookup(tracker_id, key)
    return ticket.to_dict() if ticket is not None else None


def _lookup_person(person_id: str) -> str:
    """A person id to the name a PM should read. Empty for an id nothing resolves, which
    reads on the context line as no assignee at all - the honest answer, since a stale id
    names nobody."""
    person = people_store.get(person_id)
    return person.name if person else ""


def _with_ticket(item: dict) -> dict:
    """Joins a roadmap change - or a project, which carries the same two link fields -
    to its linked ticket for the wire.

    The item stores only `tracker_id` + `ticket_key` (see roadmap.py); everything the card
    renders - canonical type for the colour, the tracker's own type name for the label,
    title, state, URL - comes from the synced catalog here. A link whose ticket is not in
    the catalog yet returns `unresolved: true` rather than being dropped, so the UI can say
    "linked, not seen in the last sync" instead of silently showing no badge at all.
    """
    tracker_id, key = item.get("tracker_id"), item.get("ticket_key")
    if not tracker_id or not key:
        return _with_assignee({**item, "ticket": None})
    ticket = tracker_store.lookup(tracker_id, key)
    if ticket is None:
        config = CONFIG.tracker(tracker_id)
        return _with_assignee({
            **item,
            "ticket": {
                "tracker_id": tracker_id,
                "tracker_label": config.label if config else tracker_id,
                "provider": config.provider if config else "",
                "key": key,
                "type": "other",
                "raw_type": "",
                "title": "",
                "state": "",
                "url": "",
                "unresolved": True,
            },
        })
    config = CONFIG.tracker(tracker_id)
    return _with_assignee({
        **item,
        "ticket": {
            **ticket.to_dict(),
            "tracker_label": config.label if config else tracker_id,
            "unresolved": False,
        },
    })


def _with_assignee(item: dict) -> dict:
    """Adds `assigned` - who is on this change and on whose authority.

    Applied inside `_with_ticket` rather than beside it, so the two joins a card needs
    cannot come apart: the tracker's assignee is only knowable once the ticket has been
    resolved, and an endpoint that remembered one join and forgot the other would render a
    board that quietly disagrees with the next one. Projects go through the same function
    and get the epic's assignee, which is the right answer for "who is on this epic" and
    costs nothing on a project with no link.
    """
    return {**item, "assigned": effective_assignee(item, item.get("ticket"), people_store)}


def _initiative_context(initiative_id: str) -> tuple[str, list[str]] | None:
    """The initiative block for an initiative-scoped session: what the initiative is,
    which goals it serves, and every open change under its projects at full depth.

    Composed here rather than in either store, because it is a join across both: which
    projects belong to the initiative is the portfolio's knowledge, which changes belong
    to a project is the roadmap's. Returns None for an id that no longer resolves, so a
    session that outlived its initiative degrades to the plain product view instead of
    erroring on every turn.

    Returns the block and the ids of every change it rendered, so the awareness digest can
    leave those out - an initiative's changes sit on boards the session may not own, and
    without this each one would reappear as a one-liner immediately below.
    """
    scope = portfolio_store.initiative_scope(initiative_id)
    if scope is None:
        return None
    initiative = scope["initiative"]
    by_project: dict[str, list[dict]] = {}
    for items in roadmap_store.list_all().values():
        for change in items:
            if change.get("project_id"):
                by_project.setdefault(change["project_id"], []).append(change)

    goals = ", ".join(g["title"] for g in scope["goals"]) or "none yet (unaligned)"
    heading = (
        f'INITIATIVE: "{initiative["title"]}" [{initiative["status"]}] - this session\'s scope.\n'
        f"Serves: {goals}."
    )
    if initiative.get("description"):
        heading += f'\n{initiative["description"]}'
    # Derived from its projects (see portfolio.initiative_in_ideation). Said to the PM
    # explicitly so it treats the emptiness as the phase, not as a board to fill:
    # ideation sessions produce sharper ideas, not synthetic changes.
    if initiative.get("in_ideation"):
        heading += (
            "\nThis initiative is in IDEATION: its projects are ideas being explored, "
            "not committed work. Researching, checking data and shaping the ideas IS "
            "this session's output - do not invent changes to make the board look "
            "busy. When the stakeholder decides to build, set the project's status "
            "to open; that is what graduates it."
        )

    groups = []
    for project in scope["projects"]:
        changes = by_project.get(project["id"], [])
        # The initiative's auto-created catch-all is plumbing, not a project anyone
        # planned, so an empty one is left out entirely rather than shown as a project
        # with no changes - it exists to hold cost, and reading it back as work would
        # invite the PM to treat it as part of the plan. Once something has actually
        # landed in it, it shows like any other group: that IS unplanned work worth
        # seeing.
        if project.get("catch_all_for_initiative") and not changes:
            continue
        project_heading = (
            f'Project "{project["title"]}" [{project["status"]}] (id `{project["id"]}`)'
        )
        # The same tracked-as annotation a change's line carries, one rung up: the PM
        # should see which epic a project IS in Jira/ADO without asking. Its absence is
        # only stated when there are trackers to be absent from - and it means "created
        # locally, no epic filed yet", never an error. Whether filing one is POSSIBLE
        # differs per deployment, so the annotation says which: a PM told upload is
        # unavailable on a pushable deployment would refuse a click-away action, and one
        # told the opposite would promise an upload nobody can perform.
        if project.get("ticket_key"):
            ticket = _lookup_ticket(project["tracker_id"], project["ticket_key"])
            if ticket:
                project_heading += (
                    f' [tracked as {ticket["raw_type"]} {project["ticket_key"]}'
                    f' ({ticket["state"]}) in {project["tracker_id"]}]'
                )
            else:
                project_heading += (
                    f' [linked to {project["ticket_key"]} in {project["tracker_id"]}]'
                )
        elif tracker_store.is_configured and not project.get("catch_all_for_initiative"):
            project_heading += (
                " [local only - no epic in the tracker yet; the stakeholder can file one"
                " with Push epic on the board]"
                if any(t.can_push for t in CONFIG.trackers)
                else " [local only - no epic in the tracker yet; upload is not available]"
            )
        groups.append((project_heading, changes))
    if not groups:
        return (
            f"{heading}\n\nNo projects under this initiative yet - so there are no changes "
            "to show. Establishing what it is made of is part of this session's job.",
            [],
        )
    # Only the changes describe_initiative actually renders - it drops done ones, and a
    # change already shipped is exactly the kind of thing the digest should still be free
    # to mention on another board's line.
    shown = [
        change["id"]
        for _, changes in groups
        for change in changes
        if change["status"] != "done"
    ]
    return (
        roadmap_store.describe_initiative(
            heading,
            groups,
            ticket_lookup=_lookup_ticket,
            person_lookup=_lookup_person,
        ),
        shown,
    )


def _roadmap_context_for(session_id: str) -> str:
    """Builds the roadmap block injected into a PM's turn: full depth on its own
    product SUBTREE (the pinned product and, for a parent, its child products - see
    roadmap.subtree_products), a one-line digest of every product outside that subtree
    for general awareness only. A session with no pinned product (e.g. the default
    session) gets the shallow digest of every product and nothing deep - see sessions.py's
    Session.product.

    An initiative-scoped session (Session.initiative_id) gets its initiative at full depth
    FIRST, then the same product blocks for whichever boards it owns - the initiative is
    what that session is accountable for, and its changes cut across boards, so leading
    with one product's roadmap would bury the actual scope. Ownership is the union of its
    pinned and adopted subtrees (Session.owned_products), so a session that has adopted
    several boards reads each at full depth and still gets a digest of everything else.
    """
    session = sessions.get(session_id)
    if session is None:
        return roadmap_store.describe_other_products("")

    owned = session.owned_products()
    blocks: list[str] = []
    shown_ids: list[str] = []
    if session.initiative_id:
        rendered = _initiative_context(session.initiative_id)
        if rendered is not None:
            initiative_block, shown_ids = rendered
            blocks.append(initiative_block)
    # Each owned ROOT, not each owned product: describe_own_product already covers a
    # product's whole subtree, so passing every owned id would repeat a child's changes
    # under its own heading and again under its parent's.
    for root in [p for p in owned if parent_of(p) not in owned]:
        blocks.append(
            roadmap_store.describe_own_product(
                root, ticket_lookup=_lookup_ticket, person_lookup=_lookup_person
            )
        )

    others = roadmap_store.describe_other_products(owned or "", exclude_item_ids=shown_ids)
    if not blocks:
        # Owns nothing and has no initiative to lead with: the default session, whose
        # whole context IS the digest. Returned bare, with no "other products" heading -
        # there is no "own" block for them to be other than.
        return others
    if others:
        blocks.append(f"Other products (brief, for awareness only):\n{others}")
    return "\n\n".join(blocks)


def _public_session(session) -> dict:
    """The session payload the sessions list page consumes: the persisted Session plus
    a derived `activity` key (live working/waiting/idle - see SessionManager.activity_of).
    The single place that shape is built, so GET /sessions and every session-websocket
    broadcast the page reads stay consistent. `activity` is never persisted."""
    data = session.to_dict()
    data["activity"] = sessions.activity_of(session.id)
    return data


def _enrich_session_event(event: dict) -> dict:
    """Adds live `activity` to a broadcast event's session payload, matching
    _public_session's shape, so the sessions page's websocket events carry the same
    activity signal as its initial GET /sessions fetch."""
    session = event.get("session")
    if not isinstance(session, dict) or "id" not in session:
        return event
    return {**event, "session": {**session, "activity": sessions.activity_of(session["id"])}}


def _get_runtime(session_id: str) -> SessionRuntime:
    runtime = sessions.get_runtime(session_id)
    if runtime is None:
        raise HTTPException(status_code=404, detail=f"Unknown or inactive session: {session_id}")
    return runtime


def _wire_task_broadcast(session_id: str) -> None:
    """Subscribes a session's TaskRegistry to the websocket fan-out the moment
    it exists - at startup for every session already in the registry, and
    on-the-fly for sessions created afterwards via the session-update broadcast."""
    runtime = sessions.get_runtime(session_id)
    if runtime is not None:
        runtime.task_registry.subscribe(lambda task: _on_task_update(session_id, task))


def _on_session_update(event: dict) -> None:
    if event.get("type") == "session_created":
        _wire_task_broadcast(event["session"]["id"])
    if _main_loop is not None:
        _main_loop.call_soon_threadsafe(asyncio.create_task, _broadcast_session_update(event))


async def _broadcast_session_update(event: dict) -> None:
    message = json.dumps(_enrich_session_event(event))
    dead = []
    for ws in session_ws_clients:
        try:
            await ws.send_text(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        session_ws_clients.discard(ws)


def _on_task_update(session_id: str, task: dict) -> None:
    # Called from a dev-agent's background thread - hop onto the event loop thread.
    if _main_loop is not None:
        _main_loop.call_soon_threadsafe(asyncio.create_task, _broadcast_task_update(session_id, task))
        # A dev task starting/finishing changes the session's live activity, so push an
        # enriched session event to the sessions page too (it flips the card between
        # "Running dev task" and idle/waiting without a manual reload).
        session = sessions.get(session_id)
        if session is not None:
            _main_loop.call_soon_threadsafe(
                asyncio.create_task,
                _broadcast_session_update({"type": "session_updated", "session": session.to_dict()}),
            )
    # Only ordinary dev tasks re-engage the PM - a merge-conflict resolution isn't part
    # of its own plan (see tasks.py's "kind" field) and finishing one shouldn't make it
    # start narrating an unrelated merge to the stakeholder. A stopped task is settled
    # (its spend is recorded) but never re-engages the PM: someone stopped it on purpose.
    if task.get("status") in ("done", "error", "stopped"):
        usage = task.get("agent_usage") or {}
        if usage.get("cost_usd") or usage.get("input_tokens"):
            # Measured spend, recorded against the dispatcher but carrying no extra
            # labour weight of its own (the dispatch signal above already did that).
            _record_signal(
                None,
                KIND_DEV_TASK,
                session_id,
                usage={**usage, "cost_usd": usage.get("cost_usd", 0.0)},
                user_id="",
            )
        # The compliance judge's spend is agent spend of the same task - measured
        # separately (it's a different run on a different model) but recorded the same
        # way, so judged deployments don't under-report their token cost.
        judge_usage = (task.get("judge") or {}).get("agent_usage") or {}
        if judge_usage.get("cost_usd") or judge_usage.get("input_tokens"):
            _record_signal(
                None,
                KIND_DEV_TASK,
                session_id,
                usage={**judge_usage, "cost_usd": judge_usage.get("cost_usd", 0.0)},
                user_id="",
            )
        _dev_task_dispatchers.pop(task.get("id", ""), None)
    if task.get("kind") == "dev" and task.get("status") in ("done", "error"):
        threading.Thread(target=_auto_continue_pm, args=(session_id, task), daemon=True).start()


async def _broadcast_task_update(session_id: str, task: dict) -> None:
    message = json.dumps({"type": "task_update", "task": task})
    clients = task_ws_clients.get(session_id, set())
    dead = []
    for ws in clients:
        try:
            await ws.send_text(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


def _on_roadmap_update(event: dict) -> None:
    if _main_loop is not None:
        # Enrich here rather than in the store: the store deliberately knows nothing about
        # the tracker catalog, and the board needs the same joined shape from a websocket
        # event as it gets from GET /roadmap/data - otherwise a live edit would blank out
        # a card's ticket badge until the next full reload.
        if isinstance(event.get("item"), dict):
            event = {**event, "item": _with_ticket(event["item"])}
        _main_loop.call_soon_threadsafe(asyncio.create_task, _broadcast_roadmap_update(event))


async def _broadcast_roadmap_update(event: dict) -> None:
    message = json.dumps(event)
    dead = []
    for ws in roadmap_ws_clients:
        try:
            await ws.send_text(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        roadmap_ws_clients.discard(ws)


def _auto_continue_pm(session_id: str, task: dict) -> None:
    """Runs on its own thread (spawned from the dev task's own completion callback,
    already off the event loop) so the PM reacts to a finished task without the
    stakeholder having to send anything. Unlike a normal turn, nothing is waiting on a
    request/response websocket loop for this, so results are pushed to every open chat
    connection for the session instead of a single one."""
    runtime = sessions.get_runtime(session_id)
    if runtime is None:
        return
    try:
        other_ctx = sessions.describe_other_active_sessions(session_id)
        roadmap_ctx = _roadmap_context_for(session_id)
        for event in runtime.pm_agent.handle_task_completion(task, other_ctx, roadmap_ctx):
            # user_id="" - nobody was at the keyboard for an auto-continuation, so its
            # tokens count and its labour weight must not. One signal per completed turn,
            # as for a stakeholder's turn: pm_working and pm_progress carry no usage.
            if event.get("agent_usage") is not None:
                _record_signal(
                    None, KIND_PM_TURN, session_id, usage=event["agent_usage"], user_id=""
                )
            _broadcast_chat_event_threadsafe(session_id, {**event, "auto": True})
    except Exception as exc:
        _broadcast_chat_event_threadsafe(
            session_id, {"type": "pm_error", "message": str(exc), "auto": True}
        )


def _broadcast_chat_event_threadsafe(session_id: str, event: dict) -> None:
    if _main_loop is not None:
        _main_loop.call_soon_threadsafe(asyncio.create_task, _broadcast_chat_event(session_id, event))


async def _broadcast_chat_event(session_id: str, event: dict) -> None:
    message = json.dumps(event)
    clients = chat_ws_clients.get(session_id, {})
    dead = []
    for ws, lock in list(clients.items()):
        try:
            async with lock:
                await ws.send_text(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.pop(ws, None)


def _report_agent_auth() -> None:
    status = agent_env_module.agent_auth()
    if status["logged_in"]:
        print(f"[pm_studio] agents signed in to Claude as {status.get('email') or status.get('auth_method')}")
    else:
        reason = status.get("error") or "not signed in"
        print(f"[pm_studio] WARNING: agents cannot use Claude ({reason}) - sign them in from "
              "any chat or /systems (Sign in), or with `python -m pm_studio.agent_env --ensure-login`.")


def _quiet_connection_resets(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """Drops one known-harmless error and reports every other one as asyncio would.

    On Windows' Proactor loop, a browser that closes a connection abruptly (a reload, a
    closed tab, a dropped websocket) makes the loop's own cleanup callback
    (`_ProactorBasePipeTransport._call_connection_lost`) fail on a socket that is already
    gone, and asyncio prints a full traceback for it. Nothing is lost - the connection was
    over anyway - but a console full of them hides the errors that matter."""
    exc = context.get("exception")
    if isinstance(exc, ConnectionResetError) and "_call_connection_lost" in str(context.get("handle", "")):
        return
    loop.default_exception_handler(context)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _main_loop
    _main_loop = asyncio.get_event_loop()
    _main_loop.set_exception_handler(_quiet_connection_resets)
    # Say at startup, not at the first failed chat, when the agents cannot sign in to
    # Claude. In the background: it runs the CLI, and startup must not wait on it.
    threading.Thread(target=_report_agent_auth, daemon=True, name="agent-auth-check").start()
    for session_id in list(sessions.runtimes):
        _wire_task_broadcast(session_id)
    sessions.subscribe(_on_session_update)
    roadmap_store.subscribe(_on_roadmap_update)
    # Portfolio edits ride the roadmap socket: anything watching the board already
    # cares when a project is re-parented or an initiative closes.
    portfolio_store.subscribe(_on_roadmap_update)
    # Tracker syncs are MANUAL (the board's "Sync now" button / POST /trackers/sync).
    # There is no background poller: an unattended pass that rewrites the board on its
    # own schedule is how a stale or second instance quietly double-imports tickets, and
    # a pull from Jira/ADO is something to ask for when you want it, not something that
    # happens to the board while nobody is looking.
    # The intelligence ledger refreshes on its own cadence ([signals]
    # auto_refresh_minutes); 0 leaves it to the page's Refresh button.
    if CONFIG.signals.auto_refresh_minutes > 0:
        threading.Thread(target=intelligence_service.background_loop, daemon=True).start()
    # One line naming the [agents.mcp] servers agents will get, and any skipped (by
    # variable NAME only) - the first place to look when an agent lacks a tool.
    if agent_mcp.AGENT_MCP.servers():
        agent_mcp.AGENT_MCP.log_summary()
    # Stops abandoned session environments ([local_env] idle_stop_minutes); no thread
    # at all when that is 0. Stopped again by local_env.shutdown() below.
    local_env.start_idle_reaper()
    try:
        yield
    finally:
        # Local-env services run in their own process groups (so a Ctrl+C aimed at the
        # server does not reach them), which makes stopping them here the server's job.
        # The manager's atexit hook is the backstop for exits that skip the lifespan.
        local_env.shutdown()
        # Dev tasks first, so each one that gets to record itself in time reads
        # "stopped" rather than failed (one that does not is reconciled at the next start).
        tasks_stopped = 0
        for runtime in list(sessions.runtimes.values()):
            try:
                tasks_stopped += len(runtime.task_registry.stop_all("the server shutting down", wait=0))
            except Exception as exc:
                print(f"[pm_studio] stopping dev tasks failed: {exc}")
        # A PM turn mid-flight holds an executor thread on its `claude` process, and the
        # interpreter joins those threads on exit - so Ctrl+C would wait out the turn.
        # Every agent runs in its own process group, out of Ctrl+C's reach, so this is
        # what ends them (whole trees: MCP servers and all).
        stopped = stop_running_turns()
        if stopped or tasks_stopped:
            print(f"[pm_studio] stopped {stopped} agent process(es) still running "
                  f"({tasks_stopped} dev task(s))")


app = FastAPI(lifespan=lifespan)


# ---- authentication (enterprise mode only) ----
#
# Personal mode short-circuits every check below, so a deployment that never sets
# `[enterprise] mode` keeps the exact request path it has always had: no cookie, no
# login page, no roster. Enterprise mode is opt-in precisely because turning it on
# changes who may reach the dev agents.


def _wants_html(request: Request) -> bool:
    """A browser navigating to a page should be redirected to the login screen; an
    XHR or a curl call from a PM agent should get a 401 it can act on."""
    return "text/html" in request.headers.get("accept", "")


def _current_user(request: Request) -> User | None:
    return getattr(request.state, "user", None)


def _require_user(request: Request) -> User:
    """In personal mode there is no user object at all - callers that need an identity
    (invites, the roster) are enterprise-only endpoints and say so."""
    user = _current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def _require(request: Request, capability: Capability) -> User | None:
    """The single authorization gate for every mutating endpoint.

    Returns None in personal mode - there is no identity to authorize, and the request
    proceeds exactly as it did before accounts existed. In enterprise mode it returns
    the acting user, so callers can hand it straight to the audit log.

    The 403 says what the capability *was*, not just "forbidden": a viewer who pokes at
    an endpoint should learn what role would have been needed.
    """
    if not CONFIG.is_enterprise:
        return None
    user = _require_user(request)
    if not role_has(user.role, capability):
        raise HTTPException(
            status_code=403,
            detail=f"Your role ({user.role}) is not allowed to {CAPABILITY_LABELS[capability]}.",
        )
    return user


def _audit(actor: User | None, action: str, target: str = "", detail: str = "") -> None:
    """No-op in personal mode: a single trusted user acting alone is what the git
    snapshot history already records."""
    if actor is not None:
        audit_log.record(actor, action, target, detail)


def _require_enterprise() -> None:
    if not CONFIG.is_enterprise:
        raise HTTPException(
            status_code=404,
            detail="This instance runs in personal mode; enterprise features are off.",
        )


def _set_login_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=LOGIN_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        path="/",
    )


@app.middleware("http")
async def authenticate(request: Request, call_next):
    if not CONFIG.is_enterprise:
        return await call_next(request)

    path = request.url.path
    # A PM agent's curl calls carry the process's agent token instead of a cookie -
    # without this the core loop (dispatch a dev task, update the board) would 401 the
    # moment enterprise mode was switched on.
    if is_agent_token(request.headers.get(AGENT_HEADER_NAME)):
        request.state.user = agent_principal()
    else:
        request.state.user = account_store.resolve_login(
            request.cookies.get(SESSION_COOKIE_NAME)
        )

    # First run: an enterprise instance with no admin yet can only go to setup.
    # Without this an operator who flips the mode flag would be locked out of their
    # own server with no way in.
    if account_store.needs_setup and path not in ("/setup", "/auth/setup", "/auth/me"):
        if _wants_html(request):
            return RedirectResponse("/setup")
        return JSONResponse({"detail": "This instance has not been set up yet."}, status_code=503)

    if path in _PUBLIC_PATHS or request.state.user is not None:
        return await call_next(request)

    if _wants_html(request):
        return RedirectResponse("/login")
    return JSONResponse({"detail": "Authentication required"}, status_code=401)


async def _ws_reject(websocket: WebSocket, capability: Capability = "view") -> bool:
    """HTTP middleware never sees a websocket handshake, so every socket checks the
    cookie and its own capability itself. Returns True when the connection was refused.

    1008 is the policy-violation close code; the page treats it as "reload and you'll
    get the login screen". This matters most for the chat socket: sending a turn to a
    PM is `run_session`, so a viewer must not be able to skip the HTTP layer and drive
    an agent through the websocket instead.
    """
    if not CONFIG.is_enterprise:
        return False
    user = account_store.resolve_login(websocket.cookies.get(SESSION_COOKIE_NAME))
    if user is not None and role_has(user.role, capability):
        return False
    await websocket.close(code=1008)
    return True


# The shared navigation chrome, the only asset more than one page pulls in. Named
# routes rather than a StaticFiles mount so that the set of files this server will hand
# out stays an explicit list and no path can be traversed into.
#
# `no-cache` is load-bearing, not boilerplate. FileResponse sends ETag and Last-Modified
# but no Cache-Control, and a browser given neither a max-age nor a no-cache directive
# falls back to HEURISTIC caching: it reuses a subresource for a fraction of its age
# WITHOUT revalidating. A page is a navigation and gets revalidated; nav.js is a
# subresource and does not - so an upgrade could pair a freshly-fetched page with a
# stale nav.js from cache. That combination is not merely cosmetic: a page calling a
# `window.PMNav` API the cached nav.js predates throws during its inline script and
# renders NOTHING AT ALL - an empty board under a working nav bar.
#
# Every asset here is served from disk and carries no version in its URL, so there is
# nothing to cache-bust with and the only safe answer is to revalidate. Note that a bare
# FileResponse does not implement conditional requests (that lives in StaticFiles, which
# this deliberately isn't), so revalidation re-sends the body rather than 304-ing. That
# is the right trade here and nowhere near a general one: this server binds to localhost
# for one user, and the whole set is a few hundred KB.
def _app_asset(name: str, media_type: str | None = None) -> FileResponse:
    return FileResponse(
        STATIC_DIR / name,
        media_type=media_type,
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/static/nav.css")
def nav_css() -> FileResponse:
    return _app_asset("nav.css", "text/css")


@app.get("/static/nav.js")
def nav_js() -> FileResponse:
    return _app_asset("nav.js", "application/javascript")


# The chat's markdown renderer (marked) and HTML sanitizer (DOMPurify), vendored.
@app.get("/static/marked.min.js")
def marked_js() -> FileResponse:
    return _app_asset("marked.min.js", "application/javascript")


@app.get("/static/purify.min.js")
def purify_js() -> FileResponse:
    return _app_asset("purify.min.js", "application/javascript")


@app.get("/login")
def login_page() -> FileResponse:
    return _app_asset("login.html")


@app.get("/setup")
def setup_page() -> FileResponse:
    return _app_asset("setup.html")


@app.get("/accept-invite")
def accept_invite_page() -> FileResponse:
    return _app_asset("accept-invite.html")


@app.get("/auth/me")
def auth_me(request: Request) -> dict:
    """The one endpoint every page calls to learn what mode it is in and who it is.
    Deliberately public: in personal mode it answers `{"mode": "personal"}` and the
    pages hide all account UI."""
    user = _current_user(request)
    return {
        "mode": CONFIG.mode,
        "enterprise": CONFIG.is_enterprise,
        "needs_setup": CONFIG.is_enterprise and account_store.needs_setup,
        "user": user.to_public_dict() if user is not None else None,
        "roles": ROLE_LABELS,
        "smtp_configured": bool(CONFIG.smtp and CONFIG.smtp.is_usable),
        # Deployment shape, like smtp_configured: whether [systems] is declared at all.
        # The nav uses it to decide whether the Systems tab exists, so a deployment that
        # does not use the layer is offered no tab into an empty taxonomy.
        "systems_declared": systems_declared(),
        # Where GET / points, so the nav can keep a Sessions tab that works when the
        # front door serves the overview instead.
        "landing": CONFIG.landing,
        # So a page can hide controls that would only 403. Every one of these is
        # still enforced server-side, independently of what the UI chooses to show.
        "capabilities": capabilities_of(user.role) if user is not None else [],
        "capability_matrix": describe_matrix(),
    }


@app.post("/auth/setup")
def auth_setup(response: Response, payload: dict = Body(...)) -> dict:
    """First-run owner creation. The account that converts a personal instance to
    enterprise becomes its admin - see the `[enterprise]` docs."""
    _require_enterprise()
    try:
        user = account_store.create_owner(
            email=(payload.get("email") or ""),
            name=(payload.get("name") or ""),
            password=(payload.get("password") or ""),
        )
    except AccountError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _set_login_cookie(response, account_store.start_login(user.id))
    return user.to_public_dict()


@app.post("/auth/login")
def auth_login(response: Response, payload: dict = Body(...)) -> dict:
    _require_enterprise()
    try:
        user = account_store.authenticate(
            email=(payload.get("email") or ""), password=(payload.get("password") or "")
        )
    except AccountError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    _set_login_cookie(response, account_store.start_login(user.id))
    return user.to_public_dict()


@app.post("/auth/logout")
def auth_logout(request: Request, response: Response) -> dict:
    account_store.end_login(request.cookies.get(SESSION_COOKIE_NAME))
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return {"status": "logged_out"}


@app.get("/auth/invite")
def auth_peek_invite(token: str) -> dict:
    """Lets the accept-invite page show who the invite is for before a password is
    typed. Consumes nothing."""
    _require_enterprise()
    try:
        invite = account_store.peek_invite(token)
    except AccountError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"email": invite.email, "role": invite.role, "role_label": ROLE_LABELS.get(invite.role, invite.role)}


@app.post("/auth/accept-invite")
def auth_accept_invite(response: Response, payload: dict = Body(...)) -> dict:
    _require_enterprise()
    try:
        user = account_store.accept_invite(
            token=(payload.get("token") or ""),
            name=(payload.get("name") or ""),
            password=(payload.get("password") or ""),
        )
    except AccountError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _set_login_cookie(response, account_store.start_login(user.id))
    return user.to_public_dict()


# ---- roster + invites (admin) ----


@app.get("/users")
def list_users(request: Request) -> list[dict]:
    _require_enterprise()
    _require(request, "manage_users")
    return account_store.list_users()


@app.post("/users/{user_id}/role")
def set_user_role(user_id: str, request: Request, payload: dict = Body(...)) -> dict:
    _require_enterprise()
    actor = _require(request, "manage_users")
    try:
        updated = account_store.set_role(user_id, (payload.get("role") or ""))
        _audit(actor, "user.role_changed", updated.email, f"now {updated.role}")
        return updated.to_public_dict()
    except AccountError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/users/{user_id}/status")
def set_user_status(user_id: str, request: Request, payload: dict = Body(...)) -> dict:
    _require_enterprise()
    actor = _require(request, "manage_users")
    status = (payload.get("status") or "").strip()
    if status not in ("active", "disabled"):
        raise HTTPException(status_code=400, detail="status must be 'active' or 'disabled'")
    try:
        updated = account_store.set_status(user_id, status)
        _audit(actor, "user.status_changed", updated.email, status)
        return updated.to_public_dict()
    except AccountError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/invites")
def list_invites(request: Request) -> list[dict]:
    _require_enterprise()
    _require(request, "manage_users")
    return account_store.list_invites()


@app.post("/invites")
def create_invite(request: Request, payload: dict = Body(...)) -> dict:
    """Invites one address at a given role. Always returns the accept URL - if SMTP is
    configured we also mail it, but the link is what makes the flow work on a machine
    with no mail server (see mailer.send_invite)."""
    _require_enterprise()
    admin = _require(request, "manage_users")
    try:
        new_invite = account_store.invite(
            email=(payload.get("email") or ""),
            role=(payload.get("role") or "viewer"),
            invited_by=admin.id,
        )
    except AccountError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    accept_url = new_invite.accept_url(CONFIG.base_url)
    emailed = mailer.send_invite(
        CONFIG, new_invite.invite.email, admin.name, accept_url
    )
    return {
        "invite": new_invite.invite.to_public_dict(),
        "accept_url": accept_url,
        "emailed": emailed,
    }


@app.delete("/invites/{invite_id}")
def revoke_invite(invite_id: str, request: Request) -> dict:
    _require_enterprise()
    actor = _require(request, "manage_users")
    try:
        invite = account_store.revoke_invite(invite_id)
        _audit(actor, "invite.revoked", invite.email)
    except AccountError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "revoked"}


@app.get("/audit")
def read_audit(request: Request, limit: int = 200) -> list[dict]:
    """Who did what. Admin-only: it names people and quotes dispatched task text."""
    _require_enterprise()
    _require(request, "manage_users")
    return audit_log.tail(limit=limit)


@app.get("/people")
def people_page() -> FileResponse:
    """Who is doing the work - the people directory - plus, for an admin on an enterprise
    instance, the roster: users, roles, invites. The page hides the half you cannot use."""
    return _app_asset("people.html")


# ---- the people directory -------------------------------------------------------------
#
# Deliberately NOT behind _require_enterprise, unlike the roster endpoints above. The
# directory is not an access-control list - it is who is doing the work, discovered from the
# trackers - and a one-person deployment syncing a shared Jira has exactly the same need to
# see who else is on what. Writes take `manage_roadmap` (admin + pm) rather than
# `manage_users`: repairing a name match or adding a designer nobody invited is roadmap
# housekeeping, not handing out access.


@app.get("/people/directory")
def list_directory() -> dict:
    """Everybody the deployment knows about, plus the load table.

    Reads are open, like every other read here (see authz.py) - hence no `request` to
    authorize. The load rides along so the people page can show it without duplicating the
    board's join.
    """
    changes = [
        _with_ticket(item)
        for items in roadmap_store.list_all().values()
        for item in items
    ]
    return {
        "people": people_store.list_people(),
        "workload": workload(changes),
        "unassigned_key": UNASSIGNED,
        # So the page can offer "this person signs in as ..." without a second request.
        # Empty in personal mode, where there are no accounts at all.
        "accounts": account_store.list_users() if CONFIG.is_enterprise else [],
    }


@app.post("/people/directory")
def create_person(request: Request, payload: dict = Body(...)) -> dict:
    """Somebody the trackers have never seen: a designer, a partner team's lead, whoever a
    locally-planned change is for before a ticket exists."""
    actor = _require(request, "manage_roadmap")
    try:
        person = people_store.create_local(
            name=payload.get("name") or "", email=payload.get("email") or ""
        )
    except PeopleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(actor, "people.created", person.id, person.name)
    return person.to_public_dict()


@app.patch("/people/directory/{person_id}")
def update_person(person_id: str, request: Request, payload: dict = Body(default={})) -> dict:
    actor = _require(request, "manage_roadmap")
    if "account_id" in payload:
        account_id = (payload.get("account_id") or "").strip()
        if account_id and account_store.get(account_id) is None:
            raise HTTPException(status_code=400, detail=f"Unknown account: {account_id}")
    try:
        person = people_store.update(
            person_id,
            name=payload.get("name"),
            email=payload.get("email"),
            status=payload.get("status"),
            # "" unlinks the sign-in account, same convention as the roadmap fields.
            account_id=payload.get("account_id"),
        )
    except PeopleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(actor, "people.updated", person.id, person.name)
    return person.to_public_dict()


@app.post("/people/directory/{person_id}/merge")
def merge_person(person_id: str, request: Request, payload: dict = Body(...)) -> dict:
    """Fold `from` into `person_id`: they were one human all along.

    The repair for the reconciliation that could not know - both trackers hid the email and
    the display names differ, so nothing could have matched them automatically.

    Local assignments are re-pointed BEFORE the merge, deliberately. Done the other way, a
    failure between the two steps would leave changes naming an id that no longer exists;
    done this way, the worst case is changes correctly naming the survivor while the
    absorbed entry lingers, which the next attempt simply finishes.
    """
    actor = _require(request, "manage_roadmap")
    from_id = (payload.get("from") or "").strip()
    if people_store.get(person_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown person: {person_id}")
    if people_store.get(from_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown person: {from_id}")
    moved = 0
    for item in roadmap_store.items_assigned_to(from_id):
        roadmap_store.update(item["id"], assignee=person_id)
        moved += 1
    try:
        person = people_store.merge(person_id, from_id)
    except PeopleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(actor, "people.merged", person.id, f"absorbed {from_id}, {moved} change(s) moved")
    return {**person.to_public_dict(), "changes_moved": moved}


@app.post("/people/directory/{person_id}/split")
def split_person_identity(person_id: str, request: Request, payload: dict = Body(...)) -> dict:
    """Pull one tracker identity back out into its own entry: the name match was wrong, and
    these are two different people."""
    actor = _require(request, "manage_roadmap")
    try:
        split = people_store.split_identity(
            person_id,
            tracker_id=(payload.get("tracker_id") or "").strip(),
            key=(payload.get("key") or "").strip(),
        )
    except PeopleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(actor, "people.split", split.id, f"split out of {person_id}")
    return split.to_public_dict()


@app.delete("/people/directory/{person_id}")
def delete_person(person_id: str, request: Request) -> dict:
    """Only ever a local entry no tracker knows about - anybody else returns on the next
    sync, so `inactive` is the honest way to retire them. Refused while any change still
    names them, because deleting one silently unassigns real work."""
    actor = _require(request, "manage_roadmap")
    assigned = roadmap_store.items_assigned_to(person_id)
    if assigned:
        raise HTTPException(
            status_code=400,
            detail=f"{len(assigned)} change(s) are still assigned to this person. "
            "Reassign them first, or set the person inactive to keep the history.",
        )
    try:
        people_store.delete(person_id)
    except PeopleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(actor, "people.deleted", person_id)
    return {"ok": True}


# ---- session picker / lifecycle ----

@app.get("/")
def index():
    """The front door. `[server] landing = "overview"` points it at the read-first
    answer to "what are we working on?"; the default stays the sessions list. A
    redirect rather than serving the file here, so the overview keeps one canonical
    URL wherever a visitor enters."""
    if CONFIG.landing == "overview":
        return RedirectResponse("/overview", status_code=307)
    return _app_asset("sessions.html")


@app.get("/sessions-page")
def sessions_page() -> FileResponse:
    """The sessions list at a stable address, whatever the landing setting - the nav's
    Sessions tab points here when / no longer serves it."""
    return _app_asset("sessions.html")


@app.get("/sessions")
def list_sessions() -> list[dict]:
    return [_public_session(s) for s in sessions.list_sessions()]


@app.post("/sessions")
def create_session(request: Request, payload: dict = Body(default={})) -> dict:
    actor = _require(request, "manage_session_lifecycle")
    name = (payload.get("name") or "").strip() or None
    product = (payload.get("product") or "").strip() or None
    model = (payload.get("model") or "").strip() or None
    mode = (payload.get("mode") or "").strip() or None
    project_id = _validated_project_id(payload.get("project_id"))
    initiative_id = (payload.get("initiative_id") or "").strip() or None
    initiative = None
    if initiative_id is not None:
        initiative = portfolio_store.get_initiative(initiative_id)
        if initiative is None:
            raise HTTPException(
                status_code=400, detail=f"Unknown initiative: {initiative_id}"
            )
        # Same reason as set_session_initiative: the attribution fallback is a pure read,
        # so the project it falls back to has to exist before the first turn runs.
        try:
            portfolio_store.ensure_initiative_catch_all(initiative_id)
        except PortfolioError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    # An initiative-scoped session with no product has no product label to be named
    # after, and "Session 4f2a1b" tells nobody anything - so it takes the initiative's
    # title, which is exactly what it is about.
    if name is None and product is None and initiative is not None:
        name = initiative.title
    try:
        session = sessions.create(
            name, product, model, project_id=project_id, initiative_id=initiative_id,
            mode=mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(actor, "session.created", session.id, session.name)
    return session.to_dict()


@app.get("/sessions/{session_id}")
def get_session(session_id: str) -> dict:
    session = sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown session")
    # _public_session so the response carries live `activity` (working/waiting/idle),
    # matching GET /sessions and the websocket - the chat page reads activity.reason
    # here to tell whether the PM is mid-turn ("thinking") on a mid-turn reload.
    return _public_session(session)


@app.post("/sessions/{session_id}/model")
def set_session_model(session_id: str, request: Request, payload: dict = Body(...)) -> dict:
    """Changes which Claude model this session's PM turns and dev tasks run on,
    live - see SessionManager.set_model."""
    _require(request, "run_session")
    model = (payload.get("model") or "").strip()
    try:
        session = sessions.set_model(session_id, model)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return session.to_dict()


@app.post("/sessions/{session_id}/mode")
def set_session_mode(session_id: str, request: Request, payload: dict = Body(...)) -> dict:
    """Switches a session between build and research/strategy (see sessions.Mode), live.
    Audited because it moves the code-execution boundary: switching to build is the
    stakeholder deciding this session's work may now become dev dispatches. Not reachable
    by the PM itself - its allowlist carries no curl for this URL, so graduating a
    research session is always a human's call, never the agent's."""
    actor = _require(request, "run_session")
    mode = (payload.get("mode") or "").strip()
    try:
        session = sessions.set_mode(session_id, mode)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(actor, "session.mode_set", session_id, mode)
    return _public_session(session)


@app.post("/sessions/{session_id}/meta")
def set_session_meta(session_id: str, request: Request, payload: dict = Body(...)) -> dict:
    """Sets the PM-maintained title and/or goal for this session - the self-updating
    identity shown in the sessions list (see SessionManager.set_meta). Reuses
    _public_session so the response carries live activity + the new title/goal, the
    same shape the sessions page consumes from GET /sessions and the websocket."""
    _require(request, "run_session")
    title = payload.get("title")
    goal = payload.get("goal")
    try:
        session = sessions.set_meta(session_id, title=title, goal=goal)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _public_session(session)


@app.post("/sessions/{session_id}/project")
def set_session_project(session_id: str, request: Request, payload: dict = Body(...)) -> dict:
    """Points a session's work at a Project in the work model. `project_id: ""` clears
    it, after which the session's activity falls back to the catch-all project."""
    actor = _require(request, "run_session")
    project_id = _validated_project_id(payload.get("project_id"))
    try:
        session = sessions.set_project(session_id, project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _audit(actor, "session.project_set", session_id, project_id or "(cleared)")
    return _public_session(session)


@app.post("/sessions/{session_id}/initiative")
def set_session_initiative(session_id: str, request: Request, payload: dict = Body(...)) -> dict:
    """Scopes a session to an Initiative - work that spans several products rather than
    sitting on one board. `initiative_id: ""` clears it.

    Creates that initiative's catch-all project as a side effect, so the session's turns
    are attributed to the initiative it is actually working in from the very first one,
    rather than to maintenance (see _session_project_id). Done here, once, because the
    read on the signal-recording path must stay a read.
    """
    actor = _require(request, "run_session")
    initiative_id = (payload.get("initiative_id") or "").strip() or None
    if initiative_id is not None:
        if portfolio_store.get_initiative(initiative_id) is None:
            raise HTTPException(
                status_code=400, detail=f"Unknown initiative: {initiative_id}"
            )
        try:
            portfolio_store.ensure_initiative_catch_all(initiative_id)
        except PortfolioError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        session = sessions.set_initiative(session_id, initiative_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _audit(actor, "session.initiative_set", session_id, initiative_id or "(cleared)")
    return _public_session(session)


@app.post("/sessions/{session_id}/scope")
def set_session_scope(session_id: str, request: Request, payload: dict = Body(...)) -> dict:
    """Adopts or releases one product board for this session - how an initiative-scoped
    session widens as it works out which products its initiative actually touches.

    Called by the PM itself (see agent.py's INITIATIVE_GUIDANCE_TEMPLATE) as well as from
    the sessions page, which is why the URL carries the session id: the PM's allowlist
    grants this exact literal prefix, so a session can only ever widen ITS OWN authority.
    Refused for a session with no initiative - a product-pinned session's scope is the
    stakeholder's to change, not something the PM talks itself into.
    """
    actor = _require(request, "run_session")
    adopt = (payload.get("adopt_product") or "").strip() or None
    release = (payload.get("release_product") or "").strip() or None
    if bool(adopt) == bool(release):
        raise HTTPException(
            status_code=400,
            detail="Pass exactly one of adopt_product or release_product.",
        )
    session = sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}")
    if not session.initiative_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "This session is not scoped to an initiative, so its product boards are "
                "fixed. Pin it to an initiative first if it needs to span products."
            ),
        )
    try:
        if adopt:
            session = sessions.adopt_product(session_id, adopt)
        else:
            session = sessions.release_product(session_id, release)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(
        actor,
        "session.product_adopted" if adopt else "session.product_released",
        session_id,
        adopt or release,
    )
    return _public_session(session)


# ---- time & cost attribution (admin only) ----


@app.get("/costing")
def costing_page() -> FileResponse:
    return _app_asset("costing.html")


@app.get("/costing/roster")
def costing_roster(request: Request) -> dict:
    """Rates and capacities, joined to the user roster so the screen can show names.

    Admin-only via `view_cost`: this is compensation data, and it stays the narrowest
    grant in the matrix even though the roadmap it hangs off is visible to everyone.
    """
    _require(request, "view_cost")
    configured = {row["user_id"]: row for row in costing_store.list_roster()}
    people = account_store.list_users() if CONFIG.is_enterprise else [
        {"id": "local", "name": "This machine", "email": "", "role": "admin"}
    ]
    rows = []
    for person in people:
        entry = costing_store.entry_for(person["id"])
        rows.append(
            {
                "user_id": person["id"],
                "name": person.get("name"),
                "email": person.get("email"),
                "role": person.get("role"),
                "rate_per_hour": entry.rate_per_hour,
                "capacity_hours_per_week": entry.capacity_hours_per_week,
                "configured": person["id"] in configured,
            }
        )
    return {
        "currency": CONFIG.costing.currency,
        "blended_rate": CONFIG.costing.blended_rate,
        "default_capacity_hours": CONFIG.costing.default_capacity_hours,
        "rows": rows,
    }


@app.post("/costing/roster/{user_id}")
def set_costing_entry(user_id: str, request: Request, payload: dict = Body(...)) -> dict:
    """`rate_per_hour: ""` (or null with clear_rate) drops back to the blended rate."""
    actor = _require(request, "view_cost")
    raw_rate = payload.get("rate_per_hour")
    clear_rate = raw_rate is not None and str(raw_rate).strip() == ""
    try:
        entry = costing_store.set_entry(
            user_id,
            rate_per_hour=None if clear_rate or raw_rate is None else float(raw_rate),
            capacity_hours_per_week=(
                float(payload["capacity_hours_per_week"])
                if payload.get("capacity_hours_per_week") not in (None, "")
                else None
            ),
            clear_rate=clear_rate,
        )
    except (CostingError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Deliberately no rate in the audit detail - the log is readable by every admin.
    _audit(actor, "costing.entry_updated", user_id)
    return entry.to_dict()


@app.get("/costing/report")
def costing_report(request: Request, week: str | None = None) -> dict:
    """A week's distribution, plus the initiative rollup.

    Hours are an approximation by construction: a declared capacity split by signal
    share. What makes them usable is that they reconcile - they always sum to a real
    week - not that they are precise.
    """
    _require(request, "view_cost")
    target = (week or "").strip() or current_week()
    known = (
        [u["id"] for u in account_store.list_users()] if CONFIG.is_enterprise else ["local"]
    )
    try:
        report = costing_store.distribute_week(target, user_ids=known)
    except CostingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    report["rollup"] = costing_store.rollup_to_initiatives(
        report["by_project"], portfolio_store
    )
    # The to-date view beside the weekly one: "what has this cost so far" is the
    # portfolio question, and a week-scoped page structurally couldn't answer it.
    cumulative = costing_store.cumulative_to_date(user_ids=known)
    cumulative["rollup"] = costing_store.rollup_to_initiatives(
        cumulative["by_project"], portfolio_store
    )
    report["cumulative"] = cumulative
    report["portfolio"] = {
        "projects": portfolio_store.list_projects(),
        "initiatives": portfolio_store.list_initiatives(),
    }
    if CONFIG.is_enterprise:
        report["user_names"] = {
            u["id"]: u.get("name") or u.get("email") for u in account_store.list_users()
        }
    else:
        report["user_names"] = {"local": "This machine"}
    return report


@app.post("/costing/override")
def set_costing_override(request: Request, payload: dict = Body(...)) -> dict:
    """Replaces one person's whole week.

    Whole-week rather than per-project on purpose: the distribution's useful property is
    that it sums to a real week, and overriding a single project in isolation would
    silently break that. The derived figures are kept alongside, so a report always
    shows what the system thought as well as what a human decided.
    """
    actor = _require(request, "view_cost")
    week = (payload.get("week") or "").strip() or current_week()
    user_id = (payload.get("user_id") or "").strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")
    hours = payload.get("hours_by_project")
    try:
        if hours is None:
            costing_store.clear_override(week, user_id)
        else:
            costing_store.set_override(week, user_id, hours)
    except (CostingError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(actor, "costing.override" if hours is not None else "costing.override_cleared",
           f"{week}/{user_id}")
    return {"status": "ok", "week": week, "user_id": user_id}


@app.get("/models")
def get_models() -> list[dict]:
    return list_models()


@app.post("/sessions/{session_id}/merge")
def merge_session(session_id: str, request: Request) -> dict:
    actor = _require(request, "manage_session_lifecycle")
    try:
        session = sessions.merge(session_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(actor, "session.merged", session_id)
    return session.to_dict()


@app.post("/sessions/{session_id}/cleanup")
def cleanup_session(session_id: str, request: Request) -> dict:
    actor = _require(request, "manage_session_lifecycle")
    existing = sessions.get(session_id)
    if existing is not None and existing.status == "merged":
        _stop_for_lifecycle(session_id, actor, "cleaning up the session")
        _release_local_env(session_id, actor)
    try:
        session = sessions.cleanup(session_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(actor, "session.cleaned_up", session_id)
    return session.to_dict()


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str, request: Request) -> dict:
    actor = _require(request, "manage_session_lifecycle")
    existing = sessions.get(session_id)
    if existing is not None and not existing.is_default:
        _stop_for_lifecycle(session_id, actor, "deleting the session")
        _release_local_env(session_id, actor)
    try:
        session = sessions.delete(session_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(actor, "session.deleted", session_id)
    return session.to_dict()


@app.post("/sessions/{session_id}/archive")
def archive_session(session_id: str, request: Request) -> dict:
    """Hides the session from the default session list. A visibility flag - doesn't
    touch the session's lifecycle status, worktree, or branch - but a hidden session
    keeps nothing running: its PM turn, dev tasks and local services are stopped first
    (its environment's resources are kept, for when it is unarchived)."""
    actor = _require(request, "manage_session_lifecycle")
    if sessions.get(session_id) is not None:
        _stop_for_lifecycle(session_id, actor, "archiving the session")
    try:
        session = sessions.archive(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return session.to_dict()


@app.post("/sessions/{session_id}/unarchive")
def unarchive_session(session_id: str, request: Request) -> dict:
    _require(request, "manage_session_lifecycle")
    try:
        session = sessions.unarchive(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return session.to_dict()


@app.post("/sessions/{session_id}/stop")
def stop_session(session_id: str, request: Request) -> dict:
    """Stops everything the session has running - the PM turn, the messages queued
    behind it, every running dev task and its local environment's services (resources
    are kept) - and leaves the session itself as it is, ready for the next message.
    Returns what it stopped: {pm_turn, tasks, services, discarded}, all empty when
    nothing was running."""
    actor = _require(request, "run_session")
    if sessions.get(session_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}")
    report = _stop_session(session_id, _who(actor))
    _audit(actor, "session.stopped", session_id, json.dumps(report))
    return report


@app.post("/sessions/{session_id}/sync")
def sync_session(session_id: str, request: Request) -> dict:
    """Merges main into this session's own worktree, so it doesn't drift far enough
    from main to make its eventual terminate-time merge painful."""
    actor = _require(request, "manage_session_lifecycle")
    try:
        session = sessions.sync(session_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(actor, "session.synced", session_id)
    return session.to_dict()


@app.post("/sessions/{session_id}/terminate")
def terminate_session(session_id: str, request: Request) -> dict:
    """Merges the session into main, archives its final spec/chat, then removes the
    worktree - the only way a non-default session ends. Runs in the background;
    poll /sessions/{id} or watch /ws/sessions for the outcome.

    The session's local environment is released FIRST, synchronously for its services
    (they run from the worktree the terminate will remove) and in the background for
    its resources. Released even though the merge could still stop on a conflict: the
    session then stays active, and its environment simply comes back on its next start
    (with a fresh resource)."""
    actor = _require(request, "manage_session_lifecycle")
    existing = sessions.get(session_id)
    if existing is not None and not existing.is_default and existing.status in ("active", "conflict"):
        _stop_for_lifecycle(session_id, actor, "terminating the session")
        _release_local_env(session_id, actor)
    try:
        session = sessions.terminate(session_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(actor, "session.terminated", session_id)
    return session.to_dict()


@app.websocket("/ws/sessions")
async def sessions_ws(websocket: WebSocket) -> None:
    if await _ws_reject(websocket):
        return
    await websocket.accept()
    session_ws_clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()  # inbound messages ignored; push-only
    except WebSocketDisconnect:
        pass
    finally:
        session_ws_clients.discard(websocket)


# ---- session-scoped pages ----

@app.get("/chat/{session_id}")
def chat_page(session_id: str) -> FileResponse:
    return _app_asset("chat.html")


@app.get("/dashboard/{session_id}")
def dashboard_page(session_id: str) -> FileResponse:
    return _app_asset("dashboard.html")


# ---- roadmap board (cross-session, cross-product - not scoped to one session) ----

def _actor_id(actor: User | None) -> str:
    """Who to attribute activity to. In personal mode there is no identity, so a single
    stable id is used - a solo user still gets a useful token-cost report, and nothing
    has to branch on mode at every call site."""
    return actor.id if actor is not None else "local"


def _session_project_id(session_id: str) -> str | None:
    """Which Project a session's activity counts towards: its own if it has one, else its
    initiative's catch-all if it is initiative-scoped, else the global catch-all if the
    deployment declared one, else nothing. This mirrors how a change with no project lands
    in the catch-all - unplanned work is attributed rather than silently dropped.

    The middle step is not optional. The global catch-all hangs off the MAINTENANCE
    initiative, so falling straight through to it would bill every turn of a session
    scoped to some other initiative against maintenance - silently, and in the one report
    that exists to answer "what did this initiative cost". The per-initiative catch-all is
    created when the session is pinned (see set_session_initiative); this path only reads,
    since it runs on every recorded signal.
    """
    session = sessions.get(session_id)
    if session is None:
        return portfolio_store.catch_all_project_id
    if session.project_id:
        return session.project_id
    if session.initiative_id:
        scoped = portfolio_store.catch_all_project_for_initiative(session.initiative_id)
        if scoped is not None:
            return scoped
    return portfolio_store.catch_all_project_id


# The window "recently touched" is measured over, for the activity readout the board
# shows on ideation rows. One constant, applied server-side, so the board and any other
# consumer count the same sessions - a client never re-derives the window.
ACTIVITY_RECENT_DAYS = 30


def _project_activity() -> dict:
    """Per-project last-touched + recent-session counts (see costing.project_activity).
    Failures degrade to "no activity" rather than breaking a board load - the same
    posture as _record_signal, whose data this reads back."""
    try:
        return costing_store.project_activity(
            recent_since=time.time() - ACTIVITY_RECENT_DAYS * 86400
        )
    except Exception:
        return {}


def _record_signal(
    actor: User | None,
    kind: str,
    session_id: str,
    usage: dict | None = None,
    user_id: str | None = None,
) -> None:
    """Records one activity signal. Failures are swallowed on purpose: cost
    bookkeeping must never be able to break a PM turn or a dispatch."""
    usage = usage or {}
    try:
        costing_store.record(
            user_id=user_id if user_id is not None else _actor_id(actor),
            kind=kind,
            project_id=_session_project_id(session_id),
            session_id=session_id,
            agent_cost_usd=usage.get("cost_usd", 0.0),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
        )
    except Exception:
        pass


def _validated_project_id(raw) -> str | None:
    """Passes through None ("no change") and "" ("detach"), but rejects an unknown id -
    a change pointing at a project that doesn't exist would silently fall out of every
    rollup."""
    if raw is None:
        return None
    project_id = str(raw).strip()
    if project_id and not portfolio_store.project_exists(project_id):
        raise HTTPException(status_code=400, detail=f"Unknown project: {project_id}")
    return project_id


def _resolve_project_id(payload: dict) -> str | None:
    """The parent project for a new change.

    When the caller names one, it is validated. When it doesn't, the change lands in
    the catch-all project if the deployment has declared one - that is what keeps
    unplanned work aligned without making every creation path ask about strategy
    first. A deployment not using the work model has no catch-all, and changes simply
    carry no project, exactly as before.
    """
    explicit = _validated_project_id(payload.get("project_id"))
    if explicit:
        return explicit
    return portfolio_store.catch_all_project_id


# ---- work model: goals, initiatives, projects ----


@app.get("/portfolio")
def portfolio_page() -> FileResponse:
    return _app_asset("portfolio.html")


@app.get("/overview")
def overview_page() -> FileResponse:
    return _app_asset("overview.html")


@app.get("/overview/data")
def overview_data(product: str | None = None) -> dict:
    """One read-only payload answering "what are we working on?" - initiative rollups
    with derived health, the shipped-recently feed, the overdue list and per-person
    load. Composed here because it joins all three stores; the arithmetic itself lives
    in overview.build_overview, pure and store-free."""
    changes = [
        _with_ticket(item)
        for items in roadmap_store.list_all().values()
        for item in items
    ]
    # Reads are how the week's plan gets pinned: lazy capture needs a hook that fires
    # often, and the overview is the page every day starts on. Pinning happens BEFORE
    # any scoping - the plan is the whole board's.
    plan_store.ensure_current(changes)
    # ?product= narrows the whole answer to one product family - "what is this one
    # team doing" as a shareable page. Everything downstream (groups, feeds, load,
    # the plan classification) sees only the family's changes.
    scope = None
    if product:
        if product not in PRODUCTS:
            raise HTTPException(status_code=404, detail=f"Unknown product: {product}")
        family = set(subtree_products(product))
        changes = [c for c in changes if c["product"] in family]
        scope = {"product": product, "label": product_path_label(product)}
    groups = portfolio_store.group_changes_by_initiative(changes)
    cumulative = costing_store.cumulative_to_date(
        user_ids=None if CONFIG.is_enterprise else ["local"]
    )
    cost_rollup = costing_store.rollup_to_initiatives(
        cumulative["by_project"], portfolio_store
    )["initiatives"]
    payload = build_overview(
        groups, workload(changes), cost_by_initiative=cost_rollup
    )
    payload["currency"] = costing_store.currency
    payload["scope"] = scope
    payload["products"] = PRODUCTS
    payload["product_parents"] = PRODUCT_PARENTS
    plan = plan_store.plan_for(current_week())
    if plan and scope:
        # The scoped view classifies only the family's slice of the plan - planned
        # items carry their product for exactly this.
        plan = {**plan, "planned": [
            item for item in plan.get("planned", [])
            if item.get("product") in family
        ]}
    payload["week_plan"] = plan_vs_actual(plan, changes) if plan else None
    return payload


@app.get("/systems")
def systems_page() -> FileResponse:
    return _app_asset("systems.html")


@app.get("/systems/data")
def systems_data() -> dict:
    """The systems view's dataset: one row per declared system, plus the restructure gap.

    Readable by anyone who can see the roadmap, and behind no CAPABILITY grant: this is
    the technology taxonomy and the products built on it, which is the same
    visible-to-everyone transparency the board itself has. No cost data passes through
    here, so there is no `view_cost` grant to respect. Authentication still applies in
    enterprise mode - the blanket middleware covers this route like any other.

    `declared: false` (no [systems] table) is a first-class answer, not an empty error -
    the page renders an explanation of what a system is and how to declare one, rather
    than an empty table implying something broke.
    """
    return {
        "declared": systems_declared(),
        "systems": roadmap_store.system_rollup(),
        "unattributed": roadmap_store.unattributed_report(),
        # So the re-home control can offer real destinations without a second request.
        "products": PRODUCTS,
        "product_systems": {p: list(s) for p, s in PRODUCT_SYSTEMS.items()},
    }


@app.get("/systems/{system_id}/changes")
def list_system_changes(system_id: str) -> list[dict]:
    """Every change attributed to one system, across all product boards.

    The retrieval half of a reclassification: after a drained product's changes are
    re-homed onto real products, "all the work done on X" stops being one board and
    becomes this cross-board slice - each record still carries the board it lives on
    (`product`) and where it came from (`origin_product`), so provenance survives the
    move. Same read-visibility as the boards themselves.
    """
    if system_id not in SYSTEMS:
        raise HTTPException(status_code=404, detail=f"Unknown system: {system_id}")
    return roadmap_store.list_by_system(system_id)


def _session_scope_report() -> list[dict]:
    """Sessions whose scope doesn't hold together, one entry each.

    Reported, never blocked - the same choice the work model makes about an unaligned
    project (see portfolio.py): nobody should be stopped mid-conversation because their
    initiative got closed underneath them, but nobody should have to discover it from a
    cost report either. Three ways it can drift:

    - `missing`: pinned to an initiative that no longer exists.
    - `closed`: pinned to one that has been closed while the session runs on.
    - `mismatch`: has BOTH a project and an initiative, and the project belongs to a
      different initiative. Attribution follows the project (it is the more specific
      statement, and the only one the additive rollup can use), so the initiative pin is
      the part that is lying.

    Computed here because it joins sessions against the portfolio, and only this module
    knows about both.
    """
    report: list[dict] = []
    for session in sessions.list_sessions():
        if session.archived or not session.initiative_id:
            continue
        entry = {
            "session_id": session.id,
            "label": session.title or session.name,
            "initiative_id": session.initiative_id,
        }
        initiative = portfolio_store.get_initiative(session.initiative_id)
        if initiative is None:
            report.append({**entry, "issue": "missing"})
            continue
        if initiative.status == "closed":
            report.append({**entry, "issue": "closed", "initiative": initiative.title})
        if session.project_id:
            project = portfolio_store.get_project(session.project_id)
            if project is not None and project.initiative_id != session.initiative_id:
                report.append({
                    **entry,
                    "issue": "mismatch",
                    "initiative": initiative.title,
                    "project": project.title,
                    "attributed_to": project.initiative_id,
                })
    return report


@app.get("/portfolio/data")
def portfolio_data() -> dict:
    """Goals, initiatives, projects, the catch-all id, and the unaligned report.

    Projects come joined to their epic ticket (`ticket`, same shape as a change's),
    and `trackers_configured` is what lets the page tell "no epic yet - pending
    upload" apart from "this deployment has no trackers at all", where the whole
    linking layer should stay invisible.

    `push_trackers` is the narrower question the page's push affordance keys on: which
    trackers can actually CREATE an epic. A deployment can have trackers configured and
    none of them pushable (a read-only sync token, or no [trackers.push] table), and
    those two states must not look alike - one offers a button, the other offers linking
    only. Same per-tracker shape as GET /trackers, so the two pages read it identically.
    """
    snapshot = portfolio_store.snapshot()
    snapshot["projects"] = [_with_ticket(p) for p in snapshot["projects"]]
    return {
        **snapshot,
        "unassigned_changes": roadmap_store.unassigned_items(),
        "session_scope": _session_scope_report(),
        "trackers_configured": tracker_store.is_configured,
        "push_trackers": [
            t for t in tracker_store.describe()["trackers"] if t.get("push")
        ],
        # Same shape as /roadmap/data's portfolio.activity - see _project_activity.
        "activity": _project_activity(),
    }


@app.post("/portfolio/bootstrap")
def portfolio_bootstrap(request: Request, payload: dict = Body(default={})) -> dict:
    """Declares the maintenance goal + always-open initiative + catch-all project.

    Goals and initiatives are never auto-created, but the catch-all project needs a
    parent - so this trio is declared once, explicitly, by whoever sets the instance
    up. Idempotent."""
    actor = _require(request, "manage_roadmap")
    result = portfolio_store.ensure_maintenance_scaffold(
        goal_title=(payload.get("goal_title") or "").strip() or DEFAULT_MAINTENANCE_GOAL,
        initiative_title=(payload.get("initiative_title") or "").strip()
        or DEFAULT_MAINTENANCE_INITIATIVE,
        project_title=(payload.get("project_title") or "").strip()
        or DEFAULT_CATCH_ALL_PROJECT,
    )
    if result.get("created"):
        _audit(actor, "portfolio.bootstrapped", result["project_id"])
    return result


@app.post("/portfolio/goals")
def create_goal(request: Request, payload: dict = Body(...)) -> dict:
    _require(request, "manage_roadmap")
    try:
        goal = portfolio_store.create_goal(
            title=payload.get("title") or "", description=payload.get("description") or ""
        )
    except PortfolioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return goal.to_dict()


@app.patch("/portfolio/goals/{goal_id}")
def update_goal(goal_id: str, request: Request, payload: dict = Body(default={})) -> dict:
    _require(request, "manage_roadmap")
    try:
        goal = portfolio_store.update_goal(
            goal_id,
            title=payload.get("title"),
            description=payload.get("description"),
            status=payload.get("status"),
        )
    except PortfolioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return goal.to_dict()


@app.delete("/portfolio/goals/{goal_id}")
def delete_goal(goal_id: str, request: Request) -> dict:
    _require(request, "manage_roadmap")
    try:
        portfolio_store.delete_goal(goal_id)
    except PortfolioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "deleted"}


@app.post("/portfolio/initiatives")
def create_initiative(request: Request, payload: dict = Body(...)) -> dict:
    _require(request, "manage_roadmap")
    try:
        initiative = portfolio_store.create_initiative(
            title=payload.get("title") or "",
            description=payload.get("description") or "",
            goal_ids=payload.get("goal_ids"),
        )
    except PortfolioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return initiative.to_public_dict()


@app.patch("/portfolio/initiatives/{initiative_id}")
def update_initiative(initiative_id: str, request: Request, payload: dict = Body(default={})) -> dict:
    _require(request, "manage_roadmap")
    try:
        initiative = portfolio_store.update_initiative(
            initiative_id,
            title=payload.get("title"),
            description=payload.get("description"),
            status=payload.get("status"),
            goal_ids=payload.get("goal_ids"),
        )
    except PortfolioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return initiative.to_public_dict()


@app.delete("/portfolio/initiatives/{initiative_id}")
def delete_initiative(initiative_id: str, request: Request) -> dict:
    _require(request, "manage_roadmap")
    # Checked here rather than in the store, for the same reason project validation lives
    # in this module: the portfolio has no business knowing sessions exist. Sessions are
    # named rather than silently unpinned, because a live session losing its scope
    # mid-conversation is exactly the kind of thing whoever clicked delete should decide
    # about knowingly.
    scoped = [
        s for s in sessions.list_sessions()
        if s.initiative_id == initiative_id and not s.archived
    ]
    if scoped:
        raise HTTPException(
            status_code=400,
            detail=(
                "Sessions are still scoped to this initiative: "
                + ", ".join(f'"{s.title or s.name}"' for s in scoped)
                + ". Re-scope or archive them first."
            ),
        )
    try:
        portfolio_store.delete_initiative(initiative_id)
    except PortfolioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "deleted"}


@app.post("/portfolio/projects")
def create_project(request: Request, payload: dict = Body(...)) -> dict:
    actor = _require(request, "manage_roadmap")
    try:
        project = portfolio_store.create_project(
            title=payload.get("title") or "",
            description=payload.get("description") or "",
            initiative_id=(payload.get("initiative_id") or "").strip() or None,
            # "ideation" declares the project as an idea being worked, expected to
            # have no changes yet. Defaulted here, not just in the store, so an
            # empty/absent field can never read as a status.
            status=(payload.get("status") or "").strip() or "open",
        )
    except PortfolioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Linking at creation is one call instead of create-then-PATCH, same as a change. A
    # failed link raises and leaves the project created but unlinked - i.e. in the
    # ordinary local/pending-upload state, which is exactly what an unlinked project
    # means, so there is nothing to roll back.
    if payload.get("ticket") or payload.get("ticket_key"):
        project = _apply_epic_link(actor, project.id, payload)
    return _with_ticket(project.to_public_dict())


@app.patch("/portfolio/projects/{project_id}")
def update_project(project_id: str, request: Request, payload: dict = Body(default={})) -> dict:
    """`initiative_id: ""` deliberately makes the project unaligned; omitting the key
    leaves its parent alone. `ticket` / `ticket_key` link the project to its epic
    (`""` unlinks, returning it to the local/pending-upload state), keyed on presence
    like every other clearing field here."""
    actor = _require(request, "manage_roadmap")
    raw_initiative = payload.get("initiative_id")
    try:
        project = portfolio_store.update_project(
            project_id,
            title=payload.get("title"),
            description=payload.get("description"),
            status=payload.get("status"),
            initiative_id=(raw_initiative or "").strip() or None,
            clear_initiative=raw_initiative is not None and not str(raw_initiative).strip(),
        )
    except PortfolioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if "ticket" in payload or "ticket_key" in payload:
        project = _apply_epic_link(actor, project_id, payload)
    return _with_ticket(project.to_public_dict())


@app.delete("/portfolio/projects/{project_id}")
def delete_project(project_id: str, request: Request) -> dict:
    _require(request, "manage_roadmap")
    try:
        # The roadmap store owns changes, so the count is passed in rather than having
        # portfolio.py reach across into it.
        portfolio_store.delete_project(
            project_id, change_count=roadmap_store.count_by_project(project_id)
        )
    except PortfolioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "deleted"}


@app.get("/roadmap")
def roadmap_page() -> FileResponse:
    return _app_asset("roadmap.html")


# ---- external trackers (Jira / Azure DevOps) ----
#
# Reads are open like every other read in this system (see authz.py's transparency note);
# triggering a sync is a `manage_roadmap` write because it costs an outbound API call
# against someone else's rate limit.


@app.get("/trackers")
def get_trackers() -> dict:
    """Configured trackers plus each one's last sync outcome.

    Never includes a token: the payload is assembled field by field in
    TrackerStore.describe() precisely so that adding a config field later cannot leak one
    by accident.

    `imports` is the last import pass per routing tracker: changes created, and the
    components that had importable tickets but no route - the "reported, never guessed"
    half of import routing. Empty for a deployment with no routes.
    """
    return _described_trackers()


@app.get("/trackers/tickets")
def search_tickets(q: str = "", tracker_id: str = "", limit: int = 50, type: str = "") -> dict:
    """Candidate tickets for the link picker, from the synced catalog only - this endpoint
    never calls out to a tracker, so typing in the picker cannot generate API traffic.

    Every ticket returned is FREE to link and worth linking: anything already backing a
    change or a project is left out, and so is anything resolved won't-do - a decision not
    to build is not a candidate. `hidden` counts both, by reason. Pasting a key still
    reaches either: this governs what is offered, not what a human may choose.

    `type` filters on the CANONICAL type slug (see trackers.CANONICAL_TYPES): the
    project picker asks for `type=epic` so it offers only the rung a project can link
    to, whatever the tracker's own name for that rung is.
    """
    # Which candidates are already taken. A set of tuples rather than a joined string:
    # how a catalog key is spelled is trackers.py's business, and duplicating that format
    # here is what invites the two to drift apart. BOTH stores' links count as taken - one
    # ticket backs one thing, change or project (see _apply_ticket_link/_apply_epic_link).
    #
    # Taken tickets are HIDDEN, not offered-and-refused. Every row in this list is meant to
    # be a choice, and on a mature board most of the catalog is already linked - so greying
    # them out spent the page on rows nobody could pick. Won't-do tickets are withheld for
    # the same reason, one rung further: not merely unavailable but not worth having. The
    # per-reason `hidden` counts are what stop the short list from reading as a broken search.
    taken = frozenset(
        (tid, normalize_key(tid, key))
        for tid, key in (
            roadmap_store.linked_ticket_refs() + portfolio_store.linked_ticket_refs()
        )
    )
    tickets, hidden = tracker_store.search(
        q, tracker_id, max(1, min(limit, 200)), canonical=type, exclude=taken
    )
    # `hidden` is per-reason (see TrackerStore.search): linked already backs something,
    # declined is resolved won't-do. Both are stated so a short list is explained rather
    # than suspicious, and they stay separate because the fixes differ - unlink it there,
    # versus it is not happening at all.
    return {"tickets": tickets, "hidden": hidden}


@app.get("/trackers/releases")
def get_tracker_releases(tracker_id: str = "") -> dict:
    """The synced release catalog (Jira project versions, ADO iterations), from the
    cache only - like the ticket search, this endpoint never calls out to a tracker.

    Data only for now: nothing on the board consumes releases yet, so this read is the
    feature's whole surface until something does. Open like every other read (see
    authz.py's transparency note); the sync that fills it stays a `manage_roadmap`
    write."""
    if tracker_id and CONFIG.tracker(tracker_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown tracker: {tracker_id}")
    return {"releases": tracker_store.list_releases(tracker_id)}


@app.post("/trackers/sync")
def sync_trackers(request: Request, payload: dict = Body(default={})) -> dict:
    """Kicks off a sync and returns immediately.

    Runs on a daemon thread, like merges and dev tasks (see sessions.py) - a Jira with
    thousands of issues would otherwise block the event loop for the whole pull. The board
    learns it finished from the roadmap websocket.
    """
    _require(request, "manage_roadmap")
    if not tracker_store.is_configured:
        raise HTTPException(
            status_code=400,
            detail="No trackers are configured. Add a [[trackers]] block to "
            "pm_studio_local/config.toml.",
        )
    if tracker_store.is_syncing:
        # Not an error: the caller wanted a fresh catalog and one is already being built.
        return {"status": "already_syncing", **_described_trackers()}
    tracker_id = (payload.get("tracker_id") or "").strip() or None
    if tracker_id and CONFIG.tracker(tracker_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown tracker: {tracker_id}")
    threading.Thread(target=_run_tracker_sync, args=(tracker_id,), daemon=True).start()
    return {"status": "syncing", **_described_trackers()}


def _described_trackers() -> dict:
    """tracker_store.describe() plus the last import passes - the one shape every
    consumer of TRACKERS gets, so the reports can't exist on one surface only."""
    return {
        **tracker_store.describe(),
        "imports": dict(_import_report),
        "epic_imports": dict(_epic_report),
    }


# One tracker pass at a time, process-wide. TrackerStore.is_syncing used to be the only
# guard and it covers the FETCH alone - it is already False while the import passes
# below are still creating changes, so a second pass starting in that window computed
# its "already linked" set from a half-finished board and imported the same tickets
# again. That is the in-process half of the duplicate-change bug; the cross-process half
# is workspace_lock's.
_sync_run_lock = threading.Lock()


def _run_tracker_sync(tracker_id: str | None) -> None:
    """One sync pass, off the event loop. Never raises - TrackerStore.sync records each
    tracker's failure in its own status entry, and this wrapper is the last backstop so a
    bug here cannot kill the thread silently.

    Skipped outright if a pass is already running: two passes over one board duplicate
    work at best and double-import at worst, and the caller asked for a fresh catalog,
    which the pass already in flight is busy producing.
    """
    if not _sync_run_lock.acquire(blocking=False):
        print("[trackers] sync already running - this request was skipped")
        return
    try:
        # Held across every pass below, not just the fetch, so the board's "Syncing..."
        # state and the POST /trackers/sync guard both stay true until the import,
        # assignment, removal and mirror passes have finished.
        with tracker_store.syncing():
            _tracker_sync_passes(tracker_id)
    finally:
        _sync_run_lock.release()
    _on_roadmap_update({"type": "trackers_synced", "trackers": _described_trackers()})


def _tracker_sync_passes(tracker_id: str | None) -> None:
    """The passes themselves, run under _run_tracker_sync's guard.

    Pass order matters. The people directory is folded first, so a change the import
    creates can name whoever the ticket is assigned to straight away. Epics become
    projects next, so that by the time the change import runs, an epic-level ticket is already project-held and cannot be imported as
    a change; the assignment pass runs after, so changes the import just created land in
    their epic's project in the same cycle instead of waiting for the next one. The
    won't-do removal runs LAST and changes-before-projects internally, so a declined
    epic's declined changes are gone by the time its project's emptiness is judged -
    the whole subtree can leave in one cycle. The status mirror runs after that removal
    rather than before it, so a change about to be deleted is not written to first. The
    epic mirror runs after the change mirror, so a project's closure is judged against
    changes that already followed their tickets this cycle - reversed, a project would
    close on last sync's reading of its own children. The stale-exclusion report runs
    after all of them, so it reads the board as this cycle left it and never names
    something another pass was about to remove anyway.
    """
    try:
        tracker_store.sync(tracker_id)
        # Linked tickets outside the configured projects are not in the catalog pull, so
        # fetch those individually - otherwise they would render as unresolved forever.
        # Projects' epic links ride along: their badges resolve from the same catalog.
        tracker_store.refresh_missing(
            roadmap_store.linked_ticket_refs() + portfolio_store.linked_ticket_refs()
        )
        # Before any import pass: a change the import is about to create should be able
        # to name its assignee the moment it lands, not one sync later.
        _reconcile_people()
        _sync_epic_projects()
        _import_routed_tickets()
        _assign_changes_to_epic_projects()
        _remove_wont_do_imports()
        _mirror_ticket_status()
        _mirror_epic_status()
        _report_stale_excluded_imports()
    except Exception as exc:  # noqa: BLE001
        print(f"[trackers] sync failed: {exc}")


def _reconcile_people() -> None:
    """The sync step that keeps the people directory current.

    Every distinct assignee in the catalog is folded into the directory: a name no identity
    claims yet becomes a person, a name a second tracker spells differently attaches to the
    person already there (see PeopleStore.reconcile for the matching order). Nobody is ever
    removed - an assignee who drops out of the sync window has left the query, not the
    history of the work they did.

    Counted and printed like the import passes, and for the same reason: a directory that
    silently gained forty entries because two trackers disagree about somebody's name is a
    thing you want to see in the log rather than discover in the load table.
    """
    seen: dict[tuple[str, str], tuple[str, str, str, str]] = {}
    for config in CONFIG.trackers:
        for ticket in tracker_store.tickets_of(config.id):
            key = ticket.assignee_key or ticket.assignee
            if not key:
                continue
            seen.setdefault(
                (config.id, key),
                (config.id, key, ticket.assignee, ticket.assignee_email),
            )
    if not seen:
        return
    report = people_store.reconcile(seen.values())
    if report["created"] or report["attached"]:
        print(
            f"[trackers] people -> {report['created']} new, "
            f"{report['attached']} identity(ies) attached to someone already known"
        )


# Per-tracker outcome of the last import pass, additive on GET /trackers: how many
# changes the last cycle created, and which components had importable tickets but no
# route. The unrouted side is the report half of "reported, never guessed" - no
# catch-all product is invented for a component nobody has routed. `stale_excluded`
# is the same posture for the other direction: what is still linked to a ticket the
# deployment has since excluded (see _report_stale_excluded_imports), named rather
# than deleted.
_import_report: dict[str, dict] = {}

# Per-tracker outcome of the last EPIC pass (see _sync_epic_projects): projects linked
# to existing epics vs created from new ones, changes filed under their epic's project
# (`changes_assigned`) or moved to follow a re-parenting upstream
# (`changes_reparented`), and everything that was skipped with the reason - held by a
# change, unrouted, done, ambiguous, or filed by hand somewhere the tracker may not
# override (`reparent_kept`). Same posture as _import_report: the skips are the report
# half of "reported, never guessed".
_epic_report: dict[str, dict] = {}


_TITLE_NORM = re.compile(r"[^0-9a-z]+")


def _normalize_title(title: str) -> str:
    """A title as humans repeat it: case, punctuation and spacing dropped, so
    'Vendor "Pay To" Changes' in the tracker and 'Vendor Pay To Changes' typed by hand
    are the same name. Matching only - never stored, never shown."""
    return _TITLE_NORM.sub(" ", (title or "").casefold()).strip()


def _excluded_keys(config, by_key: dict[str, Ticket]) -> set[str]:
    """Normalized keys of every ticket the deployment declared OUT of the automatic
    passes (see TrackerConfig.exclude_components): any ticket carrying an excluded
    component, plus everything below one in the parent chain - children routinely
    carry no components of their own, and exclusion is about who owns the tree.

    Run to a fixpoint rather than one hop, so a task under a story under an excluded
    epic is covered however deep the tracker nests.
    """
    if not config.exclude_components:
        return set()
    wanted = {c.casefold() for c in config.exclude_components}
    excluded = {
        key for key, ticket in by_key.items()
        if any(
            _component_hit(w, c.casefold(), config.provider)
            for c in ticket.components
            for w in wanted
        )
    }
    grew = True
    while grew:
        grew = False
        for key, ticket in by_key.items():
            if key in excluded or not ticket.parent_key:
                continue
            if normalize_key(config.id, ticket.parent_key) in excluded:
                excluded.add(key)
                grew = True
    return excluded


def _epic_ancestor_key(
    config, ticket: Ticket, by_key: dict[str, Ticket]
) -> str | None:
    """The normalized key of the epic ABOVE a ticket, however many rungs up - the
    hierarchy fact both epic passes turn on.

    One hop is not enough: ADO nests User Story under Feature under Epic, so a story's
    parent is a Feature and the epic is its grandparent. The walk climbs the catalog
    until it reaches epic level, cycle-guarded because parent links are tracker data,
    not something this code may assume well-formed. For a parent OUTSIDE the synced
    catalog the child's own parent_type is all we know: epic-level means that key is
    the answer, anything else means the chain cannot be followed and there is no epic
    to name - None, the honest answer.
    """
    seen: set[str] = set()
    current = ticket
    while current.parent_key:
        parent_key = normalize_key(config.id, current.parent_key)
        if parent_key in seen:
            return None
        seen.add(parent_key)
        parent = by_key.get(parent_key)
        if parent is None:
            return parent_key if canonical_type(current.parent_type) == TYPE_EPIC else None
        if parent.type == TYPE_EPIC:
            return parent_key
        current = parent
    return None


def _sync_epic_projects() -> None:
    """Epics become projects - the same posture as _import_routed_tickets one rung up,
    plus the one-time cleanup a deployment that predates the link needs.

    Dedupe comes FIRST, because most deployments already created their projects by hand
    while mirroring the tracker's epics - importing blindly would twin every one of
    them. So, per routing tracker, three idempotent steps:

    1. LINK by evidence: an unlinked project whose linked changes' tickets all parent
       to ONE epic already is that epic locally - link the two rather than creating a
       twin. This is the hierarchy argument run in reverse: a change's parent epic
       should be its project, so a project's changes point back at its epic.
    2. LINK by title: an unlinked project titled exactly like exactly one epic (and no
       other unlinked project competing for that title) is linked too - the fallback
       for projects whose changes aren't ticket-linked yet.
    3. CREATE the rest: a routed, still-open epic nobody holds becomes a new linked
       project. It lands unaligned - which initiative an epic serves is a human call,
       and the unaligned report is the existing place that call is requested.

    Before any of that, the pass RECLAIMS its own mess: a deployment whose
    import_types included the epic rung had every epic imported as a CHANGE before
    projects could link, and each of those changes now blocks its epic from backing a
    project. One is deleted only when it is provably still the import artifact and
    nothing more - the description is exactly the stamp the import wrote, no project,
    no owner - so the only thing removed is a duplicate of the epic this pass is about
    to represent properly. A change anyone touched is somebody's work and stays put.

    Anything else is counted, not guessed: an epic held by a change someone touched
    (unlink it there to let it become a project), an unrouted one, a won't-do one
    (declined, not a plan), a done-category one (history, not a plan), and any project
    whose evidence points at several epics.
    """
    for config in CONFIG.trackers:
        if not config.imports:
            continue
        report = {
            "projects_linked": 0,
            "projects_created": 0,
            "changes_assigned": 0,
            "changes_reparented": 0,
            "reparent_kept": 0,
            "reclaimed_from_changes": 0,
            "twins_merged": 0,
            "held_by_changes": 0,
            "unrouted_epics": 0,
            "skipped_done": 0,
            "skipped_wont_do": 0,
            "contested_epics": 0,
            "ambiguous": [],
        }
        _epic_report[config.id] = report
        tickets = tracker_store.tickets_of(config.id)
        by_key = {normalize_key(config.id, t.key): t for t in tickets}
        # Excluded epics are invisible to this whole pass: never created, never
        # evidence- or title-linked, never reclaimed. The slice belongs elsewhere.
        excluded = _excluded_keys(config, by_key)
        epics = {
            k: t for k, t in by_key.items() if t.type == TYPE_EPIC and k not in excluded
        }
        report["excluded_epics"] = sum(
            1 for k, t in by_key.items() if t.type == TYPE_EPIC and k in excluded
        )

        # Reclaim first, so a freed epic flows through the very steps below in the
        # same pass instead of blocking its project for one more sync (see docstring).
        for epic_key in epics:
            holder = roadmap_store.item_for_ticket(config.id, epic_key)
            if holder is None:
                continue
            pristine = (
                holder.description.startswith("Imported from ")
                and holder.project_id is None
                and holder.owner is None
            )
            if not pristine:
                continue
            roadmap_store.delete(holder.id)
            report["reclaimed_from_changes"] += 1
            _audit(None, "roadmap.epic_change_reclaimed", holder.id, epic_key)

        # Merge the twins this pass itself made while title matching was still
        # exact-only: an import-created project claiming an epic while a hand-made
        # unlinked project wears the same normalized title is one project written
        # twice. The HAND-MADE one survives - it is the one carrying human context
        # (its initiative, its wording) - and the twin must still be provably
        # untouched plumbing: the import stamp for a description and no initiative
        # anyone filed it under. Its changes (auto-assigned here, one step down)
        # move over before it goes, so nothing is orphaned.
        unlinked_by_title: dict[str, list[dict]] = {}
        for project in portfolio_store.list_projects():
            if (
                project["tracker_id"] is None
                and not project["is_catch_all"]
                and not project["catch_all_for_initiative"]
            ):
                unlinked_by_title.setdefault(
                    _normalize_title(project["title"]), []
                ).append(project)
        for twin in portfolio_store.list_projects():
            if (
                twin["tracker_id"] != config.id
                or not (twin["description"] or "").startswith("Imported from ")
                or twin["initiative_id"] is not None
            ):
                continue
            candidates = unlinked_by_title.get(_normalize_title(twin["title"]), [])
            if len(candidates) != 1:
                continue
            survivor = candidates[0]
            unlinked_by_title[_normalize_title(twin["title"])] = []
            for change in roadmap_store.list_by_project(twin["id"]):
                roadmap_store.update(change["id"], project_id=survivor["id"])
            epic_key = twin["ticket_key"]
            portfolio_store.unlink_epic(twin["id"])
            portfolio_store.delete_project(twin["id"])
            portfolio_store.link_epic(survivor["id"], config.id, epic_key)
            report["twins_merged"] += 1
            _audit(
                None, "portfolio.twin_project_merged", survivor["id"],
                f"{epic_key} (absorbed {twin['id']})",
            )

        def free(epic_key: str) -> bool:
            """Nobody holds this epic yet, in either store."""
            return (
                portfolio_store.project_for_ticket(config.id, epic_key) is None
                and roadmap_store.item_for_ticket(config.id, epic_key) is None
            )

        # Which epic(s) each project's changes point back at. Keys the epic may carry
        # even when it is outside the synced catalog - the parent TYPE travels on the
        # child ticket, so linking to an unsynced epic is legitimate and resolves on
        # the next refresh_missing.
        evidence: dict[str, set[str]] = {}
        for items in roadmap_store.list_all().values():
            for change in items:
                if change.get("tracker_id") != config.id or not change.get("ticket_key"):
                    continue
                ticket = by_key.get(normalize_key(config.id, change["ticket_key"]))
                if ticket is None:
                    continue
                parent = _epic_ancestor_key(config, ticket, by_key)
                if parent and parent not in excluded and change.get("project_id"):
                    evidence.setdefault(change["project_id"], set()).add(parent)

        # Epics tangled in an ambiguity are withheld from the creation step below: one
        # of them very likely IS the project we just declined to link, and importing it
        # anyway would create exactly the twin this pass exists to avoid. A human
        # resolves the link; whatever is genuinely new imports on the next sync.
        contested: set[str] = set()

        # Step 1: evidence. Catch-alls are exempt exactly as they are from linking.
        still_unlinked = []
        for project in portfolio_store.list_projects():
            if (
                project["tracker_id"] is not None
                or project["is_catch_all"]
                or project["catch_all_for_initiative"]
            ):
                continue
            parents = evidence.get(project["id"], set())
            if len(parents) > 1:
                # Its changes span several epics: not this pass's call to make.
                report["ambiguous"].append(project["title"])
                contested.update(parents)
                continue
            if len(parents) == 1:
                epic_key = next(iter(parents))
                if free(epic_key):
                    portfolio_store.link_epic(project["id"], config.id, epic_key)
                    report["projects_linked"] += 1
                    continue
            still_unlinked.append(project)

        # Step 2: NORMALIZED title match (see _normalize_title - a hand-typed project
        # title and the tracker's epic routinely disagree only in quotes or casing),
        # unique on BOTH sides: two projects sharing a normalized title, or two epics
        # sharing one, is ambiguity, not a coin toss.
        epic_titles: dict[str, list[str]] = {}
        for key, epic in epics.items():
            title = _normalize_title(epic.title)
            if title:
                epic_titles.setdefault(title, []).append(key)
        project_titles: dict[str, list[dict]] = {}
        for project in still_unlinked:
            project_titles.setdefault(_normalize_title(project["title"]), []).append(project)
        for title, candidates in project_titles.items():
            keys = epic_titles.get(title) or []
            if not keys:
                continue
            if len(candidates) > 1 or len(keys) > 1:
                report["ambiguous"].extend(p["title"] for p in candidates)
                contested.update(keys)
                continue
            if free(keys[0]):
                portfolio_store.link_epic(candidates[0]["id"], config.id, keys[0])
                report["projects_linked"] += 1

        # Step 3: what remains genuinely new. Routed like the change import, so an
        # area nobody has declared yet stays out; done-category epics are history and
        # would import as instant clutter, so they are counted instead.
        maps = _route_maps(config)
        for epic_key, epic in epics.items():
            if portfolio_store.project_for_ticket(config.id, epic_key) is not None:
                continue
            if epic_key in contested:
                report["contested_epics"] += 1
                continue
            if roadmap_store.item_for_ticket(config.id, epic_key) is not None:
                report["held_by_changes"] += 1
                continue
            if _match_route(maps, epic) is None:
                report["unrouted_epics"] += 1
                continue
            # Checked before the done-category skip, which would otherwise swallow it:
            # won't-do sits in the Done category, but "declined" and "history" are
            # different answers to "where did my epic go".
            if _is_wont_do(epic):
                report["skipped_wont_do"] += 1
                continue
            if _imported_status(epic) == "done":
                report["skipped_done"] += 1
                continue
            project = portfolio_store.create_project(
                title=epic.title or epic.key,
                description=f"Imported from {config.label} {epic.key}.",
            )
            try:
                portfolio_store.link_epic(project.id, config.id, epic_key)
            except EpicAlreadyLinked:
                # Lost the race to a concurrent manual link - theirs wins; remove the
                # project this pass just created so the epic keeps exactly one.
                portfolio_store.delete_project(project.id)
                continue
            report["projects_created"] += 1
            _audit(None, "portfolio.project_imported", project.id, epic_key)
        if report["projects_created"] or report["projects_linked"]:
            print(
                f"[trackers] {config.id}: epics -> {report['projects_created']} project(s) "
                f"created, {report['projects_linked']} linked"
            )


def _assign_changes_to_epic_projects() -> None:
    """A change whose ticket parents to an epic belongs to that epic's project - filled
    in where it is MISSING, and corrected where the tracker's hierarchy has since moved.
    Runs after the change import so a change and its project meet in the same sync cycle.

    Re-parenting a story in the tracker is a deliberate act, and a more recent one than
    the import that filed the change, so the tracker wins - but only over slots the
    tracker owns. Three cases, by what the change sits in now:

    - NOTHING: filled, the original behaviour. (A dangling id whose project has since
      been deleted counts as nothing - that is rot, not somebody's filing.)
    - A project linked to an epic in THIS tracker: the tracker put the change there, so
      the tracker may move it. This is what makes re-parenting upstream replicate onto
      the board instead of stranding the change under its old epic forever.
    - A project with no epic link, or one linked to a DIFFERENT tracker: a human filed
      it there by hand, against the hierarchy. That outranks the tracker and is counted
      (`reparent_kept`) rather than overridden - the board must not fight its own users.
      Catch-alls land here too, since an epic link is refused on them by construction.

    Nothing here ever CLEARS an assignment. `_epic_ancestor_key` answers None for a
    parent outside the synced catalog just as readily as for a genuinely parentless
    ticket, and excluded epics are invisible to these passes by design - so "no epic
    visible" is UNKNOWN, not "no epic", the same posture _remove_wont_do_imports takes
    on a ticket missing from the catalog. Unassigning on that would drop a story
    parented to another team's epic out of its project on the next sync.
    """
    for config in CONFIG.trackers:
        if not config.imports:
            continue
        report = _epic_report.get(config.id)
        by_key = {
            normalize_key(config.id, t.key): t for t in tracker_store.tickets_of(config.id)
        }
        excluded = _excluded_keys(config, by_key)
        for items in roadmap_store.list_all().values():
            for change in items:
                if (
                    change.get("tracker_id") != config.id
                    or not change.get("ticket_key")
                ):
                    continue
                ticket = by_key.get(normalize_key(config.id, change["ticket_key"]))
                if ticket is None:
                    continue
                parent = _epic_ancestor_key(config, ticket, by_key)
                if parent is None or parent in excluded:
                    continue
                project = portfolio_store.project_for_ticket(config.id, parent)
                if project is None:
                    continue
                current_id = change.get("project_id")
                if current_id == project.id:
                    continue
                current = (
                    portfolio_store.get_project(current_id) if current_id else None
                )
                if current is None:
                    roadmap_store.update(change["id"], project_id=project.id)
                    if report is not None:
                        report["changes_assigned"] += 1
                    continue
                if current.tracker_id != config.id:
                    if report is not None:
                        report["reparent_kept"] += 1
                    continue
                roadmap_store.update(change["id"], project_id=project.id)
                if report is not None:
                    report["changes_reparented"] += 1
                _audit(
                    None, "roadmap.change_reparented", change["id"],
                    f"{change['ticket_key']}: {current.ticket_key} -> {parent}",
                )
        if report is not None and report["changes_reparented"]:
            print(
                f"[trackers] {config.id}: {report['changes_reparented']} change(s) "
                f"moved to follow their ticket's epic"
            )


def _remove_wont_do_imports() -> None:
    """A ticket declined AFTER it was imported must leave the board the way it would
    never have arrived: the won't-do checks in the import passes only stop NEW imports,
    so this pass re-reads every linked change and project each sync and removes the
    ones whose ticket has since been resolved won't-do.

    Only provably untouched imports are removed - the same posture as the epic pass's
    reclaim. For a change: the import stamp still opens the description, nobody owns
    it, and it still sits in the "later" bucket the import filed it in (project_id
    does NOT count as a touch - the assignment pass sets it automatically). For a
    project: the import stamp, no initiative anyone filed it under, and no changes
    left beneath it. Anything a human touched is somebody's work: it stays, and is
    counted in the report instead so the decline is surfaced rather than guessed at.

    A linked ticket absent from the catalog is UNKNOWN, not declined - it may simply
    have aged out of the sync window - so it is never removed."""
    for config in CONFIG.trackers:
        if not config.imports:
            continue
        by_key = {
            normalize_key(config.id, t.key): t for t in tracker_store.tickets_of(config.id)
        }

        removed = kept = 0
        for items in roadmap_store.list_all().values():
            for change in items:
                if change.get("tracker_id") != config.id or not change.get("ticket_key"):
                    continue
                ticket = by_key.get(normalize_key(config.id, change["ticket_key"]))
                if ticket is None or not _is_wont_do(ticket):
                    continue
                pristine = (
                    change["description"].startswith("Imported from ")
                    and not change.get("owner")
                    and change.get("bucket") == "later"
                )
                if not pristine:
                    kept += 1
                    continue
                roadmap_store.delete(change["id"])
                removed += 1
                _audit(None, "roadmap.wont_do_removed", change["id"], change["ticket_key"])

        projects_removed = projects_kept = 0
        # Recounted AFTER the change removals above, so a declined epic whose declined
        # changes just left counts as empty in the same cycle.
        change_counts: dict[str, int] = {}
        for items in roadmap_store.list_all().values():
            for change in items:
                if change.get("project_id"):
                    change_counts[change["project_id"]] = (
                        change_counts.get(change["project_id"], 0) + 1
                    )
        for project in portfolio_store.list_projects():
            if project["tracker_id"] != config.id or not project["ticket_key"]:
                continue
            ticket = by_key.get(normalize_key(config.id, project["ticket_key"]))
            if ticket is None or not _is_wont_do(ticket):
                continue
            pristine = (
                project["description"].startswith("Imported from ")
                and project["initiative_id"] is None
                and not project["is_catch_all"]
                and not project["catch_all_for_initiative"]
                and change_counts.get(project["id"], 0) == 0
            )
            if not pristine:
                projects_kept += 1
                continue
            portfolio_store.delete_project(project["id"])
            projects_removed += 1
            _audit(
                None, "portfolio.wont_do_project_removed", project["id"], project["ticket_key"]
            )

        # Stashed onto the reports the import passes just rebuilt this same cycle, so
        # GET /trackers tells the whole won't-do story in one place.
        report = _import_report.get(config.id)
        if report is not None:
            report["wont_do_removed"] = removed
            report["wont_do_kept"] = kept
        epic_report = _epic_report.get(config.id)
        if epic_report is not None:
            epic_report["wont_do_projects_removed"] = projects_removed
            epic_report["wont_do_projects_kept"] = projects_kept
        if removed or projects_removed:
            print(
                f"[trackers] {config.id}: won't-do -> removed {removed} change(s), "
                f"{projects_removed} project(s)"
            )


def _mirror_ticket_status() -> None:
    """A linked change's `status` mirrors its ticket's state, every sync.

    The one thing the board does NOT get to disagree with the tracker about. `bucket` is
    the plan and stays untouched - now/next/later is this deployment's call, and nothing
    here ever moves a card between columns. But status is a claim about EXECUTION, and
    for work that has a ticket the tracker is the system that actually knows: a change
    reading "in progress" against a closed ticket is not a plan, it is a stale snapshot.
    Before this pass existed, `_imported_status` ran once at import and never again, so
    every linked change froze at whatever state its ticket had on the day it landed.

    Both directions, deliberately. A ticket reopened after it was imported drags its
    change back out of done, because "done" here was only ever a reading of the ticket -
    `roadmap_store.create` stamps `shipped_at` from the clock at IMPORT time, so there
    is no real ship date to protect.

    Three kinds of ticket are left alone, each for a reason the other passes share:

    - ABSENT from the catalog: unknown, not "not done" (it may simply have aged out of
      `since`). Mirroring on absence would reopen the whole board the first time a sync
      came back short - the same posture _remove_wont_do_imports takes.
    - WON'T-DO: declined is not shipped. Its Done category maps to "done", so mirroring
      would quietly relabel abandoned work as delivered - exactly what the import
      refuses to do. The removal pass above has already deleted the untouched ones and
      counted the rest (`wont_do_kept`); the survivors keep the status a human gave them.
    - EXCLUDED: this slice is tracked authoritatively elsewhere, and exclusion means
      invisible to every automatic pass, status included.

    Unlike every other pass here this one does NOT skip a tracker that imports nothing.
    `imports` gates whether a tracker MANUFACTURES changes; this pass only keeps changes
    that already exist honest, and a ticket somebody linked by hand from a catalog-only
    tracker (no `import_types`, no routes - the shape the ADO instance runs in) has
    exactly the same claim to a current status as one the import created. Gating on
    `imports` here stranded every hand-linked ADO change at its import-day state.
    """
    for config in CONFIG.trackers:
        by_key = {
            normalize_key(config.id, t.key): t for t in tracker_store.tickets_of(config.id)
        }
        excluded = _excluded_keys(config, by_key)
        refreshed = 0
        for items in roadmap_store.list_all().values():
            for change in items:
                if change.get("tracker_id") != config.id or not change.get("ticket_key"):
                    continue
                key = normalize_key(config.id, change["ticket_key"])
                ticket = by_key.get(key)
                if ticket is None or key in excluded or _is_wont_do(ticket):
                    continue
                status = _imported_status(ticket)
                if status == change.get("status"):
                    continue
                roadmap_store.update(change["id"], status=status)
                refreshed += 1
                _audit(
                    None, "roadmap.status_synced", change["id"],
                    f"{change['ticket_key']}: {change.get('status')} -> {status} "
                    f"({ticket.state})",
                )
        # Stashed onto the report the import pass rebuilt earlier this cycle, the same
        # way the won't-do counts are. `setdefault` because a catalog-only tracker has
        # no import report of its own and its count would otherwise vanish.
        _import_report.setdefault(config.id, {})["status_refreshed"] = refreshed
        if refreshed:
            print(f"[trackers] {config.id}: {refreshed} change status(es) followed the tracker")


def _outstanding_changes(project_id: str) -> list[dict]:
    """The changes under a project that are genuinely still to do.

    Not simply `status != "done"`. A change linked to a ticket the tracker resolved
    won't-do is SETTLED, not outstanding: `_mirror_ticket_status` refuses to mark those
    done (declined is not delivered) and `_remove_wont_do_imports` only deletes the ones
    nobody touched - so a declined change a human had already filed into now/next stays
    `pending` for good. Counting it as open work pinned its project on the board
    permanently, unable to ever become history however many times the epic closed.

    Named as a function rather than inlined at its one caller because the same rule is
    spelled twice more and all three have to agree: `_is_outstanding` below, which the
    candidate report applies to its own grouping, and `isOutstanding` on the board, which
    reads `is_wont_do` off the ticket the wire now carries.
    """
    return [c for c in roadmap_store.list_by_project(project_id) if _is_outstanding(c)]


def _is_outstanding(change: dict) -> bool:
    """The per-change half of the rule above, so a caller holding its own grouping of
    changes does not have to walk every board once per project to reuse it."""
    if change["status"] == "done":
        return False
    ticket = tracker_store.lookup(change.get("tracker_id"), change.get("ticket_key"))
    return ticket is None or not ticket.is_wont_do


def _close_candidates(boards: dict | None = None) -> dict:
    """project_id -> why this project looks finished, for the board to offer a close.

    The counterpart to `_mirror_epic_status` for everything that pass will NOT touch on
    its own. Two different facts, kept apart because acting on them is not equally safe:

    `reason: "tracker"` - the epic itself is done. This is the one the close pass acts on
    when `close_done_epics` is set; reported here too so a deployment running the pass in
    its default report-only mode still sees the row it would have closed.

    `reason: "changes"` - every change under the project is settled and the epic is NOT
    done. Never closed automatically, because an epic still open is a live claim by the
    system that actually runs the work: mostly ADO epics parked in Active, and Jira epics
    nobody transitioned. The board offers the close and a human takes it.

    `blocked` is the reason neither is offered, and it is the one that matters most:
    every LOCAL change may be done while the epic still has open children in the catalog
    that were never imported - a partially routed epic reads as finished from here and is
    not. Naming the count is what stops the board from claiming otherwise.

    Recomputed per board load like `_project_activity`, and its indexes are built ONCE
    rather than asked of the stores per project: `list_by_project` walks every board to
    answer for one project, which made a board load quadratic in projects x changes.
    `boards` is the caller's own already-serialized changes where it has them - the board
    endpoint does, and serializing 3500 changes a second time was most of what this cost.
    """
    changes_by_project: dict[str, list[dict]] = {}
    for items in (boards if boards is not None else roadmap_store.list_all()).values():
        for change in items:
            if change.get("project_id"):
                changes_by_project.setdefault(change["project_id"], []).append(change)

    kids_of: dict[tuple[str, str], list] = {}
    for config in CONFIG.trackers:
        for ticket in tracker_store.tickets_of(config.id):
            if ticket.parent_key:
                kids_of.setdefault(
                    (config.id, normalize_key(config.id, ticket.parent_key)), []
                ).append(ticket)

    candidates: dict[str, dict] = {}
    for project in portfolio_store.list_projects():
        if project["status"] == "closed":
            continue
        if project["is_catch_all"] or project["catch_all_for_initiative"]:
            continue
        changes = changes_by_project.get(project["id"], [])
        # No changes is no evidence, not completion: 89 of this deployment's projects sit
        # there, half of them never linked to a tracker at all. Offering to close an
        # empty project would fire on every idea somebody had filed and not started.
        if not changes:
            continue
        if any(_is_outstanding(c) for c in changes):
            continue
        ticket = tracker_store.lookup(project["tracker_id"], project["ticket_key"])
        if ticket is None:
            # Unlinked, or linked to something outside the sync window. Nothing else can
            # speak for it, so its own changes are the whole story.
            candidates[project["id"]] = {"reason": "changes", "shipped": len(changes)}
            continue
        if _is_wont_do(ticket):
            continue
        epic_open = [
            k for k in kids_of.get(
                (project["tracker_id"], normalize_key(project["tracker_id"], ticket.key)), []
            )
            if _imported_status(k) != "done" and not k.is_wont_do
        ]
        if epic_open:
            candidates[project["id"]] = {
                "reason": "blocked",
                "shipped": len(changes),
                "tracker_open": len(epic_open),
                # Capped: the chip shows the count, and `keys` is for the tooltip. The
                # count beside it is the whole number, so a consumer can see the list is
                # short rather than mistaking five for all of them.
                "keys": [k.key for k in epic_open[:5]],
            }
            continue
        candidates[project["id"]] = {
            "reason": "tracker" if _imported_status(ticket) == "done" else "changes",
            "shipped": len(changes),
            "epic_state": ticket.state,
        }
    return candidates


def _mirror_epic_status() -> None:
    """A project whose epic the tracker calls done gets closed. Nothing here reopens one.

    The project-level counterpart to `_mirror_ticket_status`, and deliberately NOT its
    mirror image. A change's status was only ever a reading of its ticket, so that pass
    follows the tracker in both directions. A project's `closed` is a human act carrying
    a `closed_at`: somebody decided the bet was over, sometimes ahead of an epic nobody
    got round to transitioning. Reopening that on a tracker read would fight the PM every
    sync and lose the date, so the disagreement is REPORTED (`epic_open_on_closed`) and
    the local decision stands - the same posture `_report_stale_excluded_imports` takes
    on a mismatch it refuses to resolve by itself.

    Closing on a done epic can leave open changes beneath it, and does so out loud: a
    closed project still holding outstanding work keeps its place on the board wearing
    its "closed" chip, which is precisely what the fold rule was built to surface (see
    isHistoryRow). `closed_with_open_changes` names it in the cycle it happens rather
    than leaving it to be noticed later.

    Four kinds of project are left alone, each for a reason a sibling pass shares:

    - ABSENT from the catalog: unknown, not done - it may have aged out of `since`.
      Mirroring on absence would close the board the first time a sync came back short.
    - WON'T-DO epic: declined is not delivered. `_remove_wont_do_imports` owns those,
      and the ones it kept are somebody's work - they keep the status a human gave them.
    - EXCLUDED: this slice is tracked authoritatively elsewhere, status included.
    - CATCH-ALL: unplanned work never stops arriving, so `update_project` refuses to
      close it. Skipped here so that refusal is not raised as an error every sync.

    Gated per tracker by `close_done_epics`, default OFF: off, the pass counts what it
    WOULD close (`projects_closable`) and writes nothing, so a deployment sees the list
    before a tracker transition is given the power to close local planning work. Not
    gated on `imports` - like the status mirror this only keeps EXISTING records honest
    rather than manufacturing new ones, and a project hand-linked from a catalog-only
    tracker has the same claim to a current status as an imported one.
    """
    for config in CONFIG.trackers:
        by_key = {
            normalize_key(config.id, t.key): t for t in tracker_store.tickets_of(config.id)
        }
        excluded = _excluded_keys(config, by_key)
        closed = closable = stranded = disagreed = 0
        for project in portfolio_store.list_projects():
            if project["tracker_id"] != config.id or not project["ticket_key"]:
                continue
            key = normalize_key(config.id, project["ticket_key"])
            ticket = by_key.get(key)
            if ticket is None or key in excluded or _is_wont_do(ticket):
                continue
            if _imported_status(ticket) != "done":
                # Close-only. An epic that moved back out of done leaves a project
                # somebody closed exactly as it is, and is counted so the board can say
                # the two disagree instead of silently picking a side.
                if project["status"] == "closed":
                    disagreed += 1
                continue
            if project["status"] == "closed":
                continue
            if project["is_catch_all"] or project["catch_all_for_initiative"]:
                continue
            open_changes = len(_outstanding_changes(project["id"]))
            if not config.close_done_epics:
                closable += 1
                continue
            portfolio_store.update_project(project["id"], status="closed")
            closed += 1
            if open_changes:
                stranded += 1
            _audit(
                None, "portfolio.closed_by_tracker", project["id"],
                f"{project['ticket_key']} ({ticket.state})"
                + (f", {open_changes} change(s) left open" if open_changes else ""),
            )
        # setdefault, not [config.id]: _sync_epic_projects skips a tracker that imports
        # nothing and never builds it a report, and a catalog-only tracker's closes must
        # still be reportable.
        report = _epic_report.setdefault(config.id, {})
        report["projects_closed"] = closed
        report["projects_closable"] = closable
        report["closed_with_open_changes"] = stranded
        report["epic_open_on_closed"] = disagreed
        if closed or stranded:
            print(
                f"[trackers] {config.id}: closed {closed} project(s) on a done epic"
                + (f", {stranded} still holding open changes" if stranded else "")
            )
        elif closable:
            print(
                f"[trackers] {config.id}: {closable} project(s) closable on a done epic "
                "(close_done_epics is off - nothing written)"
            )


def _report_stale_excluded_imports() -> None:
    """Names every change and project still linked to a ticket the deployment has since
    declared excluded - reported, never removed.

    The exclusion in `_excluded_keys` is a gate on the automatic passes: nothing is
    created, linked or assigned for an excluded ticket. It is not retroactive, and it
    deliberately is not. A component is one dropdown in the tracker, and by the time
    someone sets it the local side may hold a project a human filed under an initiative,
    renamed, and planned around - deleting that on a tracker edit would let a one-field
    change in Jira erase local planning work. So the mismatch is surfaced and the call
    stays with the PM. Contrast `_remove_wont_do_imports`, which does remove: a won't-do
    resolution is a decision not to build the thing at all, and an untouched import of it
    has no defensible reading.

    Both halves land on the import report rather than splitting change-side from
    project-side, because "this slice lives in another tracker now" is ONE fact about
    one component and reads as one line on the board.

    A linked ticket absent from the catalog is not reported: `_excluded_keys` only knows
    the tickets this sync pulled, so an aged-out link is simply unknown, the same posture
    the won't-do pass takes."""
    for config in CONFIG.trackers:
        if not config.imports:
            continue
        report = _import_report.get(config.id)
        if report is None:
            continue
        report["stale_excluded"] = stale = {"changes": [], "projects": [], "total": 0}
        by_key = {
            normalize_key(config.id, t.key): t for t in tracker_store.tickets_of(config.id)
        }
        excluded = _excluded_keys(config, by_key)
        if not excluded:
            continue

        for items in roadmap_store.list_all().values():
            for change in items:
                if change.get("tracker_id") != config.id or not change.get("ticket_key"):
                    continue
                if normalize_key(config.id, change["ticket_key"]) not in excluded:
                    continue
                stale["changes"].append(
                    {
                        "id": change["id"],
                        "ticket_key": change["ticket_key"],
                        "title": change["title"],
                    }
                )

        for project in portfolio_store.list_projects():
            if project["tracker_id"] != config.id or not project["ticket_key"]:
                continue
            if normalize_key(config.id, project["ticket_key"]) not in excluded:
                continue
            stale["projects"].append(
                {
                    "id": project["id"],
                    "ticket_key": project["ticket_key"],
                    "title": project["title"],
                }
            )

        stale["total"] = len(stale["changes"]) + len(stale["projects"])
        if stale["total"]:
            keys = ", ".join(
                item["ticket_key"] for item in stale["changes"] + stale["projects"]
            )
            print(
                f"[trackers] {config.id}: {len(stale['changes'])} change(s) and "
                f"{len(stale['projects'])} project(s) are still linked to excluded "
                f"tickets - not removed: {keys}"
            )


def _is_wont_do(ticket) -> bool:
    """Whether the tracker resolved this ticket as not-happening - see Ticket.is_wont_do,
    which owns the spelling. Kept as a named helper because it reads as a rule at each of
    its call sites, but it must not be a SECOND definition: the import passes refuse to
    import a declined ticket and the link picker refuses to offer one, and the day those
    two disagree is the day declined work reappears through whichever door still allows
    it."""
    return ticket.is_wont_do


def _imported_status(ticket) -> str:
    """The coarse state a fresh import starts in, keyed on the ticket's state category
    (stable across workflow-specific names) rather than its raw state."""
    category = (ticket.state_category or ticket.state or "").casefold()
    if any(word in category for word in ("done", "closed", "resolved", "completed")):
        return "done"
    if any(word in category for word in ("progress", "active", "doing")):
        return "in_progress"
    return "pending"


def _route_maps(config) -> tuple[str, dict, dict, dict]:
    """The lookup tables one tracker's routes compile to, built once per pass.
    Shared by the change import and the epic import so "routed" means one thing."""
    by_proj_comp = {
        (r.project.casefold(), r.component.casefold()): r
        for r in config.routes if r.project and r.component
    }
    by_component = {
        r.component.casefold(): r for r in config.routes if r.component and not r.project
    }
    by_project = {
        r.project.casefold(): r for r in config.routes if r.project and not r.component
    }
    return config.provider, by_proj_comp, by_component, by_project


def _component_hit(route_comp: str, ticket_comp: str, provider: str) -> bool:
    """Whether a route's component claims a ticket's component (both casefolded).

    Jira components are flat labels, so equality is the only sane match. An ADO area
    path is a TREE node, so a route on a node claims its whole subtree - matched on
    the path separator, so a route on "portal" never accidentally claims
    "portal-legacy"."""
    if ticket_comp == route_comp:
        return True
    return provider == "ado" and ticket_comp.startswith(route_comp + "\\")


def _match_route(maps: tuple[str, dict, dict, dict], ticket):
    """Most-specific match wins, in two dimensions. Across tiers, the order it always
    was: component within a project, component anywhere, whole project - a project-wide
    default must not swallow the exceptions declared beside it. WITHIN a tier, the
    longest matching area path wins, so on ADO a route for `Portal\\Authorizations`
    beats one for `Portal` on the tickets they both claim, and the shallow route keeps
    the rest of the subtree. None when nothing routes the ticket."""
    provider, by_proj_comp, by_component, by_project = maps
    proj = (ticket.project or "").casefold()
    comps = [c.casefold() for c in ticket.components]
    for tier in (
        {rc: route for (rp, rc), route in by_proj_comp.items() if rp == proj},
        by_component,
    ):
        best = None
        best_len = -1
        for route_comp, route in tier.items():
            if len(route_comp) > best_len and any(
                _component_hit(route_comp, c, provider) for c in comps
            ):
                best, best_len = route, len(route_comp)
        if best is not None:
            return best
    return by_project.get(proj)


def _import_routed_tickets() -> None:
    """The tail of every sync: turn routed, unlinked tickets into linked changes.

    Idempotent by construction - the 1:1 change⇄ticket link is the dedupe, checked
    against linked_ticket_refs up front and enforced by link_ticket's own guard under
    the store lock, so re-syncs and restarts never double-import. One direction only:
    nothing here ever writes back to the tracker, and the change's bucket/status are
    the PM's to manage from the moment it lands.
    """
    for config in CONFIG.trackers:
        if not config.imports:
            continue
        # BOTH stores' links count: a ticket a project holds must not also become a
        # change - the same one-ticket-one-thing rule the interactive paths enforce.
        # The epic pass runs first (see _run_tracker_sync), so on a deployment whose
        # import_types accidentally include the epic rung, an epic is already a project
        # by the time this pass sees it, and is skipped here rather than duplicated.
        linked = {
            normalize_key(tid, key)
            for tid, key in (
                roadmap_store.linked_ticket_refs() + portfolio_store.linked_ticket_refs()
            )
        }
        types = {t.casefold() for t in config.import_types}
        maps = _route_maps(config)
        tickets = tracker_store.tickets_of(config.id)
        excluded = _excluded_keys(
            config, {normalize_key(config.id, t.key): t for t in tickets}
        )
        # Task-level children, indexed by parent. Tasks are HOW a story gets done, not
        # separate planning material - importing each one as its own change buries the
        # board in execution detail. They ride along as a snapshot inside the parent's
        # description instead (see below), so the story imports as ONE change that
        # still says what it is made of.
        tasks_by_parent: dict[str, list[Ticket]] = {}
        for t in tickets:
            if t.parent_key and t.type in (TYPE_TASK, TYPE_SUBTASK):
                tasks_by_parent.setdefault(
                    normalize_key(config.id, t.parent_key), []
                ).append(t)
        imported = 0
        excluded_count = 0
        wont_do_count = 0
        unrouted: dict[str, int] = {}
        for ticket in tickets:
            if ticket.raw_type.casefold() not in types:
                continue
            # Declared out of the automatic passes - counted, not unrouted: unrouted
            # means "route this someday", excluded means "never, it lives elsewhere".
            if normalize_key(config.id, ticket.key) in excluded:
                excluded_count += 1
                continue
            # Resolved as won't-do: a decision NOT to build. It has no place on the
            # board at all - imported, it would read as shipped work, since the only
            # coarse state its Done category maps to is "done".
            if _is_wont_do(ticket):
                wont_do_count += 1
                continue
            # Epic-level tickets are NEVER changes, whatever import_types says - they
            # are the epic pass's to represent, as projects. Without this, a done epic
            # ping-pongs forever on a deployment whose import_types include the epic
            # rung: the reclaim deletes its epic-shaped change, the epic pass skips it
            # (done epics are history, not projects), and this pass would then re-import
            # it as a change for the next sync's reclaim to delete again.
            if ticket.type == TYPE_EPIC:
                continue
            route = _match_route(maps, ticket)
            if route is None:
                for c in ticket.components or ["(no component)"]:
                    unrouted[c] = unrouted.get(c, 0) + 1
                continue
            if normalize_key(config.id, ticket.key) in linked:
                continue
            try:
                matched = (
                    f"component route {route.component!r}" if route.component
                    else f"project route {route.project!r}"
                )
                description = f"Imported from {config.label} {ticket.key} by {matched}."
                children = sorted(
                    tasks_by_parent.get(normalize_key(config.id, ticket.key), ()),
                    key=lambda t: t.key,
                )
                if children:
                    shown = children[:30]
                    lines = "\n".join(
                        f"- [{t.state or '?'}] {t.title or t.key} ({t.key})"
                        for t in shown
                    )
                    if len(children) > len(shown):
                        lines += f"\n- …and {len(children) - len(shown)} more"
                    description += (
                        f"\n\nTasks under this ticket (snapshot at import - "
                        f"{config.label} has the live list):\n{lines}"
                    )
                item = roadmap_store.create(
                    product=route.product,
                    title=ticket.title or ticket.key,
                    description=description,
                    bucket="later",
                    status=_imported_status(ticket),
                    system=route.system or None,
                    system_required=False,
                )
                roadmap_store.link_ticket(item.id, config.id, ticket.key)
            except TicketAlreadyLinked:
                # Lost the race to a concurrent manual link - theirs wins; remove the
                # change this pass just created so the ticket keeps exactly one.
                roadmap_store.delete(item.id)
                continue
            except ValueError as exc:
                print(f"[trackers] import of {ticket.key} skipped: {exc}")
                continue
            imported += 1
            _audit(None, "roadmap.item_imported", f"{route.product}/{item.id}", ticket.key)
        _import_report[config.id] = {
            "imported": imported,
            "excluded": excluded_count,
            "wont_do": wont_do_count,
            "unrouted": {c: n for c, n in sorted(unrouted.items(), key=lambda kv: -kv[1])},
            "unrouted_total": sum(unrouted.values()),
        }
        if imported:
            print(f"[trackers] {config.id}: imported {imported} change(s)")


@app.get("/roadmap/data")
def roadmap_data() -> dict:
    """The board's dataset, grouped by product (the original shape, unchanged).

    `portfolio` is additive: the board ships it so the page can re-group into the
    initiative lens locally as websocket events arrive, without refetching the whole
    board on every keystroke elsewhere. Existing consumers can ignore it.

    `product_parents` is the taxonomy's hierarchy (child id -> parent id) and is
    additive in the same way: `products` still lists every product flat, so a consumer
    that only needs labels - the session picker's badges, a chat header - keeps working
    without knowing the tree exists.

    `systems` / `product_systems` / `unattributed` are additive in that same way, and all
    three are empty on a deployment that declares no [systems] - which is what lets the
    board render its system chips and its restructure banner without a second request,
    and show neither when the layer is dormant.
    """
    items = {
        product: [_with_ticket(item) for item in items]
        for product, items in roadmap_store.list_all().items()
    }
    return {
        "products": PRODUCTS,
        "product_parents": PRODUCT_PARENTS,
        # Only the products that declared any metadata - additive like everything else
        # here, and empty on a deployment that never wrote a metadata key.
        "product_meta": {p: asdict(m) for p, m in PRODUCT_META.items()},
        "systems": {s: spec.label for s, spec in SYSTEMS.items()},
        "product_systems": {p: list(s) for p, s in PRODUCT_SYSTEMS.items()},
        "unattributed": roadmap_store.unattributed_report(),
        "items": items,
        # The directory, so the board's person lens and its assignment picker need no
        # second request - same reason `trackers` rides along below. Deliberately NOT the
        # load table: the board derives that from the very sections it draws (see
        # roadmap.html's loadRows), which is what keeps it right after an optimistic
        # assignment instead of stale until the next fetch.
        "people": people_store.list_people(),
        "portfolio": {
            "initiatives": portfolio_store.list_initiatives(),
            # Joined to their epic tickets like the items above: the initiative lens
            # draws a badge per project row, and it must not need a second request.
            "projects": [_with_ticket(p) for p in portfolio_store.list_projects()],
            "catch_all_project_id": portfolio_store.catch_all_project_id,
            # project_id -> {last_at, recent_sessions}: what lets the board show an
            # ideation project as ALIVE (sessions touched it) when it has no changes
            # for the roadmap to read liveness from. Timestamps and counts only -
            # cost stays behind the admin endpoints.
            "activity": _project_activity(),
            # project_id -> why the project looks finished (see _close_candidates). The
            # board draws a "ready to close" affordance from it, and names the epic's
            # own open children where they contradict the local roll-up - which the page
            # cannot work out for itself, since it holds no catalog.
            "close_candidates": _close_candidates(items),
        },
        # So the board can render the badge palette and the picker without a second
        # round trip. Empty/`configured: false` when no [[trackers]] are declared.
        "trackers": _described_trackers(),
    }


@app.get("/roadmap/by-initiative")
def roadmap_by_initiative() -> dict:
    """The same changes under the other lens: Initiative -> Project -> Change.

    One dataset, two pivots. Nothing is dropped when you switch - projects with no
    initiative, and changes with no project, come back in a trailing group with
    `initiative: null` rather than vanishing.
    """
    changes = [
        _with_ticket(item)
        for items in roadmap_store.list_all().values()
        for item in items
    ]
    groups = portfolio_store.group_changes_by_initiative(changes)
    # The nested project entries get the same epic join the flat lists get - the
    # grouping is the portfolio's, the catalog is this module's, so the join happens
    # here on the assembled shape.
    for group in groups:
        for entry in group["projects"]:
            if entry["project"] is not None:
                entry["project"] = _with_ticket(entry["project"])
    return {
        "pivot": "initiative",
        "products": PRODUCTS,
        "product_parents": PRODUCT_PARENTS,
        "systems": {s: spec.label for s, spec in SYSTEMS.items()},
        "groups": groups,
        "trackers": _described_trackers(),
    }


@app.get("/roadmap/{product}")
def list_roadmap_items(product: str) -> list[dict]:
    if product not in PRODUCTS:
        raise HTTPException(status_code=404, detail=f"Unknown product: {product}")
    return roadmap_store.list_product(product)


@app.post("/roadmap/{product}/items")
def create_roadmap_item(product: str, request: Request, payload: dict = Body(...)) -> dict:
    actor = _require(request, "manage_roadmap")
    if product not in PRODUCTS:
        raise HTTPException(status_code=404, detail=f"Unknown product: {product}")
    title = (payload.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title required")
    try:
        item = roadmap_store.create(
            product=product,
            title=title,
            description=(payload.get("description") or "").strip(),
            bucket=payload.get("bucket") or "later",
            status=payload.get("status") or "pending",
            origin_product=(payload.get("origin_product") or "").strip() or None,
            owner=(payload.get("owner") or "").strip() or None,
            assignee=_validated_assignee(payload.get("assignee")) or None,
            project_id=_resolve_project_id(payload),
            start_at=payload.get("start_at"),
            target_at=payload.get("target_at"),
            # Required once [systems] is declared - the store raises, and this 400s with
            # a message naming the systems this product actually touches.
            system=payload.get("system"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(actor, "roadmap.item_created", f"{product}/{item.id}", item.title)
    # Linking at creation is one call instead of create-then-PATCH. A failed link raises,
    # which leaves the change created but unlinked - deliberately not rolled back: the
    # change is the thing worth keeping, and the error says exactly what to retry.
    if payload.get("ticket") or payload.get("ticket_key"):
        item = _apply_ticket_link(actor, item.id, payload)
    return _with_ticket(item.to_public_dict())


@app.patch("/roadmap/{product}/items/{item_id}")
def update_roadmap_item(product: str, item_id: str, request: Request, payload: dict = Body(default={})) -> dict:
    actor = _require(request, "manage_roadmap")
    existing = roadmap_store.get(item_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Unknown roadmap item")
    # Enforced on the SOURCE product's path segment, same as any other PATCH here -
    # this is what makes a move safe to grant through a PM's existing own-product-only
    # PATCH allowlist (see agent.py): it can only move items it currently owns, never
    # reach into another product's board to pull one out.
    if existing.product != product:
        raise HTTPException(
            status_code=400,
            detail=f"Item {item_id} belongs to product '{existing.product}', not '{product}'",
        )

    move_to = (payload.get("move_to_product") or "").strip() or None
    if move_to:
        if move_to not in PRODUCTS:
            raise HTTPException(status_code=400, detail=f"Unknown product: {move_to}")
        try:
            item = roadmap_store.move(
                item_id,
                to_product=move_to,
                triaged=bool(payload.get("triaged", False)),
                bucket=payload.get("bucket"),
                status=payload.get("status"),
                title=payload.get("title"),
                description=payload.get("description"),
                # Re-attribute as part of the move. Omitted, the current system carries
                # over and the move is refused if the destination does not touch it.
                system=payload.get("system"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _audit(actor, "roadmap.item_moved", item_id, f"{product} -> {move_to}")
        # Joined like every other item response - a move must not hand back a shape whose
        # `ticket` key is missing, or the board would drop the badge until a reload.
        return _with_ticket(item.to_public_dict())

    try:
        item = roadmap_store.update(
            item_id,
            bucket=payload.get("bucket"),
            status=payload.get("status"),
            triaged=payload.get("triaged"),
            title=payload.get("title"),
            description=payload.get("description"),
            # None = no change; "" = clear external ownership (see RoadmapStore.update).
            owner=payload.get("owner"),
            # Same convention, and "" on a LINKED change hands the answer back to the
            # tracker's own assignee rather than to nobody (see people.effective_assignee).
            assignee=_validated_assignee(payload.get("assignee")),
            # Same convention: "" detaches the change from its project.
            project_id=_validated_project_id(payload.get("project_id")),
            # And again for the schedule: "" clears a date, a malformed one or a start
            # after its target is a 400 with the reason, and the item is left untouched.
            start_at=payload.get("start_at"),
            target_at=payload.get("target_at"),
            # NOT the ""-clears convention, deliberately: None leaves the attribution
            # alone, any other value re-attributes, and "" is a 400 rather than a way to
            # make a change unattributed again (see roadmap.validate_system).
            system=payload.get("system"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Ticket linking is applied after the ordinary field update so one PATCH can do both,
    # and it is keyed off `ticket` being PRESENT rather than truthy - `"ticket": ""` is how
    # a caller unlinks, matching the `owner`/`project_id` convention above.
    if "ticket" in payload or "ticket_key" in payload:
        item = _apply_ticket_link(actor, item_id, payload)
    _audit(actor, "roadmap.item_updated", f"{product}/{item_id}")
    return _with_ticket(item.to_public_dict())


def _validated_assignee(value) -> str | None:
    """The `assignee` half of a roadmap payload, checked against the directory.

    Same three-state convention as `owner` and `project_id`: None leaves it alone, "" clears
    it, anything else must be a person the directory actually knows. Refused rather than
    stored blindly, because an id nothing resolves renders as "assigned to somebody
    unknown" on every card forever, which looks exactly like a bug in the join.
    """
    if value is None:
        return None
    person_id = str(value).strip()
    if not person_id:
        return ""
    if people_store.get(person_id) is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown person: {person_id}. Assign by person id - GET "
            "/people/directory lists them, or POST one to add somebody the trackers "
            "have never seen.",
        )
    return person_id


def _is_unlink(payload: dict) -> bool:
    """Whether a link payload means "clear the link": `"ticket": ""` or
    `"ticket_key": ""`, the same present-but-empty convention owner/project_id use."""
    reference = payload.get("ticket")
    return (reference is not None and not str(reference).strip()) or (
        "ticket_key" in payload and not (payload.get("ticket_key") or "").strip()
    )


def _resolve_ticket(payload: dict) -> tuple[str, str, Ticket]:
    """Resolves a link payload to (tracker_id, key, ticket), or raises the HTTP error
    that says why not. The shared half of linking a change and linking a project - one
    payload convention, one existence check, one set of failure shapes.

    Accepts either a single `ticket` value - a full URL or a bare key, resolved against the
    configured trackers - or an explicit `tracker_id` + `ticket_key` pair for a caller that
    already knows both.

    A link is refused unless the ticket actually EXISTS in its tracker: without that, a
    typo would sit on the card forever as an unresolved badge, which looks identical to a
    tracker being down. The existence check reuses the synced catalog and only calls out
    for a ticket the last sync did not cover.
    """
    reference = payload.get("ticket")
    explicit_key = (payload.get("ticket_key") or "").strip()
    explicit_tracker = (payload.get("tracker_id") or "").strip()

    if not tracker_store.is_configured:
        raise HTTPException(
            status_code=400,
            detail="No trackers are configured, so a ticket cannot be linked. Add a "
            "[[trackers]] block to pm_studio_local/config.toml.",
        )

    if explicit_key and explicit_tracker:
        tracker_id, key = explicit_tracker, explicit_key
    else:
        resolved = tracker_store.resolve(str(reference or explicit_key))
        if resolved is None:
            raise HTTPException(
                status_code=400,
                detail=f"Could not tell which tracker {str(reference or explicit_key)!r} "
                "belongs to. Paste the ticket's URL, or send tracker_id and ticket_key "
                "explicitly. Configured trackers: "
                + (", ".join(t.id for t in CONFIG.trackers) or "none"),
            )
        tracker_id, key = resolved

    if CONFIG.tracker(tracker_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown tracker: {tracker_id}")

    try:
        ticket = tracker_store.ensure_ticket(tracker_id, key)
    except TrackerError as exc:
        # 502: the tracker, not this request, is what failed - and the message is already
        # scrubbed of the token by trackers._scrub.
        raise HTTPException(
            status_code=502, detail=f"Could not reach {tracker_id}: {exc}"
        ) from exc
    if ticket is None:
        raise HTTPException(
            status_code=404,
            detail=f"{key} was not found in {tracker_id}. Check the key, or that the "
            "configured credential can see that project.",
        )
    return tracker_id, key, ticket


def _apply_ticket_link(actor: User | None, item_id: str, payload: dict) -> RoadmapItem:
    """Links or unlinks a change's ticket from a roadmap PATCH. An empty value unlinks;
    resolution and existence checking are _resolve_ticket's (see there)."""
    if _is_unlink(payload):
        _audit(actor, "roadmap.ticket_unlinked", item_id)
        return roadmap_store.unlink_ticket(item_id)

    tracker_id, key, ticket = _resolve_ticket(payload)

    # One ticket backs ONE thing in PM Studio, across both stores: each store enforces
    # 1:1 among its own records, and this is the cross-store half. Without it, an epic
    # could be a project here and a change there, and the two would drift apart while
    # both claiming to BE that ticket.
    holder = portfolio_store.project_for_ticket(tracker_id, key)
    if holder is not None:
        raise HTTPException(
            status_code=409,
            detail=f"{key} is already linked to the project \"{holder.title}\" "
            f"(id {holder.id}). A ticket can back one change or one project, not "
            "both - unlink it from the project first.",
        )

    try:
        item = roadmap_store.link_ticket(item_id, tracker_id, key)
    except TicketAlreadyLinked as exc:
        # 409, not 400: the request was well-formed, it collided with existing state. The
        # detail names the change already holding the ticket.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(actor, "roadmap.ticket_linked", item_id, f"{tracker_id}/{ticket.key}")
    return item


def _apply_epic_link(actor: User | None, project_id: str, payload: dict):
    """Links or unlinks a project's epic from a portfolio POST/PATCH - the project-rung
    twin of _apply_ticket_link, with one extra rule: the ticket must actually BE an
    epic-level one. A project is what an epic is in the tracker's own hierarchy, and
    silently letting a Story stand in for one would make every "tracked as" annotation
    a small lie. The check keys on the canonical type, so ADO's "Epic" and a renamed
    "Initiative"/"Theme" all pass while a Story or Bug is refused by name.
    """
    if _is_unlink(payload):
        _audit(actor, "portfolio.epic_unlinked", project_id)
        try:
            return portfolio_store.unlink_epic(project_id)
        except PortfolioError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    tracker_id, key, ticket = _resolve_ticket(payload)

    if ticket.type != TYPE_EPIC:
        raise HTTPException(
            status_code=400,
            detail=f"{ticket.key} is a {ticket.raw_type}, not an epic-level ticket. "
            "A project links to the epic it is tracked as; stories, tasks and bugs "
            "link to the project's changes instead.",
        )

    # The other direction of the cross-store rule in _apply_ticket_link.
    holder = roadmap_store.item_for_ticket(tracker_id, key)
    if holder is not None:
        raise HTTPException(
            status_code=409,
            detail=f"{key} is already linked to the change \"{holder.title}\" "
            f"(id {holder.id}) on the {holder.product} board. A ticket can back one "
            "change or one project, not both - unlink it there first.",
        )

    try:
        project = portfolio_store.link_epic(project_id, tracker_id, key)
    except EpicAlreadyLinked as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PortfolioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(actor, "portfolio.epic_linked", project_id, f"{tracker_id}/{ticket.key}")
    return project


# ---- push: creating a ticket for work planned here ----
#
# The write half of a tracker connection, and the inverse of the import pass. Import
# takes a ticket that already exists and makes a change from it; push takes a change
# planned here and makes the ticket. Both end in the same 1:1 link, which is what keeps
# them from fighting: a pushed change is a linked change, so the next import pass sees
# its ticket as already held and leaves it alone.
#
# One property worth stating plainly, because it bounds the whole feature: a push writes
# ONCE. It creates the ticket and links it, and from then on the tracker owns that
# ticket's type, state and title - a later edit here is never mirrored there, and a push
# never transitions or deletes anything. That is the same direction of authority the
# catalog already assumes (see trackers.py), so nothing about how a card renders changes.


def _push_target(payload: dict, *, epic: bool) -> tuple[TrackerConfig, str, str]:
    """Which tracker, project and issue type this push should use.

    Resolved from the payload where the caller stated something and from
    `[trackers.push]` where it did not, so the ordinary call is `POST` with an empty body
    and the dialog's overrides are the same endpoint with two more fields. `epic` picks
    which of the two configured types is the default - a project pushes as an epic, a
    change as a story - which is the only asymmetry between the two callers.
    """
    pushable = [t for t in CONFIG.trackers if t.can_push]
    if not pushable:
        declared = [t for t in CONFIG.trackers if t.push is not None]
        raise HTTPException(
            status_code=400,
            detail=(
                # Two different fixes, so two different messages: a tracker with a push
                # target but no usable credential is a .env problem, not a config one.
                f"Tracker {declared[0].id!r} declares a push target but cannot be "
                f"reached: {declared[0].unusable_reason}."
                if declared
                else "No tracker is set up to create tickets. Add a [trackers.push] "
                "table to a [[trackers]] block in pm_studio_local/config.toml."
            ),
        )

    requested = (payload.get("tracker_id") or "").strip()
    if requested:
        config = next((t for t in pushable if t.id == requested), None)
        if config is None:
            raise HTTPException(
                status_code=400,
                detail=f"Tracker {requested!r} cannot create tickets. Trackers that can: "
                + ", ".join(t.id for t in pushable),
            )
    elif len(pushable) > 1:
        # Never guessed: creating a ticket on the wrong board is not something a default
        # should be allowed to do silently, and the dialog always sends one anyway.
        raise HTTPException(
            status_code=400,
            detail="More than one tracker can create tickets, so say which: "
            + ", ".join(t.id for t in pushable),
        )
    else:
        config = pushable[0]

    assert config.push is not None  # can_push guarantees it
    project = (payload.get("project") or "").strip() or config.push.project
    if project not in config.projects:
        # Same rule config enforces on the declared default, applied to an override: a
        # ticket in a project the catalog never pulls reads as unresolved on its own card.
        raise HTTPException(
            status_code=400,
            detail=f"{config.id} does not sync project {project!r}, so a ticket created "
            f"there could not be read back. Projects: {', '.join(config.projects)}",
        )
    issue_type = (payload.get("issue_type") or "").strip() or (
        config.push.epic_type if epic else config.push.change_type
    )
    # A project must be backed by an epic-LEVEL ticket, the same rule _apply_epic_link
    # enforces on a manual link. Checked here, BEFORE the create, because the check after
    # it (push_project_epic) can only report an orphan it is too late to prevent: the
    # ticket already exists in Jira and we never delete. Not at config-load time instead,
    # for two reasons - config.py cannot import the type vocabulary from trackers.py
    # without a cycle, and a load-time check would miss a per-push `issue_type` override,
    # which is the same mistake by a different route.
    if epic and canonical_type(issue_type) != TYPE_EPIC:
        raise HTTPException(
            status_code=400,
            detail=f"{issue_type!r} is not recognised as an epic-level issue type, and a "
            "project must be backed by an epic. Nothing was created. Use the name your "
            "instance gives that rung (Epic, Initiative, Theme...) in `epic_type` under "
            "[trackers.push], or link the epic by hand from the board.",
        )
    return config, project, issue_type


def _push_description(description: str, *, kind: str, product: str, local_id: str) -> str:
    """What lands in the ticket's description: the plan as written here, plus one line
    saying where it came from.

    The provenance line is not decoration. A ticket that appears on a board with no
    author and no context reads as noise to everyone who did not push it, and the id is
    what lets someone here find the change again from the ticket. One line, at the end,
    after the content the PM actually wrote - the mirror of the import stamp that opens
    the description of a change created from a ticket.
    """
    stamp = f"Pushed from PM Studio - {product} {kind} {local_id}."
    body = (description or "").strip()
    return f"{body}\n\n{stamp}" if body else stamp


def _push_and_report(
    config: TrackerConfig,
    *,
    project: str,
    issue_type: str,
    summary: str,
    description: str,
    parent_key: str | None,
) -> tuple[Ticket, bool]:
    """The create call, with every failure turned into the HTTP error that says why."""
    try:
        return tracker_store.push_ticket(
            config.id,
            project=project,
            issue_type=issue_type,
            summary=summary,
            description=description,
            parent_key=parent_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TrackerError as exc:
        # 502, like every other tracker-side failure here: this request was well-formed
        # and the tracker refused it. The message is already scrubbed of the token, and it
        # carries Jira's own body - which is what names an issue type the project does
        # not have, or a required field nobody configured.
        raise HTTPException(
            status_code=502,
            detail=f"{config.label} would not create the ticket: {exc}",
        ) from exc


def _parent_epic_for(item: RoadmapItem, tracker_id: str) -> str | None:
    """The epic key a pushed change should hang under: the ticket its PROJECT is linked
    to, when that link is in the tracker being pushed to.

    This is the whole reason projects are pushable too. Push the project first and its
    epic exists; every change pushed afterwards lands underneath it, so the tracker ends
    up with the same shape the plan has here instead of a flat pile of orphan stories.
    A change with no project, a project with no epic, or an epic in a DIFFERENT tracker
    all answer None - cross-tracker parenting is not a thing Jira can express.
    """
    if not item.project_id:
        return None
    project = portfolio_store.get_project(item.project_id)
    if project is None or project.tracker_id != tracker_id:
        return None
    return project.ticket_key or None


@app.post("/roadmap/{product}/items/{item_id}/push")
def push_roadmap_item(product: str, item_id: str, request: Request, payload: dict = Body(default={})) -> dict:
    """Creates this change's ticket in the tracker and links it, in one call.

    `tracker_id`, `project` and `issue_type` are all optional overrides of
    `[trackers.push]`; an empty body is the ordinary one-click push.

    A change that is ALREADY linked is refused rather than pushed again - that is the
    duplicate this endpoint exists to prevent, and the 409 names the ticket it already
    has. Unlink first if the intent really is a second ticket.
    """
    actor = _require(request, "manage_roadmap")
    item = roadmap_store.get(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Unknown roadmap item")
    if item.product != product:
        raise HTTPException(
            status_code=400,
            detail=f"Item {item_id} belongs to product '{item.product}', not '{product}'",
        )
    if item.ticket_key:
        raise HTTPException(
            status_code=409,
            detail=f"\"{item.title}\" is already linked to {item.ticket_key}. Unlink it "
            "first if you meant to create a second ticket for this change.",
        )

    config, project, issue_type = _push_target(payload, epic=False)
    parent_key = _parent_epic_for(item, config.id)
    ticket, parent_set = _push_and_report(
        config,
        project=project,
        issue_type=issue_type,
        summary=item.title,
        description=_push_description(
            item.description, kind="change", product=item.product, local_id=item.id
        ),
        parent_key=parent_key,
    )

    # The ticket now EXISTS upstream, so from here on nothing may raise without saying
    # its key: an error that loses the key strands a real ticket with nothing pointing at
    # it, which is the one outcome worse than a failed push.
    #
    # The cross-store half of the 1:1 rule, same as _apply_ticket_link: each store
    # enforces uniqueness among its own records, and one ticket backs one thing across
    # both. A freshly created key should collide with nothing - but "should" is not the
    # guarantee this rule is, and a Jira project deleted and recreated restarts its
    # numbering, so a stale link can be pointing at the key we just got back.
    holder = portfolio_store.project_for_ticket(config.id, ticket.key)
    if holder is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Created {ticket.key} in {config.label}, but that key is already "
            f"linked to the project \"{holder.title}\" (id {holder.id}), so it was not "
            "linked to this change. The ticket exists - unlink the project first, or "
            "delete the ticket in the tracker.",
        )
    try:
        item = roadmap_store.link_ticket(item_id, config.id, ticket.key)
    except (TicketAlreadyLinked, ValueError, KeyError) as exc:
        raise HTTPException(
            status_code=409,
            detail=f"Created {ticket.key} in {config.label}, but could not link it here: "
            f"{exc}. The ticket exists - link it by hand, or delete it in the tracker.",
        ) from exc
    _audit(actor, "roadmap.ticket_pushed", f"{product}/{item_id}", f"{config.id}/{ticket.key}")

    return {
        **_with_ticket(item.to_public_dict()),
        # Reported, never silent: the parent is best-effort (see JiraClient.create_issue),
        # and a story that quietly landed outside its epic is exactly the kind of thing a
        # PM finds out about a week later from someone else's board.
        "push": {
            "key": ticket.key,
            "url": ticket.url,
            "parent_key": parent_key if parent_set else None,
            "parent_skipped": bool(parent_key) and not parent_set,
        },
    }


@app.post("/portfolio/projects/{project_id}/push")
def push_project_epic(project_id: str, request: Request, payload: dict = Body(default={})) -> dict:
    """Creates this project's epic in the tracker and links it - the project rung of
    push_roadmap_item, and what closes the pending-upload gap the portfolio page reports.

    Pushing the epic before the project's changes is what gives those changes a parent to
    land under (see _parent_epic_for).
    """
    actor = _require(request, "manage_roadmap")
    project = portfolio_store.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Unknown project")
    if project.ticket_key:
        raise HTTPException(
            status_code=409,
            detail=f"\"{project.title}\" is already tracked as {project.ticket_key}. "
            "Unlink it first if you meant to create a second epic.",
        )
    if project.is_any_catch_all:
        # Catch-alls are auto-created cost-attribution plumbing, not work anyone would
        # file an epic for - the same exemption pending_upload_report makes.
        raise HTTPException(
            status_code=400,
            detail="Catch-all projects are internal cost-attribution plumbing, not work "
            "to file an epic for.",
        )

    config, tracker_project, issue_type = _push_target(payload, epic=True)
    ticket, _ = _push_and_report(
        config,
        project=tracker_project,
        issue_type=issue_type,
        summary=project.title,
        description=_push_description(
            project.description, kind="project", product="portfolio", local_id=project.id
        ),
        parent_key=None,
    )
    # The epic-level check _apply_epic_link enforces on a manual link applies to a pushed
    # one too: if the configured epic_type is not actually an epic rung in this instance,
    # the honest outcome is to say so rather than let a Story stand in for a project.
    if ticket.type != TYPE_EPIC:
        raise HTTPException(
            status_code=502,
            detail=f"Created {ticket.key}, but {config.label} made it a "
            f"{ticket.raw_type} rather than an epic, so it was not linked as this "
            "project's epic. Fix `epic_type` in [trackers.push] and delete "
            f"{ticket.key}, or link it to a change instead.",
        )

    # The other direction of the cross-store rule above.
    change_holder = roadmap_store.item_for_ticket(config.id, ticket.key)
    if change_holder is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Created {ticket.key} in {config.label}, but that key is already "
            f"linked to the change \"{change_holder.title}\" (id {change_holder.id}) on "
            f"the {change_holder.product} board, so it was not linked to this project. "
            "The ticket exists - unlink the change first, or delete the ticket.",
        )
    try:
        project = portfolio_store.link_epic(project_id, config.id, ticket.key)
    except (EpicAlreadyLinked, PortfolioError) as exc:
        raise HTTPException(
            status_code=409,
            detail=f"Created {ticket.key} in {config.label}, but could not link it here: "
            f"{exc}. The epic exists - link it by hand, or delete it in the tracker.",
        ) from exc
    _audit(actor, "portfolio.epic_pushed", project_id, f"{config.id}/{ticket.key}")
    return {
        **_with_ticket(project.to_public_dict()),
        "push": {"key": ticket.key, "url": ticket.url},
    }


@app.delete("/roadmap/{product}/items/{item_id}")
def delete_roadmap_item(product: str, item_id: str, request: Request) -> dict:
    actor = _require(request, "manage_roadmap")
    existing = roadmap_store.get(item_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Unknown roadmap item")
    if existing.product != product:
        raise HTTPException(
            status_code=400,
            detail=f"Item {item_id} belongs to product '{existing.product}', not '{product}'",
        )
    roadmap_store.delete(item_id)
    _audit(actor, "roadmap.item_deleted", f"{product}/{item_id}", existing.title)
    return {"status": "deleted"}


@app.websocket("/ws/roadmap")
async def roadmap_ws(websocket: WebSocket) -> None:
    if await _ws_reject(websocket):
        return
    await websocket.accept()
    roadmap_ws_clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()  # inbound messages ignored; push-only
    except WebSocketDisconnect:
        pass
    finally:
        roadmap_ws_clients.discard(websocket)


# ---- session-scoped data ----

@app.get("/history/{session_id}")
def history(session_id: str) -> list[dict]:
    return _get_runtime(session_id).pm_agent.load_history()


@app.get("/chat/{session_id}/pending")
def pending_messages(session_id: str) -> list[dict]:
    """The messages this session has accepted but not started yet. The chat page merges
    these into the bottom of the timeline on load, which is what makes a message typed
    while the PM was thinking survive a reload."""
    return _get_runtime(session_id).pm_agent.load_pending()


@app.get("/chat/{session_id}/uploads/{filename}")
def get_attachment(session_id: str, filename: str) -> FileResponse:
    path = _get_runtime(session_id).pm_agent.uploads_dir / Path(filename).name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Attachment not found")
    return FileResponse(path)


@app.post("/chat/{session_id}/reset")
def reset_chat(session_id: str, request: Request) -> dict:
    """Archives the current spec/chat, then clears them and drops the Claude session
    pointer so the next turn starts a brand-new conversation. PROJECT_STATUS.md,
    PROJECT_INDEX.md, and docs/ are untouched."""
    actor = _require(request, "run_session")
    # Anything still parked belongs to the conversation being cleared - running it
    # afterwards would land it in the fresh one, which is the opposite of a reset. The
    # turn already in flight can't be recalled; only what hasn't started.
    _discard_queued_turns(session_id)
    _get_runtime(session_id).pm_agent.reset()
    _audit(actor, "chat.reset", session_id)
    return {"status": "reset"}


@app.post("/chat/{session_id}/stop")
def stop_chat(session_id: str, request: Request) -> dict:
    """Stops the PM turn in flight - it ends as an error, "Stopped by <who>.", written
    to the transcript like any failed turn - and discards the messages queued behind
    it, so nothing starts in its place. Dev tasks keep running (POST
    /tasks/{session_id}/{task_id}/stop, or /sessions/{session_id}/stop for everything).
    Returns {pm_turn: whether a turn was running, discarded: queued message ids}."""
    actor = _require(request, "run_session")
    runtime = _get_runtime(session_id)
    discarded = _discard_pending_messages(session_id, runtime)
    stopped = runtime.pm_agent.stop_turn(_who(actor))
    report = {"session": session_id, "pm_turn": stopped, "discarded": discarded}
    _audit(actor, "chat.stopped", session_id, json.dumps(report))
    return report


@app.post("/tasks/{session_id}")
def create_task(session_id: str, request: Request, payload: dict = Body(...)) -> dict:
    """Dispatching a dev agent is the one endpoint that is equivalent to running
    arbitrary code on the host - agents run with bypassed permissions inside the repo.
    It is gated here, on the HTTP path, and audited by actor."""
    actor = _require(request, "dispatch_dev_task")
    # A research/strategy session cannot dispatch, whoever asks: its PM's allowlist
    # already omits the dispatch curl, but the allowlist only binds the PM subprocess -
    # this is the check that binds everyone else (and any future caller). 403 with the
    # remedy, so a refused dispatch never reads as a transient error worth retrying.
    session = sessions.get(session_id)
    if session is not None and session.mode == "research":
        _audit(actor, "dev_task.refused", session_id, "research/strategy session")
        raise HTTPException(
            status_code=403,
            detail=(
                "This session is in research/strategy mode - dev tasks cannot be "
                "dispatched from it. Switch the session to build mode "
                "(POST /sessions/{id}/mode) if this work is ready to build."
            ),
        )
    description = (payload.get("task") or "").strip()
    if not description:
        return {"error": "task description required"}
    # Attribution is what routes a system's non-negotiable git workflow rules into the
    # dev agent's prompt, so it's enforced here with the same posture as roadmap
    # creates: a 400 that names the valid ids, never a silent default.
    system = str(payload.get("system") or "").strip()
    system_error = validate_dispatch_system(system)
    if system_error is not None:
        raise HTTPException(status_code=400, detail=system_error)
    # A quick task looks rather than builds (see tasks.QUICK_TASK_PREAMBLE). Still a dev
    # agent with the same powers, so it is gated and audited exactly like any dispatch.
    quick = payload.get("quick") is True
    task = _get_runtime(session_id).task_registry.start_task(description, system, quick=quick)
    _audit(actor, "dev_task.dispatched", session_id, ("[quick] " if quick else "") + description[:300])
    # Dispatching is the strongest signal of human intent in the system - it is a
    # decision to build something - so it carries the most weight in the split.
    _record_signal(actor, KIND_DEV_TASK, session_id)
    _dev_task_dispatchers[task["id"]] = _actor_id(actor)
    return task


@app.get("/tasks/{session_id}")
def list_tasks(session_id: str) -> list[dict]:
    return _get_runtime(session_id).task_registry.list_tasks()


@app.get("/tasks/{session_id}/{task_id}")
def get_task(session_id: str, task_id: str) -> dict:
    task = _get_runtime(session_id).task_registry.get_task(task_id)
    return task if task is not None else {"error": "not found"}


@app.post("/tasks/{session_id}/{task_id}/stop")
def stop_task(session_id: str, task_id: str, request: Request) -> dict:
    """Stops a running dev task: kills its agent and everything it started, and records
    it as "stopped" - not judged, and not something the PM is woken up to react to.
    409 when the task is not running (or is a merge-conflict resolution, which the
    merge flow owns). Returns the task as it then stands."""
    actor = _require(request, "dispatch_dev_task")
    registry = _get_runtime(session_id).task_registry
    task = registry.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Unknown task: {task_id}")
    stopped = registry.stop_task(task_id, _who(actor))
    if stopped is None:
        raise HTTPException(
            status_code=409, detail=f"Task {task_id} is not running (status: {task.get('status')})."
        )
    _audit(actor, "dev_task.stopped", session_id, task_id)
    return {"session": session_id, "task": stopped}


@app.get("/tasks/{session_id}/{task_id}/artifacts/{name:path}")
def get_task_artifact(session_id: str, task_id: str, name: str) -> FileResponse:
    """One file a dev task left for the stakeholder (a screenshot, a report), as listed
    in the task's "artifacts". Resolved strictly inside that task's own folder: no
    `..`, no absolute paths, no links out of it."""
    path = _get_runtime(session_id).task_registry.artifact_path(task_id, name)
    if path is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return FileResponse(path)


@app.post("/sessions/{session_id}/checkout/{system_id}")
def checkout_system_code(session_id: str, system_id: str, request: Request) -> dict:
    """Makes a system's code present in this session's worktree, where submodules start
    as empty folders - so the session's PM can READ the code it plans against, at the
    commit its own branch pins. Idempotent and cheap once done; dev tasks and local
    environments do the same on their own before they run. Blocking: the first call
    for a system can take a few seconds per repository."""
    actor = _require(request, "run_session")
    runtime = _get_runtime(session_id)
    error = validate_dispatch_system(system_id)
    if error is not None or not system_id:
        raise HTTPException(status_code=404, detail=error or "system required")
    try:
        notes = ensure_system_code(runtime.pm_agent.repo_root, system_id)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"Could not check {system_id} out: {exc}") from exc
    _audit(actor, "session.checkout", session_id, system_id)
    return {"system": system_id, "status": "ready", "notes": [str(n) for n in notes]}


async def _run_tasks_ws(websocket: WebSocket, session_id: str) -> None:
    if await _ws_reject(websocket):
        return
    await websocket.accept()
    task_ws_clients.setdefault(session_id, set()).add(websocket)
    try:
        while True:
            await websocket.receive_text()  # inbound messages ignored; push-only
    except WebSocketDisconnect:
        pass
    finally:
        task_ws_clients.get(session_id, set()).discard(websocket)


@app.websocket("/ws/tasks/{session_id}")
async def tasks_ws(websocket: WebSocket, session_id: str) -> None:
    await _run_tasks_ws(websocket, session_id)


def _enqueue_pm_turn(session_id: str, item: dict) -> int:
    """Parks one stakeholder message on the session's PM queue and makes sure a worker
    is draining it. Returns how many messages are ahead of this one: 0 means it starts
    right away, anything higher means the PM is still busy with an earlier message."""
    queue = _chat_queues.setdefault(session_id, asyncio.Queue())
    ahead = queue.qsize() + (1 if session_id in _chat_inflight else 0)
    queue.put_nowait(item)
    _chat_queued_ids.setdefault(session_id, set()).add(item["pending"]["id"])
    if session_id not in _chat_workers:
        _chat_workers[session_id] = asyncio.create_task(_drain_pm_queue(session_id))
    return ahead


def _discard_queued_turns(session_id: str) -> None:
    """Throws away every message a session has queued but not started. Hops onto the
    event loop first: its callers are endpoints (reset, and the stops - see
    _discard_pending_messages), which FastAPI runs on worker threads, and these
    registries belong to the loop."""

    def drop() -> None:
        queue = _chat_queues.get(session_id)
        ids = _chat_queued_ids.get(session_id, set())
        while queue is not None:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            ids.discard(item["pending"]["id"])

    if _main_loop is not None:
        _main_loop.call_soon_threadsafe(drop)
    else:  # no loop running (tests, or a call before startup) - nothing else can race
        drop()


async def _drain_pm_queue(session_id: str) -> None:
    """Runs a session's queued PM turns one at a time, in the order they were sent, and
    stops as soon as the queue runs dry. Nothing awaits between the empty check and
    deregistering the worker, so _enqueue_pm_turn can never hand work to a worker that
    is already on its way out - it starts a fresh one instead."""
    loop = asyncio.get_event_loop()
    queue = _chat_queues[session_id]
    try:
        while True:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            _chat_inflight.add(session_id)
            try:
                await _run_queued_pm_turn(session_id, item, loop)
            finally:
                _chat_inflight.discard(session_id)
                _chat_queued_ids.get(session_id, set()).discard(item["pending"]["id"])
    finally:
        _chat_workers.pop(session_id, None)
        if queue.empty():
            _chat_queues.pop(session_id, None)
        if not _chat_queued_ids.get(session_id):
            _chat_queued_ids.pop(session_id, None)


async def _run_queued_pm_turn(
    session_id: str, item: dict, loop: asyncio.AbstractEventLoop
) -> None:
    """One dequeued message -> one PM turn, with its events streamed back to the
    connection that sent it. A sender that has since disconnected simply loses them:
    the turn still runs and is still written to the transcript, so the message is never
    silently dropped just because its tab closed."""
    runtime = sessions.get_runtime(session_id)
    if runtime is None:
        return
    pending = item["pending"]
    events: asyncio.Queue = asyncio.Queue()

    def produce() -> None:
        try:
            other_ctx = sessions.describe_other_active_sessions(session_id)
            roadmap_ctx = _roadmap_context_for(session_id)
            for event in runtime.pm_agent.handle_user_message(
                pending["text"],
                pending.get("attachments") or [],
                other_ctx,
                roadmap_ctx,
                pending_id=pending["id"],
            ):
                loop.call_soon_threadsafe(events.put_nowait, event)
        except Exception as exc:  # surface dev/PM errors to the chat instead of dying silently
            loop.call_soon_threadsafe(events.put_nowait, {"type": "error", "message": str(exc)})
        finally:
            loop.call_soon_threadsafe(events.put_nowait, None)

    future = loop.run_in_executor(None, produce)
    websocket, send_lock = item["websocket"], item["send_lock"]
    while True:
        event = await events.get()
        if event is None:
            break
        # One signal per completed turn, carrying that turn's measured token spend. A
        # turn the stakeholder actually took, so it counts toward their week - unlike an
        # auto-continuation (see _auto_continue_pm).
        if event.get("agent_usage") is not None:
            _record_signal(item["user"], KIND_PM_TURN, session_id, usage=event["agent_usage"])
        if event["type"] == "pm_working":
            # Names the queue record this turn came from, so a page showing it as a
            # queued bubble (hydrated after a reload) can promote it to a sent one.
            event = {**event, "queued_id": pending["id"]}
        try:
            async with send_lock:
                await websocket.send_text(json.dumps(event))
        except Exception:
            pass  # sender's connection is gone; keep draining the rest of the turn
    await future


def _resume_pending(
    session_id: str,
    runtime,
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    ws_user,
) -> None:
    """Re-queues any message left in the session's queue file that no live worker is
    handling. Those are the ones a restart stranded: written when they arrived, never
    run, and otherwise stuck reading "queued" on the chat page forever. Records already
    in memory (a plain page reload, where the worker kept going the whole time) are
    skipped by id, so nothing runs twice. Deliberately on connect rather than at
    startup: the turn runs when someone is there to read it, not unattended at boot."""
    live = _chat_queued_ids.get(session_id, set())
    for record in runtime.pm_agent.load_pending():
        if record.get("id") in live:
            continue
        _enqueue_pm_turn(
            session_id,
            {
                "pending": record,
                "websocket": websocket,
                "send_lock": send_lock,
                "user": ws_user,
            },
        )


async def _run_chat_ws(websocket: WebSocket, session_id: str) -> None:
    # Driving a PM conversation is `run_session`, not `view`.
    if await _ws_reject(websocket, "run_session"):
        return
    await websocket.accept()
    runtime = sessions.get_runtime(session_id)
    if runtime is None:
        await websocket.send_text(
            json.dumps({"type": "error", "message": f"Unknown or inactive session: {session_id}"})
        )
        await websocket.close()
        return

    # Who is driving this socket, for activity attribution. Resolved once at connect:
    # the cookie cannot change mid-connection.
    ws_user = (
        account_store.resolve_login(websocket.cookies.get(SESSION_COOKIE_NAME))
        if CONFIG.is_enterprise
        else None
    )

    # Registered so a dev-task completion firing while this connection is just sitting
    # idle on receive_text() can still push the PM's auto-continuation reply here - see
    # _broadcast_chat_event. The lock is shared with that path so the two never send
    # concurrently on the same websocket.
    send_lock = asyncio.Lock()
    chat_ws_clients.setdefault(session_id, {})[websocket] = send_lock
    _resume_pending(session_id, runtime, websocket, send_lock, ws_user)
    try:
        while True:
            raw = await websocket.receive_text()
            payload = json.loads(raw)
            text = (payload.get("text") or "").strip()
            attachments = payload.get("attachments") or []
            if not text and not attachments:
                continue

            # Write the message (and decode its images) to the session's queue file
            # before anything else - in an executor, since that touches disk. From here
            # on the message exists independently of this connection: a reload, a crash
            # or a closed tab can't lose it.
            pending = await asyncio.get_event_loop().run_in_executor(
                None, runtime.pm_agent.enqueue_pending, text, attachments
            )
            # Then hand it to the queue and go straight back to receiving. Nothing here
            # waits on the turn, so the stakeholder can send again (and again) while the
            # PM is still working on an earlier message.
            ahead = _enqueue_pm_turn(
                session_id,
                {
                    "pending": pending,
                    "websocket": websocket,
                    "send_lock": send_lock,
                    "user": ws_user,
                },
            )
            # Acknowledge every accepted message with the id it was filed under, so the
            # page can label its own bubble - `ahead` says whether it is parked behind
            # earlier messages (> 0) or starting now (0). Sent for both, because the
            # page needs the id either way to promote the bubble when its turn starts.
            async with send_lock:
                await websocket.send_text(
                    json.dumps({"type": "pm_queued", "ahead": ahead, "id": pending["id"]})
                )
    except WebSocketDisconnect:
        pass
    finally:
        chat_ws_clients.get(session_id, {}).pop(websocket, None)


@app.websocket("/ws/chat/{session_id}")
async def chat_ws(websocket: WebSocket, session_id: str) -> None:
    await _run_chat_ws(websocket, session_id)


# ---- backward-compat aliases to the default session ----
#
# `/tasks/{task_id}` (single lookup) is deliberately NOT aliased - it would be
# indistinguishable from the new `/tasks/{session_id}` (list) route at the same
# URL shape. Everything else here has a zero-segment legacy form that doesn't
# collide with the new one-segment session-scoped routes.

@app.get("/dashboard")
def dashboard_alias() -> RedirectResponse:
    return RedirectResponse(f"/dashboard/{DEFAULT_SESSION_ID}")


@app.get("/history")
def history_alias() -> list[dict]:
    return history(DEFAULT_SESSION_ID)


@app.post("/tasks")
def create_task_alias(request: Request, payload: dict = Body(...)) -> dict:
    return create_task(DEFAULT_SESSION_ID, request, payload)


@app.get("/tasks")
def list_tasks_alias() -> list[dict]:
    return list_tasks(DEFAULT_SESSION_ID)


@app.websocket("/ws/chat")
async def chat_ws_alias(websocket: WebSocket) -> None:
    await _run_chat_ws(websocket, DEFAULT_SESSION_ID)


@app.websocket("/ws/tasks")
async def tasks_ws_alias(websocket: WebSocket) -> None:
    await _run_tasks_ws(websocket, DEFAULT_SESSION_ID)


# ---- local environments ([systems.<id>.local]) ----
#
# Thin on purpose: every rule (param allowlist, port conflicts, dependency order, what
# "ready" means, scopes, the session-environment limit, resources) lives in
# local_env.LocalEnvManager, so the routes only translate its refusals into status
# codes - 404 unknown system/service/resource/session, 400 invalid params, 409 port in
# use / session limit reached / resource busy. Starting never waits for readiness; it
# answers with the "starting" state and the page polls GET /local-env, while an agent
# blocks in GET .../wait until the outcome is known.
# Reads are `view` like every other read; starting, stopping and destroying are
# `manage_local_env`, which the agent principal holds, so a PM or dev agent can bring
# its system up with the header it is given.
#
# Every route takes an optional `?session=<id>` (default: the default session): which
# chat session's environment it acts on. Agents get theirs as $PM_STUDIO_SESSION.


def _local_env_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except LocalEnvError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


def _local_env_params(payload: dict | None):
    """The optional `params` member of a start/restart body; None when absent."""
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="The body must be a JSON object.")
    params = payload.get("params")
    if params is not None and not isinstance(params, dict):
        raise HTTPException(status_code=400, detail='"params" must be an object.')
    return params


def _local_env_target(session: str, target: str) -> str:
    """The audit target: unchanged for the default session, so existing audit queries
    keep matching; prefixed with the session otherwise."""
    return target if session == DEFAULT_SESSION_ID else f"{session}:{target}"


@app.get("/local-env")
def local_env_status(request: Request, session: str = DEFAULT_SESSION_ID) -> dict:
    _require(request, "view")
    systems = _local_env_call(local_env.status, session=session)
    return {"session": session, "systems": systems}


@app.get("/local-env/overview")
def local_env_overview(request: Request) -> dict:
    """Every active run and held resource across all sessions, with session names - what
    is using the machine, and how close it is to `[local_env] max_session_envs`."""
    _require(request, "view")
    return local_env.overview()


@app.post("/local-env/{system_id}/start")
def local_env_start_all(
    system_id: str,
    request: Request,
    payload: dict | None = Body(default=None),
    session: str = DEFAULT_SESSION_ID,
) -> dict:
    """Starts every service of the system in dependency order. Body (optional):
    {"params": {"<service id>": {"<name>": "<value>"}}}."""
    actor = _require(request, "manage_local_env")
    params = _local_env_params(payload)
    result = _local_env_call(local_env.start_all, system_id, params, session=session)
    _audit(actor, "local_env.started", _local_env_target(session, system_id), json.dumps(params or {}))
    return result


@app.post("/local-env/{system_id}/stop")
def local_env_stop_all(system_id: str, request: Request, session: str = DEFAULT_SESSION_ID) -> dict:
    actor = _require(request, "manage_local_env")
    result = _local_env_call(local_env.stop_all, system_id, session=session)
    _audit(actor, "local_env.stopped", _local_env_target(session, system_id))
    return result


@app.post("/local-env/{system_id}/resources/{resource_id}/create")
def local_env_create_resource(
    system_id: str, resource_id: str, request: Request, session: str = DEFAULT_SESSION_ID
) -> dict:
    """Explicitly provisions the session's copy of a resource (or the shared one) - the
    only way a resource a service merely `uses` comes to exist. Answers at once with
    state "creating"; 409 when it is already creating, ready or being destroyed."""
    actor = _require(request, "manage_local_env")
    result = _local_env_call(local_env.create_resource, system_id, resource_id, session=session)
    _audit(actor, "local_env.resource_created", _local_env_target(session, f"{system_id}/{resource_id}"))
    return result


@app.post("/local-env/{system_id}/resources/{resource_id}/destroy")
def local_env_destroy_resource(
    system_id: str, resource_id: str, request: Request, session: str = DEFAULT_SESSION_ID
) -> dict:
    """Destroys the session's copy of a resource (or the shared one). Answers at once
    with state "destroying"; 409 while a running service uses it."""
    actor = _require(request, "manage_local_env")
    result = _local_env_call(local_env.destroy_resource, system_id, resource_id, session=session)
    _audit(actor, "local_env.resource_destroyed", _local_env_target(session, f"{system_id}/{resource_id}"))
    return result


@app.post("/local-env/{system_id}/{service_id}/start")
def local_env_start(
    system_id: str,
    service_id: str,
    request: Request,
    payload: dict | None = Body(default=None),
    session: str = DEFAULT_SESSION_ID,
) -> dict:
    """Starts one service, its dependencies first. Body (optional):
    {"params": {"<name>": "<value>"}}."""
    actor = _require(request, "manage_local_env")
    params = _local_env_params(payload)
    result = _local_env_call(local_env.start, system_id, service_id, params, session=session)
    _audit(
        actor, "local_env.started", _local_env_target(session, f"{system_id}/{service_id}"),
        json.dumps(params or {}),
    )
    return result


@app.post("/local-env/{system_id}/{service_id}/stop")
def local_env_stop(
    system_id: str, service_id: str, request: Request, session: str = DEFAULT_SESSION_ID
) -> dict:
    actor = _require(request, "manage_local_env")
    result = _local_env_call(local_env.stop, system_id, service_id, session=session)
    _audit(actor, "local_env.stopped", _local_env_target(session, f"{system_id}/{service_id}"))
    return result


@app.post("/local-env/{system_id}/{service_id}/restart")
def local_env_restart(
    system_id: str,
    service_id: str,
    request: Request,
    payload: dict | None = Body(default=None),
    session: str = DEFAULT_SESSION_ID,
) -> dict:
    """Stop then start. Without `params` it reuses the ones the service last ran with."""
    actor = _require(request, "manage_local_env")
    params = _local_env_params(payload)
    result = _local_env_call(local_env.restart, system_id, service_id, params, session=session)
    _audit(
        actor, "local_env.restarted", _local_env_target(session, f"{system_id}/{service_id}"),
        json.dumps(params or {}),
    )
    return result


@app.get("/local-env/{system_id}/{service_id}/logs")
def local_env_logs(
    system_id: str,
    service_id: str,
    request: Request,
    lines: int = 200,
    session: str = DEFAULT_SESSION_ID,
) -> dict:
    """A service's output - or a resource's create/destroy output (pass its id)."""
    _require(request, "view")
    return {"lines": _local_env_call(local_env.logs, system_id, service_id, lines, session=session)}


@app.get("/local-env/{system_id}/{service_id}/wait")
def local_env_wait(
    system_id: str,
    service_id: str,
    request: Request,
    timeout: float = 180,
    session: str = DEFAULT_SESSION_ID,
) -> dict:
    """Blocks until the service leaves "starting" (running, failed, exited...) or
    `timeout` seconds pass (capped at 600), then answers its status plus
    `waited_seconds` and `timed_out` - what an agent calls after a start instead of
    sleeping blind. A plain `def`, so the wait parks a worker thread, never the event
    loop."""
    _require(request, "view")
    return _local_env_call(local_env.wait, system_id, service_id, session=session, timeout=timeout)


# ---- agent MCP servers ([agents.mcp.<name>]) ----
#
# Read-only view of what agents are given (agent_mcp.py) - names, transports, commands as
# written, URL hosts, missing variable NAMES; never values - plus a forced re-read and a
# smoke test. Reloading and checking are `manage_local_env`: like starting local services
# they act on the developer's machine setup, and the agent principal holds it too, so a
# PM can pick up a server the stakeholder just added without anyone restarting anything.


@app.get("/agents/mcp")
def agents_mcp_status(request: Request) -> dict:
    _require(request, "view")
    return agent_mcp.AGENT_MCP.status()


@app.get("/agents/auth")
def agents_auth_status(request: Request, refresh: bool = False) -> dict:
    """Whether the agents' Claude config (PM_AGENT_HOME, or the operator's own) is
    signed in, as whom, and whether a sign-in is running. Names and emails only."""
    _require(request, "view")
    return agent_env_module.agent_auth(refresh=refresh)


@app.post("/agents/auth/login")
def agents_auth_login(request: Request) -> dict:
    """Starts `claude auth login` for the agents' config: a browser sign-in on this
    machine that finishes by itself. Poll GET /agents/auth for the outcome."""
    actor = _require(request, "manage_local_env")
    _audit(actor, "agents.sign_in_started", "agents.auth")
    return agent_env_module.start_login()


@app.post("/agents/mcp/reload")
def agents_mcp_reload(request: Request) -> dict:
    actor = _require(request, "manage_local_env")
    agent_mcp.AGENT_MCP.refresh(force=True)
    _audit(actor, "agents.mcp_reloaded", "agents.mcp")
    return agent_mcp.AGENT_MCP.status()


@app.post("/agents/mcp/{name}/check")
def agents_mcp_check(name: str, request: Request) -> dict:
    """Starts a stdio server once (OS-adapted, resolved env, stdin held open) and says
    whether it is still running after a few seconds. Does not speak MCP."""
    actor = _require(request, "manage_local_env")
    try:
        result = agent_mcp.AGENT_MCP.check(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"No [agents.mcp.{name}] server is configured.") from exc
    _audit(actor, "agents.mcp_checked", name, json.dumps({"ok": result.get("ok")}))
    return result


@app.post("/agents/mcp/{name}/test")
def agents_mcp_test(name: str, request: Request) -> dict:
    """A real connection test (agent_mcp.AgentMcp.test): starts the server as agents
    get it, does the MCP handshake (initialize, tools/list) and calls its configured
    `probe`, within 30 s. A plain `def`, so the wait parks a worker thread."""
    actor = _require(request, "manage_local_env")
    try:
        result = agent_mcp.AGENT_MCP.test(name, activity=mcp_activity.ACTIVITY_LOG)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"No [agents.mcp.{name}] server is configured.") from exc
    _audit(actor, "agents.mcp_tested", name, json.dumps({"ok": result.get("ok"), "stage": result.get("stage")}))
    return result


@app.get("/agents/mcp/activity")
def agents_mcp_activity(request: Request, server: str = "", limit: int = 50) -> dict:
    """The newest MCP tool calls agents made (mcp_activity.py) - tool, a sanitized
    argument summary, ok/outcome/duration; never results - plus per-server counters
    (calls, errors, last_error, last_ok_at, consecutive_errors)."""
    _require(request, "view")
    return mcp_activity.ACTIVITY_LOG.report(server=server or None, limit=max(1, min(limit, 500)))


def _can(request: Request, capability: Capability) -> bool:
    """Non-raising twin of _require, for shaping a payload by role."""
    if not CONFIG.is_enterprise:
        return True
    user = _current_user(request)
    return user is not None and role_has(user.role, capability)


signals_api.mount(
    app,
    intelligence_service,
    require=_require,
    can=_can,
    audit=_audit,
    static_dir=STATIC_DIR,
)
