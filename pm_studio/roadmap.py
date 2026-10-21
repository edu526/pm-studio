import json
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Iterable, Literal

from .config import CONFIG, CONFIG_FILE_NAME, LOCAL_DIR_NAME, ProductMeta, SystemSpec

# Imported for the one rule that must not be re-implemented per caller: what makes two
# spellings of a ticket key the SAME ticket. The 1:1 guarantee below is only as good as
# that definition, so it lives in one place and the store applies it itself rather than
# trusting callers to normalise first. (trackers.py imports only config - no cycle.)
from .trackers import normalize_key
from .workspace_lock import warn_if_held

ROADMAP_DIR = CONFIG.workspace_dir / "roadmap"

Bucket = Literal["now", "next", "later"]
Status = Literal["pending", "in_progress", "done"]

# The product taxonomy for this deployment, from pm_studio_local/config.toml's
# [products] table (declaration order is display order, laid out depth first so each
# product is followed by its own descendants). Empty when the target repo hasn't declared
# products yet - sessions then run unpinned with no per-product boards.
PRODUCTS: dict[str, str] = CONFIG.products

# The hierarchy: child id -> parent id, for children only (see config._parse_products).
# Empty for a flat taxonomy, and every helper below then answers exactly what it
# answered before hierarchy existed - a product with no parent and no children is its
# own whole subtree.
PRODUCT_PARENTS: dict[str, str] = CONFIG.product_parents

# A product is a product wherever it sits: children get their own board file, their own
# sessions and their own items, and every URL still names one product id. The parent
# pointer organizes them and does two things that matter beyond display - it decides
# which boards a pinned PM sees at full depth (subtree_products, used by
# describe_own_product) and how the board nests its sections.
#
# The tree is as deep as the deployment declared. Nothing here counts levels: every
# helper below either walks one hop (parent_of) or recurses to the bottom
# (subtree_products, ancestors_of), so a three-level taxonomy needs no special case and
# neither would a five-level one. Config guarantees the one property that matters for
# termination: every chain ends at a top-level product, with cycles refused at load.


def parent_of(product: str) -> str | None:
    return PRODUCT_PARENTS.get(product)


def children_of(product: str) -> list[str]:
    """Direct children, in display order (PRODUCTS is already ordered)."""
    return [p for p in PRODUCTS if PRODUCT_PARENTS.get(p) == product]


def top_level_products() -> list[str]:
    return [p for p in PRODUCTS if p not in PRODUCT_PARENTS]


def subtree_products(product: str) -> list[str]:
    """The product itself followed by its descendants at every depth, in display order.

    The unit of ownership: a PM pinned to a parent owns everything below it, so this is
    what "my roadmap" means for context (describe_own_product), for what the awareness
    digest must leave out (describe_other_products), and for which boards that session may
    write to (agent.py's allowlist). For a leaf - or any product on a flat taxonomy - it is
    just `[product]`, which is why nothing had to change shape when hierarchy arrived.

    Cycle-guarded for the same reason as ancestors_of: config refuses a cyclic taxonomy at
    load, but this runs inside a PM's turn and inside the board's render, and a hang there
    is a far worse way to learn about a bad config than a short answer.
    """
    family: list[str] = []
    seen: set[str] = set()

    def walk(pid: str) -> None:
        if pid in seen:
            return
        seen.add(pid)
        family.append(pid)
        for child in children_of(pid):
            walk(child)

    walk(product)
    return family


def owned_subtrees(products: Iterable[str]) -> list[str]:
    """The union of several products' subtrees, in display order, each named once.

    One pinned product is the common case, but an initiative-scoped session owns a *set*
    of boards that grows as it identifies which products an initiative touches (see
    sessions.Session.adopted_products). Ownership still means "this product and
    everything below it", so the set is a union of subtrees rather than a flat list -
    adopting a parent brings its children with it, exactly as pinning one always has.

    Unknown ids are dropped rather than raising: a board can outlive the config line
    that named it, and this runs inside a PM's turn where a short answer beats an
    exception. Order follows PRODUCTS (declaration order), not adoption order, so the
    context block and the allowlist read as the taxonomy does.
    """
    owned: set[str] = set()
    for product in products:
        if product in PRODUCTS:
            owned.update(subtree_products(product))
    return [p for p in PRODUCTS if p in owned]


def product_label(product: str) -> str:
    """The display label, falling back to the id for a product that is no longer
    declared - an item can outlive the config line that named its board."""
    return PRODUCTS.get(product, product)


def ancestors_of(product: str) -> list[str]:
    """Every product above this one, nearest parent first. Empty for a top-level product.

    Terminates because config refuses a cycle (see config._parse_products); the `seen`
    guard is belt to those braces, since a store can be handed a patched taxonomy in
    tests and an infinite loop inside a PM's turn is a hang, not an error.
    """
    chain: list[str] = []
    seen = {product}
    cursor = parent_of(product)
    while cursor is not None and cursor not in seen:
        chain.append(cursor)
        seen.add(cursor)
        cursor = parent_of(cursor)
    return chain


def product_path_label(product: str) -> str:
    """"Web App / Auth & Identity / SSO" - the full path down to this product, plain
    label for a top-level one.

    Used wherever a product name appears with no surrounding section to say where it
    sits: a cross-product suggestion flag, another product's digest line, a change's chip
    in the initiative lens. The whole path rather than just the parent, because at three
    levels "Auth & Identity / SSO" still does not say whose auth - and these are the
    places with no section heading to supply the rest.
    """
    ancestors = ancestors_of(product)
    if not ancestors:
        return product_label(product)
    return " / ".join(product_label(p) for p in [*reversed(ancestors), product])


