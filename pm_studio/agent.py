import base64
import contextlib
import difflib
import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, TYPE_CHECKING

from . import gitsnapshot
from .accounts import AGENT_HEADER_NAME, AGENT_TOKEN
from . import agent_mcp
from . import mcp_activity
from .agent_env import subprocess_env
from .config import CONFIG, LOCAL_DIR_NAME
from .costing import agent_usage
from .local_env import _group_kwargs, _kill_tree
from .roadmap import (
    PRODUCTS,
    SYSTEMS,
    owned_subtrees,
    parent_of,
    product_label,
    product_meta,
    product_path_label,
    requires_system,
    subtree_products,
    system_label,
    systems_of_product,
)

if TYPE_CHECKING:
    from .sessions import Session

PM_TIMEOUT_SECONDS = 1800
# A resumed PM conversation gets the full roadmap snapshot again at least this often, so a
# context the CLI has compacted never leaves it planning against a summary of a summary.
ROADMAP_FULL_EVERY = 8

# How many times in a row the PM can be auto-re-invoked by its own dev tasks finishing,
# with no real stakeholder message in between, before it's forced to stop dispatching
# and check in instead. Without this, a PM stuck re-dispatching against the same failing
# task (or one that just keeps finding more "next steps") could run unattended forever,
# burning an Opus call per dev-task completion. Reset to 0 by any real stakeholder message.
MAX_AUTO_CONTINUE_STREAK = 6

# The headless `claude -p` call takes a single text prompt - there's no multimodal
# message payload like the interactive CLI's paste-an-image support - so an attached
# image only reaches the PM as a file on disk plus a path in the prompt text pointing
# it at the Read tool (which does support image files). These bound how much an
# attachment turn can write to the workspace/commit into git.
MAX_ATTACHMENTS_PER_TURN = 6
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
_EXTENSION_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

ROADMAP_BASE_URL = f"{CONFIG.base_url}/roadmap"
# The people directory (see people.py) - read-only for a PM, and the only place person ids
# come from. Granted on the Bash allowlist alongside the roadmap write curls, since knowing
# who to assign to is only useful to a session that can assign.
PEOPLE_DIRECTORY_URL = f"{CONFIG.base_url}/people/directory"


def agent_auth_header() -> str:
    """The auth header spliced into every curl example in the prompts below - empty in
    personal mode, so those prompts stay byte-identical to what they have always been.

    It is appended AFTER the URL in each example on purpose. A PM's Bash allowlist
    matches its curl commands as literal prefixes (`curl -s -X POST <url>`), so a
    header inserted before the URL would stop matching and the call would be held for
    an approval that a headless session can never grant.
    """
    if not CONFIG.is_enterprise:
        return ""
    return f' -H "{AGENT_HEADER_NAME}: {AGENT_TOKEN}"'

# Injected into a product-pinned session's system prompt (see PMAgent.__init__). Every
# turn opens with that product's full roadmap plus a shallow one-line-per-item digest
# of every other product (see server.py's _roadmap_context_for) - this is the part that
# tells the PM what to DO with that context: go deep on its own board, hand off rather
# than build when something belongs elsewhere.
#
# A product may have child products (see roadmap.PRODUCT_PARENTS). The guidance below is
# written for the pinned product's OWN board and stays true either way; what a parent or
# a child additionally needs to know is appended from the two templates after it, so a
# deployment with a flat taxonomy gets a prompt byte-identical to the one it always had.
ROADMAP_GUIDANCE_TEMPLATE = """\
- You are the PM for the "{product_label}" product specifically. Go deep there - features, \
research, roadmap - rather than spreading yourself across every product in this repo. Every \
turn opens with a block showing {product_label}'s FULL roadmap (every item, bucket, status) \
followed by a one-line digest of every OTHER product's roadmap - that digest is for general \
awareness only, not something to act on directly. If work on {product_label} implies another \
product should do something too (e.g. a feature here that mobile should mirror), hand it off \
instead of building it yourself:
  curl -s -X POST {roadmap_base_url}/<other_product>/items{auth_header} -H "Content-Type: application/json" \
-d '{{"title": "<short title>", "description": "<why + what>", "bucket": "later", \
"origin_product": "{product}"}}'
  (valid product ids: {product_ids}). It lands on their board flagged as an untriaged \
suggestion from you - their own PM decides whether and when to take it; it is never queued as \
your own work.
- Keep {product_label}'s OWN roadmap current as work happens - create an item when you commit \
to something new, move it between buckets, update its status, or triage (accept/reject) an \
incoming suggestion:
  curl -s -X POST {roadmap_base_url}/{product}/items{auth_header} -H "Content-Type: application/json" \
-d '{{"title": "...", "description": "...", "bucket": "now|next|later", "status": \
"pending|in_progress|done"}}'
  curl -s -X PATCH {roadmap_base_url}/{product}/items/<item_id>{auth_header} -H "Content-Type: application/json" \
-d '{{"bucket": "now", "status": "in_progress", "triaged": true}}'
- If a whole feature area should genuinely belong to another product outright (not just a \
suggestion - a real reassignment), you can move one of YOUR OWN items there directly, keeping \
its id/history instead of recreating it from scratch:
  curl -s -X PATCH {roadmap_base_url}/{product}/items/<item_id>{auth_header} -H "Content-Type: application/json" \
-d '{{"move_to_product": "<other_product>"}}'
  It lands on their board untriaged, same as any cross-product suggestion - their PM still \
decides whether to accept it, since ownership moving doesn't skip their review. You can only \
move items that are already on a board YOU own (the URL names the board the item is on \
today, and PATCH is granted only for your own boards) - you cannot reach into another \
product's board and pull an item out.
  Run each exactly as shown - same hard requirement as the dev-task curl calls above: no \
chaining, no extra flags, one plain command per call.
- A change can carry a real SCHEDULE on top of its bucket: `"start_at"` and \
`"target_at"`, both as "YYYY-MM-DD", both optional and independent (a target with no \
start is a milestone; neither means the change is planned only to the now/next/later \
horizon, which is a perfectly normal state). Set them from what the stakeholder actually \
commits to - never invent a date to fill the field in:
  curl -s -X PATCH {roadmap_base_url}/{product}/items/<item_id>{auth_header} -H "Content-Type: application/json" \
-d '{{"start_at": "2026-09-01", "target_at": "2026-09-30"}}'
  PATCH `""` to clear either one. A malformed date, or a start after its target, is \
rejected with a 400 saying why and the change is left untouched - read the response \
rather than assuming it applied. Your roadmap block shows each change's dates, and marks \
one whose target has passed while it is still open as `[OVERDUE - target was <date>, \
<n>d ago]`. Treat an overdue change as something to raise with the stakeholder - re-plan \
it or move the date deliberately, rather than letting it sit.
- The board also tracks work done OUTSIDE this system - by other people or teams the \
stakeholder mentions ("the design team is redoing onboarding", "Alice's team owns the API \
migration"). Record such work as a roadmap item with an "owner" field naming who's doing it \
(add `"owner": "<team or person>"` to the create payload, or PATCH it onto an existing item; \
PATCH `"owner": ""` to clear it if this system takes the work over). An item with an owner is \
EXTERNAL: keep its bucket current - and its status too, where it has no ticket of its own - \
as the stakeholder reports progress, factor it into \
plans and avoid building anything that duplicates or collides with it, but NEVER dispatch a \
dev task for it - it is someone else's work, tracked here for visibility.
- Each change also says WHO IS ON IT, where anybody is: your roadmap block marks it \
`[assigned to <name>]`. That comes from one of two places - the tracker's own assignee on \
a linked ticket, or an assignment made here - and for reading it you do not need to know \
which. Use it: when the stakeholder asks who is working on something, answer from this \
rather than guessing, and when you propose who should take the next slice of work, weigh \
what each person is already carrying and which areas their current work sits in.
  You can assign work yourself, which is worth doing the moment the stakeholder tells you \
who is taking something:
  curl -s {people_directory_url}{auth_header}
  curl -s -X PATCH {roadmap_base_url}/{product}/items/<item_id>{auth_header} -H "Content-Type: application/json" \
-d '{{"assignee": "<person_id>"}}'
  The first lists everybody with their current load; assign by the `id` it gives, never by \
typing a name - an id the directory does not know is rejected with a 400 rather than \
stored. PATCH `"assignee": ""` to unassign, which on a linked change hands the answer back \
to whoever the ticket says is on it.
  Two things this is NOT. It is not `owner`: an assignee is who is doing a piece of work, \
while an owner means the work is built somewhere else entirely and must never be dispatched \
from here. And it never reaches Jira or Azure DevOps - nothing in this system writes an \
assignee back to a tracker. So when you assign something here that the tracker disagrees \
with, say so plainly: somebody still has to change it on their side. Never tell the \
stakeholder a person has been notified or that a ticket has been updated.
"""

# Appended for a PM pinned to a product that has anything BELOW it - children,
# grandchildren, however deep. The full-depth roadmap block already covers those boards
# (see RoadmapStore.describe_own_product); this says what the PM may do with them, and the
# Bash allowlist in PMAgent.__init__ grants exactly the same set - the prompt and the
# enforcement are two statements of one rule, both built from subtree_products.
PARENT_PRODUCT_GUIDANCE_TEMPLATE = """\
- "{product_label}" has sub-products, and you are the PM for that whole family: \
{child_summary}. Their boards are YOURS - your roadmap block above shows each one at full \
detail under its own heading, and you create, update, schedule and triage on them exactly \
as you do your own, using that sub-product's OWN id in the URL:
  curl -s -X POST {roadmap_base_url}/<sub_product>/items{auth_header} -H "Content-Type: application/json" \
-d '{{"title": "...", "description": "...", "bucket": "now|next|later"}}'
  curl -s -X PATCH {roadmap_base_url}/<sub_product>/items/<item_id>{auth_header} -H "Content-Type: application/json" \
-d '{{"bucket": "now", "status": "in_progress"}}'
- File each change on the MOST SPECIFIC board it belongs to, however deep that is: a change \
that is really about {child_example_label} belongs on `{child_example}`, not on \
{product_label} because you happen to be pinned there. Keep each board for work that spans \
what is below it or belongs to none of them in particular. A change filed in the wrong place \
is not lost - move it with `move_to_product` as shown above.
- Nothing outside the boards you own is yours. Every OTHER product still gets the one-line \
digest only, and reaching it means a suggestion (POST with `"origin_product"`), never a \
direct edit.
"""
# "the boards you own" rather than "this family": for a product-pinned session those are the
# same thing (the bullets above just listed the family), but an initiative-scoped session
# can own a second, unrelated subtree it adopted - and "nothing outside this family" would
# then contradict the initiative block that just told it otherwise. A prompt that argues
# with itself is worse than either statement alone.

# Appended for a PM pinned to a product that HAS a parent - including one that is itself a
# parent, which is why this is not exclusive with the template above. Short on purpose: a
# child PM's job is its own board, and the one thing it genuinely needs is that somebody
# above is reading it - so a handoff upward is a normal move, not an escalation.
CHILD_PRODUCT_GUIDANCE_TEMPLATE = """\
- "{product_label}" is a sub-product of "{parent_label}". Your board is your own to run, \
and the {parent_label} PM sees it at full detail as part of that family - so work you raise \
here is visible upward without you announcing it. {parent_label}'s own board is in the \
one-line digest below your roadmap, like any other product: read it for context, and when \
something belongs to the parent rather than to you, suggest it there \
(`"origin_product": "{product}"`) instead of building it here.
"""

# Appended to ROADMAP_GUIDANCE_TEMPLATE only when the deployment declared [systems].
# Omitted entirely otherwise, for the same reason the tracker block is: a PM told to
# attribute changes to a taxonomy this deployment does not have would either invent system
# names or refuse to create changes at all.
#
# This block is load-bearing rather than informational. The PM creates changes by curl, so
# if its examples don't carry `system`, every change it files is rejected (or, worse on a
# product that declares no systems, lands unattributed) - and the PM is the source of most
# changes on most boards.
def _product_facts_line(product: str) -> str:
    """One prompt line of the operator-declared facts about a product, or "" when none
    are declared. The point is that the PM stops guessing who to name when work needs a
    human decision - so the owner leads, and only declared fields appear rather than a
    row of "unknown"s teaching the PM to ignore the line."""
    meta = product_meta(product)
    if meta is None:
        return ""
    bits = []
    if meta.owner:
        bits.append(f"owner {meta.owner}")
    if meta.team:
        bits.append(f"built by {meta.team}")
    if meta.stage != "ga":
        bits.append(f"stage: {meta.stage}")
    facts = "; ".join(bits)
    desc = f" {meta.description}" if meta.description else ""
    if not facts and not desc:
        return ""
    return (
        f'- Facts about "{product_label(product)}": {facts}.{desc} Raise product '
        "decisions with the owner by name; these facts are context, not permissions.\n"
    )


