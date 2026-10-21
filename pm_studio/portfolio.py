"""The work model above the roadmap: Goals, Initiatives and Projects.

The shape is a single-parent chain with exactly one many-to-many relationship, sitting
on top of the roadmap items that already exist:

    Goals  <->  Initiative  ->  Project  ->  Change
                                              `- belongs to exactly ONE Product

- A **Change** is the existing roadmap item (see roadmap.py). This module adds no new
  concept for it; a change simply gains a single `project_id`.
- A **Project** belongs to exactly one Initiative. `initiative_id = None` is a real,
  reportable state called **unaligned** - allowed, so nobody is blocked mid-work, but
  surfaced so it gets fixed. A project may also be linked 1:1 to the epic-level ticket
  it is tracked as in Jira/ADO (see `link_epic`); unlinked means it was created locally
  and is pending upload to the tracker, which the sync cannot do yet.
- An **Initiative** has many Projects and may serve **several Goals**. That is the only
  many-to-many relationship in the model.

Why single-parent below the initiative: it is what makes cost attribution
unambiguous. Change -> Project -> Initiative is a unique path, so those totals are
exact and additive. **The additive tree stops at Initiative**: because an initiative
can serve several goals, the same spend legitimately contributes to more than one
goal, so goal-level figures overlap and must never be summed into a grand total.
`rollup_paths()` returns that unique path per change, and `goal_ids_for_initiative()`
is deliberately separate so a caller cannot accidentally treat goals as additive.

Why Product is not a level: it hangs off the Change, not the tree. That is what gives
the cross-cutting flexibility for free - an initiative spans products because its
projects' changes each carry their own product, and a product appears in many
initiatives for the same reason. No association records needed.

That is also what lets a *session* be scoped to an initiative rather than pinned to one
product (see sessions.Session.initiative_id): the set of products an initiative touches
is derived from its changes, so it can be discovered as the work goes instead of declared
up front. Such a session has no project of its own at first, which is what
`ensure_initiative_catch_all` exists for - see the note there on why its spend gets a
real project rather than being recorded against the initiative directly.

Products are persistent (they come from config). Goals, Initiatives and Projects are
temporary: they open and close, so each carries a lifecycle status.

Storage mirrors roadmap.py and sessions.py: one server-owned JSON file under
`workspace/`, read and written only by the single always-running process, so every
per-session git worktree sees the same portfolio instead of its own drifting copy.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Literal

from .config import CONFIG
from .workspace_lock import warn_if_held

# Same import, same reason as roadmap.py: what makes two spellings of a ticket key the
# SAME ticket is trackers.py's one rule, and the project⇄epic 1:1 below is only as good
# as that definition. (trackers.py imports only config - no cycle.)
from .trackers import normalize_key

PORTFOLIO_PATH = CONFIG.workspace_dir / "portfolio.json"

Lifecycle = Literal["open", "closed"]
LIFECYCLES: tuple[Lifecycle, ...] = ("open", "closed")

# Projects alone get a third, pre-delivery state. Ideation is a declared phase, not an
# activity signal: it says "the absence of changes here is expected, not neglect", so
# the board can stop reading an idea being researched as a dead project. Goals and
# initiatives never carry it - an initiative is "in ideation" only derivatively, when
# every real project under it is (see PortfolioStore.initiative_in_ideation), which is
# what makes graduating ONE project flip the initiative without touching it.
ProjectLifecycle = Literal["ideation", "open", "closed"]
PROJECT_LIFECYCLES: tuple[ProjectLifecycle, ...] = ("ideation", "open", "closed")

# Defaults for the catch-all scaffold. They are only defaults - every deployment names
# its own, and the labels are the operator's, never this package's opinion about how a
# company organizes work.
DEFAULT_MAINTENANCE_GOAL = "Keep the product healthy"
DEFAULT_MAINTENANCE_INITIATIVE = "Maintenance & operations"
DEFAULT_CATCH_ALL_PROJECT = "Unplanned work"


class PortfolioError(Exception):
    """A rejected portfolio operation. The message is safe to show a user."""


class EpicAlreadyLinked(Exception):
    """Raised when an epic ticket is already linked to a different project.

    The project⇄epic link is 1:1 in both directions, exactly like the change⇄ticket
    link (see roadmap.TicketAlreadyLinked, whose shape this mirrors): one project holds
    at most one ticket by construction, and this is the check that stops two projects
    naming the same epic. Carries the conflicting project so the caller can say WHICH
    project already owns it.
    """

    def __init__(self, tracker_id: str, ticket_key: str, project: "Project") -> None:
        self.tracker_id = tracker_id
        self.ticket_key = ticket_key
        self.project = project
        super().__init__(
            f"{ticket_key} is already linked to the project \"{project.title}\" "
            f"(id {project.id}). An epic can be linked to one project only - "
            "unlink it there first."
        )


def _now() -> float:
    return time.time()


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


@dataclass
class Goal:
    id: str
    title: str
    description: str
    status: Lifecycle
    created_at: float
    updated_at: float
    closed_at: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Goal":
        return cls(**data)


@dataclass
class Initiative:
    id: str
    title: str
    description: str
    status: Lifecycle
    # The one many-to-many edge in the model: an initiative may serve several goals.
    # Empty means it serves none yet, which makes it unaligned.
    goal_ids: list[str] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0
    closed_at: float | None = None
    # True for the always-open initiative that parents the catch-all project. Never
    # closed and never deleted, so unplanned work always has somewhere to land.
    is_maintenance: bool = False

    @property
    def is_unaligned(self) -> bool:
        return not self.goal_ids

    def to_dict(self) -> dict:
        return asdict(self)

    def to_public_dict(self) -> dict:
        data = self.to_dict()
        data["is_unaligned"] = self.is_unaligned
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Initiative":
        return cls(**data)


@dataclass
class Project:
    id: str
    title: str
    description: str
    status: ProjectLifecycle
    # Exactly one initiative, or None. None is the "unaligned" state: permitted (so a
    # bug fix is never blocked on someone inventing a strategy first) but reported.
    initiative_id: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    closed_at: float | None = None
    # The single catch-all. Unplanned work lands here rather than floating unparented.
    # Cannot be deleted; there is at most one.
    is_catch_all: bool = False
    # Set (to that initiative's id) on the per-initiative catch-all: where an
    # initiative-scoped session's activity is attributed while it has no project of its
    # own. Distinct from `is_catch_all`, and the reason it must be: the global catch-all
    # hangs off the MAINTENANCE initiative, so reusing it for a session pinned to some
    # other initiative would bill that initiative's spend to maintenance. Identified by
    # this field rather than by title so renaming it can't orphan the attribution.
    # Optional and defaulted, so portfolio.json files written before it existed load
    # unchanged.
    catch_all_for_initiative: str | None = None
    # The 1:1 link to the EPIC-level ticket this project is tracked as in one configured
    # external tracker (see trackers.py) - the project-rung counterpart of the
    # change⇄ticket link on RoadmapItem, with the same split: only the reference lives
    # here, and the ticket's type/title/state/URL come from the synced catalog at read
    # time. Both None means the project exists ONLY here: created locally and pending
    # upload to the tracker - a real, reportable state, not an error, since the upload
    # direction of the sync is not built yet. Defaults keep pre-existing portfolio.json
    # files loading untouched.
    tracker_id: str | None = None
    ticket_key: str | None = None

    @property
    def is_unaligned(self) -> bool:
        return self.initiative_id is None

    @property
    def is_any_catch_all(self) -> bool:
        """True for the global catch-all and for any per-initiative one. What the delete
        guards test: both are auto-created plumbing, neither is a project someone made."""
        return self.is_catch_all or self.catch_all_for_initiative is not None

    def to_dict(self) -> dict:
        return asdict(self)

    def to_public_dict(self) -> dict:
        data = self.to_dict()
        data["is_unaligned"] = self.is_unaligned
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Project":
        return cls(**data)


class PortfolioStore:
    """Goals, initiatives and projects for one deployment.

    Mirrors RoadmapStore's conventions (single lock, subscriber fan-out, whole-file
    JSON writes) so the two stores behave the same way under the same server.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._path = path or PORTFOLIO_PATH
        self._goals: dict[str, Goal] = {}
        self._initiatives: dict[str, Initiative] = {}
        self._projects: dict[str, Project] = {}
        self._subscribers: list[Callable[[dict], None]] = []
        # Same warning as RoadmapStore's, for the same reason: whole-file writes assume
        # this process owns the workspace (see workspace_lock).
        if path is None:
            warn_if_held()
        self._load()

    # ---- persistence ----

    def _load(self) -> None:
        if not self._path.exists():
            return
        raw = json.loads(self._path.read_text())
        self._goals = {g["id"]: Goal.from_dict(g) for g in raw.get("goals", [])}
        self._initiatives = {
            i["id"]: Initiative.from_dict(i) for i in raw.get("initiatives", [])
        }
        self._projects = {p["id"]: Project.from_dict(p) for p in raw.get("projects", [])}

    def _save(self) -> None:
        """Caller holds the lock. Written via a temp file and replaced, so a crash
        mid-write cannot leave a half-serialized portfolio behind."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "goals": [g.to_dict() for g in self._sorted(self._goals)],
            "initiatives": [i.to_dict() for i in self._sorted(self._initiatives)],
            "projects": [p.to_dict() for p in self._sorted(self._projects)],
        }
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(self._path)

    @staticmethod
    def _sorted(records: dict):
        return sorted(records.values(), key=lambda r: (r.created_at, r.id))

    # ---- broadcast (mirrors RoadmapStore) ----

    def subscribe(self, callback: Callable[[dict], None]) -> None:
        self._subscribers.append(callback)

    def _notify(self, event: dict) -> None:
        for callback in list(self._subscribers):
            callback(event)

    # ---- reads ----

    @property
    def is_empty(self) -> bool:
        return not (self._goals or self._initiatives or self._projects)

    def list_goals(self) -> list[dict]:
        return [g.to_dict() for g in self._sorted(self._goals)]

    def list_initiatives(self) -> list[dict]:
        return [self._initiative_public(i) for i in self._sorted(self._initiatives)]

    def _initiative_public(self, initiative: Initiative) -> dict:
        """The public dict plus the derived `in_ideation` flag - every read path that
        hands an initiative to a page or a session goes through here, so no consumer
        can see an initiative without the flag riding along."""
        data = initiative.to_public_dict()
        data["in_ideation"] = self.initiative_in_ideation(initiative)
        return data

    def initiative_in_ideation(self, initiative: Initiative) -> bool:
        """Derived, never stored: an initiative is in ideation when every project that
        counts under it is. Deriving it is what keeps it honest - the moment one
        project graduates to "open", the initiative reads as delivering without anyone
        editing the initiative, and it can never drift out of sync with its projects.

        Two exclusions make "every project that counts" mean what a stakeholder means:
        catch-alls are attribution plumbing every initiative has (counting them would
        make the state unreachable), and closed projects are history (an initiative
        that shipped three things and is now exploring its next wave is back in
        ideation). Vacuously true for an initiative with no real projects yet - a
        brand-new initiative that is nothing but brainstorming so far is exactly the
        thing this flag exists to represent. The maintenance initiative is never in
        ideation: it is standing infrastructure, not a bet being formed.
        """
        if initiative.status == "closed" or initiative.is_maintenance:
            return False
        counted = [
            p
            for p in self._projects.values()
            if p.initiative_id == initiative.id
            and not p.is_any_catch_all
            and p.status != "closed"
        ]
        return all(p.status == "ideation" for p in counted)

    def list_projects(self) -> list[dict]:
        return [p.to_public_dict() for p in self._sorted(self._projects)]

    def snapshot(self) -> dict:
        """Everything, in one payload - what the portfolio page and the roadmap's
        initiative pivot both read."""
        return {
            "goals": self.list_goals(),
            "initiatives": self.list_initiatives(),
            "projects": self.list_projects(),
            "catch_all_project_id": self.catch_all_project_id,
            "unaligned": self.unaligned_report(),
            # Which open projects have no epic link yet (see pending_upload_report).
            # Additive; consumers on a deployment with no [[trackers]] ignore it.
            "pending_upload": self.pending_upload_report(),
        }

    def get_goal(self, goal_id: str) -> Goal | None:
        return self._goals.get(goal_id)

    def get_initiative(self, initiative_id: str) -> Initiative | None:
        return self._initiatives.get(initiative_id)

    def get_project(self, project_id: str) -> Project | None:
        return self._projects.get(project_id)

    def project_exists(self, project_id: str) -> bool:
        return project_id in self._projects

    def project_for_ticket(self, tracker_id: str, ticket_key: str) -> Project | None:
        """Which project holds this epic, if any - the read side of the 1:1 link,
        mirroring roadmap.RoadmapStore.item_for_ticket."""
        wanted = normalize_key(tracker_id, ticket_key)
        return next(
            (
                p
                for p in self._projects.values()
                if p.tracker_id == tracker_id
                and p.ticket_key
                and normalize_key(tracker_id, p.ticket_key) == wanted
            ),
            None,
        )

    def linked_ticket_refs(self) -> list[tuple[str, str]]:
        """Every (tracker_id, key) a project is linked to, for the sync to keep fresh -
        joined with the roadmap's own refs by the caller (see server._run_tracker_sync)."""
        return [
            (p.tracker_id, p.ticket_key)
            for p in self._projects.values()
            if p.tracker_id and p.ticket_key
        ]

    def pending_upload_report(self) -> list[dict]:
        """Open projects that exist only here: no epic link, so nothing in the tracker
        knows about them yet. Reported, never blocked - same posture as unaligned_report:
        this is the work statement, not an error list.

        Still a report rather than an action even now that pushing exists (see
        server.push_project_epic): whether a project deserves an epic on a shared board,
        and when, is a product decision, so this names the candidates and a human presses
        the button. Catch-alls are exempt: they are auto-created plumbing for cost
        attribution, not projects anyone would file an epic for. Ideation projects are
        exempt too, deliberately: pre-commitment ideas have nothing to file an epic
        for yet - graduating to "open" is the moment a project earns a ticket, and
        that is when it starts appearing here."""
        return [
            {"id": p.id, "title": p.title}
            for p in self._sorted(self._projects)
            if p.status == "open"
            and p.tracker_id is None
            and not p.is_any_catch_all
        ]

    @property
    def catch_all_project_id(self) -> str | None:
        for project in self._projects.values():
            if project.is_catch_all:
                return project.id
        return None

    def catch_all_project_for_initiative(self, initiative_id: str | None) -> str | None:
        """The per-initiative catch-all's project id, or None if there isn't one yet.

        A pure read, deliberately: this is called on the hot path that records every
        activity signal (see server.py's _session_project_id), which must never write,
        lock for long, or broadcast. `ensure_initiative_catch_all` does the creating, once,
        when a session is pinned to the initiative.
        """
        if initiative_id is None:
            return None
        for project in self._projects.values():
            if project.catch_all_for_initiative == initiative_id:
                return project.id
        return None

    def ensure_initiative_catch_all(self, initiative_id: str) -> Project:
        """Finds or creates the project an initiative-scoped session's work is attributed
        to while it has no project of its own. Idempotent.

        Why a real project rather than attributing to the initiative directly: the additive
        tree's exactness rests on Change -> Project -> Initiative being a unique path (see
        this module's docstring). Recording spend one level up would leave the by-project
        rollup with a hole that the by-initiative totals don't have. A per-initiative
        catch-all keeps one code path, one shape, and rollups that still sum.
        """
        with self._lock:
            initiative = self._initiatives.get(initiative_id)
            if initiative is None:
                raise PortfolioError(f"Unknown initiative: {initiative_id}")
            for project in self._projects.values():
                if project.catch_all_for_initiative == initiative_id:
                    return project
            project = Project(
                id=_new_id(),
                title=f"Unplanned work — {initiative.title}",
                description=(
                    "Auto-created. Holds work done in a session scoped to this "
                    "initiative before it names a specific project."
                ),
                status="open",
                initiative_id=initiative_id,
                created_at=_now(),
                updated_at=_now(),
                catch_all_for_initiative=initiative_id,
            )
            self._projects[project.id] = project
            self._save()
        self._notify({"type": "portfolio_changed", "entity": "project", "id": project.id})
        return project

    def initiative_scope(self, initiative_id: str) -> dict | None:
        """What an initiative-scoped session needs to know about its own initiative:
        the initiative itself, the goals it serves, and its projects in creation order.

        Returned as plain data so the caller can join it against the roadmap's changes
        without this module learning what a change is (see roadmap.describe_initiative).
        None for an unknown id - a session can outlive the initiative it was pinned to,
        and every reader of this treats that as "unscoped", not as an error.
        """
        initiative = self._initiatives.get(initiative_id)
        if initiative is None:
            return None
        return {
            "initiative": self._initiative_public(initiative),
            "goals": [
                {"id": g.id, "title": g.title}
                for g in self._sorted(self._goals)
                if g.id in initiative.goal_ids
            ],
            "projects": [
                p.to_public_dict()
                for p in self._sorted(self._projects)
                if p.initiative_id == initiative_id
            ],
        }

    def unaligned_report(self) -> dict:
        """Projects with no initiative, and initiatives serving no goal. Alignment is
        mandatory as a practice, so the system reports the gap rather than blocking the
        work that created it."""
        return {
            # Ideation counts: an idea nobody has aligned to an initiative is still a
            # reportable gap, and surfacing it early is cheaper than after graduation.
            "projects": [
                {"id": p.id, "title": p.title}
                for p in self._sorted(self._projects)
                if p.is_unaligned and p.status != "closed"
            ],
            "initiatives": [
                {"id": i.id, "title": i.title}
                for i in self._sorted(self._initiatives)
                if i.is_unaligned and i.status == "open"
            ],
        }

    # ---- rollup ----

    def rollup_path(self, project_id: str | None) -> dict:
        """The unique parent chain for a change, given its project.

        Returns project/initiative ids (either may be None when unaligned). Goals are
        deliberately NOT included: an initiative can serve several, so folding them in
        here would invite double-counting downstream. Use
        `goal_ids_for_initiative` explicitly, and never sum across goals.
        """
        if project_id is None:
            return {"project_id": None, "initiative_id": None}
        project = self._projects.get(project_id)
        if project is None:
            return {"project_id": None, "initiative_id": None}
        return {"project_id": project.id, "initiative_id": project.initiative_id}

    def goal_ids_for_initiative(self, initiative_id: str | None) -> list[str]:
        """The goals an initiative serves. Overlapping by design - see this module's
        docstring on why goal-level totals are not additive."""
        if initiative_id is None:
            return []
        initiative = self._initiatives.get(initiative_id)
        return list(initiative.goal_ids) if initiative is not None else []

    def projects_of_initiative(self, initiative_id: str) -> list[str]:
        return [
            p.id for p in self._sorted(self._projects) if p.initiative_id == initiative_id
        ]

    # ---- validation helpers ----

    def _validate_status(self, status: str) -> Lifecycle:
        if status not in LIFECYCLES:
            raise PortfolioError(
                f"Unknown status: {status!r}. Must be one of: {', '.join(LIFECYCLES)}"
            )
        return status  # type: ignore[return-value]

    def _validate_project_status(self, status: str) -> ProjectLifecycle:
        if status not in PROJECT_LIFECYCLES:
            raise PortfolioError(
                f"Unknown status: {status!r}. Must be one of: "
                + ", ".join(PROJECT_LIFECYCLES)
            )
        return status  # type: ignore[return-value]

    def _validate_title(self, title: str) -> str:
        cleaned = (title or "").strip()
        if not cleaned:
            raise PortfolioError("A title is required.")
        return cleaned

    def _validate_goal_ids(self, goal_ids) -> list[str]:
        if goal_ids is None:
            return []
        cleaned = []
        for goal_id in goal_ids:
            if goal_id not in self._goals:
                raise PortfolioError(f"Unknown goal: {goal_id}")
            if goal_id not in cleaned:
                cleaned.append(goal_id)
        return cleaned

    def _validate_initiative_id(self, initiative_id: str | None) -> str | None:
        if initiative_id is None or initiative_id == "":
            return None
        if initiative_id not in self._initiatives:
            raise PortfolioError(f"Unknown initiative: {initiative_id}")
        return initiative_id

    # ---- goals ----

    def create_goal(self, title: str, description: str = "") -> Goal:
        with self._lock:
            goal = Goal(
                id=_new_id(),
                title=self._validate_title(title),
                description=(description or "").strip(),
                status="open",
                created_at=_now(),
                updated_at=_now(),
            )
            self._goals[goal.id] = goal
            self._save()
        self._notify({"type": "portfolio_changed", "entity": "goal", "id": goal.id})
        return goal

    def update_goal(
        self,
        goal_id: str,
        title: str | None = None,
        description: str | None = None,
        status: str | None = None,
    ) -> Goal:
        with self._lock:
            goal = self._goals.get(goal_id)
            if goal is None:
                raise PortfolioError(f"Unknown goal: {goal_id}")
            if title is not None:
                goal.title = self._validate_title(title)
            if description is not None:
                goal.description = description.strip()
            if status is not None:
                goal.status = self._validate_status(status)
                goal.closed_at = _now() if goal.status == "closed" else None
            goal.updated_at = _now()
            self._save()
        self._notify({"type": "portfolio_changed", "entity": "goal", "id": goal_id})
        return goal

    def delete_goal(self, goal_id: str) -> None:
        """Refuses while any initiative still serves this goal. Silently unlinking
        would quietly change what those initiatives are for."""
        with self._lock:
            if goal_id not in self._goals:
                raise PortfolioError(f"Unknown goal: {goal_id}")
            serving = [i.title for i in self._initiatives.values() if goal_id in i.goal_ids]
            if serving:
                raise PortfolioError(
                    "Still serving this goal: "
                    + ", ".join(serving)
                    + ". Unlink them first."
                )
            del self._goals[goal_id]
            self._save()
        self._notify({"type": "portfolio_changed", "entity": "goal", "id": goal_id})

    # ---- initiatives ----

    def create_initiative(
        self,
        title: str,
        description: str = "",
        goal_ids: list[str] | None = None,
        is_maintenance: bool = False,
    ) -> Initiative:
        with self._lock:
            initiative = Initiative(
                id=_new_id(),
                title=self._validate_title(title),
                description=(description or "").strip(),
                status="open",
                goal_ids=self._validate_goal_ids(goal_ids),
                created_at=_now(),
                updated_at=_now(),
                is_maintenance=is_maintenance,
            )
            self._initiatives[initiative.id] = initiative
            self._save()
        self._notify(
            {"type": "portfolio_changed", "entity": "initiative", "id": initiative.id}
        )
        return initiative

    def update_initiative(
        self,
        initiative_id: str,
        title: str | None = None,
        description: str | None = None,
        status: str | None = None,
        goal_ids: list[str] | None = None,
    ) -> Initiative:
        with self._lock:
            initiative = self._initiatives.get(initiative_id)
            if initiative is None:
                raise PortfolioError(f"Unknown initiative: {initiative_id}")
            if title is not None:
                initiative.title = self._validate_title(title)
            if description is not None:
                initiative.description = description.strip()
            if status is not None:
                new_status = self._validate_status(status)
                if new_status == "closed" and initiative.is_maintenance:
                    # The catch-all's parent must stay open or unplanned work has
                    # nowhere aligned to land.
                    raise PortfolioError(
                        "The maintenance initiative stays open - it parents the "
                        "catch-all project."
                    )
                initiative.status = new_status
                initiative.closed_at = _now() if new_status == "closed" else None
            if goal_ids is not None:
                initiative.goal_ids = self._validate_goal_ids(goal_ids)
            initiative.updated_at = _now()
            self._save()
        self._notify(
            {"type": "portfolio_changed", "entity": "initiative", "id": initiative_id}
        )
        return initiative

    def delete_initiative(self, initiative_id: str) -> None:
        with self._lock:
            initiative = self._initiatives.get(initiative_id)
            if initiative is None:
                raise PortfolioError(f"Unknown initiative: {initiative_id}")
            if initiative.is_maintenance:
                raise PortfolioError("The maintenance initiative cannot be deleted.")
            # Its own auto-created catch-all doesn't count as a reason to refuse - it was
            # never a project anyone declared, and demanding it be moved first would make
            # every initiative that ever hosted a scoped session undeletable. It goes with
            # the initiative. A catch-all still holding changes is caught below, since
            # those changes need a real home either way.
            own_catch_all = [
                p for p in self._projects.values()
                if p.catch_all_for_initiative == initiative_id
            ]
            children = [
                p.title for p in self._projects.values()
                if p.initiative_id == initiative_id
                and p.catch_all_for_initiative != initiative_id
            ]
            if children:
                raise PortfolioError(
                    "Projects still belong to this initiative: "
                    + ", ".join(children)
                    + ". Move or delete them first."
                )
            for project in own_catch_all:
                del self._projects[project.id]
            del self._initiatives[initiative_id]
            self._save()
        self._notify(
            {"type": "portfolio_changed", "entity": "initiative", "id": initiative_id}
        )

    # ---- projects ----

    def create_project(
        self,
        title: str,
        description: str = "",
        initiative_id: str | None = None,
        is_catch_all: bool = False,
        status: str = "open",
    ) -> Project:
        """`status="ideation"` creates the project as a declared idea: real, visible,
        expected to have no changes yet. Catch-alls are always open - they are
        attribution plumbing, and plumbing is never an idea."""
        with self._lock:
            if is_catch_all and self.catch_all_project_id is not None:
                raise PortfolioError("A catch-all project already exists.")
            project = Project(
                id=_new_id(),
                title=self._validate_title(title),
                description=(description or "").strip(),
                status="open" if is_catch_all else self._validate_project_status(status),
                initiative_id=self._validate_initiative_id(initiative_id),
                created_at=_now(),
                updated_at=_now(),
                is_catch_all=is_catch_all,
            )
            self._projects[project.id] = project
            self._save()
        self._notify({"type": "portfolio_changed", "entity": "project", "id": project.id})
        return project

    def update_project(
        self,
        project_id: str,
        title: str | None = None,
        description: str | None = None,
        status: str | None = None,
        initiative_id: str | None = None,
        clear_initiative: bool = False,
    ) -> Project:
        """`initiative_id` re-parents; `clear_initiative=True` makes the project
        unaligned on purpose. The two are separate because None already means "no
        change" for every other field."""
        with self._lock:
            project = self._projects.get(project_id)
            if project is None:
                raise PortfolioError(f"Unknown project: {project_id}")
            if title is not None:
                project.title = self._validate_title(title)
            if description is not None:
                project.description = description.strip()
            if status is not None:
                new_status = self._validate_project_status(status)
                if new_status == "closed" and project.is_catch_all:
                    raise PortfolioError(
                        "The catch-all project stays open - unplanned work lands there."
                    )
                if new_status == "ideation" and project.is_any_catch_all:
                    raise PortfolioError(
                        "A catch-all project cannot be in ideation - it is attribution "
                        "plumbing, not an idea."
                    )
                project.status = new_status
                project.closed_at = _now() if new_status == "closed" else None
            if (clear_initiative or initiative_id is not None) and project.is_any_catch_all:
                raise PortfolioError(
                    "A catch-all project cannot move between initiatives - where it "
                    "hangs is what makes its spend attribution mean anything."
                )
            if clear_initiative:
                project.initiative_id = None
            elif initiative_id is not None:
                project.initiative_id = self._validate_initiative_id(initiative_id)
            project.updated_at = _now()
            self._save()
        self._notify({"type": "portfolio_changed", "entity": "project", "id": project_id})
        return project

    def link_epic(self, project_id: str, tracker_id: str, ticket_key: str) -> Project:
        """Links a project to one epic-level ticket, 1:1.

        Same contract as roadmap.RoadmapStore.link_ticket, on purpose: re-linking the
        same pair is a no-op rather than a conflict against the project itself, a
        DIFFERENT project already holding the ticket raises EpicAlreadyLinked, and the
        check and the write share one lock hold so two concurrent links cannot both
        pass. Whether the ticket actually IS an epic is the caller's check (server.py) -
        this store deliberately knows nothing about the ticket catalog.

        Catch-alls are refused: they are auto-created attribution plumbing, and pinning
        one to a tracker epic would present unplanned-work bookkeeping as a planned
        deliverable in the other system.
        """
        key = normalize_key(tracker_id, ticket_key)
        if not tracker_id or not key:
            raise PortfolioError(
                "tracker_id and ticket_key are both required to link an epic"
            )
        with self._lock:
            project = self._projects.get(project_id)
            if project is None:
                raise PortfolioError(f"Unknown project: {project_id}")
            if project.is_any_catch_all:
                raise PortfolioError(
                    "A catch-all project cannot be linked to an epic - it is "
                    "auto-created plumbing for unplanned work, not a deliverable "
                    "the tracker should know about."
                )
            for other in self._projects.values():
                if other.id == project_id or other.tracker_id != tracker_id or not other.ticket_key:
                    continue
                if normalize_key(tracker_id, other.ticket_key) == key:
                    raise EpicAlreadyLinked(tracker_id, key, other)
            project.tracker_id = tracker_id
            project.ticket_key = key
            project.updated_at = _now()
            self._save()
        self._notify({"type": "portfolio_changed", "entity": "project", "id": project_id})
        return project

    def unlink_epic(self, project_id: str) -> Project:
        """Clears the epic link, returning the project to the local-only (pending
        upload) state. Idempotent, like unlink_ticket: clearing a clear field is not
        an error."""
        with self._lock:
            project = self._projects.get(project_id)
            if project is None:
                raise PortfolioError(f"Unknown project: {project_id}")
            project.tracker_id = None
            project.ticket_key = None
            project.updated_at = _now()
            self._save()
        self._notify({"type": "portfolio_changed", "entity": "project", "id": project_id})
        return project

    def delete_project(self, project_id: str, change_count: int = 0) -> None:
        """`change_count` is supplied by the caller (the roadmap store owns changes, so
        this module never reaches into it) and blocks deleting a project that still has
        work under it."""
        with self._lock:
            project = self._projects.get(project_id)
            if project is None:
                raise PortfolioError(f"Unknown project: {project_id}")
            if project.is_catch_all:
                raise PortfolioError("The catch-all project cannot be deleted.")
            if project.catch_all_for_initiative is not None:
                raise PortfolioError(
                    "This is an initiative's catch-all project - it is removed with the "
                    "initiative itself, not on its own."
                )
            if change_count:
                raise PortfolioError(
                    f"{change_count} change(s) still belong to this project. "
                    "Move them first."
                )
            del self._projects[project_id]
            self._save()
        self._notify({"type": "portfolio_changed", "entity": "project", "id": project_id})

    # ---- pivots ----

    def group_changes_by_initiative(self, changes: list[dict]) -> list[dict]:
        """Regroups roadmap items (changes) under Initiative -> Project.

        The second lens on the same dataset: the board's own view groups by product,
        this one walks the work model. Takes the changes as an argument rather than
        reaching into the roadmap store, so this module stays independent of it and the
        grouping is testable on its own.

        Every change lands exactly once. Two synthetic groups catch what the tree
        doesn't reach, so nothing silently disappears from the board when you switch
        lens:

        - a project whose `initiative_id` is None appears under an "unaligned"
          initiative group (`initiative: None`)
        - a change whose `project_id` is None (or points at a deleted project) appears
          under an "unassigned" project group (`project: None`)
        """
        by_project: dict[str | None, list[dict]] = {}
        for change in changes:
            project_id = change.get("project_id")
            if project_id not in self._projects:
                project_id = None
            by_project.setdefault(project_id, []).append(change)

        groups: list[dict] = []

        def project_entry(project: Project) -> dict:
            return {
                "project": project.to_public_dict(),
                "changes": by_project.get(project.id, []),
            }

        # Real initiatives, in creation order, each with its own projects.
        for initiative in self._sorted(self._initiatives):
            children = [
                p for p in self._sorted(self._projects) if p.initiative_id == initiative.id
            ]
            groups.append(
                {
                    "initiative": self._initiative_public(initiative),
                    "projects": [project_entry(p) for p in children],
                }
            )

        # Everything the tree doesn't reach goes into ONE trailing group, not two:
        # projects with no initiative, then changes with no (or a dangling) project.
        loose = [project_entry(p) for p in self._sorted(self._projects) if p.is_unaligned]
        unassigned = by_project.get(None, [])
        if unassigned:
            loose.append({"project": None, "changes": unassigned})
        if loose:
            groups.append({"initiative": None, "projects": loose})
        return groups

    # ---- the maintenance scaffold ----

    def ensure_maintenance_scaffold(
        self,
        goal_title: str = DEFAULT_MAINTENANCE_GOAL,
        initiative_title: str = DEFAULT_MAINTENANCE_INITIATIVE,
        project_title: str = DEFAULT_CATCH_ALL_PROJECT,
    ) -> dict:
        """Creates the goal + always-open initiative + catch-all project that unplanned
        work rolls up into, and returns their ids.

        This exists because the two rules interact: goals and initiatives are never
        auto-created, but the catch-all project needs a parent. So an operator declares
        this trio once, explicitly, and from then on a bug fix has somewhere aligned to
        land instead of floating. Idempotent - running it again returns what is already
        there rather than making a second catch-all.
        """
        existing_catch_all = self.catch_all_project_id
        if existing_catch_all is not None:
            project = self._projects[existing_catch_all]
            initiative_id = project.initiative_id
            goal_ids = self.goal_ids_for_initiative(initiative_id)
            return {
                "goal_id": goal_ids[0] if goal_ids else None,
                "initiative_id": initiative_id,
                "project_id": project.id,
                "created": False,
            }
        goal = self.create_goal(goal_title, "Keep existing work healthy and unblocked.")
        initiative = self.create_initiative(
            initiative_title,
            "Always open. Parents unplanned work so nothing is left unaligned.",
            goal_ids=[goal.id],
            is_maintenance=True,
        )
        project = self.create_project(
            project_title,
            "Where unplanned changes land when no specific project fits.",
            initiative_id=initiative.id,
            is_catch_all=True,
        )
        return {
            "goal_id": goal.id,
            "initiative_id": initiative.id,
            "project_id": project.id,
            "created": True,
        }