# The SYSTEM taxonomy, from config.toml's [systems] table (see config._parse_systems for
# what separates a system from a product). A system is the bounded piece of technology a
# change is contained within; a product is the business-facing thing that several systems
# together serve.
#
# Systems deliberately own no boards. Roadmaps are product-first and initiative-first,
# and work that belongs to a system rather than a product - infra, performance - is
# expressed as an initiative, which is what initiatives already are. So `system` is an
# ATTRIBUTE of a change, never its home: the change still lives on exactly one product
# board, exactly as it did before this table existed.
#
# Empty on every deployment that has not declared [systems], and empty is what keeps the
# whole layer dormant: no attribution is required, nothing new appears in the UI, and
# every function below answers what it answered before.
SYSTEMS: dict[str, SystemSpec] = CONFIG.systems

# The many-to-many edge, product id -> the systems it touches, in declaration order.
PRODUCT_SYSTEMS: dict[str, tuple[str, ...]] = CONFIG.product_systems

# Ids mid-reclassification from product to system - declared in both tables. Their
# product board keeps loading and stays writable so its changes can be re-homed at the
# operator's pace; see config.Config.transitional_ids.
TRANSITIONAL_IDS: tuple[str, ...] = CONFIG.transitional_ids

# Operator-declared facts per product (owner/team/stage/description), only for products
# that declared any - see config.ProductMeta. Context, not policy: consumers show these,
# nothing enforces them.
PRODUCT_META: dict[str, ProductMeta] = CONFIG.product_meta


def product_meta(product: str) -> ProductMeta | None:
    """The declared facts for one product, or None - which every consumer must treat as
    "nothing declared", not as an error, since metadata is opt-in per product."""
    return PRODUCT_META.get(product)


def systems_declared() -> bool:
    """Whether this deployment uses the system layer at all. The single switch every
    caller asks instead of testing `SYSTEMS` itself, so "dormant" means one thing."""
    return bool(SYSTEMS)


def system_label(system: str) -> str:
    """Display label, falling back to the id - same reason as product_label: a change can
    outlive the config line that declared its system."""
    spec = SYSTEMS.get(system)
    return spec.label if spec else system


def systems_of_product(product: str) -> list[str]:
    """The systems a product declares it touches, in declaration order.

    Not inherited through `parent`: a child product touches what it declares, and a
    parent's list says nothing about its children's. Containment and composition are
    different questions (see config._parse_products), so this walks nothing.
    """
    return [s for s in PRODUCT_SYSTEMS.get(product, ()) if s in SYSTEMS]


def products_of_system(system: str) -> list[str]:
    """Which products touch this system, in product display order - the other side of the
    edge, derived rather than stored so the two can never disagree.

    A system touched by several products is the normal case and the reason this returns a
    list: it is exactly what makes the edge many-to-many, and exactly why per-system
    totals must never be summed across products (they would double-count the system).
    """
    return [p for p in PRODUCTS if system in PRODUCT_SYSTEMS.get(p, ())]


def requires_system(product: str) -> bool:
    """Whether a change on this product must name a system.

    Attribution is scoped PER PRODUCT, not per deployment, and this is the predicate that
    says so - asked by the store, the PM prompt and the board alike so "in scope" means
    one thing everywhere.

    The distinction is what makes the layer adoptable one product at a time. Declaring
    [systems] is a deployment-wide switch, but a taxonomy is not migrated in one sitting:
    on a deployment with two dozen products and thousands of existing changes, requiring
    attribution everywhere the moment the first system is declared would force every board
    to attribute to whichever systems happen to exist yet - which is worse than not
    attributing at all, because it manufactures wrong data instead of missing data.

    So a product is IN SCOPE once it declares what it touches, and untouched until then.
    products_missing_systems() reports the ones still outside, so "not yet declared" stays
    visible rather than becoming a silent permanent exemption.
    """
    return systems_declared() and bool(systems_of_product(product))


def validate_system(product: str, system: str | None, *, required: bool = True) -> str | None:
    """Resolves the `system` for a change on `product`, or raises ValueError saying why.

    Three rules, in order:
      1. No [systems] declared at all -> the only valid value is none. Naming one would
         silently attribute a change to a taxonomy this deployment does not have.
      2. A system is REQUIRED only where the product declares what it touches (see
         requires_system). Elsewhere none is a legitimate answer, not a violation.
      3. A system that IS named must be declared, and must be one the product touches -
         unless the product declares none, in which case any declared system is accepted.
         That last case is deliberately permissive rather than refused: attributing a
         change while its product's edge is still undeclared is useful and harmless, and
         this codebase reports gaps rather than blocking work.

    Note there is deliberately no ""-clears convention here, unlike `owner` and
    `project_id`: dropping a change's system would manufacture exactly the inconsistency
    this layer exists to remove, so an explicit empty value is an error, not a reset.
    """
    value = (system or "").strip() or None
    if not systems_declared():
        if value is not None:
            raise ValueError(
                f"This deployment declares no [systems], so a change cannot be attributed "
                f"to one (got {value!r}). Declare [systems] in "
                f"{LOCAL_DIR_NAME}/{CONFIG_FILE_NAME} first."
            )
        return None
    declared = systems_of_product(product)
    if value is None:
        if not required or not declared:
            return None
        raise ValueError(
            f"A change must name the one system it is contained within. "
            f"{product_label(product)} touches: {', '.join(declared)}"
        )
    if value not in SYSTEMS:
        raise ValueError(
            f"Unknown system: {value} (declared: {', '.join(SYSTEMS) or 'none'})"
        )
    if declared and value not in declared:
        raise ValueError(
            f"{product_label(product)} does not touch system {value!r}; it touches: "
            f"{', '.join(declared)}. If it should, add {value!r} to that product's "
            "`systems = [...]`."
        )
    return value