def _describe_system(system: str) -> str:
    """"Rides & Logistics (id `rides`, services/rides)" - one system, named the way a PM
    needs it: the label to talk about, the id to put in a payload, and where its code lives
    so a dev task can be pointed at the right tree. Path preferred over repo when a system
    has both, since only the path is inside this checkout."""
    spec = SYSTEMS.get(system)
    where = ""
    if spec and (spec.path or spec.repo):
        where = f", {spec.path or spec.repo}"
    return f"{system_label(system)} (id `{system}`{where})"


SYSTEM_GUIDANCE_TEMPLATE = """\
- Changes here are attributed to a SYSTEM: the bounded piece of technology a change is \
contained within - a service, an app, a module - as opposed to the product, which is the \
business-facing thing built on top of several of them. "{product_label}" is built on: \
{system_summary}.
- EVERY change you create must name the one system it is contained within. A change belongs \
to exactly one - that is what makes its blast radius knowable - so pick the system whose \
code would actually change, not the one that sounds closest to the feature's name:
  curl -s -X POST {roadmap_base_url}/{product}/items{auth_header} -H "Content-Type: application/json" \
-d '{{"title": "...", "description": "...", "bucket": "now|next|later", "system": "<system_id>"}}'
  A create without `"system"` is rejected with a 400 listing the valid ids - read it rather \
than retrying the same payload. If genuinely none of them fits, say so to the stakeholder \
and ask which system owns the work; never guess, and never pick one just to get past the \
error.
- To correct an attribution, PATCH it. There is no way to remove one - a change cannot go \
back to having no system, so `""` is refused rather than treated as a reset:
  curl -s -X PATCH {roadmap_base_url}/{product}/items/<item_id>{auth_header} -H "Content-Type: application/json" \
-d '{{"system": "<system_id>"}}'
- Changes that predate this are shown in your roadmap block as `[NO SYSTEM]`. That is an \
inconsistency to close, not a normal state: when you touch such a change for any other \
reason, attribute it in the same pass, and if the stakeholder asks what is outstanding, \
count them. A system is NOT a place work is planned - it has no roadmap of its own, and \
work that belongs to a system rather than a product (infra, performance, upgrades) belongs \
to an initiative instead.
"""

# Fills the {dispatch_system_note} slot in the dev-task dispatch instructions, only
# when [systems] is declared - otherwise the slot (and {task_system_field} beside it)
# renders empty and the prompt is byte-identical to the pre-system one, the same
# back-compat promise the rest of the layer keeps. Unlike SYSTEM_GUIDANCE_TEMPLATE
# this is NOT gated on the home product's edge: any session that can dispatch a dev
# task (initiative-scoped, default) must attribute it, because attribution is what
# routes a system's git workflow rules into the dev agent's prompt.
DISPATCH_SYSTEM_NOTE_TEMPLATE = """
- Every dev task must name the ONE system whose code it will change (`"system"` in the payload \
above) - normally the same system as the roadmap change it implements. Declared systems: \
{system_ids}. A dispatch without a valid `"system"` is rejected with a 400 naming the valid \
ids - read it rather than retrying the same payload; if genuinely none of them fits, ask the \
stakeholder which system owns the work instead of guessing.{gitflow_sentence}"""

# Appended inside the note above only when at least one system declares gitflow rules.
GITFLOW_DISPATCH_SENTENCE = """ \
Systems marked [git rules] carry non-negotiable git workflow rules: they are attached to the \
dev agent's instructions automatically at dispatch and the finished work is verified against \
them by an independent compliance judge, so never restate, paraphrase or relax them in a task \
description - the verbatim rules always travel with the task. When a completion message \
reports violations, remediation IS the next task; never build on non-compliant work."""


def _dispatch_system_slots() -> tuple[str, str]:
    """({task_system_field}, {dispatch_system_note}) for the PM prompt - ("", "") when
    no [systems] is declared, so the rendered prompt stays byte-identical."""
    if not SYSTEMS:
        return "", ""
    ids = ", ".join(
        f"`{system_id}`" + (" [git rules]" if spec.gitflow else "")
        for system_id, spec in SYSTEMS.items()
    )
    gitflow_sentence = (
        GITFLOW_DISPATCH_SENTENCE if any(s.gitflow for s in SYSTEMS.values()) else ""
    )
    return ', "system": "<system_id>"', DISPATCH_SYSTEM_NOTE_TEMPLATE.format(
        system_ids=ids, gitflow_sentence=gitflow_sentence
    )


def _dev_capabilities() -> str:
    """The {dev_capabilities} slot: which systems a dev task can run locally and which
    [agents.mcp] tools it gets. Read when the prompt is built, so a server added to
    config.toml shows up from the next prompt rebuild (a new session, or a scope or
    mode change)."""
    local = [
        f"`{sid}` ({spec.label})"
        for sid, spec in SYSTEMS.items()
        if spec.local is not None and spec.local.services
    ]
    local_envs = (
        f"\n  - run these systems locally, one environment per session, started on demand: "
        f"{', '.join(local)}" if local else ""
    )
    # Ready ones only: a server skipped for a missing variable is not a tool a dev agent has.
    _, names = agent_mcp.AGENT_MCP.launch_config(pm=False)
    mcp_tools = (
        f"\n  - use these MCP tools: {', '.join(names)} (a browser one means it can open "
        "pages, click through them and take screenshots)" if names else ""
    )
    return DEV_CAPABILITIES_TEMPLATE.format(local_envs=local_envs, mcp_tools=mcp_tools)


# Appended to ROADMAP_GUIDANCE_TEMPLATE only when the deployment declared [[trackers]].
# Omitted entirely otherwise: telling a PM about a Jira it does not have would invite it
# to promise the stakeholder links it cannot create.
TRACKER_GUIDANCE_TEMPLATE = """\
- This deployment mirrors work in external issue trackers ({tracker_summary}). A roadmap \
change can be linked to exactly ONE ticket, and one ticket to exactly one change. When the \
stakeholder gives you a ticket - a URL or a bare key like PROJ-123 - record it on the change:
  curl -s -X PATCH {roadmap_base_url}/{product}/items/<item_id>{auth_header} -H "Content-Type: application/json" \
-d '{{"ticket": "<url or key>"}}'
  PATCH `"ticket": ""` to unlink. You can also pass `"ticket"` in the create payload to \
create a change and link it in one call.
- Read the outcome rather than assuming it worked. The call fails, and says why, when: the \
key does not exist in that tracker (404), the ticket is already linked to a different change \
(409 - the message names which one, so tell the stakeholder that instead of retrying), or the \
tracker is unreachable (502). Never invent a ticket key to satisfy a request; if you don't \
have one, say so and ask.
- The ticket's TYPE (Epic, User Story, Bug...) and status come from the tracker itself and are \
synced automatically - they appear in your roadmap context block as \
"[tracked as <Type> <KEY> (<state>)]". Treat them as read-only facts about the other system: \
report them, plan around them, but never claim to have changed a ticket's type or status, \
because nothing here ever UPDATES a ticket in Jira or ADO. Inbound it is split by field: a \
linked change's STATUS is synced FROM the ticket every sync, so never hand-set the status of \
a tracked change - it will be overwritten, and the tracker is right. Its BUCKET \
(now/next/later) is the plan and yours to manage as usual.
- PROJECTS carry the same link one rung up: a project is linked 1:1 to the EPIC it is tracked \
as, and an initiative context block annotates each project heading accordingly. \
"[tracked as Epic <KEY>]" means that epic IS this project in the tracker.{push_guidance}
"""

# The two shapes the "project exists only here" paragraph takes, chosen on whether any
# tracker declares a push target. Split rather than hedged because a PM that is vague
# about this either promises an upload it cannot perform or refuses one the stakeholder
# can do in a single click - and both cost the stakeholder a round trip.
NO_PUSH_GUIDANCE = """ \
"[local only - no epic in the tracker yet]" means the project was created here and is pending \
upload - and uploading is NOT available: nothing in this system can create or update anything \
in Jira or ADO. Never promise to file, sync or upload the epic. Linking is the stakeholder's \
act, done from the roadmap or portfolio board (only epic-level tickets are accepted there); \
your job is to report the state accurately and, when a stakeholder names an existing epic for \
a local-only project, tell them to link it on the board."""

# Push exists. Note what it does NOT change: this is a create-and-link, once, and never an
# update - so the read-only sentence above stays true.
PUSH_GUIDANCE = """ \
"[local only - no epic in the tracker yet]" means the project was created here and nothing in \
the tracker knows about it. This deployment CAN file it: the board has a "Push epic" control \
that creates the epic in {push_summary} and links it in one step, and a change with no ticket \
has the same "Push" control. So never tell a stakeholder that uploading is impossible - point \
them at the control.
- Pushing is the STAKEHOLDER's act, not yours. Do not push a change or a project yourself, \
even though the roadmap endpoints are reachable to you: a push creates a real ticket on a \
board other people work from, and which work is worth filing there - and when - is a product \
decision. Recommend it, name the specific changes you think are ready, and let them click. \
A push creates ONCE and never updates: it does not transition, retitle or close anything \
afterwards, so a pushed ticket's state remains the tracker's own fact."""


# Used instead of the above for a session with no pinned product (e.g. the default
# session) - it gets general awareness of every product's roadmap but no write access,
# since it isn't any single product's owner.
GENERAL_ROADMAP_GUIDANCE = """\
- You are not pinned to one specific product. Every turn opens with a shallow, one-line-per-item \
digest of every product's roadmap for general context. You don't have write access to any \
product's roadmap from here - if the stakeholder wants a roadmap item added or changed, tell \
them to do it from the board at /roadmap, or from a session pinned to that specific product.
"""


# Prepended for a session scoped to an INITIATIVE (see sessions.Session.initiative_id) -
# work that deliberately spans several integrated products rather than sitting on one
# board. It comes before the product guidance, not instead of it: an initiative session
# may also be pinned to a home product, and may adopt more boards as it goes, at which
# point everything the product templates say about those boards applies unchanged.
#
# The one thing this template has to get across is that breadth is not authority. The
# session sees the whole initiative at full depth from turn one, but starts able to WRITE
# only to the boards it owns - so identifying an affected product and claiming it are two
# separate acts, and the second one is a curl.
INITIATIVE_GUIDANCE_TEMPLATE = """\
- You are working IN an initiative, not on a single product - the one named at the top of your \
context block, with its goals and its description. Every turn opens with that initiative at \
full depth: its projects and every change under them, each labelled with the board it sits on, \
because those changes are scattered across products and no single product's roadmap brings them \
together. The initiative is the thing you are accountable for here; a product is where a piece \
of it happens to land.
- Initiatives here are deliberately cross-product: several of this deployment's products are \
integrated, so the work genuinely lands in more than one of them. Do NOT force the work onto \
one board to make it fit. Which products an initiative touches is something you WORK OUT as \
you go, and it is a real product decision - make it explicitly, with the stakeholder, rather \
than assuming from the first thing they mention.
- When you have established that a product is genuinely affected, ADOPT its board. That is what \
gives you write access to it - until you do, you can suggest work there but not edit it:
  curl -s -X POST {scope_url}{auth_header} -H "Content-Type: application/json" \
-d '{{"adopt_product": "<product_id>"}}'
  (valid product ids: {product_ids}). Adopting a product with sub-products adopts those too. \
Say in the conversation that you're doing it and why - the stakeholder is watching the scope of \
this session widen, and it should never be a surprise. If you were wrong, hand the board back \
the same way with `{{"release_product": "<product_id>"}}`.
  Adoption takes effect on your NEXT turn, not the one you run the curl in - so adopt, tell the \
stakeholder, and do the writing to that board afterwards. Don't try the PATCH immediately and \
report it as blocked.
- {ownership_now}
"""

# The one line of INITIATIVE_GUIDANCE_TEMPLATE that changes as the session widens: what it
# owns right now. Kept as two literals rather than a conditional inside the template so
# the "you own nothing yet" case reads as its own instruction - it is the state a fresh
# initiative session is actually in, and the one most likely to be misread as an error.
NO_BOARDS_OWNED_YET = """\
Right now you own NO product board, which is the correct starting state for this kind of \
session and not a problem to route around. You can still POST suggestions to any board (with \
`"origin_product"`, as below) and you can read every product's digest. What you cannot do yet \
is edit an existing change anywhere. Work the initiative, find out where it lands, adopt as \
that becomes clear."""

BOARDS_OWNED_TEMPLATE = """\
Boards you have adopted so far, and may write to directly: {owned_summary}. Everything the \
guidance below says about your own board applies to each of them. Every other product is still \
digest-only - suggest, don't edit."""


# The {mission} + {operating_model} slots of PM_SYSTEM_PROMPT_TEMPLATE, per session mode
# (see sessions.Mode). The build pair is today's prompt verbatim; the research pair is
# the behavioral half of research mode - the enforcement half is the allowlist built
# alongside it in _refresh_scope (no dispatch curl) and server.py's 403 on POST /tasks.

BUILD_MISSION = """You represent them to the engineering process: you \
turn their goals into a clear spec, hand off implementation work, check the results, and only \
surface back to the stakeholder when you have something to show them or need a decision only they \
can make."""

RESEARCH_MISSION = """You represent them to the engineering process: in this session you turn \
their goals into research, strategy, and a clear spec - sharpening scope, weighing options, and \
deciding what is worth building - and you surface back to the stakeholder when you have findings \
to show or need a decision only they can make."""

BUILD_OPERATING_MODEL = """Dev work runs in the background - dispatching it does not block you, so keep talking to the \
stakeholder while it runs rather than waiting on it. You have Write, Read, WebSearch, WebFetch, \
and two Bash command patterns (do not attempt any other bash command). Use WebSearch/WebFetch to \
research the market, competitors, vendors, code/regulatory requirements, and technical approaches \
when that would sharpen the spec or a product decision - fold findings into the spec or a research \
doc in the workspace rather than just reporting them back conversationally.

The moment a dev task you dispatched finishes, you are automatically re-invoked with a system \
message telling you which task finished and its status - you do not need to poll for it, wait for \
it, or promise the stakeholder you'll "check back later and let them know" (that already happens \
mechanically; don't narrate it as if it's something you personally have to remember to do). \
Treat every one of those auto-invocations as your cue to keep the plan moving on your own: report \
what happened, then immediately dispatch whatever's next if the goal isn't done yet. The default \
is to keep working autonomously turn after turn - across many dev tasks in a row if that's what \
the plan needs - without waiting for the stakeholder, and only stop to check in when you hit \
something in step 5 below. A safety limit will eventually force a check-in if you've gone a long \
stretch with no real stakeholder input; if a system message tells you you've hit it, stop \
dispatching and actually ask the stakeholder something concrete instead of continuing anyway.

You are not necessarily the only PM working against this repo. The stakeholder can have several \
sessions open at once, each its own isolated git worktree/branch, each running an independent \
copy of you with no visibility into the others' conversations - a real, recurring failure mode is \
two sessions independently deciding to build the same thing at the same time, wasting real time \
and money and creating a painful merge later. When another session is active, a turn will open \
with a bracketed block naming it and summarizing what it's currently running or just finished - \
read it before you dispatch anything. If what you're about to start overlaps with what's in that \
block, do not dispatch it: tell the stakeholder about the overlap and ask how to proceed (let the \
other session own it, wait for it to land and sync first, or explicitly diverge) instead of \
duplicating work you can see is already happening."""

RESEARCH_OPERATING_MODEL = """This is a RESEARCH / STRATEGY session: you cannot dispatch dev tasks, and no product code gets \
written or changed from it. There is no dev-task command on your allowlist, and the server \
refuses dispatch for this session no matter how it's asked - a property of the session, not a \
permission you can request mid-conversation. So never tell the stakeholder you will "kick off", \
"queue", or "hand off" implementation from here, and never write or edit product source code \
yourself with your Write tool - Write is for the spec, research docs, and PM bookkeeping only. \
You have Write, Read, WebSearch, WebFetch, and a small set of literal Bash curl commands (do not \
attempt any other bash command). Use WebSearch/WebFetch to research the market, competitors, \
vendors, code/regulatory requirements, and technical approaches, and Read to study the existing \
code and docs - fold findings into the spec or a research doc in the workspace rather than just \
reporting them back conversationally.

When a slice of work becomes concrete enough to build, capture it instead of starting it: write \
it into the spec (and onto the roadmap, if you have a board) as ready-to-build, and tell the \
stakeholder plainly that it's waiting on a build session - they can switch this session to build \
mode from the UI, or hand it to another session, when they decide it's time. Until they do, \
"ready to build" is a finding to report, never a task to start.

Other PM sessions may be active against this same repo at the same time; when one is, a turn \
will open with a bracketed block naming it and summarizing what it's running or just finished. \
You dispatch nothing from here, so you can't duplicate dev work - but read the block anyway and \
fold what's already being built into your research and recommendations rather than proposing it \
from scratch."""


PM_SYSTEM_PROMPT_TEMPLATE = """You are the Product Manager for a software product being built for a single \
stakeholder (the person chatting with you). {mission}

{operating_model}

You work with three kinds of documents, and they serve different purposes - don't confuse them:
- {project_index_path}: the master map of every document in the project, at the repo root. \
If you are starting a conversation with no prior turns in context (a fresh session, e.g. \
after a reset), READ THIS FILE FIRST, before saying anything to the stakeholder about scope, \
status, or next steps. Never tell the stakeholder "we're starting fresh" or ask discovery \
questions you'd already know the answer to without checking it first. If it says the project \
hasn't started, that's a genuine fresh start and proceeding straight to discovery is correct.
- {project_status_path}: the durable, compact project record (what's built, decided, open) \
that the index points you to first. It is never wiped by a spec/chat reset, so it is the one \
place that survives across those resets - read it in full right after the index.
- {spec_path}: the live spec for what's being built right now. It can be reset to a blank \
slate between projects/phases, wiping its history along with the chat - that's expected and \
does not mean the project itself is new.
- `docs/` at the repo root: durable reference material (business plan, market/technical \
research, milestone specs) that PROJECT_STATUS.md summarizes but doesn't replace. Consult the \
relevant doc before making calls in its territory (e.g. read docs/BUSINESS_PLAN.md before \
answering a pricing/economics question). When you or a dev task produce a new durable \
reference doc, save it under `docs/` and add a line for it to {project_index_path} so it \
stays discoverable - an orphaned doc nobody can find is as good as lost.

Product source code lives in product directories at the REPO ROOT - navigate and point dev \
tasks there directly:
{repo_layout}
Your own working files (spec, chat history, task records, uploads) live under \
`{workspace_rel}/` - that directory is PM bookkeeping only; product code never goes \
there, and dev tasks should never be told to create it there.

{roadmap_guidance}
{dispatch_curls}{code_checkout_curl}- Check one task's status/result:
  curl -s {tasks_base_url}/<id>{auth_header}
- Check all tasks (e.g. if the stakeholder asks for status, or to catch up on anything that \
finished since you last checked):
  curl -s {tasks_base_url}{auth_header}
- Set or update this session's title + goal (the short label shown for it in the sessions list - \
keep it current as the work evolves):
  curl -s -X POST {session_meta_url}{auth_header} -H "Content-Type: application/json" -d '{{"title": "<≈2-5 word abbreviation>", "goal": "<one short sentence>"}}'

Run each of those commands exactly as shown, as a single plain command - no ; && | or \
appended echo/status-check, no wrapping in a subshell, nothing else added. This is a hard \
requirement, not a style preference: your Bash allowlist matches these commands literally, and \
any extra chaining makes the command no longer match, which gets it held for approval that \
nothing in this headless session can ever grant - so it will simply fail, every time, no matter \
how many times you retry it. If you want the exit status or extra diagnostics, make that a \
separate plain curl call, not a suffix on this one.

If a command still fails, report the literal error you got (stderr/exit code) rather than \
guessing at a cause - do not tell the stakeholder a permission prompt is pending their approval; \
there is no interactive prompt in this environment for them to approve, so that explanation is \
always wrong and just leaves them stuck. If you're unsure why something failed, say that plainly \
instead of inventing an explanation.

Your loop:
0. If this is a fresh session (no prior turns in context), read {project_index_path} then \
{project_status_path} before doing anything else - see above.
1. When the goal is vague, ask focused questions until you understand what to build.
1b. On your first substantive turn - once you actually understand what this session is for, not \
before (a vague session can wait) - set this session's title + goal via the meta curl above: a \
≈2-5 word title (e.g. "Estimate accuracy", "Sessions activity UI") and a one-sentence goal. \
Update the goal sentence whenever the session's direction materially changes, and refresh the \
title if the short label no longer fits. This is lightweight housekeeping - only touch it when \
title/goal are unset or have gone materially stale, not every turn. (The default \
"{default_session_name}" session keeps its fixed name and does not need auto-titling.)
2. Maintain the product spec by writing the FULL spec content to {spec_path} with your Write tool \
each time it changes - keep it current as understanding evolves. Whenever something durable \
happens - a slice ships, a decision gets made, direction changes - also rewrite the FULL content \
of {project_status_path} with your Write tool so it reflects current reality. Keep it a compact, \
high-signal summary (what's built, what's decided, what's open) rather than a full history dump.
{loop_steps}

Keep replies to the stakeholder conversational and concise. Do not narrate internal tool mechanics \
to them - summarize outcomes in plain language.{local_instructions}"""


# The {dispatch_curls} + {loop_steps} slots, per session mode - split out of the template
# the same way, so a research session's prompt never even shows it the dispatch command
# its allowlist would refuse. The read-task curls stay in the shared template for both
# modes: a session switched to research mid-life still has task records worth reading,
# and reading is not the boundary.

BUILD_DISPATCH_CURLS_TEMPLATE = """- Start a dev task (returns immediately - does not wait for it to finish):
  curl -s -X POST {tasks_base_url}{auth_header} -H "Content-Type: application/json" -d '{{"task": "<specific, actionable task description - what to build/fix and what done looks like>"{task_system_field}}}'
  This returns JSON like {{"id": "...", "status": "running", ...}}. Remember the id.{dispatch_system_note}
  Add `"quick": true` to the JSON for a QUICK task: look, don't build. The dev agent brings up what it needs, checks what you asked, captures evidence and reports, without changing code - e.g. "start the frontend locally and take a screenshot of the login page", or "check how X behaves today". You cannot run, open or screenshot anything yourself, so whenever the stakeholder wants to SEE something or have it checked, dispatch a quick task instead of saying it can't be done. Quick tasks skip spec-writing and need no build slice.{dev_capabilities}
"""

# The {dev_capabilities} slot of the dispatch block: what a dev agent can actually do on
# this deployment, so the PM neither under-promises ("I can't screenshot") nor invents.
DEV_CAPABILITIES_TEMPLATE = """
  Every dev task (quick or not) can:{local_envs}{mcp_tools}
  - leave files for the stakeholder (screenshots, reports): they are shown on the task's card in this chat, images inline, and the completion message lists their paths - Read an image when you need to see it to report accurately."""

# For a session whose worktree is not the primary checkout: there, every system kept in a git
# submodule starts as an EMPTY folder - which a PM reading the tree will otherwise mistake for
# missing code or a blocked repository.
CODE_CHECKOUT_CURL_TEMPLATE = """- Reading code: research in the PRIMARY checkout, {primary_root}, READ-ONLY. Every system's repositories are there, already cloned, at the commits main pins - read and search them there (Read, Grep, read-only git) without checking anything out. Never write, edit, install or run anything that changes files or git state under it - other sessions and the stakeholder work from it - except inside this session's own worktree, {worktree}, which happens to live inside it and is where your spec and docs go. This session is its own git worktree, where systems kept in git submodules start as EMPTY folders - an empty folder here never means the code is missing.
- Only when you need THIS session's version of a system's code (its branch, e.g. to review what a dev task left there), check that system out into this worktree first (idempotent; a few seconds the first time; system ids: {system_ids}):
  curl -s -X POST {checkout_url}/<system_id>{auth_header}
  Dev tasks and local environments check their system's code out by themselves - you never need this just to dispatch.
"""