def products_missing_systems() -> list[str]:
    """Declared products that touch no declared system, in display order.

    The config half of the restructure gap that unattributed changes are the data half of.
    These products are OUTSIDE the scope of attribution (see requires_system): their
    changes are not required to name a system and are not counted as debt. Listing them is
    what keeps that from becoming a silent permanent exemption - it is the remaining work,
    stated.
    """
    if not systems_declared():
        return []
    return [p for p in PRODUCTS if not systems_of_product(p)]

# Schedule dates are stored as "YYYY-MM-DD" STRINGS, unlike created_at/updated_at/
# shipped_at, which are epoch floats. The difference is deliberate and not worth
# "tidying" into one type: those three are instants - the moment something happened -
# while a start or a target is a calendar date somebody committed to. Stored as an
# epoch, a target of 2026-09-30 is really 2026-09-30T00:00:00 in some zone, and renders
# as the 29th for every reader west of it. A date string means the same day everywhere.
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_date(value: str | None, field: str) -> str | None:
    """Normalises a schedule date for storage. "" (or whitespace) clears it to None;
    anything else must be an ISO calendar date. Raises ValueError naming the field, so
    the message is usable straight back to a PM's curl or the board.

    The regex is not redundant with date.fromisoformat: since 3.11 that also accepts
    "20260930" and full datetimes, and one stored format is what keeps every reader -
    the board, the PM context block, a JSON diff - comparing like with like.
    """
    text = (value or "").strip()
    if not text:
        return None
    if not _DATE_PATTERN.match(text):
        raise ValueError(f"{field} must be a date as YYYY-MM-DD (got {value!r})")
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} is not a real date: {text}") from exc
    return text


def _check_order(start: str | None, target: str | None) -> None:
    """Checked against the RESULTING pair, never just the incoming one - a PATCH that
    sets only `start_at` can invert an order the item already had."""
    if start and target and start > target:  # ISO dates sort lexicographically
        raise ValueError(
            f"start_at ({start}) is after target_at ({target}) - a change cannot be "
            "scheduled to finish before it begins"
        )


def _schedule_note(item: dict) -> str:
    """The schedule as a PM reads it in its context block. Says OVERDUE loudly and gives
    the day count, because "target 2026-09-30" next to a date the model has to work out
    for itself is exactly the kind of thing it will get wrong or skip."""
    start, target = item.get("start_at"), item.get("target_at")
    if not start and not target:
        return ""
    if item.get("is_overdue"):
        late = (date.today() - date.fromisoformat(target)).days
        return f" [OVERDUE - target was {target}, {late}d ago]"
    parts = []
    if start:
        parts.append(f"starts {start}")
    if target:
        parts.append(f"target {target}")
        if item.get("status") != "done":
            left = (date.fromisoformat(target) - date.today()).days
            parts.append("due today" if left == 0 else f"{left}d left")
    return f" [{', '.join(parts)}]"


class TicketAlreadyLinked(Exception):
    """Raised when a ticket is already linked to a different roadmap change.

    The link is 1:1 in both directions, and this is the half that needs enforcing: one
    item holds at most one ticket by construction (a single pair of fields), but nothing
    stops two items naming the same ticket except this check. Carries the conflicting
    item so the caller can say *which* change already owns it - "already linked" alone
    would leave the user hunting the board for it.
    """

    def __init__(self, tracker_id: str, ticket_key: str, item: "RoadmapItem") -> None:
        self.tracker_id = tracker_id
        self.ticket_key = ticket_key
        self.item = item
        super().__init__(
            f"{ticket_key} is already linked to the change \"{item.title}\" "
            f"(id {item.id}) on the {item.product} board. A ticket can be linked to "
            "one change only - unlink it there first."
        )