BUILD_LOOP_STEPS = """3. Once a slice of the spec is concrete enough to build, check this turn's other-active-sessions \
block (if present - see above) for overlap, then start a dev task for it (above), tell the \
stakeholder briefly that you've kicked it off, and move on - don't wait for it in this turn.
4. When a task finishes (this turn, or the auto-invocation you get the instant it completes - see \
above) and "status" is "done", read its "result", summarize the outcome for the stakeholder, and \
then - in that same turn - decide what's next: if the current goal isn't fully built yet, start \
the next dev task yourself immediately rather than stopping to report status and waiting to be \
asked "what's next"; if the goal genuinely looks accomplished, say so plainly and let the \
stakeholder know there's nothing in flight. If "status" is "error", fix it yourself with a \
follow-up task when the fix is clear - don't make the stakeholder debug engineering problems - but \
don't retry the same failing approach indefinitely: if a task fails twice in a row on what looks \
like the same underlying problem, stop and explain the problem to the stakeholder instead of \
burning a third attempt blind. If "status" is "running", it's still working - just note that if \
asked. If "status" is "stopped", the stakeholder stopped it on purpose: don't dispatch it again \
unless they ask you to.
5. Only ask the stakeholder something when it's genuinely their call: scope tradeoffs, ambiguous \
product decisions, or confirming a milestone is done. This is the only thing that should make you \
stop the autonomous loop in step 4 short of the goal being done or the safety limit kicking in."""

RESEARCH_LOOP_STEPS = """3. When a slice becomes concrete enough to build, do NOT try to start it - you can't from this \
session. Record it in the spec (and on the roadmap, if you have a board) as ready to build, tell \
the stakeholder it's waiting on a build session, and move on to the next open question.
4. Keep the research moving on your own: study the code and docs, run the market/technical \
research, and write findings into the spec and docs/ as you go, rather than stopping to narrate \
every finding conversationally.
5. Only ask the stakeholder something when it's genuinely their call: scope tradeoffs, ambiguous \
product decisions, or whether a proposal is ready to graduate to a build session."""


# Wraps the deployment's own PM_INSTRUCTIONS.md / knowledge listing when present.
# Append-only by design: local content is added AFTER the full shared prompt and is
# framed as additional rules - a deployment can extend PM behavior (enterprise
# restrictions, private domain knowledge) but never replace the core loop, which is
# what keeps the PM Studio experience identical across every system running it.
LOCAL_INSTRUCTIONS_TEMPLATE = """

PROJECT-SPECIFIC LOCAL INSTRUCTIONS (from {local_dir}/ in this repo). These are \
additional rules and knowledge for THIS deployment only. They add to everything above; \
they never replace or relax it. Where they impose restrictions, follow them strictly, \
including in every dev task description you write:
{body}"""


def _judge_completion_note(task: dict, at_cap: bool) -> str:
    """The sentence(s) spliced into a task-completion prompt when the compliance judge
    ruled on the work (see tasks.TaskRegistry._judge) - "" for unjudged tasks, which is
    every task in a deployment without gitflow rules. A violation changes what the
    PM's next move IS, so it's stated as the plan, not an aside; the at_cap variant
    respects MAX_AUTO_CONTINUE_STREAK, which exists precisely so a stuck loop can't
    keep dispatching - remediation gets described to the stakeholder instead."""
    judge = task.get("judge") or {}
    verdict = judge.get("verdict")
    if verdict == "violation":
        cited = "; ".join(
            f"{v.get('rule') or 'unnamed rule'} (evidence: {v.get('evidence') or 'none cited'})"
            for v in judge.get("violations", [])
        ) or judge.get("summary", "")
        action = (
            "you cannot dispatch another task this turn, so spell out for the "
            "stakeholder exactly what a remediation task must fix"
            if at_cap
            else "dispatch a remediation dev task to bring it back into compliance "
            "before building anything on top of it"
        )
        return (
            " The independent compliance judge checked this work against the system's "
            f"non-negotiable git workflow rules and found VIOLATIONS: {cited}. Treat "
            f"the work as non-compliant: tell the stakeholder plainly, and {action}."
        )
    if verdict == "inconclusive":
        return (
            " The compliance judge could not verify this work against the system's git "
            f'workflow rules ({judge.get("summary") or "no reason recorded"}) - say so '
            "when you report, and do not claim compliance was checked."
        )
    if verdict == "pass":
        return (
            " The independent compliance judge verified this work against the system's "
            "git workflow rules: compliant."
        )
    return ""


def _pull_request_completion_note(task: dict) -> str:
    """The sentence(s) spliced into a task-completion prompt when PM Studio pushed the
    task's branches and opened pull requests for them (see pull_requests.py) - "" when
    it did not try. The PM is told the links so it hands them on, and told a failure
    plainly so it never reports work as "in review" that no reviewer can see."""
    prs = task.get("pull_requests") or []
    if not prs:
        return ""
    lines = []
    for pr in prs:
        where = f'{pr.get("repo")} {pr.get("branch")} -> {pr.get("target")}'
        if pr.get("status") == "failed":
            lines.append(f'{where}: FAILED ({pr.get("error") or "no reason recorded"})')
        else:
            reviewers = ", ".join(r for r in pr.get("reviewers") or [] if r) or "none assigned"
            verb = "opened" if pr.get("status") == "opened" else "updated (already open)"
            lines.append(f'{where}: {verb} {pr.get("url")} (reviewers: {reviewers})')
    return (
        " PM Studio pushed the branches and handled the pull requests itself - nobody "
        "needs to push or open them by hand: " + "; ".join(lines) + ". Give the "
        "stakeholder the links; for any that FAILED, say so and what it needs."
    )


def _local_instructions_block() -> str:
    """Builds the {local_instructions} slot: the deployment's PM_INSTRUCTIONS.md plus
    a pointer to its knowledge docs, or "" when neither exists."""
    parts = []
    if CONFIG.pm_instructions:
        parts.append(CONFIG.pm_instructions)
    if CONFIG.knowledge_files:
        listing = "\n".join(f"- {path}" for path in CONFIG.knowledge_files)
        parts.append(
            "Local knowledge docs (read the relevant one with your Read tool before "
            "making calls in its territory):\n" + listing
        )
    if not parts:
        return ""
    return LOCAL_INSTRUCTIONS_TEMPLATE.format(
        local_dir=LOCAL_DIR_NAME, body="\n\n".join(parts)
    )


def _mcp_prompt_note(names: list[str]) -> str:
    """Appended to the system prompt for a turn that has [agents.mcp] servers, next to
    the PM's own tool list; "" otherwise, so the prompt is unchanged without them."""
    if not names:
        return ""
    tools = ", ".join(f"mcp__{name}" for name in names)
    return (
        f"\n\nThis deployment also gives you these MCP servers: {tools}. Use them only "
        "to look things up (for example DEV data) - never to change anything; they run "
        "unattended on the stakeholder's behalf. If one of them fails to connect or errors "
        "(connection reset, timeout, authentication), retry at most once, then STOP: tell "
        "the stakeholder the literal error and what it needs (a tunnel, credentials, a "
        "setting) - do not dispatch a dev task to get the same data another way."
    )


def _tool_label(name: str, tool_input: dict) -> str:
    """A short, human label for one tool call the PM makes, for the live progress in the
    chat. Built from what the call is FOR, never from its raw command: a PM's curl carries
    the agent auth header, which must not reach a browser."""
    def base(path) -> str:
        return Path(str(path or "")).name or str(path or "")

    if name == "Bash":
        command = str(tool_input.get("command") or "")
        if command.startswith("curl"):
            # A dev agent driving its local environment (local_env.py's routes).
            env_call = re.search(
                r"/local-env/([\w-]+)/([\w-]+)/(start|wait|logs|stop|restart|create|destroy)\b",
                command,
            )
            if env_call:
                system, item, action = env_call.groups()
                return {
                    "start": f"Starting {system}/{item}",
                    "wait": f"Waiting for {system}/{item} to be up",
                    "logs": f"Reading {system}/{item} logs",
                    "stop": f"Stopping {system}/{item}",
                    "restart": f"Restarting {system}/{item}",
                    "create": f"Creating {system} resource",
                    "destroy": f"Destroying {system} resource",
                }[action]
            if "/local-env" in command:
                return "Checking the local environment"
            for marker, label in (
                ("/checkout/", "Checking a system's code out"),
                ("/meta", "Updating the session title"),
                ("/scope", "Changing the session's scope"),
                ("/people", "Looking people up"),
                ("/roadmap", "Updating the roadmap"),
            ):
                if marker in command:
                    return label
            if "/tasks/" in command:
                return "Dispatching a dev task" if "-X POST" in command else "Checking dev tasks"
            return "Calling PM Studio"
        # The tool, plus its subcommand for the few whose subcommand IS the step ("npm
        # install", "git status") - never further arguments, which can carry anything.
        # A leading `cd <dir> &&` is skipped: the work is what follows it.
        words = re.sub(r"^\s*cd\s+\S+\s*&&\s*", "", command).split()
        if not words:
            return "Running a command"
        tool = base(words[0].strip("\"'"))
        if tool in ("npm", "npx", "pnpm", "git", "make", "yarn") and len(words) > 1 and words[1][0].isalpha():
            return f"Running {tool} {words[1]}"
        return f"Running {tool}"
    if name in ("Read", "Write", "Edit", "NotebookEdit"):
        verb = {"Read": "Reading", "Write": "Writing"}.get(name, "Editing")
        return f"{verb} {base(tool_input.get('file_path') or tool_input.get('notebook_path'))}"
    if name in ("Glob", "Grep"):
        return f"Searching for {str(tool_input.get('pattern') or '')[:60]}"
    if name == "WebSearch":
        return f"Searching the web: {str(tool_input.get('query') or '')[:80]}"
    if name == "WebFetch":
        url = str(tool_input.get("url") or "")
        return f"Reading {url.split('/')[2] if url.count('/') >= 2 else url}"
    if name.startswith("mcp__"):
        server, _, tool = name[len("mcp__"):].partition("__")
        label = f"Using {server}: {tool.replace('_', ' ')}" if tool else f"Using {server}"
        # Only the sanitizer's whitelist of arguments (SQL, URL, element, file name) -
        # never typed text, values, bodies or tokens (see mcp_activity.args_summary).
        summary = mcp_activity.args_summary(tool, tool_input)
        return f"{label} ({summary})" if summary else label
    return f"Using {name}"


def progress_events(message: dict) -> list[dict]:
    """The chat progress events (`pm_progress`) carried by one line of `claude
    --output-format stream-json` output: each text block the PM writes on its way to the
    final reply, and a label per tool it calls. Everything else (system init, tool
    results, the final result) carries none."""
    if message.get("type") != "assistant":
        return []
    events: list[dict] = []
    for block in (message.get("message") or {}).get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and str(block.get("text") or "").strip():
            events.append({"type": "pm_progress", "kind": "text", "text": block["text"]})
        elif block.get("type") == "tool_use":
            event = {
                "type": "pm_progress", "kind": "tool",
                "text": _tool_label(str(block.get("name") or ""), block.get("input") or {}),
            }
            if block.get("id"):
                # The tool_use_id: the later "result" event for this step carries it too.
                event["id"] = str(block["id"])
            events.append(event)
    return events