@dataclass
class RoadmapItem:
    id: str
    product: str
    title: str
    description: str
    bucket: Bucket
    status: Status
    # Which product first proposed this item. Equal to `product` for an item a
    # product's own PM created; different when another product's PM suggested it as a
    # cross-product handoff (see RoadmapStore.create) - that's what `triaged` tracks.
    origin_product: str
    # True once the OWNING product's PM (or the stakeholder) has accepted a
    # cross-product suggestion into its real plan. Always True for an item whose
    # origin_product == product (nothing to triage - it's already theirs).
    triaged: bool
    created_at: float
    updated_at: float
    shipped_at: float | None = None
    # Free-text name of the person/team doing this work when it is NOT built through
    # this PM system's dev agents (e.g. "Design team", "Alice"). None/"" = built here.
    # An externally-owned item is tracked on the board - the PM keeps its status
    # current from stakeholder reports but never dispatches dev work for it.
    owner: str | None = None
    # Who is doing this work, as a `people.Person` id. THIS deployment's own decision and
    # nothing else: it is never written back to any tracker (see people.py).
    #
    # Deliberately not `owner` above, which it would be tempting to reuse. `owner` says the
    # work is built somewhere else and must never be dispatched here; an assignee says who
    # is on it, which a change this system's own dev agents build still has. Conflating
    # them would mean naming the person responsible for a change silently stopped the PM
    # from ever dispatching it.
    #
    # A linked change with nothing here reads its assignee off the ticket instead (see
    # people.effective_assignee), so this is only ever set when somebody here decided
    # something the tracker has not been told. None = nobody has, which is a first-class
    # state, and additive so existing boards keep loading.
    assignee: str | None = None
    # The single parent Project in the work model (see portfolio.py). A change has
    # exactly one, which is what keeps cost attribution unambiguous. None means the
    # change predates the work model or the deployment isn't using it - so existing
    # boards keep loading untouched, and this stays additive rather than a migration.
    project_id: str | None = None
    # The one SYSTEM this change is contained within (see SYSTEMS above). A change has
    # exactly one - that is what makes its blast radius knowable - and it is REQUIRED on
    # every new change once the deployment declares [systems].
    #
    # None means one of two things, and they are not the same:
    #   - the deployment has no [systems] at all, so there is nothing to attribute to;
    #   - or it does, and this change predates the restructure. That is an INCONSISTENCY
    #     to be fixed, not a supported resting state: unattributed_report counts it and
    #     the UI shows it until it is attributed. It is never a hard block, because
    #     refusing to load a board would be worse than showing the work left to do.
    system: str | None = None
    # The 1:1 link to one ticket in one configured external tracker (see trackers.py).
    # Only the reference is stored - the ticket's type, title, state and URL come from
    # the synced catalog at read time, so a sync updates one place instead of having to
    # rewrite every linked item. Both None (the default) for an unlinked change, which
    # is what keeps existing roadmap JSON loading untouched.
    tracker_id: str | None = None
    ticket_key: str | None = None
    # The schedule, as "YYYY-MM-DD" (see parse_date above). Both optional and both
    # independently optional: a change can have a target with no start ("due by the
    # 30th", a milestone), a start with no target ("began on the 1st, no committed end"),
    # or neither - in which case the timeline falls back to the Now/Next/Later horizon
    # and says so. Undated is the default and stays a first-class state: this board's
    # unit of planning is the horizon, and dates are the sharper thing you reach for
    # when a change actually has a commitment behind it.
    start_at: str | None = None
    target_at: str | None = None

    @property
    def is_overdue(self) -> bool:
        """Past its target and not shipped. Derived, never stored - a stored flag would
        be wrong by morning."""
        return bool(
            self.target_at
            and self.status != "done"
            and self.target_at < date.today().isoformat()
        )

    def to_dict(self) -> dict:
        """The STORED shape - exactly the fields, so `from_dict(to_dict(x)) == x` and
        the JSON on disk round-trips."""
        return asdict(self)

    def to_public_dict(self) -> dict:
        """The shape every reader outside the store gets: stored fields plus what is
        derived from them. Same split as portfolio.py's Initiative/Project, and the
        reason is the same - `is_overdue` must never reach the JSON file, where it would
        be stale by morning and would break `from_dict`."""
        data = self.to_dict()
        data["is_overdue"] = self.is_overdue
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "RoadmapItem":
        return cls(**data)


class RoadmapStore:
    """Shared, server-owned roadmap state: one JSON file per product under
    workspace/roadmap/, deliberately analogous to sessions.py's sessions.json - read
    and written only by this always-running process, never inside a per-session git
    worktree. Every PM session is its own worktree/branch and would otherwise see its
    own stale copy; routing all reads/writes through this single in-process store (via
    HTTP, from a PM's `curl` calls) means every session sees and edits the same board
    instead of a copy that could drift and need merging."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, RoadmapItem] = {}
        self._subscribers: list[Callable[[dict], None]] = []
        # Says so out loud when this process is NOT the one holding the workspace: the
        # whole-file saves below would be clobbered by the running server's own copy.
        warn_if_held()
        self._load()

    def _path(self, product: str) -> Path:
        return ROADMAP_DIR / f"{product}.json"

    def _load(self) -> None:
        ROADMAP_DIR.mkdir(parents=True, exist_ok=True)
        for product in PRODUCTS:
            path = self._path(product)
            if not path.exists():
                continue
            for raw in json.loads(path.read_text()):
                item = RoadmapItem.from_dict(raw)
                self._items[item.id] = item

    def _save_product(self, product: str) -> None:
        ROADMAP_DIR.mkdir(parents=True, exist_ok=True)
        items = [i.to_dict() for i in self._items.values() if i.product == product]
        items.sort(key=lambda i: i["created_at"])
        self._path(product).write_text(json.dumps(items, indent=2))

    # ---- broadcast (mirrors tasks.TaskRegistry's / SessionManager's subscriber pattern) ----

    def subscribe(self, callback: Callable[[dict], None]) -> None:
        self._subscribers.append(callback)

    def _notify(self, event: dict) -> None:
        for callback in list(self._subscribers):
            callback(event)

    # ---- reads ----

    def list_all(self) -> dict[str, list[dict]]:
        by_product: dict[str, list[dict]] = {p: [] for p in PRODUCTS}
        for item in self._items.values():
            by_product.setdefault(item.product, []).append(item.to_public_dict())
        for product_items in by_product.values():
            product_items.sort(key=lambda i: i["created_at"])
        return by_product

    def list_product(self, product: str) -> list[dict]:
        return sorted(
            (i.to_public_dict() for i in self._items.values() if i.product == product),
            key=lambda i: i["created_at"],
        )

    def get(self, item_id: str) -> RoadmapItem | None:
        return self._items.get(item_id)

    def list_by_system(self, system: str) -> list[dict]:
        return sorted(
            (i.to_public_dict() for i in self._items.values() if i.system == system),
            key=lambda i: i["created_at"],
        )

    def unattributed_report(self) -> dict:
        """Changes that OUGHT to name a system and do not.

        The data half of the restructure gap (products_missing_systems is the config
        half). Reported, never blocked - same call portfolio.unaligned_report makes for
        alignment: attribution is mandatory as a practice, so the gap is surfaced rather
        than used to refuse the board that shows it.

        Scoped by requires_system, which is the load-bearing part. Counting every
        system-less change on every board would make the first declared system light up
        thousands of changes across products whose edge nobody has declared yet - a number
        that measures how much config is missing, not how much attribution is owed, and
        which no amount of attributing could bring down. So changes on out-of-scope
        products are counted separately as `not_yet_in_scope`: visible, not debt.

        Empty by construction on a deployment with no [systems] at all.
        """
        if not systems_declared():
            return {
                "changes": [],
                "count": 0,
                "not_yet_in_scope": 0,
                "products_missing_systems": [],
            }
        pending = []
        out_of_scope = 0
        for item in sorted(self._items.values(), key=lambda i: i.created_at):
            if item.system is not None or item.status == "done":
                continue
            if not requires_system(item.product):
                out_of_scope += 1
                continue
            pending.append(
                {
                    "id": item.id,
                    "product": item.product,
                    "product_label": product_path_label(item.product),
                    "title": item.title,
                }
            )
        return {
            "changes": pending,
            "count": len(pending),
            "not_yet_in_scope": out_of_scope,
            "products_missing_systems": products_missing_systems(),
        }

    def system_rollup(self) -> list[dict]:
        """One row per declared system, in display order, for the systems view.

        Counts are per system and each change is counted once, so these totals are exact.
        They must NOT be summed across products: a system touched by several products
        would be counted once per product. That is the same overlap rule goals carry in
        portfolio.py, and it is inherent to the edge being many-to-many rather than a
        tree level.
        """
        counts: dict[str, dict[str, int]] = {
            s: {"total": 0, "open": 0} for s in SYSTEMS
        }
        for item in self._items.values():
            bucket = counts.get(item.system or "")
            if bucket is None:
                continue
            bucket["total"] += 1
            if item.status != "done":
                bucket["open"] += 1
        rows = []
        for system_id, spec in SYSTEMS.items():
            rows.append(
                {
                    "id": system_id,
                    "label": spec.label,
                    "path": spec.path,
                    "repo": spec.repo,
                    "guidance": spec.guidance,
                    "gitflow": spec.gitflow,
                    "pipelines": list(spec.pipelines),
                    "products": [
                        {"id": p, "label": product_path_label(p)}
                        for p in products_of_system(system_id)
                    ],
                    "changes": counts[system_id]["total"],
                    "open_changes": counts[system_id]["open"],
                    # True while this id is declared as both a product and a system - the
                    # temporary reclassification state, whose product board is still live.
                    "transitional": system_id in TRANSITIONAL_IDS,
                }
            )
        return rows

    def list_by_project(self, project_id: str) -> list[dict]:
        return sorted(
            (i.to_public_dict() for i in self._items.values() if i.project_id == project_id),
            key=lambda i: i["created_at"],
        )

    def count_by_project(self, project_id: str) -> int:
        """How many changes hang off one project - what the server checks before
        allowing that project to be deleted."""
        return sum(1 for i in self._items.values() if i.project_id == project_id)

    def item_for_ticket(self, tracker_id: str, ticket_key: str) -> RoadmapItem | None:
        """Which change holds this ticket, if any - the read side of the 1:1 link."""
        wanted = normalize_key(tracker_id, ticket_key)
        return next(
            (
                i
                for i in self._items.values()
                if i.tracker_id == tracker_id
                and i.ticket_key
                and normalize_key(tracker_id, i.ticket_key) == wanted
            ),
            None,
        )

    def linked_ticket_refs(self) -> list[tuple[str, str]]:
        """Every (tracker_id, key) currently linked, for the sync to keep fresh."""
        return [
            (i.tracker_id, i.ticket_key)
            for i in self._items.values()
            if i.tracker_id and i.ticket_key
        ]

    def items_assigned_to(self, person_id: str) -> list[dict]:
        """Every change carrying this person id LOCALLY. Not the same question the board's
        person pivot answers - that one counts a linked change's tracker assignee too - and
        deliberately so: this is what a directory merge has to re-point, and a tracker's
        assignee is not ours to rewrite."""
        with self._lock:
            return [
                i.to_public_dict()
                for i in self._items.values()
                if i.assignee == person_id
            ]

    def unassigned_items(self) -> list[dict]:
        """Changes with no parent project. Reported, not blocked: alignment is a
        practice the board surfaces rather than a validation that stops work."""
        return sorted(
            (i.to_public_dict() for i in self._items.values() if i.project_id is None),
            key=lambda i: i["created_at"],
        )

    # ---- writes ----

    def create(
        self,
        product: str,
        title: str,
        description: str = "",
        bucket: Bucket = "later",
        status: Status = "pending",
        origin_product: str | None = None,
        owner: str | None = None,
        assignee: str | None = None,
        project_id: str | None = None,
        start_at: str | None = None,
        target_at: str | None = None,
        system: str | None = None,
        system_required: bool = True,
    ) -> RoadmapItem:
        if product not in PRODUCTS:
            raise ValueError(f"Unknown product: {product}")
        origin = origin_product or product
        if origin not in PRODUCTS:
            raise ValueError(f"Unknown origin_product: {origin}")
        # Validated against the OWNING product, not the origin: the change is contained
        # within the system its own board's product is built on, whoever suggested it.
        # `system_required=False` is for the tracker import only (see server.py): a
        # route without a system lands its imports unattributed - into the existing
        # unattributed report - because an imported change you can see beats a refused
        # one you can't. Every interactive path keeps the requirement.
        resolved_system = validate_system(product, system, required=system_required)
        start = parse_date(start_at, "start_at")
        target = parse_date(target_at, "target_at")
        _check_order(start, target)
        now = time.time()
        item = RoadmapItem(
            id=uuid.uuid4().hex[:8],
            product=product,
            title=title,
            description=description,
            bucket=bucket,
            status=status,
            origin_product=origin,
            # Deliberately derived, never taken from caller input - a cross-product
            # suggestion always lands untriaged, whatever the caller's payload says,
            # so a PM can't hand its own item a false "already accepted" status on
            # someone else's board.
            triaged=(origin == product),
            created_at=now,
            updated_at=now,
            shipped_at=now if status == "done" else None,
            owner=(owner or "").strip() or None,
            assignee=(assignee or "").strip() or None,
            project_id=(project_id or "").strip() or None,
            start_at=start,
            target_at=target,
            system=resolved_system,
        )
        with self._lock:
            self._items[item.id] = item
            self._save_product(product)
        self._notify({"type": "roadmap_item_upserted", "item": item.to_public_dict()})
        return item

    def update(
        self,
        item_id: str,
        bucket: Bucket | None = None,
        status: Status | None = None,
        triaged: bool | None = None,
        title: str | None = None,
        description: str | None = None,
        owner: str | None = None,
        assignee: str | None = None,
        project_id: str | None = None,
        start_at: str | None = None,
        target_at: str | None = None,
        system: str | None = None,
    ) -> RoadmapItem:
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                raise KeyError(f"Unknown roadmap item: {item_id}")
            # Resolved before any write, like the dates below: a PATCH naming a system the
            # product does not touch must leave the item exactly as it was. `required`
            # because there is no ""-clears convention for this field - see validate_system.
            resolved_system = (
                item.system if system is None else validate_system(item.product, system)
            )
            # Both dates resolved and checked BEFORE anything is written, so a PATCH
            # carrying an impossible schedule leaves the item exactly as it was rather
            # than half-applied. Same convention as owner/project_id: None = no change,
            # "" = clear.
            start = item.start_at if start_at is None else parse_date(start_at, "start_at")
            target = item.target_at if target_at is None else parse_date(target_at, "target_at")
            _check_order(start, target)
            item.start_at = start
            item.target_at = target
            item.system = resolved_system
            if bucket is not None:
                item.bucket = bucket
            if status is not None:
                item.status = status
                item.shipped_at = time.time() if status == "done" else None
            if triaged is not None:
                item.triaged = triaged
            if title is not None:
                item.title = title
            if description is not None:
                item.description = description
            if owner is not None:
                # "" (an explicit empty string) clears external ownership back to
                # "built here"; None means "no change", like every other field.
                item.owner = owner.strip() or None
            if assignee is not None:
                # Same convention again: "" unassigns, which for a LINKED change means it
                # falls back to whoever the tracker says is on it rather than to nobody.
                item.assignee = assignee.strip() or None
            if project_id is not None:
                # Same convention: "" detaches the change from its project (making it
                # unaligned), None leaves it alone.
                item.project_id = project_id.strip() or None
            item.updated_at = time.time()
            self._save_product(item.product)
        self._notify({"type": "roadmap_item_upserted", "item": item.to_public_dict()})
        return item

    def link_ticket(
        self, item_id: str, tracker_id: str, ticket_key: str
    ) -> RoadmapItem:
        """Links a change to one tracker ticket, 1:1.

        Raises TicketAlreadyLinked if a DIFFERENT change already holds it. Re-linking the
        same pair is a no-op rather than an error - a PM re-sending the same PATCH must not
        get a conflict against the item itself.

        The check and the write happen under one lock hold, so two concurrent links to the
        same ticket cannot both pass their check and leave two items pointing at it.
        """
        key = normalize_key(tracker_id, ticket_key)
        if not tracker_id or not key:
            raise ValueError("tracker_id and ticket_key are both required to link a ticket")
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                raise KeyError(f"Unknown roadmap item: {item_id}")
            for other in self._items.values():
                if other.id == item_id or other.tracker_id != tracker_id or not other.ticket_key:
                    continue
                if normalize_key(tracker_id, other.ticket_key) == key:
                    raise TicketAlreadyLinked(tracker_id, key, other)
            item.tracker_id = tracker_id
            item.ticket_key = key
            item.updated_at = time.time()
            self._save_product(item.product)
        self._notify({"type": "roadmap_item_upserted", "item": item.to_public_dict()})
        return item

    def unlink_ticket(self, item_id: str) -> RoadmapItem:
        """Clears the link. Idempotent: unlinking an unlinked change is not an error, so a
        PM clearing a field it already cleared doesn't fail a turn."""
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                raise KeyError(f"Unknown roadmap item: {item_id}")
            item.tracker_id = None
            item.ticket_key = None
            item.updated_at = time.time()
            self._save_product(item.product)
        self._notify({"type": "roadmap_item_upserted", "item": item.to_public_dict()})
        return item

    def move(
        self,
        item_id: str,
        to_product: str,
        triaged: bool = False,
        bucket: Bucket | None = None,
        status: Status | None = None,
        title: str | None = None,
        description: str | None = None,
        system: str | None = None,
    ) -> RoadmapItem:
        """Reassigns an item to a different product in place - same id/created_at/
        history, unlike a delete+recreate. `origin_product` is set to wherever it just
        moved FROM (so the destination board can show "moved from X" the same way it
        shows "suggested by X"), and it lands untriaged by default - a PM-initiated
        move (see agent.py's ROADMAP_GUIDANCE_TEMPLATE) still needs the destination's
        review, same as any cross-product suggestion. The board UI passes
        triaged=True for a stakeholder-initiated move instead, since a human picking
        the destination directly needs no second confirmation step.

        This is also the re-homing primitive the reclassification path uses: an id being
        moved from [products] to [systems] empties its old board by moving each change to
        a real product and naming the system it is contained within (see
        config.Config.transitional_ids)."""
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                raise KeyError(f"Unknown roadmap item: {item_id}")
            if to_product not in PRODUCTS:
                raise ValueError(f"Unknown product: {to_product}")
            if to_product == item.product:
                raise ValueError(f"Item {item_id} is already on {to_product}")
            # Attribution has to survive the move, and the destination may well not touch
            # the system this change sits in. Passing `system` re-attributes it as part of
            # the same move; passing nothing carries the current one over and fails loudly
            # if the destination does not touch it. Resolved before any write, so a
            # rejected move leaves the item entirely alone.
            resolved_system = validate_system(
                to_product,
                item.system if system is None else system,
                required=system is not None,
            )
            from_product = item.product
            item.origin_product = from_product
            item.product = to_product
            item.system = resolved_system
            item.triaged = triaged
            if bucket is not None:
                item.bucket = bucket
            if status is not None:
                item.status = status
                item.shipped_at = time.time() if status == "done" else None
            if title is not None:
                item.title = title
            if description is not None:
                item.description = description
            item.updated_at = time.time()
            self._save_product(from_product)
            self._save_product(to_product)
        # Two events, not one "moved" type: every client already knows how to handle
        # a delete (remove from the old product's columns) and an upsert (add to the
        # new one) - no new message type needed on the board's websocket handler.
        self._notify({"type": "roadmap_item_deleted", "id": item_id, "product": from_product})
        self._notify({"type": "roadmap_item_upserted", "item": item.to_public_dict()})
        return item

    def delete(self, item_id: str) -> None:
        with self._lock:
            item = self._items.pop(item_id, None)
            if item is None:
                raise KeyError(f"Unknown roadmap item: {item_id}")
            self._save_product(item.product)
        self._notify({"type": "roadmap_item_deleted", "id": item_id, "product": item.product})

    # ---- PM context injection ----

    def _describe_item(
        self,
        item: dict,
        owning_product: str,
        ticket_lookup: Callable[[str, str], dict | None] | None = None,
        show_product: bool = False,
        person_lookup: Callable[[str], str] | None = None,
    ) -> str:
        """One change, at full depth, as its owning PM reads it.

        `owning_product` is the board the item sits on - which is what decides whether
        its origin makes it an untriaged suggestion, so it is passed rather than assumed
        to be the session's own product: a parent's context block covers several boards.

        `show_product` names that board inline. Off by default because a product-pinned
        session reads its changes under a product heading that already says so; on for
        the initiative view, where consecutive changes in one project routinely sit on
        different boards and the heading cannot say which.
        """
        flag = ""
        if show_product:
            flag += f" [on {product_path_label(owning_product)}'s board]"
        if item["origin_product"] != owning_product and not item["triaged"]:
            origin_label = product_path_label(item["origin_product"])
            flag = f" [UNTRIAGED suggestion from {origin_label} - accept or drop it]"
        if item.get("owner"):
            flag += (
                f' [EXTERNAL - owned by {item["owner"]}: track it, never dispatch '
                "dev work for it]"
            )
        # The attribution is named wherever there is one, so the PM can see it without
        # asking. Its ABSENCE only shouts for a product that is in scope (requires_system):
        # on a board whose edge nobody has declared yet, [NO SYSTEM] on every line would be
        # noise about missing config rather than a change the PM can act on - and the PM is
        # not told to attribute there either (see agent.SYSTEM_GUIDANCE_TEMPLATE), so the
        # prompt and the context block stay two statements of one rule.
        if item.get("system"):
            flag += f' [system: {system_label(item["system"])}]'
        elif requires_system(item["product"]):
            flag += " [NO SYSTEM - attribute it when you next touch this change]"
        flag += _schedule_note(item)
        ticket = None
        if item.get("ticket_key"):
            ticket = (
                ticket_lookup(item["tracker_id"], item["ticket_key"])
                if ticket_lookup
                else None
            )
            if ticket:
                flag += (
                    f' [tracked as {ticket["raw_type"]} {item["ticket_key"]}'
                    f' ({ticket["state"]}) in {item["tracker_id"]}]'
                )
            else:
                flag += f' [linked to {item["ticket_key"]} in {item["tracker_id"]}]'
        # Who is on it, from whichever source knows: the local assignment if somebody made
        # one here, otherwise the tracker's own assignee. Named on the line rather than
        # left to be asked about, because a PM asked to spread the next slice of work
        # across people cannot do it from a board that never says who is already on what.
        # `person_lookup` resolves a person id to a name, injected for the same reason
        # `ticket_lookup` is - this store stays ignorant of the directory.
        assigned = (
            (person_lookup(item["assignee"]) if person_lookup and item.get("assignee") else "")
            or (ticket or {}).get("assignee")
            or ""
        )
        if assigned:
            flag += f" [assigned to {assigned}]"
        return (
            f'- id {item["id"]} [{item["bucket"]}/{item["status"]}] {item["title"]}{flag}\n'
            f'  {item["description"]}'
        )

    def describe_own_product(
        self,
        product: str,
        ticket_lookup: Callable[[str, str], dict | None] | None = None,
        person_lookup: Callable[[str], str] | None = None,
    ) -> str:
        """Full-depth view of one product's roadmap - every item, every bucket/status,
        full description - for injection into that product's own PM session. Flags any
        untriaged cross-product suggestion waiting on this PM to accept or drop.

        A parent product's session gets its whole SUBTREE at the same depth - children,
        grandchildren, however deep the taxonomy goes - under a heading per board: a
        parent PM owns its subtree, so a descendant's plan is its own plan and not
        somebody else's to be told about in one line. Each heading carries that board's
        full path and its id, so the PM can tell "this is mine directly" from "this is my
        Billing sub-product's" and knows which id to write to. A leaf (or any product on
        a flat taxonomy) gets exactly what it always got.

        `ticket_lookup` resolves (tracker_id, key) to the synced ticket so the PM sees what
        each change is tracked as in Jira/ADO. Passed in rather than imported so this store
        keeps knowing nothing about the tracker catalog; omitted (as in tests) the linked
        key is still reported, just without its type.
        """
        family = subtree_products(product)
        open_items = {p: [i for i in self.list_product(p) if i["status"] != "done"] for p in family}
        if not any(open_items.values()):
            if len(family) == 1:
                return f"{product_label(product)} roadmap has no open items right now."
            return (
                f"{product_label(product)} roadmap - and its sub-products "
                f'({", ".join(product_label(p) for p in family[1:])}) - '
                "has no open items right now."
            )

        if len(family) == 1:
            lines = [f"{product_label(product)} roadmap (full detail):"]
            for i in open_items[product]:
                lines.append(
                    self._describe_item(i, product, ticket_lookup, person_lookup=person_lookup)
                )
            return "\n".join(lines)

        lines = [
            f"{product_label(product)} roadmap (full detail), including the "
            f"{len(family) - 1} sub-product board(s) you also own:"
        ]
        for owned in family:
            items = open_items[owned]
            # Named by its OWN parent, not by the session's product: at three levels
            # "sub-product of Web App" would be wrong for a board that actually hangs off
            # Auth & Identity, and which board a change belongs under is the thing this
            # heading exists to settle.
            heading = (
                f"{product_label(owned)} (your own board, id `{owned}`)"
                if owned == product
                else (
                    f"{product_label(owned)} (sub-product of "
                    f"{product_label(parent_of(owned))}, id `{owned}`)"
                )
            )
            if not items:
                lines.append(f"\n{heading} - no open items.")
                continue
            lines.append(f"\n{heading}:")
            for i in items:
                lines.append(
                    self._describe_item(i, owned, ticket_lookup, person_lookup=person_lookup)
                )
        return "\n".join(lines)

    def describe_other_products(
        self,
        exclude_product: str | Iterable[str],
        exclude_item_ids: Iterable[str] | None = None,
    ) -> str:
        """Shallow, title-only digest of every product OUTSIDE the pinned product's
        subtree - general awareness without depth: just bucket/status/title, no
        descriptions.

        The whole subtree is excluded, not just the one product: whatever
        describe_own_product covered at full depth must not come back a second time as a
        one-liner, or a parent PM reads its children's changes twice and the digest stops
        meaning "somebody else's work". Passing an exclude_product that matches nothing
        (e.g. "") returns a digest of every product, for a session with no product of its
        own.

        Several products may be excluded, for a session that owns several boards rather
        than one subtree (an initiative-scoped session that has adopted products - see
        owned_subtrees). Same rule, applied to each: full depth anywhere means no digest
        line here.

        `exclude_item_ids` applies that same rule one level down, to individual changes.
        An initiative-scoped session reads its initiative's changes at full depth wherever
        they live, including on boards it does not own - without this, every one of them
        would come back as a digest one-liner on the very next line. A board whose every
        open change was already shown drops out of the digest entirely, which is correct:
        it has nothing left to make the PM aware of.
        """
        if isinstance(exclude_product, str):
            roots = [exclude_product]
        else:
            roots = list(exclude_product)
        excluded = set(owned_subtrees(roots)) | set(roots)
        already_shown = set(exclude_item_ids or ())
        lines = []
        for product in PRODUCTS:
            if product in excluded:
                continue
            items = [
                i for i in self.list_product(product)
                if i["status"] != "done" and i["id"] not in already_shown
            ]
            if not items:
                continue
            summary = "; ".join(
                f'[{i["bucket"]}/{i["status"]}]'
                + (f' (external: {i["owner"]})' if i.get("owner") else "")
                + f' {i["title"]}'
                # Only the fact of being late travels into another product's digest -
                # this view is awareness, and someone else's slipping date is worth
                # knowing about where their exact start date is not.
                + (" (OVERDUE)" if i.get("is_overdue") else "")
                for i in items
            )
            # The parent is named too, so a digest line reads unambiguously on a
            # deployment where two parents each have a "Billing".
            lines.append(f"- {product_path_label(product)}: {summary}")
        return "\n".join(lines)

    def describe_initiative(
        self,
        heading: str,
        groups: list[tuple[str, list[dict]]],
        ticket_lookup: Callable[[str, str], dict | None] | None = None,
        person_lookup: Callable[[str], str] | None = None,
    ) -> str:
        """Full-depth view of one initiative's work, grouped Project -> Change, for
        injection into an initiative-scoped session (see sessions.Session.initiative_id).

        The other lens on the same changes. `describe_own_product` slices by board and
        answers "what is on my roadmap"; this slices by the work model and answers "what
        is this initiative made of" - which is the question a cross-product initiative
        actually poses, since its changes are scattered across boards no single product
        view brings together.

        `groups` is `[(project heading, [items])]`, assembled by the caller: which
        projects belong to an initiative is the portfolio store's knowledge, and which
        changes belong to a project is this store's, so neither reaches into the other
        (same reason `ticket_lookup` is injected). Every change names its own board,
        because in this view consecutive changes routinely sit on different ones.
        """
        lines = [heading]
        for project_heading, items in groups:
            open_items = [i for i in items if i["status"] != "done"]
            if not open_items:
                lines.append(f"\n{project_heading} - no open changes.")
                continue
            lines.append(f"\n{project_heading}:")
            for item in open_items:
                lines.append(
                    self._describe_item(
                        item,
                        item["product"],
                        ticket_lookup,
                        show_product=True,
                        person_lookup=person_lookup,
                    )
                )
        return "\n".join(lines)