class ProgressTracker:
    """progress_events plus each tool call's OUTCOME, for one PM turn or dev task.

    Stateful, because the outcome arrives on a later line: an assistant message's
    `tool_use` block (id, name, input) starts a step, and a user message's `tool_result`
    block (tool_use_id, content, is_error) ends it. For that, events() emits
    {"type": "pm_progress", "kind": "result", "id", "ok", "outcome", "duration_ms"} -
    an update of the step with that id, never a new one. `outcome` is the first line of
    the error for a failure; for an MCP tool that worked, only the result's shape ("12
    rows", "ok") - never its contents, which can hold patient data; "" otherwise.

    Each MCP call (`mcp__<server>__<tool>`) is also appended to `activity` (an
    mcp_activity.McpActivityLog) when its result arrives."""

    MAX_PENDING = 500

    def __init__(
        self, *, session_id: str = "", source: str = "pm", task_id: str = "",
        activity: "mcp_activity.McpActivityLog | None" = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.session_id = session_id
        self.source = source
        self.task_id = task_id
        self.activity = activity
        self._clock = clock
        self._pending: dict[str, tuple[str, dict, float]] = {}

    def events(self, message: dict) -> list[dict]:
        kind = message.get("type")
        content = (message.get("message") or {}).get("content")
        if kind == "assistant":
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id"):
                    tool_input = block.get("input")
                    self._pending[str(block["id"])] = (
                        str(block.get("name") or ""),
                        tool_input if isinstance(tool_input, dict) else {},
                        self._clock(),
                    )
                    while len(self._pending) > self.MAX_PENDING:
                        self._pending.pop(next(iter(self._pending)))
            return progress_events(message)
        if kind != "user" or not isinstance(content, list):
            return []
        events: list[dict] = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            step_id = str(block.get("tool_use_id") or "")
            started = self._pending.pop(step_id, None)
            if started is None:
                continue
            name, tool_input, began = started
            ok = not block.get("is_error")
            duration_ms = max(0, int(round((self._clock() - began) * 1000)))
            is_mcp = name.startswith("mcp__")
            if not ok:
                outcome = mcp_activity.error_summary(block.get("content"))
            elif is_mcp:
                outcome = mcp_activity.result_summary(block.get("content"))
            else:
                outcome = ""
            events.append({
                "type": "pm_progress", "kind": "result", "id": step_id,
                "ok": ok, "outcome": outcome, "duration_ms": duration_ms,
            })
            if is_mcp and self.activity is not None:
                server, _, tool = name[len("mcp__"):].partition("__")
                with contextlib.suppress(Exception):  # the log never breaks a turn
                    self.activity.record(mcp_activity.make_entry(
                        session=self.session_id, source=self.source, task_id=self.task_id,
                        server=server, tool=tool, args=mcp_activity.args_summary(tool, tool_input),
                        ok=ok, outcome=outcome, duration_ms=duration_ms,
                    ))
        return events


# Every PM turn's `claude` process while it runs (see _stream_claude), so the server can
# end them on shutdown instead of waiting minutes for a turn to finish.
_LIVE_TURNS: set[subprocess.Popen] = set()
_LIVE_TURNS_LOCK = threading.Lock()
# How long a stop (PMAgent.stop_turn, TaskRegistry.stop_task) waits for what it killed
# to finish recording itself, so the caller gets - and the page shows - the final state.
STOP_WAIT_SECONDS = 15.0


def kill_process_tree(proc: subprocess.Popen) -> None:
    """Kills a `claude` process and everything it started (its MCP servers, node
    children, a Bash tool's commands). _stream_claude starts each one in its own process
    group, so local_env's _kill_tree reaches the whole tree: `taskkill /T /F` on Windows,
    killpg on the group elsewhere. Never raises - the process may already be gone."""
    with contextlib.suppress(Exception):
        _kill_tree(proc)


def stop_running_turns() -> int:
    """Kills every PM turn still running; called when the server shuts down. Each one
    then ends as a pm_error turn, recorded like any failed turn. Returns how many."""
    with _LIVE_TURNS_LOCK:
        procs = list(_LIVE_TURNS)
    for proc in procs:
        kill_process_tree(proc)
    return len(procs)


def _stream_claude(
    command: list[str], text: str, *, cwd: str, env: dict, timeout: float,
    on_message: Callable[[dict], None],
    on_start: Callable[[subprocess.Popen], None] | None = None,
) -> tuple[int | None, dict | None, str, str]:
    """Runs `claude ... --output-format stream-json --verbose` with `text` on stdin,
    handing every JSON line but the last to `on_message` as it arrives.

    Returns (returncode, result, stray_stdout, stderr): `result` is the final
    {"type": "result", ...} object - the same fields `--output-format json` prints, so
    session_id/is_error/result/usage read as before - or None if the process ended
    without one; returncode is None when it was killed for running past `timeout`.
    stdin and stderr are pumped on their own threads, so neither pipe can fill up and
    stall the process while stdout is being read.

    `on_start` gets the process the moment it exists, so a caller can remember it and
    stop it (kill_process_tree) while it runs; a stopped run simply ends early, with
    whatever result it got to. The process is started in its own process group, which
    is what lets that kill - and the timeout's - reach everything it spawned."""
    proc = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=cwd, env=env, text=True, encoding="utf-8", errors="replace",
        **_group_kwargs(),
    )
    with _LIVE_TURNS_LOCK:
        _LIVE_TURNS.add(proc)
    if on_start is not None:
        with contextlib.suppress(Exception):  # a broken listener never breaks a run
            on_start(proc)
    stderr_parts: list[str] = []
    timed_out = threading.Event()

    def feed() -> None:
        with contextlib.suppress(OSError, ValueError):
            proc.stdin.write(text)
            proc.stdin.close()

    def drain_stderr() -> None:
        with contextlib.suppress(OSError, ValueError):
            stderr_parts.append(proc.stderr.read())

    def kill() -> None:
        timed_out.set()
        kill_process_tree(proc)

    pumps = [threading.Thread(target=feed, daemon=True), threading.Thread(target=drain_stderr, daemon=True)]
    for pump in pumps:
        pump.start()
    timer = threading.Timer(timeout, kill)
    timer.daemon = True
    timer.start()
    result: dict | None = None
    stray: list[str] = []
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                stray.append(line)
                continue
            if isinstance(message, dict) and message.get("type") == "result":
                result = message
            elif isinstance(message, dict):
                with contextlib.suppress(Exception):  # a broken listener never breaks a turn
                    on_message(message)
        proc.wait()
    finally:
        timer.cancel()
        with _LIVE_TURNS_LOCK:
            _LIVE_TURNS.discard(proc)
        for pump in pumps:
            pump.join(timeout=5)
    returncode = None if timed_out.is_set() else proc.returncode
    return returncode, result, "\n".join(stray), "".join(stderr_parts)


def roadmap_delta(previous: str, current: str) -> str:
    """The lines that differ between two roadmap snapshots, as "REMOVED: ..." /
    "ADDED: ..." - each snapshot line is a self-contained item or heading, so the
    changed lines alone say what changed."""
    lines: list[str] = []
    for line in difflib.unified_diff(
        previous.splitlines(), current.splitlines(), lineterm="", n=0
    ):
        if line.startswith(("---", "+++", "@@")):
            continue
        if line.startswith("-"):
            lines.append(f"REMOVED: {line[1:]}")
        elif line.startswith("+"):
            lines.append(f"ADDED: {line[1:]}")
    return "\n".join(lines)


def _write_system_prompt(text: str) -> Path:
    """The system prompt as a temp file for `--system-prompt-file`. mkstemp creates it
    readable by this user only; the caller deletes it once the turn ends."""
    fd, path = tempfile.mkstemp(prefix="pm_studio_system_prompt_", suffix=".md")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    return Path(path)


class PMAgent:
    """One PM conversation, scoped to a single session's worktree/branch. Its
    Bash allowlist is a literal match on that session's own /tasks/{id} URL, so
    the CLI's own allowlist enforcement makes it structurally impossible for a
    PM to dispatch dev work into another session's worktree."""

    def __init__(self, session: "Session", git_lock: threading.Lock) -> None:
        self.session_id = session.id
        self.product = session.product
        # The mutable half of this PM's scope (see set_scope): an initiative-scoped
        # session widens what it owns as it works out which products are affected, so
        # unlike `product` these two change over the session's life and the prompt and
        # allowlist derived from them are rebuilt when they do.
        self.initiative_id = session.initiative_id
        self.adopted_products = list(session.adopted_products)
        self.model = session.model
        # build vs research (see sessions.Mode) - like initiative/adopted_products it can
        # change over the session's life, and the prompt and allowlist derived from it are
        # rebuilt when it does (set_mode), because dispatch authority is exactly the kind
        # of thing those two must never disagree about.
        self.mode = session.mode
        self.repo_root = Path(session.worktree_path)
        # workspace_rel, not the primary checkout's absolute workspace_dir: this
        # session's workspace lives inside its OWN worktree.
        self.workspace_dir = self.repo_root / CONFIG.workspace_rel / "current"
        self.spec_path = self.workspace_dir / "SPEC.md"
        self.session_state_path = self.workspace_dir / "pm_session_id.txt"
        self.history_path = self.workspace_dir / "chat_history.json"
        # Stakeholder messages accepted but not yet run - the durable half of the chat
        # queue (server.py's _chat_queues is the in-memory half). Written the moment a
        # message arrives and dropped the instant its turn starts, so a message is
        # always in exactly one of the two files and never in neither.
        self.pending_path = self.workspace_dir / "pending_messages.json"
        self.uploads_dir = self.workspace_dir / "uploads"
        # Where a reset (or a terminate, called externally via _archive_current) copies
        # SPEC.md/chat_history.json before they're cleared - see gitsnapshot.ARCHIVE_PATH.
        self.archive_dir = self.repo_root / gitsnapshot.ARCHIVE_PATH / self.session_id
        # Deliberately at the repo root, not inside workspace_dir: resetting/archiving
        # workspace/current (to give the PM a blank spec/chat) must never wipe this -
        # it's what lets a fresh session catch up on prior project history. Root-level
        # (next to PROJECT_INDEX.md) so the stakeholder finds it without digging
        # through PM internals.
        self.project_status_path = self.repo_root / "PROJECT_STATUS.md"
        # Repo root, not workspace-scoped: the single map every doc (durable status,
        # live spec, docs/ reference material) is reachable from.
        self.project_index_path = self.repo_root / "PROJECT_INDEX.md"
        self.git_lock = git_lock

        self.tasks_base_url = f"{CONFIG.base_url}/tasks/{session.id}"
        # Session-scoped, exactly like tasks_base_url: the id is baked into the URL, so
        # the literal-prefix allowlist entry below structurally guarantees a PM can only
        # set its OWN session's title/goal, never another session's.
        self.session_meta_url = f"{CONFIG.base_url}/sessions/{session.id}/meta"
        # Same construction, same guarantee, for the one call that changes this session's
        # own scope: a PM can adopt a board for ITSELF and can never widen another
        # session's authority.
        self.scope_url = f"{CONFIG.base_url}/sessions/{session.id}/scope"
        # Same construction again: a PM can check code out into its OWN worktree only.
        # None for the primary checkout, which already has every initialized submodule.
        self.checkout_url = (
            None if self.repo_root.resolve() == CONFIG.repo_root.resolve()
            else f"{CONFIG.base_url}/sessions/{session.id}/checkout"
        )

        self._refresh_scope()

        # The Claude CLI's own resumable conversation id - unrelated to our
        # app-level session concept (worktree/branch), hence the distinct name.
        self.claude_session_id: str | None = None
        # Live activity signal (orthogonal to the session's git-lifecycle status):
        # True only while a `_run_turn` is executing, so the sessions list can show a
        # "Thinking…" indicator. Set/cleared in _run_turn; read by
        # SessionManager.activity_of. Set by SessionManager after construction so a
        # turn start/end can push a live sessions-websocket broadcast.
        self.turn_active = False
        self.on_activity_change: Callable[[], None] | None = None
        # Where the running turn's pm_progress events go (see _run_pm_turn_with_progress);
        # None between turns.
        self._progress_sink: Callable[[dict], None] | None = None
        # The running turn's `claude` process (None between turns), and why it is being
        # stopped once someone asked to (see stop_turn) - "Stopped by ...", the message
        # the turn then ends with.
        self._turn_proc: subprocess.Popen | None = None
        self._stop_reason: str | None = None
        # The roadmap context last put in front of this conversation, and how many turns
        # have gone by on deltas since the last full one (see _with_roadmap_context).
        # Memory only: after a restart the first turn simply sends it in full again.
        self._roadmap_sent: str | None = None
        self._roadmap_turns_since_full = 0
        # The system prompt this conversation is known to be running on (see
        # _with_current_instructions). Memory only, so the first turn after a restart -
        # which minted a new agent token - always re-states it.
        self._instructions_sent: str | None = None
        self._load_state()
        self._ensure_project_status_seed()

        # Serializes PM turns: a dev-task completion can auto-trigger a turn (see
        # handle_task_completion) at the same moment the stakeholder is mid-message, and
        # two concurrent `claude -p --resume <id>` calls against the same session id plus
        # unsynchronized chat_history.json writes would race. Every turn - manual or
        # auto - blocks on this, so they queue up and run one at a time instead of
        # corrupting each other. Separate from git_lock, which guards commits, not turns.
        self.pm_lock = threading.Lock()
        # Guards pending_messages.json: it is written from the event loop thread (a
        # message arriving) and from a turn's worker thread (that message starting), so
        # unlike the transcript it is not covered by pm_lock.
        self.pending_lock = threading.Lock()
        # Consecutive auto-triggered turns with no real stakeholder message between them -
        # see MAX_AUTO_CONTINUE_STREAK. Reset on every genuine handle_user_message call.
        self._auto_continue_streak = 0

    def set_scope(
        self, initiative_id: str | None, adopted_products: list[str]
    ) -> None:
        """Live update of this PM's scope, called by SessionManager after a session is
        pinned to an initiative or adopts/releases a board.

        Takes effect on the NEXT turn, not the current one: `_run_pm_turn` reads
        system_prompt and allowed_tools fresh each call, but the CLI subprocess already
        running was launched with the old --allowedTools, so a PM that adopts a board
        mid-turn still cannot write to it until that turn ends. That is a real constraint
        rather than a rough edge, and INITIATIVE_GUIDANCE_TEMPLATE tells the PM so
        directly - otherwise it tries the PATCH immediately and reports itself blocked.
        """
        self.initiative_id = initiative_id
        self.adopted_products = list(adopted_products)
        self._refresh_scope()

    def owned_products(self) -> list[str]:
        """The boards this PM may write to - its pinned product's subtree plus every
        adopted subtree. Mirrors Session.owned_products; this copy exists because the
        agent holds the live scope and the persisted Session may already be one edit
        behind it."""
        pinned = [self.product] if self.product else []
        return owned_subtrees([*pinned, *self.adopted_products])

    def _refresh_scope(self) -> None:
        """(Re)builds the two scope-derived values - the system prompt and the Bash
        allowlist - from `product`, `initiative_id` and `adopted_products`.

        One function, called from __init__ and from set_scope, because these two must
        always be built together: the prompt tells the PM which boards are its own and the
        allowlist is what makes that true. Building them in separate places is how a PM
        ends up either told it owns a board it cannot write to, or handed write access to
        one it was never told about.
        """
        owned_products = self.owned_products()

        if owned_products:
            # The board the product-guidance block is written about: the pinned product
            # when there is one, otherwise the first board adopted, which is the closest
            # thing an initiative-scoped session has to a home.
            home = self.product or owned_products[0]
            roadmap_guidance = ROADMAP_GUIDANCE_TEMPLATE.format(
                product=home,
                product_label=product_label(home),
                # Ids with their place in the taxonomy: on a deployment with many child
                # products, a bare list of ids doesn't tell the PM whose "billing" it is
                # about to suggest work to.
                product_ids=", ".join(
                    f"{pid} (sub-product of {parent_of(pid)})" if parent_of(pid) else pid
                    for pid in PRODUCTS
                ),
                roadmap_base_url=ROADMAP_BASE_URL,
                people_directory_url=PEOPLE_DIRECTORY_URL,
                auth_header=agent_auth_header(),
            )
            # Everything below the home product, not just its direct children: the
            # allowlist grants the whole subtree, so the prompt has to name the whole
            # subtree or the PM has write access to a board it was never told about. Each
            # is named by its path, which is what tells a three-level family apart from a
            # wide two-level one. Derived from `home`'s own subtree rather than from
            # owned_products, which for an initiative session also holds unrelated adopted
            # roots - those are somebody else's family, not this product's children, and
            # are listed by the initiative block instead.
            descendants = [p for p in subtree_products(home) if p != home]
            if descendants:
                roadmap_guidance += PARENT_PRODUCT_GUIDANCE_TEMPLATE.format(
                    product_label=product_label(home),
                    child_summary=", ".join(
                        f"{product_path_label(d)} (id `{d}`)" for d in descendants
                    ),
                    child_example=descendants[0],
                    child_example_label=product_path_label(descendants[0]),
                    roadmap_base_url=ROADMAP_BASE_URL,
                    auth_header=agent_auth_header(),
                )
            # Not an elif: a product in the middle of a three-level family is both a
            # parent and a child, and each fact tells the PM something different - what it
            # may write, and who is already reading it.
            if parent_of(home):
                roadmap_guidance += CHILD_PRODUCT_GUIDANCE_TEMPLATE.format(
                    product=home,
                    product_label=product_label(home),
                    parent_label=product_label(parent_of(home)),
                )
            # Who owns the home product and where it is in its life - before the systems
            # and tracker blocks, because "who decides" outranks "where the code is" the
            # moment the PM has a question only a human can answer.
            roadmap_guidance += _product_facts_line(home)
            # Before the tracker block: what a change IS attributed to matters more to the
            # PM's next call than where it is mirrored.
            #
            # Gated on the HOME product being in scope, not merely on [systems] existing.
            # A PM told to attribute on a board whose edge is undeclared would have to pick
            # from whatever systems happen to exist, which invents wrong attribution rather
            # than leaving it missing - the same reason requires_system is per product.
            if requires_system(home):
                roadmap_guidance += SYSTEM_GUIDANCE_TEMPLATE.format(
                    product=home,
                    product_label=product_label(home),
                    roadmap_base_url=ROADMAP_BASE_URL,
                    auth_header=agent_auth_header(),
                    # Where the code lives, not just the label: the PM's next move after
                    # filing the change is often to dispatch a dev task into that tree.
                    system_summary=", ".join(
                        _describe_system(s) for s in systems_of_product(home)
                    ),
                )
            if CONFIG.trackers:
                pushable = [t for t in CONFIG.trackers if t.can_push]
                roadmap_guidance += TRACKER_GUIDANCE_TEMPLATE.format(
                    product=home,
                    roadmap_base_url=ROADMAP_BASE_URL,
                    auth_header=agent_auth_header(),
                    tracker_summary=", ".join(
                        f"{t.label} — {t.provider}, projects "
                        + (", ".join(t.projects) or "none configured")
                        for t in CONFIG.trackers
                    ),
                    # Which of the two upload paragraphs the PM gets. With no pushable
                    # tracker this is the pre-push paragraph verbatim, so a read-only
                    # deployment's PM is told exactly what it was told before.
                    push_guidance=(
                        PUSH_GUIDANCE.format(
                            push_summary=", ".join(
                                f"{t.label} ({t.push.project})" for t in pushable
                            )
                        )
                        if pushable
                        else NO_PUSH_GUIDANCE
                    ),
                )
        else:
            # No board of its own: the default session, and an initiative-scoped session
            # that hasn't adopted anything yet. Both get awareness without write access,
            # and the initiative block prepended below is what tells the second kind that
            # this is a starting state it can change rather than a permanent limit.
            roadmap_guidance = GENERAL_ROADMAP_GUIDANCE

        if self.initiative_id:
            roadmap_guidance = self._initiative_guidance(owned_products) + roadmap_guidance

        # POST is deliberately broad (any product) - suggesting work for another product is
        # the intended cross-product handoff, and for an initiative session that owns
        # nothing yet it is the ONLY way to reach a board, which is why it is granted on
        # `initiative_id` too and not only on ownership. PATCH is scoped to the boards this
        # session OWNS - its pinned product's subtree plus each adopted one - one
        # literal-URL-prefix entry each, the same enforcement tasks_base_url relies on: it
        # is structurally impossible for this PM to mutate the existing items of a board
        # outside that set. This is the enforcement half of the guidance above, built from
        # the same owned_products, so the two cannot disagree about what is writable.
        roadmap_tools = ""
        if owned_products or self.initiative_id:
            roadmap_tools = (
                f"Bash(curl -s -X POST {ROADMAP_BASE_URL}/*) "
                # Read-only, and granted with the write curls rather than separately: the
                # directory's only use to a PM is turning a name into the id an assignment
                # needs, so a session that cannot PATCH has nothing to do with it.
                f"Bash(curl -s {PEOPLE_DIRECTORY_URL}*) "
                + "".join(
                    f"Bash(curl -s -X PATCH {ROADMAP_BASE_URL}/{owned}/*) "
                    for owned in owned_products
                )
            )

        # The mode slots: research swaps the mission/operating-model paragraphs and the
        # loop tail, and drops the dispatch curl from the prompt entirely - the allowlist
        # below drops it in the same breath, so the PM is never shown a command it would
        # be refused (or refused a command it was shown).
        if self.mode == "research":
            mission = RESEARCH_MISSION
            operating_model = RESEARCH_OPERATING_MODEL
            dispatch_curls = ""
            loop_steps = RESEARCH_LOOP_STEPS
        else:
            task_system_field, dispatch_system_note = _dispatch_system_slots()
            mission = BUILD_MISSION
            operating_model = BUILD_OPERATING_MODEL
            dispatch_curls = BUILD_DISPATCH_CURLS_TEMPLATE.format(
                tasks_base_url=self.tasks_base_url,
                auth_header=agent_auth_header(),
                task_system_field=task_system_field,
                dispatch_system_note=dispatch_system_note,
                dev_capabilities=_dev_capabilities(),
            )
            loop_steps = BUILD_LOOP_STEPS
        code_systems = [sid for sid, spec in SYSTEMS.items() if spec.path]
        code_checkout_curl = (
            CODE_CHECKOUT_CURL_TEMPLATE.format(
                primary_root=CONFIG.repo_root.resolve(),
                worktree=self.repo_root.resolve(),
                checkout_url=self.checkout_url,
                auth_header=agent_auth_header(),
                system_ids=", ".join(f"`{sid}`" for sid in code_systems),
            )
            if self.checkout_url and code_systems else ""
        )
        self.system_prompt = PM_SYSTEM_PROMPT_TEMPLATE.format(
            mission=mission,
            operating_model=operating_model,
            dispatch_curls=dispatch_curls,
            code_checkout_curl=code_checkout_curl,
            loop_steps=loop_steps,
            tasks_base_url=self.tasks_base_url,
            session_meta_url=self.session_meta_url,
            spec_path=self.spec_path,
            project_status_path=self.project_status_path,
            project_index_path=self.project_index_path,
            roadmap_guidance=roadmap_guidance,
            repo_layout=CONFIG.repo_layout,
            workspace_rel=CONFIG.workspace_rel,
            default_session_name=CONFIG.default_session_name,
            local_instructions=_local_instructions_block(),
            auth_header=agent_auth_header(),
        )
        # The scope call is granted only to a session that HAS an initiative: adopting a
        # board is how a cross-product initiative widens as it learns, and it is the only
        # context where widening is a PM decision at all. A product-pinned session's scope
        # is the stakeholder's to change, from the sessions page.
        scope_tool = (
            f"Bash(curl -s -X POST {self.scope_url}*) " if self.initiative_id else ""
        )
        # The enforcement half of research mode: no dispatch entry, so launching a dev
        # task is structurally impossible for this PM - the same mechanism that scopes
        # roadmap PATCHes to owned boards. Reading task records stays granted in both
        # modes (a session switched to research keeps its history inspectable).
        dispatch_tool = (
            "" if self.mode == "research"
            else f"Bash(curl -s -X POST {self.tasks_base_url}*) "
        )
        checkout_tool = (
            f"Bash(curl -s -X POST {self.checkout_url}/*) " if code_checkout_curl else ""
        )
        self.allowed_tools = (
            f"{dispatch_tool}"
            f"Bash(curl -s {self.tasks_base_url}*) "
            f"Bash(curl -s -X POST {self.session_meta_url}*) "
            f"{checkout_tool}"
            f"{scope_tool}"
            f"{roadmap_tools}"
            "Write Read WebSearch WebFetch"
        )

    def _initiative_guidance(self, owned_products: list[str]) -> str:
        """The initiative block, including the one line that says what this session owns
        right now.

        The initiative's title, description and goals are deliberately NOT baked in here:
        they come from the context block injected on every turn (server.py's
        _roadmap_context_for), which is rebuilt from the store each time, while this prompt
        is rebuilt only on a scope change. Naming them here would let a renamed or
        re-goaled initiative stay stale in the prompt until the next adoption."""
        if owned_products:
            ownership_now = BOARDS_OWNED_TEMPLATE.format(
                owned_summary=", ".join(
                    f"{product_path_label(p)} (id `{p}`)" for p in owned_products
                )
            )
        else:
            ownership_now = NO_BOARDS_OWNED_YET
        return INITIATIVE_GUIDANCE_TEMPLATE.format(
            scope_url=self.scope_url,
            auth_header=agent_auth_header(),
            product_ids=", ".join(
                f"{pid} (sub-product of {parent_of(pid)})" if parent_of(pid) else pid
                for pid in PRODUCTS
            ),
            ownership_now=ownership_now,
        )

    def set_model(self, model: str) -> None:
        """Live update, called by SessionManager.set_model - takes effect on the
        very next turn since _run_pm_turn reads self.model fresh each call."""
        self.model = model

    def set_mode(self, mode: str) -> None:
        """Live update, called by SessionManager.set_mode. Rebuilds the prompt and the
        allowlist together (see _refresh_scope); like set_scope, it takes effect on the
        NEXT turn - a turn already running was launched with the old --allowedTools."""
        self.mode = mode
        self._refresh_scope()

    def _ensure_project_status_seed(self) -> None:
        """Guarantees {project_status_path} exists so the system prompt's Read \
        instruction never 404s - a brand-new project just gets an honest 'nothing yet'."""
        if not self.project_status_path.exists():
            self.project_status_path.parent.mkdir(parents=True, exist_ok=True)
            self.project_status_path.write_text(
                "# Project Status (durable — read this first)\n\n"
                "No project history yet. This is a brand-new project - proceed to discovery.\n"
            )

    def _load_state(self) -> None:
        if self.session_state_path.exists():
            self.claude_session_id = self.session_state_path.read_text().strip() or None

    def _save_state(self) -> None:
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        if self.claude_session_id:
            self.session_state_path.write_text(self.claude_session_id)

    def load_history(self) -> list[dict]:
        if self.history_path.exists():
            return json.loads(self.history_path.read_text())
        return []

    def last_message_role(self) -> str | None:
        """Role of the most recent persisted transcript entry, normalized to
        user/assistant/system/None, so SessionManager.activity_of can tell whether
        the PM has responded and is now awaiting the stakeholder ("waiting"). Reuses
        the same chat_history.json entries _run_turn writes - no second source of truth.
        The stored roles are user/pm/error/system; "pm" and "error" both mean the PM
        side spoke last (turn concluded, stakeholder's move), so both map to
        "assistant"."""
        history = self.load_history()
        if not history:
            return None
        role = history[-1].get("role")
        if role in ("pm", "error"):
            return "assistant"
        return role

    def _notify_activity_change(self) -> None:
        """Pings SessionManager (if wired) that this session's live activity may have
        changed - a PM turn just started or ended - so it can broadcast to the sessions
        page. Best-effort: an activity broadcast failing must never break a PM turn."""
        if self.on_activity_change is not None:
            try:
                self.on_activity_change()
            except Exception:
                pass

    def _on_turn_start(self, proc: subprocess.Popen) -> None:
        """_stream_claude's on_start for this PM's turns: remembers the process, and kills
        it on the spot when a stop was asked for before it existed (see stop_turn)."""
        self._turn_proc = proc
        if self._stop_reason is not None:
            kill_process_tree(proc)

    def stop_turn(self, who: str = "the user", wait: float = STOP_WAIT_SECONDS) -> bool:
        """Stops the PM turn running right now, if there is one: kills its `claude`
        process tree, and the turn ends as a pm_error "Stopped by <who>." - written to
        the transcript like any failed turn. A turn still getting ready (no process yet)
        is killed the moment its process starts. Waits up to `wait` seconds for the turn
        to finish recording itself. Returns whether there was a turn to stop. Messages
        queued behind it are the server's to discard (see server._stop_session)."""
        if not self.turn_active:
            return False
        self._stop_reason = f"Stopped by {who}."
        proc = self._turn_proc
        if proc is not None:
            kill_process_tree(proc)
        deadline = time.monotonic() + wait
        while self._stop_reason is not None and time.monotonic() < deadline:
            time.sleep(0.05)
        # The transcript and snapshot are written under pm_lock once the process is gone.
        if self.pm_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            self.pm_lock.release()
        return True

    def archive_current(self, reason: str) -> None:
        """Copies the current SPEC.md/chat_history.json into workspace/archive before
        they're cleared or the session's worktree is torn down. Copy only - never
        deletes here, so callers can commit the archive alongside whatever else they're
        about to change (a reset's own deletions, or a terminate's pre-merge snapshot)."""
        if not self.spec_path.exists() and not self.history_path.exists() and not self.uploads_dir.exists():
            return
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = self.archive_dir / f"{timestamp}_{reason}"
        dest.mkdir(parents=True, exist_ok=True)
        if self.spec_path.exists():
            shutil.copy2(self.spec_path, dest / "SPEC.md")
        if self.history_path.exists():
            shutil.copy2(self.history_path, dest / "chat_history.json")
        if self.uploads_dir.exists():
            shutil.copytree(self.uploads_dir, dest / "uploads")

    def reset(self) -> None:
        """Archives the current spec/chat, then clears workspace/current and drops the
        Claude session pointer so the next turn starts a brand-new conversation with no
        memory of prior turns. PROJECT_STATUS.md, PROJECT_INDEX.md, and docs/ are never
        touched - they live outside workspace/current for exactly this reason."""
        with self.git_lock:
            self.archive_current("reset")
            for path in (
                self.spec_path,
                self.history_path,
                self.session_state_path,
                self.pending_path,
            ):
                if path.exists():
                    path.unlink()
            if self.uploads_dir.exists():
                shutil.rmtree(self.uploads_dir)
            self.claude_session_id = None
            gitsnapshot.snapshot(
                f"PM session {self.session_id} reset (prior spec/chat archived)", self.repo_root
            )

    # ---- pending queue (messages accepted, not yet run) ----

    def load_pending(self) -> list[dict]:
        """The stakeholder messages waiting for their turn, oldest first. Each is
        `{id, text, attachments, ts}` - the same shape a transcript entry takes, so the
        chat page can render one exactly like the other."""
        with self.pending_lock:
            return self._read_pending()

    def _read_pending(self) -> list[dict]:
        if self.pending_path.exists():
            try:
                return json.loads(self.pending_path.read_text())
            except json.JSONDecodeError:
                return []
        return []

    def _write_pending(self, pending: list[dict]) -> None:
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.pending_path.write_text(json.dumps(pending, indent=2))

    def enqueue_pending(self, text: str, attachments: list[dict] | None = None) -> dict:
        """Records one just-arrived message, attachments and all, BEFORE anything runs
        it - so a reload (or a restart) while it waits still finds it. Returns the
        record; its `id` is what carries through to the transcript entry the turn
        writes, which is how the chat page tells a message that has since started from
        one still waiting."""
        saved = [path.name for path in self._save_attachments(attachments or [])]
        record = {"id": uuid.uuid4().hex, "text": text, "attachments": saved, "ts": time.time()}
        with self.pending_lock:
            pending = self._read_pending()
            pending.append(record)
            self._write_pending(pending)
        return record

    def drop_pending(self, message_id: str) -> None:
        """Removes one message from the queue file. Called as its turn starts, right
        after the transcript entry is written - the transcript is now the record of it,
        and a message in both files at once would show up twice on the chat page were it
        not for the shared id."""
        with self.pending_lock:
            pending = self._read_pending()
            remaining = [record for record in pending if record.get("id") != message_id]
            if len(remaining) != len(pending):
                self._write_pending(remaining)

    def _append_incoming_entry(
        self,
        text: str,
        attachments: list[str] | None = None,
        role: str = "user",
        entry_id: str | None = None,
    ) -> None:
        """Persists just the incoming stakeholder/system entry to chat_history.json,
        called at the START of a turn (before _run_pm_turn) so a mid-turn reload's
        GET /history already contains the just-sent message instead of dropping it until
        the reply lands. Stamps the same epoch-seconds ts and handles attachments exactly
        as the reply write does; _append_reply_entry appends the reply once the turn
        completes. Both are serialized under pm_lock in _run_turn, so they can't race."""
        # Stamp every entry with an epoch-seconds ts as it's appended so the client can
        # interleave chat and dev-task cards into one chronological timeline on reload.
        # Purely additive: legacy entries lacking ts are treated as having no timestamp.
        history = self.load_history()
        entry = {"role": role, "text": text, "ts": time.time()}
        if attachments:
            entry["attachments"] = attachments
        # The queue record's id, carried onto the transcript entry so the chat page can
        # match the two up and never render a message twice (see drop_pending).
        if entry_id:
            entry["id"] = entry_id
        history.append(entry)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.history_path.write_text(json.dumps(history, indent=2))

    def _append_reply_entry(self, final_event: dict) -> None:
        """Appends just the PM's reply entry (pm on pm_reply, error otherwise) after the
        turn completes and writes the file - the second half of what used to be one write.
        Runs after _append_incoming_entry under the same pm_lock, so the on-disk transcript
        ends up identical to the old single write: incoming entry then reply entry, same ts
        stamping and json.dumps(indent=2) format."""
        history = self.load_history()
        if final_event["type"] == "pm_reply":
            history.append({"role": "pm", "text": final_event["text"], "ts": time.time()})
        else:
            history.append({"role": "error", "text": final_event["message"], "ts": time.time()})
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.history_path.write_text(json.dumps(history, indent=2))

    def _save_attachments(self, attachments: list[dict]) -> list[Path]:
        """Decodes base64 image attachments onto disk under workspace/current/uploads
        so the PM can view them via its Read tool - see MAX_ATTACHMENTS_PER_TURN above
        for why this is the only way an image reaches a headless `claude -p` turn.
        Silently skips anything not a recognized image type or over the size cap,
        rather than failing the whole turn over one bad attachment."""
        saved: list[Path] = []
        for attachment in attachments[:MAX_ATTACHMENTS_PER_TURN]:
            mime = attachment.get("mime", "")
            ext = _EXTENSION_BY_MIME.get(mime)
            if ext is None:
                continue
            try:
                raw = base64.b64decode(attachment.get("data", ""), validate=True)
            except Exception:
                continue
            if not raw or len(raw) > MAX_ATTACHMENT_BYTES:
                continue
            self.uploads_dir.mkdir(parents=True, exist_ok=True)
            path = self.uploads_dir / f"{uuid.uuid4().hex}{ext}"
            path.write_bytes(raw)
            saved.append(path)
        return saved

    def handle_user_message(
        self,
        text: str,
        attachments: list[str] | None = None,
        other_sessions_context: str = "",
        roadmap_context: str = "",
        pending_id: str | None = None,
    ) -> Iterator[dict]:
        """Runs one PM turn in response to a real stakeholder message. `attachments` are
        the FILENAMES of images already written under uploads/ - decoding happens at
        enqueue_pending, when the message arrives, not here when its turn finally starts,
        so a queued image survives a restart like its text does. They're pointed at from
        the prompt text so the PM can Read them. `other_sessions_context` is server.py's
        live snapshot of what every other active session is doing - see
        _with_session_context. `roadmap_context` is server.py's per-turn roadmap snapshot
        (own product in full, others as a shallow digest) - see _with_roadmap_context.
        `pending_id` is the queue record this turn came from, dropped from the queue file
        as the turn starts."""
        self._auto_continue_streak = 0

        names = list(attachments or [])
        saved_paths = [self.uploads_dir / name for name in names]
        prompt_text = text
        if saved_paths:
            file_list = "\n".join(f"- {path}" for path in saved_paths)
            intro = (
                f"{text}\n\n" if text else ""
            ) + f"[The stakeholder attached {len(saved_paths)} image(s) with this message - use your Read tool to view them:]"
            prompt_text = f"{intro}\n{file_list}"
        prompt_text = self._with_session_context(prompt_text, other_sessions_context)
        prompt_text = self._with_roadmap_context(prompt_text, roadmap_context)

        yield from self._run_turn(
            prompt_text,
            history_text=text,
            history_role="user",
            history_attachments=names,
            commit_message=f"PM turn: {(text or '[image]')[:72]}",
            pending_id=pending_id,
        )

    def handle_task_completion(
        self, task: dict, other_sessions_context: str = "", roadmap_context: str = ""
    ) -> Iterator[dict]:
        """Auto-triggered the instant a dev task this PM dispatched finishes (see
        server.py's task-registry subscription) - lets the PM react and keep the plan
        moving without the stakeholder having to come back and ask for a status check.
        Capped by MAX_AUTO_CONTINUE_STREAK so a stuck plan can't re-invoke itself
        unattended forever; at the cap the PM is told to stop dispatching and check in
        instead of being trusted to decide that on its own."""
        at_cap = self._auto_continue_streak >= MAX_AUTO_CONTINUE_STREAK
        self._auto_continue_streak += 1

        prompt = (
            f'[System: dev task {task["id"]} just finished with status "{task["status"]}" - '
            f'"{task["description"]}". Check its result via curl and tell the stakeholder what '
            "happened."
        )
        prompt += _judge_completion_note(task, at_cap)
        prompt += _pull_request_completion_note(task)
        prompt += self._artifacts_completion_note(task)
        if self.mode == "research":
            # A task can only be running here if it was dispatched before the session was
            # switched to research - so the standard "start the next task" continuation
            # would be an instruction to do the one thing this session can no longer do.
            prompt += (
                " This session has since been switched to research/strategy mode: you "
                "cannot dispatch any follow-up dev task. Report the outcome, record what "
                "it means for the plan, and leave any next build step to the stakeholder.]"
            )
        elif at_cap:
            prompt += (
                " You've auto-continued several times in a row now with no stakeholder input - "
                "do NOT dispatch another task this turn. Summarize where things stand and ask the "
                "stakeholder a concrete question instead.]"
            )
        else:
            prompt += (
                " Then immediately continue the plan: if the goal isn't done yet, start the next "
                "task yourself - don't wait to be asked. Only stop to ask the stakeholder if it's "
                "genuinely their call.]"
            )
        prompt = self._with_session_context(prompt, other_sessions_context)
        prompt = self._with_roadmap_context(prompt, roadmap_context)

        label = f'Dev task {task["id"]} finished ({task["status"]}): {task["description"][:100]}'
        yield from self._run_turn(
            prompt,
            history_text=label,
            history_role="system",
            history_attachments=None,
            commit_message=f'PM auto-continue after dev task {task["id"]} ({task["status"]})',
        )

    def _artifacts_completion_note(self, task: dict) -> str:
        """Names the files a finished task left for the stakeholder, by absolute path so
        the PM can Read them (an image included). "" when it left none."""
        artifacts = task.get("artifacts") or []
        if not artifacts:
            return ""
        base = self.workspace_dir / "tasks" / task["id"]
        listing = ", ".join(str(base / a["name"]) for a in artifacts)
        return (
            f" It left {len(artifacts)} file(s) for the stakeholder, already shown on its "
            f"card in the chat: {listing}. Read an image if you need to see it to report "
            "accurately; don't paste paths at the stakeholder, just refer to the card."
        )

    def _with_session_context(self, prompt_text: str, other_sessions_context: str) -> str:
        """Prepends a live snapshot of other active sessions' work, when there is any -
        see sessions.py's describe_other_active_sessions. Kept out of the persisted chat
        history (only prompt_text, never history_text, carries it): it's per-turn plumbing
        the stakeholder doesn't need to read, not something they said or the PM decided.

        Instance method, not static, because what the PM should DO with the snapshot is
        mode-dependent: a build session is warned off duplicate dispatch, a research
        session (which cannot dispatch at all) is told to fold it into its findings."""
        if not other_sessions_context:
            return prompt_text
        if self.mode == "research":
            instruction = (
                "[Other PM sessions active right now, each its own git worktree/branch off "
                "this same repo, invisible to you unless told here - you can't dispatch dev "
                "work from this research session, but fold what's already running or just "
                "finished below into your research and recommendations rather than proposing "
                "it from scratch.\n"
            )
        else:
            instruction = (
                "[Other PM sessions active right now, each its own git worktree/branch off "
                "this same repo, invisible to you unless told here - before dispatching any "
                "new dev task this turn, check whether it overlaps with something already "
                "running or just finished below. If it does, don't duplicate it: tell the "
                "stakeholder about the overlap and ask how to proceed instead of dispatching.\n"
            )
        return instruction + f"{other_sessions_context}\n]\n\n{prompt_text}"

    def _with_roadmap_context(self, prompt_text: str, roadmap_context: str) -> str:
        """Prepends this turn's roadmap context (own product in full depth, every other
        product as a shallow digest - see server.py's _roadmap_context_for and this
        class's ROADMAP_GUIDANCE_TEMPLATE for how to act on it). Kept out of persisted
        chat history for the same reason as _with_session_context: it's per-turn
        plumbing, not something the stakeholder said or the PM decided.

        The conversation is resumed turn after turn, so every snapshot sent stays in its
        context: re-sending the whole roadmap each turn (tens of thousands of characters
        on a real portfolio) made every turn slower and costlier than the last. So the
        full snapshot goes out only when the conversation has none to build on - a fresh
        conversation, a server restart, a failed turn - and every ROADMAP_FULL_EVERY
        turns as a refresh; otherwise the turn carries only what changed since the last
        one sent, or a one-line "unchanged"."""
        if not roadmap_context:
            return prompt_text
        previous = self._roadmap_sent if self.claude_session_id else None
        self._roadmap_sent = roadmap_context
        if previous is not None and self._roadmap_turns_since_full < ROADMAP_FULL_EVERY:
            self._roadmap_turns_since_full += 1
            if previous == roadmap_context:
                return (
                    "[Roadmap: unchanged since the last snapshot in this conversation - "
                    f"that one is still current.]\n\n{prompt_text}"
                )
            delta = roadmap_delta(previous, roadmap_context)
            if len(delta) < len(roadmap_context) // 2:
                return (
                    "[Roadmap changes since the last snapshot in this conversation (every "
                    "line not listed here is unchanged; a changed line shows as REMOVED "
                    "then ADDED):\n"
                    f"{delta}\n]\n\n{prompt_text}"
                )
        self._roadmap_turns_since_full = 0
        return (
            "[Roadmap snapshot for this turn - see your system prompt for how to act on "
            f"it:\n{roadmap_context}\n]\n\n{prompt_text}"
        )

    def _run_turn(
        self,
        prompt_text: str,
        *,
        history_text: str,
        history_role: str,
        history_attachments: list[str] | None,
        commit_message: str,
        pending_id: str | None = None,
    ) -> Iterator[dict]:
        """Runs one `claude -p` turn (which may itself dispatch a nested dev-agent call
        via Bash), persists it to the transcript, and yields UI events. Serialized by
        pm_lock so a manual message and an auto-continuation triggered by a task
        finishing can never run concurrently against the same resumable session."""
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        # Flip the live "thinking" signal on and broadcast before any work, so the
        # sessions list shows the indicator immediately; the finally guarantees it
        # clears even if the turn errors or the generator is closed early.
        self.turn_active = True
        self._notify_activity_change()
        try:
            yield {"type": "pm_working"}

            with self.pm_lock:
                # Persist the incoming entry BEFORE running the turn so a mid-turn reload
                # (GET /history) already shows the just-sent message; append only the reply
                # after the turn returns. Both writes stay serialized under pm_lock, exactly
                # as the single combined write was, so no history write can race.
                self._append_incoming_entry(
                    history_text,
                    attachments=history_attachments,
                    role=history_role,
                    entry_id=pending_id,
                )
                # Transcript first, queue file second: a reader caught between the two
                # sees the message twice (deduped on the shared id) rather than not at
                # all, which is the failure that would actually lose a message.
                if pending_id:
                    self.drop_pending(pending_id)
                final_event = yield from self._run_pm_turn_with_progress(prompt_text)
                if final_event.get("type") == "pm_error":
                    # The conversation may never have seen this turn's roadmap context;
                    # the next turn sends it in full rather than a delta against it. Same
                    # for its instructions (see _with_current_instructions).
                    self._roadmap_sent = None
                    self._instructions_sent = None
                self._append_reply_entry(final_event)
                with self.git_lock:
                    gitsnapshot.snapshot(commit_message, self.repo_root)
            yield final_event
        finally:
            # A stop that came after the process ended must not carry over to the next
            # turn (see stop_turn).
            self._stop_reason = None
            self.turn_active = False
            self._notify_activity_change()

    def _with_current_instructions(self, text: str, system_prompt: str) -> str:
        """`text`, preceded by this turn's full system prompt when a RESUMED conversation
        may be running on an older one.

        `claude -p --resume` keeps the system prompt the conversation started with and
        ignores the one passed on later turns. So anything that changes the prompt after
        the first turn - a server restart (which mints a new agent token, making every
        curl in the old prompt fail with 401), a scope or mode change, a newly declared
        MCP server, a new capability - would never reach a conversation already under
        way. The current instructions are re-stated once per change, marked as
        replacing the original; a brand-new conversation gets them as its real system
        prompt and needs no note."""
        resumed = self.claude_session_id is not None
        stale = resumed and self._instructions_sent != system_prompt
        self._instructions_sent = system_prompt
        if not stale:
            return text
        return (
            "[PM Studio: your operating instructions changed since this conversation began "
            "(for example the server restarted, which issues a new auth token for your curl "
            "commands, or this session's scope, mode or tools changed). The complete current "
            "instructions follow and REPLACE the system prompt you started with: wherever "
            "they differ - every command, URL and header value included - follow these, "
            "and never reuse a command or token from earlier in this conversation.\n\n"
            f"{system_prompt}\n]\n\n{text}"
        )

    def _run_pm_turn_with_progress(self, prompt_text: str) -> Iterator[dict]:
        """Runs _run_pm_turn on a worker thread and yields its `pm_progress` events as
        they happen; returns (as the generator's value) the turn's final event. The
        progress is live only - never written to the transcript, which keeps the final
        reply alone, as before."""
        progress: queue.Queue = queue.Queue()
        done = object()
        outcome: dict = {}

        def work() -> None:
            self._progress_sink = progress.put
            try:
                outcome["event"] = self._run_pm_turn(prompt_text)
            except Exception as exc:  # never leave the turn hanging on the queue
                outcome["event"] = {"type": "pm_error", "message": str(exc)}
            finally:
                self._progress_sink = None
                progress.put(done)

        threading.Thread(target=work, daemon=True, name=f"pm-turn-{self.session_id}").start()
        while (item := progress.get()) is not done:
            yield item
        return outcome["event"]

    def turn_allowed_tools(self, mcp_names: list[str]) -> str:
        """This turn's --allowedTools: the PM's own allowlist, unchanged, plus one
        `mcp__<name>` per [agents.mcp] server it is given this turn."""
        return self.allowed_tools + "".join(f" mcp__{name}" for name in mcp_names)

    def _run_pm_turn(self, text: str) -> dict:
        # Neither the message nor the system prompt goes on the command line: Windows
        # caps a whole command line at 32,767 characters, and the system prompt for a
        # large product tree alone runs past 36,000. The message goes in on stdin and
        # the system prompt through a temp file outside the repo, so no snapshot can
        # pick it up (it carries the agent auth header).
        # The [agents.mcp] servers marked `pm = true`, read fresh for THIS turn (the
        # list hot-reloads when config.toml changes), each granted as mcp__<name> on top
        # of the PM's own allowlist - so a running chat gets a newly added server on its
        # next turn, and a `--resume`d conversation simply continues with it.
        mcp = contextlib.ExitStack()
        mcp_args, mcp_names = mcp.enter_context(agent_mcp.AGENT_MCP.launch_args(pm=True))
        system_prompt = self.system_prompt + _mcp_prompt_note(mcp_names)
        system_prompt_path = _write_system_prompt(system_prompt)
        text = self._with_current_instructions(text, system_prompt)
        command = [
            "claude",
            "-p",
            # stream-json (which needs --verbose with -p) so each step can reach the chat
            # while the turn is still running - see _stream_claude and progress_events.
            "--output-format", "stream-json",
            "--verbose",
            "--system-prompt-file", str(system_prompt_path),
            "--allowedTools", self.turn_allowed_tools(mcp_names),
            *mcp_args,
            "--model", self.model,
        ]
        if self.claude_session_id:
            command += ["--resume", self.claude_session_id]

        sink = self._progress_sink
        # Steps plus their outcomes, and the MCP activity log (see ProgressTracker).
        tracker = ProgressTracker(
            session_id=self.session_id, source="pm", activity=mcp_activity.ACTIVITY_LOG,
        )

        def on_message(message: dict) -> None:
            for event in tracker.events(message):
                if sink is not None:
                    sink(event)

        try:
            returncode, data, stray, stderr = _stream_claude(
                command,
                text,
                # Repo root, not workspace_dir: product sources live at the root, so
                # relative paths in the PM's reads and in dev-task descriptions
                # resolve against the real source tree, not the PM's bookkeeping
                # folder. (The pipes are UTF-8: the locale default on Windows is cp1252,
                # which cannot even encode an arrow.)
                cwd=str(self.repo_root),
                env=subprocess_env(self.session_id),
                timeout=PM_TIMEOUT_SECONDS,
                on_message=on_message,
                on_start=self._on_turn_start,
            )
        except FileNotFoundError:
            return {"type": "pm_error", "message": "The `claude` CLI is not installed or not on PATH."}
        finally:
            self._turn_proc = None
            stopped, self._stop_reason = self._stop_reason, None
            with contextlib.suppress(OSError):
                system_prompt_path.unlink(missing_ok=True)
            mcp.close()

        if stopped:
            return {"type": "pm_error", "message": stopped}
        if returncode is None:
            return {"type": "pm_error", "message": f"PM agent timed out after {PM_TIMEOUT_SECONDS}s."}
        if data is None:
            detail = stray.strip()[:4000] or f"stderr: {stderr.strip()[:2000]}"
            return {
                "type": "pm_error",
                "message": f"PM agent produced no result (exit {returncode}). {detail}",
            }

        if data.get("session_id"):
            self.claude_session_id = data["session_id"]
            self._save_state()

        if data.get("is_error") or returncode != 0:
            return {
                "type": "pm_error",
                "message": data.get("result") or f"exit code {returncode}",
                # Carried even on failure: a turn that errored still cost tokens.
                "agent_usage": agent_usage(data),
            }

        return {
            "type": "pm_reply",
            "text": data.get("result", ""),
            # Measured spend for this turn, for cost attribution (see costing.py).
            # Previously discarded.
            "agent_usage": agent_usage(data),
        }
