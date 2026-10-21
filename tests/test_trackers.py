"""Tests for external tracker sync (Jira / Azure DevOps) and the 1:1 ticket link.

No test here touches the network: trackers._request is the module's single HTTP seam and
every test replaces it with a recorder. That is deliberate - it means these tests pin the
exact requests we send (URL shape, method, auth header, pagination) as well as how we parse
what comes back, and they do it without anybody needing a Jira instance to run the suite.
"""

import base64
import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from pm_studio import config as config_module
from pm_studio import trackers as trackers_module
from pm_studio.config import TrackerConfig, _parse_trackers
from pm_studio.trackers import (
    AdoClient,
    JiraClient,
    Ticket,
    TrackerError,
    TrackerStore,
    canonical_type,
    parse_reference,
)

JIRA = TrackerConfig(
    id="jira",
    provider="jira",
    label="Acme Jira",
    base_url="https://acme.atlassian.net",
    projects=("PROJ", "PLAT"),
    username="pm@acme.com",
    token="jira-token-secret",
)
ADO = TrackerConfig(
    id="ado",
    provider="ado",
    label="Acme ADO",
    base_url="https://dev.azure.com/acme",
    projects=("Platform",),
    token="ado-pat-secret",
    organization="acme",
)


class _Recorder:
    """Stands in for trackers._request, recording calls and replaying scripted responses."""

    def __init__(self, handler) -> None:
        self.calls: list[dict] = []
        self._handler = handler

    def __call__(self, url, *, headers, method="GET", body=None, timeout=20):
        self.calls.append({"url": url, "headers": headers, "method": method, "body": body})
        return self._handler(url, method, body)


class _PatchRequest:
    def __init__(self, handler) -> None:
        self.recorder = _Recorder(handler)

    def __enter__(self) -> _Recorder:
        self._original = trackers_module._request
        trackers_module._request = self.recorder
        return self.recorder

    def __exit__(self, *exc) -> None:
        trackers_module._request = self._original


def jira_issue(key, kind, summary="s", state="To Do", category="To Do", parent=None):
    fields = {
        "summary": summary,
        "issuetype": {"name": kind},
        "status": {"name": state, "statusCategory": {"name": category}},
    }
    if parent:
        parent_key, parent_type = parent
        fields["parent"] = {"key": parent_key, "fields": {"issuetype": {"name": parent_type}}}
    return {"key": key, "fields": fields}


class CanonicalTypeTest(unittest.TestCase):
    def test_jira_and_ado_vocabularies_land_on_the_same_slugs(self) -> None:
        """The whole point of normalising: ADO's "Product Backlog Item" and Jira's "Story"
        are the same rung and must colour the same."""
        self.assertEqual(canonical_type("Story"), "story")
        self.assertEqual(canonical_type("User Story"), "story")
        self.assertEqual(canonical_type("Product Backlog Item"), "story")
        self.assertEqual(canonical_type("Epic"), "epic")
        self.assertEqual(canonical_type("Feature"), "feature")
        self.assertEqual(canonical_type("Bug"), "bug")
        self.assertEqual(canonical_type("Defect"), "bug")
        self.assertEqual(canonical_type("Sub-task"), "subtask")
        self.assertEqual(canonical_type("Spike"), "spike")

    def test_case_and_whitespace_are_irrelevant(self) -> None:
        self.assertEqual(canonical_type("  ePiC  "), "epic")

    def test_compound_names_match_on_a_word(self) -> None:
        self.assertEqual(canonical_type("Bug (Production)"), "bug")
        self.assertEqual(canonical_type("Technical Task"), "task")

    def test_unknown_type_is_other_rather_than_a_guess(self) -> None:
        """A custom issue type must not be silently mislabelled - "other" plus the raw name
        is the honest answer (see the Ticket.raw_type the UI actually displays)."""
        self.assertEqual(canonical_type("Frobnicator"), "other")
        self.assertEqual(canonical_type(""), "other")
        self.assertEqual(canonical_type(None), "other")

    def test_a_word_boundary_is_required(self) -> None:
        """"Debug" contains "bug" but is not a bug."""
        self.assertEqual(canonical_type("Debugging"), "other")


class JiraClientTest(unittest.TestCase):
    def test_credentials_go_in_the_header_never_the_url(self) -> None:
        with _PatchRequest(lambda u, m, b: {"issues": [], "isLast": True}) as rec:
            JiraClient(JIRA).fetch_catalog()
        call = rec.calls[0]
        self.assertNotIn("jira-token-secret", call["url"])
        expected = base64.b64encode(b"pm@acme.com:jira-token-secret").decode()
        self.assertEqual(call["headers"]["Authorization"], f"Basic {expected}")

    def test_jql_is_scoped_to_the_configured_projects(self) -> None:
        """The `projects` list is what bounds a sync; a query that forgot it would pull the
        whole instance."""
        with _PatchRequest(lambda u, m, b: {"issues": [], "isLast": True}) as rec:
            JiraClient(JIRA).fetch_catalog()
        self.assertIn("project+in+%28%22PROJ%22%2C+%22PLAT%22%29", rec.calls[0]["url"])

    def test_token_pagination_follows_next_page_token(self) -> None:
        pages = [
            {"issues": [jira_issue("PROJ-1", "Bug")], "nextPageToken": "t2", "isLast": False},
            {"issues": [jira_issue("PROJ-2", "Epic")], "isLast": True},
        ]

        def handler(url, method, body):
            return pages[1] if "nextPageToken=t2" in url else pages[0]

        with _PatchRequest(handler) as rec:
            tickets, truncated = JiraClient(JIRA).fetch_catalog()
        self.assertEqual([t.key for t in tickets], ["PROJ-1", "PROJ-2"])
        self.assertEqual([t.type for t in tickets], ["bug", "epic"])
        self.assertFalse(truncated)
        self.assertEqual(len(rec.calls), 2)

    def test_falls_back_to_the_offset_endpoint_when_search_jql_is_absent(self) -> None:
        """Atlassian replaced /search with /search/jql, and Server/DC never had the new one.
        A 404/410 on the first is the documented signal to try the older shape."""

        def handler(url, method, body):
            if "/search/jql" in url:
                raise TrackerError("HTTP 410: gone")
            if "/rest/api/3/search" in url:
                raise TrackerError("HTTP 404: not here")
            return {"issues": [jira_issue("PROJ-9", "Story")], "total": 1, "startAt": 0}

        with _PatchRequest(handler) as rec:
            tickets, _ = JiraClient(JIRA).fetch_catalog()
        self.assertEqual([t.key for t in tickets], ["PROJ-9"])
        self.assertIn("/rest/api/2/search", rec.calls[-1]["url"])

    def test_a_real_error_is_not_swallowed_by_the_fallback_chain(self) -> None:
        """Only 404/410 means "wrong endpoint". A 401 must surface, not silently walk the
        chain and end up reporting something misleading."""
        with _PatchRequest(lambda u, m, b: (_ for _ in ()).throw(TrackerError("HTTP 401: nope"))):
            with self.assertRaises(TrackerError) as caught:
                JiraClient(JIRA).fetch_catalog()
        self.assertIn("401", str(caught.exception))

    def test_offset_pagination_stops_at_total(self) -> None:
        def handler(url, method, body):
            if "/search/jql" in url:
                raise TrackerError("HTTP 404: no")
            start = int(url.split("startAt=")[1].split("&")[0])
            return {
                "issues": [jira_issue(f"PROJ-{start + 1}", "Task")],
                "total": 3,
                "startAt": start,
            }

        with _PatchRequest(handler) as rec:
            tickets, truncated = JiraClient(JIRA).fetch_catalog()
        self.assertEqual([t.key for t in tickets], ["PROJ-1", "PROJ-2", "PROJ-3"])
        self.assertFalse(truncated)

    def test_url_points_at_the_human_browse_page(self) -> None:
        with _PatchRequest(lambda u, m, b: {"issues": [jira_issue("PROJ-5", "Bug")], "isLast": True}):
            tickets, _ = JiraClient(JIRA).fetch_catalog()
        self.assertEqual(tickets[0].url, "https://acme.atlassian.net/browse/PROJ-5")

    def test_fetch_one_returns_none_for_a_missing_issue(self) -> None:
        with _PatchRequest(lambda u, m, b: (_ for _ in ()).throw(TrackerError("HTTP 404: gone"))):
            self.assertIsNone(JiraClient(JIRA).fetch_one("PROJ-404"))

    def test_parent_is_requested_in_one_call(self) -> None:
        """The parent's own type has to come back with the search, or learning "is my parent
        an Epic?" would cost an extra request per issue across thousands of them."""
        with _PatchRequest(lambda u, m, b: {"issues": [], "isLast": True}) as rec:
            JiraClient(JIRA).fetch_catalog()
        self.assertIn("parent", rec.calls[0]["url"])

    def test_parent_key_and_type_are_captured(self) -> None:
        issue = jira_issue("PROJ-2", "Story", parent=("PROJ-1", "Epic"))
        with _PatchRequest(lambda u, m, b: {"issues": [issue], "isLast": True}):
            tickets, _ = JiraClient(JIRA).fetch_catalog()
        self.assertEqual(tickets[0].parent_key, "PROJ-1")
        self.assertEqual(tickets[0].parent_type, "Epic")

    def test_parent_key_is_normalised_to_upper(self) -> None:
        issue = jira_issue("PROJ-2", "Story", parent=("proj-1", "Epic"))
        with _PatchRequest(lambda u, m, b: {"issues": [issue], "isLast": True}):
            tickets, _ = JiraClient(JIRA).fetch_catalog()
        self.assertEqual(tickets[0].parent_key, "PROJ-1")

    def test_a_top_level_issue_has_no_parent(self) -> None:
        with _PatchRequest(
            lambda u, m, b: {"issues": [jira_issue("PROJ-1", "Epic")], "isLast": True}
        ):
            tickets, _ = JiraClient(JIRA).fetch_catalog()
        self.assertIsNone(tickets[0].parent_key)
        self.assertIsNone(tickets[0].parent_type)

    def test_status_category_is_captured_separately_from_the_workflow_name(self) -> None:
        """A deployment renames its statuses freely ("QA Testing", "Wont Do"); the category
        is the stable value a bucket mapping can rely on."""
        issue = jira_issue("PROJ-3", "Bug", state="QA Testing", category="In Progress")
        with _PatchRequest(lambda u, m, b: {"issues": [issue], "isLast": True}):
            tickets, _ = JiraClient(JIRA).fetch_catalog()
        self.assertEqual(tickets[0].state, "QA Testing")
        self.assertEqual(tickets[0].state_category, "In Progress")


class AdoClientTest(unittest.TestCase):
    def test_pat_authenticates_as_an_empty_username(self) -> None:
        with _PatchRequest(lambda u, m, b: {"workItems": []}) as rec:
            AdoClient(ADO).fetch_catalog()
        expected = base64.b64encode(b":ado-pat-secret").decode()
        self.assertEqual(rec.calls[0]["headers"]["Authorization"], f"Basic {expected}")
        self.assertNotIn("ado-pat-secret", rec.calls[0]["url"])

    def test_wiql_then_batch_hydrate(self) -> None:
        """Two calls is how the ADO API is shaped: WIQL yields ids, then ids are hydrated."""

        def handler(url, method, body):
            if "wiql" in url:
                return {"workItems": [{"id": 1}, {"id": 2}]}
            return {
                "value": [
                    {
                        "id": 1,
                        "fields": {
                            "System.Title": "Portal",
                            "System.WorkItemType": "Feature",
                            "System.State": "Active",
                        },
                    },
                    {
                        "id": 2,
                        "fields": {
                            "System.Title": "Crash",
                            "System.WorkItemType": "Bug",
                            "System.State": "New",
                        },
                    },
                ]
            }

        with _PatchRequest(handler) as rec:
            tickets, _ = AdoClient(ADO).fetch_catalog()
        self.assertEqual(rec.calls[0]["method"], "POST")
        self.assertIn("System.TeamProject", rec.calls[0]["body"]["query"])
        self.assertEqual([t.key for t in tickets], ["1", "2"])
        self.assertEqual([t.type for t in tickets], ["feature", "bug"])
        self.assertEqual(tickets[0].url, "https://dev.azure.com/acme/Platform/_workitems/edit/1")

    def test_a_project_name_with_an_apostrophe_cannot_break_the_wiql_literal(self) -> None:
        """WIQL escapes a single quote by doubling it, like SQL. Without this, a project
        called "Bob's Team" would produce a syntax error - or worse, be injectable."""
        config = dataclasses.replace(ADO, projects=("Bob's Team",))
        with _PatchRequest(lambda u, m, b: {"workItems": []}) as rec:
            AdoClient(config).fetch_catalog()
        self.assertIn("'Bob''s Team'", rec.calls[0]["body"]["query"])

    def test_parent_type_is_resolved_from_the_synced_batch(self) -> None:
        """ADO's projection gives the parent id but not its type, so the type is filled in
        from the same pull rather than costing one extra call per work item."""

        def handler(url, method, body):
            if "wiql" in url:
                return {"workItems": [{"id": 10}, {"id": 11}]}
            return {
                "value": [
                    {"id": 10, "fields": {"System.Title": "E", "System.WorkItemType": "Epic",
                                          "System.State": "Active"}},
                    {"id": 11, "fields": {"System.Title": "S", "System.WorkItemType": "User Story",
                                          "System.State": "New", "System.Parent": 10}},
                ]
            }

        with _PatchRequest(handler):
            tickets, _ = AdoClient(ADO).fetch_catalog()
        child = next(t for t in tickets if t.key == "11")
        self.assertEqual(child.parent_key, "10")
        # Unresolved before the store's post-pass; the store fills it in (see _sync_one).
        self.assertIsNone(child.parent_type)

    def test_ids_are_hydrated_in_batches(self) -> None:
        """200 ids per call is a documented server limit, not a preference."""
        ids = list(range(1, 251))

        def handler(url, method, body):
            if "wiql" in url:
                return {"workItems": [{"id": i} for i in ids]}
            return {"value": []}

        with _PatchRequest(handler) as rec:
            AdoClient(ADO).fetch_catalog()
        hydrates = [c for c in rec.calls if "workitems?" in c["url"]]
        self.assertEqual(len(hydrates), 2)
        # Parse the `ids` param rather than counting commas in the whole URL - `fields`
        # contributes commas of its own.
        batches = [
            parse_qs(urlsplit(call["url"]).query)["ids"][0].split(",") for call in hydrates
        ]
        self.assertEqual([len(b) for b in batches], [200, 50])
        self.assertEqual(batches[0][0], "1")
        self.assertEqual(batches[1][-1], "250")


class ParseReferenceTest(unittest.TestCase):
    TRACKERS = (JIRA, ADO)

    def test_jira_browse_url(self) -> None:
        self.assertEqual(
            parse_reference("https://acme.atlassian.net/browse/PROJ-77", self.TRACKERS),
            ("jira", "PROJ-77"),
        )

    def test_jira_board_url_with_selected_issue(self) -> None:
        url = "https://acme.atlassian.net/jira/software/projects/PROJ/boards/1?selectedIssue=PLAT-3"
        self.assertEqual(parse_reference(url, self.TRACKERS), ("jira", "PLAT-3"))

    def test_ado_work_item_url(self) -> None:
        self.assertEqual(
            parse_reference(
                "https://dev.azure.com/acme/Platform/_workitems/edit/4242", self.TRACKERS
            ),
            ("ado", "4242"),
        )

    def test_bare_jira_key_is_attributed_by_project_prefix(self) -> None:
        self.assertEqual(parse_reference("PROJ-5", self.TRACKERS), ("jira", "PROJ-5"))

    def test_bare_key_is_upper_cased(self) -> None:
        """Jira keys are case-insensitive, so `proj-5` and `PROJ-5` must resolve to one
        ticket - otherwise the 1:1 guarantee could be sidestepped by typing lowercase."""
        self.assertEqual(parse_reference("proj-5", self.TRACKERS), ("jira", "PROJ-5"))

    def test_bare_numeric_id_goes_to_the_single_ado_tracker(self) -> None:
        self.assertEqual(parse_reference("991", self.TRACKERS), ("ado", "991"))

    def test_ambiguous_numeric_id_is_refused_when_two_ado_trackers_exist(self) -> None:
        """Guessing here would silently link to the wrong instance."""
        second = dataclasses.replace(ADO, id="ado2")
        self.assertIsNone(parse_reference("991", (ADO, second)))

    def test_unknown_prefix_resolves_when_only_one_jira_is_connected(self) -> None:
        """A key outside the configured projects is still unambiguous with one Jira - which
        is what lets someone link a one-off dependency on another team's board."""
        self.assertEqual(parse_reference("OTHER-1", (JIRA,)), ("jira", "OTHER-1"))

    def test_unknown_prefix_is_refused_when_two_jiras_are_connected(self) -> None:
        second = dataclasses.replace(JIRA, id="jira2", base_url="https://other.atlassian.net")
        self.assertIsNone(parse_reference("OTHER-1", (JIRA, second)))

    def test_url_host_wins_over_project_prefix(self) -> None:
        """The same key can exist in two instances; the URL says which one is meant."""
        second = dataclasses.replace(
            JIRA, id="jira2", base_url="https://other.atlassian.net", projects=("PROJ",)
        )
        self.assertEqual(
            parse_reference("https://other.atlassian.net/browse/PROJ-1", (JIRA, second)),
            ("jira2", "PROJ-1"),
        )

    def test_garbage_and_empty_input(self) -> None:
        self.assertIsNone(parse_reference("not a ticket", self.TRACKERS))
        self.assertIsNone(parse_reference("", self.TRACKERS))
        self.assertIsNone(parse_reference("PROJ-1", ()))


class ScrubTest(unittest.TestCase):
    def test_a_token_never_survives_into_an_error_string(self) -> None:
        scrubbed = trackers_module._scrub("HTTP 401 for jira-token-secret", "jira-token-secret")
        self.assertNotIn("jira-token-secret", scrubbed)
        self.assertIn("***", scrubbed)

    def test_userinfo_in_a_url_is_stripped(self) -> None:
        scrubbed = trackers_module._scrub("failed on https://bob:hunter2@jira.example.com/x")
        self.assertNotIn("hunter2", scrubbed)

    def test_a_very_short_secret_is_not_used_as_a_pattern(self) -> None:
        """Replacing a 1-3 character "secret" would redact ordinary text into nonsense."""
        self.assertEqual(trackers_module._scrub("abc def", "a"), "abc def")


class _StoreCase(unittest.TestCase):
    """Base for tests needing a real TrackerStore over a temp workspace."""

    TRACKERS: tuple[TrackerConfig, ...] = (JIRA, ADO)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._orig_config = config_module.CONFIG
        config_module.CONFIG = dataclasses.replace(
            config_module.CONFIG,
            repo_root=root,
            workspace_root="ws",
            products={"app": "App", "web": "Web"},
            trackers=self.TRACKERS,
        )
        # The module read CONFIG at import for its own module-level constants.
        trackers_module.CONFIG = config_module.CONFIG
        trackers_module.CATALOG_PATH = config_module.CONFIG.workspace_dir / "trackers.json"
        trackers_module.CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        config_module.CONFIG = self._orig_config
        trackers_module.CONFIG = self._orig_config
        trackers_module.CATALOG_PATH = self._orig_config.workspace_dir / "trackers.json"
        self._tmp.cleanup()


class TrackerStoreSyncTest(_StoreCase):
    def _handler(self, url, method, body):
        if "/search/jql" in url:
            return {
                "issues": [jira_issue("PROJ-1", "Bug"), jira_issue("PLAT-2", "Epic")],
                "isLast": True,
            }
        if "wiql" in url:
            return {"workItems": [{"id": 7}]}
        return {
            "value": [
                {
                    "id": 7,
                    "fields": {
                        "System.Title": "Portal",
                        "System.WorkItemType": "User Story",
                        "System.State": "New",
                    },
                }
            ]
        }

    def test_sync_populates_both_trackers_and_persists(self) -> None:
        store = TrackerStore()
        with _PatchRequest(self._handler):
            store.sync()
        self.assertEqual(store.lookup("jira", "PROJ-1").type, "bug")
        self.assertEqual(store.lookup("ado", "7").type, "story")
        # A fresh store reads the cache back rather than needing another sync.
        reloaded = TrackerStore()
        self.assertEqual(reloaded.lookup("jira", "PLAT-2").type, "epic")

    def test_lookup_is_case_insensitive_for_jira(self) -> None:
        store = TrackerStore()
        with _PatchRequest(self._handler):
            store.sync()
        self.assertIsNotNone(store.lookup("jira", "proj-1"))

    def test_a_failing_tracker_records_its_error_without_raising(self) -> None:
        """One unreachable instance must not take out the board, nor the other tracker."""

        def handler(url, method, body):
            if "atlassian" in url:
                raise TrackerError("could not reach acme.atlassian.net: refused")
            return self._handler(url, method, body)

        store = TrackerStore()
        with _PatchRequest(handler):
            described = store.sync()
        by_id = {t["id"]: t for t in described["trackers"]}
        self.assertFalse(by_id["jira"]["status"]["ok"])
        self.assertIn("refused", by_id["jira"]["status"]["error"])
        # The healthy one still synced.
        self.assertTrue(by_id["ado"]["status"]["ok"])

    def test_a_failed_sync_keeps_the_previous_catalog(self) -> None:
        """A card showing last week's type beats one that suddenly claims no ticket."""
        store = TrackerStore()
        with _PatchRequest(self._handler):
            store.sync()
        with _PatchRequest(lambda u, m, b: (_ for _ in ()).throw(TrackerError("boom"))):
            store.sync()
        self.assertEqual(store.lookup("jira", "PROJ-1").type, "bug")

    def test_a_successful_sync_drops_tickets_deleted_upstream(self) -> None:
        store = TrackerStore()
        with _PatchRequest(self._handler):
            store.sync()
        self.assertIsNotNone(store.lookup("jira", "PLAT-2"))

        def shrunk(url, method, body):
            if "/search/jql" in url:
                return {"issues": [jira_issue("PROJ-1", "Bug")], "isLast": True}
            return self._handler(url, method, body)

        with _PatchRequest(shrunk):
            store.sync()
        self.assertIsNone(store.lookup("jira", "PLAT-2"))
        # ...and does not disturb the other tracker's slice.
        self.assertIsNotNone(store.lookup("ado", "7"))

    def test_an_unexpected_exception_is_captured_not_propagated(self) -> None:
        """A client bug must not kill the sync thread silently."""

        def handler(url, method, body):
            raise ValueError("a bug, not an HTTP failure")

        store = TrackerStore()
        with _PatchRequest(handler):
            described = store.sync()
        status = {t["id"]: t["status"] for t in described["trackers"]}["jira"]
        self.assertFalse(status["ok"])
        self.assertIn("unexpected error", status["error"])

    def test_describe_never_serialises_a_token(self) -> None:
        """The payload is built field by field precisely so this stays true."""
        store = TrackerStore()
        with _PatchRequest(self._handler):
            store.sync()
        blob = json.dumps(store.describe())
        self.assertNotIn("jira-token-secret", blob)
        self.assertNotIn("ado-pat-secret", blob)

    def test_the_persisted_cache_never_contains_a_token(self) -> None:
        store = TrackerStore()
        with _PatchRequest(self._handler):
            store.sync()
        text = trackers_module.CATALOG_PATH.read_text()
        self.assertNotIn("jira-token-secret", text)
        self.assertNotIn("ado-pat-secret", text)

    def test_search_matches_key_or_title(self) -> None:
        store = TrackerStore()
        with _PatchRequest(self._handler):
            store.sync()
        self.assertEqual([t["key"] for t in store.search("plat")[0]], ["PLAT-2"])
        self.assertEqual([t["key"] for t in store.search("portal")[0]], ["7"])
        self.assertEqual(len(store.search("")[0]), 3)
        # Nothing excluded and nothing declined, so nothing hidden for either reason.
        self.assertEqual(store.search("")[1], {"linked": 0, "declined": 0})

    def test_search_hides_excluded_tickets_and_counts_them(self) -> None:
        """Already-linked tickets are left out rather than offered: every row in the
        picker is meant to be a choice."""
        store = TrackerStore()
        with _PatchRequest(self._handler):
            store.sync()
        tickets, hidden = store.search("", exclude=frozenset({("jira", "PLAT-2")}))
        self.assertEqual(sorted(t["key"] for t in tickets), ["7", "PROJ-1"])
        self.assertEqual(hidden["linked"], 1)

    def test_exclusion_is_case_insensitive_for_jira(self) -> None:
        """The exclusion set is compared through normalize_key, or a link stored as
        `plat-2` would fail to hide the catalog's `PLAT-2`."""
        store = TrackerStore()
        with _PatchRequest(self._handler):
            store.sync()
        tickets, hidden = store.search("plat", exclude=frozenset({("jira", "plat-2".upper())}))
        self.assertEqual(tickets, [])
        self.assertEqual(hidden["linked"], 1)

    def test_exclusion_is_applied_before_the_limit(self) -> None:
        """The whole point of filtering server-side. On a board where most of the catalog
        is already linked, limiting first would spend the page on rows nobody can pick."""
        store = TrackerStore()
        with _PatchRequest(self._handler):
            store.sync()
        # Exclude the entry that sorts first, and ask for one row: the survivor must fill
        # it rather than the page coming back empty.
        tickets, hidden = store.search(
            "", tracker_id="jira", limit=1, exclude=frozenset({("jira", "PLAT-2")})
        )
        self.assertEqual([t["key"] for t in tickets], ["PROJ-1"])
        self.assertEqual(hidden["linked"], 1)

    def test_declined_tickets_are_never_offered(self) -> None:
        """A won't-do ticket is a decision NOT to build. The import passes already refuse
        it; offering it for a human to link by hand would put declined work back on the
        board through the other door."""
        store = TrackerStore()
        with _PatchRequest(self._handler):
            store.sync()
        with store._lock:
            key = next(k for k, t in store._state.tickets.items() if t.key == "PROJ-1")
            store._state.tickets[key] = dataclasses.replace(
                store._state.tickets[key], state="Done", resolution="Won't Do"
            )
        tickets, hidden = store.search("")
        self.assertNotIn("PROJ-1", [t["key"] for t in tickets])
        self.assertEqual(hidden["declined"], 1)
        # And it stays IN the catalog: the won't-do removal pass reads it there to notice a
        # ticket declined after it was imported, and treats absence as unknown.
        self.assertIsNotNone(store.lookup("jira", "PROJ-1"))

    def test_a_declined_ticket_is_counted_apart_from_a_linked_one(self) -> None:
        """Two reasons a candidate is withheld, two different fixes - unlink it there,
        versus it is not happening at all - so the picker must not conflate them."""
        store = TrackerStore()
        with _PatchRequest(self._handler):
            store.sync()
        with store._lock:
            key = next(k for k, t in store._state.tickets.items() if t.key == "PROJ-1")
            store._state.tickets[key] = dataclasses.replace(
                store._state.tickets[key], resolution="Wont-Do"
            )
        _, hidden = store.search("", exclude=frozenset({("jira", "PLAT-2")}))
        self.assertEqual(hidden, {"linked": 1, "declined": 1})

    def test_ensure_ticket_fetches_one_outside_the_synced_projects(self) -> None:
        """Linking a ticket the catalog pull never covered must work, or it would render as
        unresolved forever."""

        def handler(url, method, body):
            if "/issue/OTHER-1" in url:
                return jira_issue("OTHER-1", "Task", "One-off")
            return self._handler(url, method, body)

        store = TrackerStore()
        with _PatchRequest(handler):
            store.sync()
            ticket = store.ensure_ticket("jira", "OTHER-1")
        self.assertEqual(ticket.type, "task")
        self.assertIsNotNone(store.lookup("jira", "OTHER-1"))

    def test_ensure_ticket_returns_none_for_a_typo(self) -> None:
        """This is what lets the link endpoint refuse a bad key instead of storing a dead
        reference."""
        with _PatchRequest(lambda u, m, b: (_ for _ in ()).throw(TrackerError("HTTP 404: no"))):
            self.assertIsNone(TrackerStore().ensure_ticket("jira", "NOPE-1"))

    def test_syncing_window_outlives_the_fetch(self) -> None:
        """is_syncing is what the board's Sync button and POST /trackers/sync read to
        refuse a second pass. It used to cover the fetch alone, which left the server's
        own import passes unguarded - the window in which two passes both decided a
        ticket was unlinked and imported it twice."""
        store = TrackerStore()
        self.assertFalse(store.is_syncing)
        with store.syncing():
            self.assertTrue(store.is_syncing)
            # The fetch marks its own (nested) window; leaving it must not clear ours.
            with _PatchRequest(self._handler):
                store.sync()
            self.assertTrue(store.is_syncing)
        self.assertFalse(store.is_syncing)

    def test_syncing_window_clears_when_the_pass_raises(self) -> None:
        store = TrackerStore()
        with self.assertRaises(RuntimeError):
            with store.syncing():
                raise RuntimeError("pass blew up")
        self.assertFalse(store.is_syncing)

    def test_an_unusable_tracker_reports_why_instead_of_being_skipped(self) -> None:
        """Silently ignoring a misconfigured tracker looks exactly like one with no
        tickets, which is the confusing outcome."""
        self.TRACKERS = (dataclasses.replace(JIRA, token=""),)
        self.tearDown()
        self.setUp()
        store = TrackerStore()
        described = store.sync()
        status = described["trackers"][0]["status"]
        self.assertFalse(status["ok"])
        self.assertIn("token", status["error"])

    def test_a_corrupt_cache_does_not_break_construction(self) -> None:
        trackers_module.CATALOG_PATH.write_text("{not json at all")
        store = TrackerStore()  # must not raise
        self.assertIsNone(store.lookup("jira", "PROJ-1"))


class TicketLinkTest(_StoreCase):
    """The 1:1 guarantee, against a real RoadmapStore."""

    def setUp(self) -> None:
        super().setUp()
        from pm_studio import roadmap as roadmap_module

        self.roadmap_module = roadmap_module
        roadmap_module.CONFIG = config_module.CONFIG
        roadmap_module.PRODUCTS = config_module.CONFIG.products
        roadmap_module.ROADMAP_DIR = config_module.CONFIG.workspace_dir / "roadmap"
        self.store = roadmap_module.RoadmapStore()
        self.a = self.store.create(product="app", title="Fix login")
        self.b = self.store.create(product="web", title="Redo nav")

    def test_link_and_read_back(self) -> None:
        self.store.link_ticket(self.a.id, "jira", "PROJ-1")
        item = self.store.get(self.a.id)
        self.assertEqual((item.tracker_id, item.ticket_key), ("jira", "PROJ-1"))

    def test_key_is_normalised_on_write(self) -> None:
        self.store.link_ticket(self.a.id, "jira", "proj-1")
        self.assertEqual(self.store.get(self.a.id).ticket_key, "PROJ-1")

    def test_a_second_item_cannot_take_the_same_ticket(self) -> None:
        self.store.link_ticket(self.a.id, "jira", "PROJ-1")
        with self.assertRaises(self.roadmap_module.TicketAlreadyLinked) as caught:
            self.store.link_ticket(self.b.id, "jira", "PROJ-1")
        # The message must name the change already holding it, or the user has to go
        # hunting the board for the conflict.
        self.assertIn("Fix login", str(caught.exception))
        self.assertIn(self.a.id, str(caught.exception))

    def test_the_conflict_check_is_case_insensitive(self) -> None:
        """Otherwise `proj-1` would happily link alongside `PROJ-1`, quietly breaking 1:1."""
        self.store.link_ticket(self.a.id, "jira", "PROJ-1")
        with self.assertRaises(self.roadmap_module.TicketAlreadyLinked):
            self.store.link_ticket(self.b.id, "jira", "proj-1")

    def test_the_same_key_in_a_different_tracker_is_a_different_ticket(self) -> None:
        self.store.link_ticket(self.a.id, "jira", "PROJ-1")
        self.store.link_ticket(self.b.id, "ado", "PROJ-1")
        self.assertEqual(self.store.get(self.b.id).tracker_id, "ado")

    def test_relinking_the_same_pair_is_idempotent(self) -> None:
        """A PM re-sending an identical PATCH must not get a conflict against itself."""
        self.store.link_ticket(self.a.id, "jira", "PROJ-1")
        self.store.link_ticket(self.a.id, "jira", "PROJ-1")
        self.assertEqual(self.store.get(self.a.id).ticket_key, "PROJ-1")

    def test_moving_a_link_frees_the_previous_ticket(self) -> None:
        self.store.link_ticket(self.a.id, "jira", "PROJ-1")
        self.store.link_ticket(self.a.id, "jira", "PROJ-2")
        self.store.link_ticket(self.b.id, "jira", "PROJ-1")
        self.assertEqual(self.store.item_for_ticket("jira", "PROJ-1").id, self.b.id)

    def test_unlink_frees_the_ticket_and_is_idempotent(self) -> None:
        self.store.link_ticket(self.a.id, "jira", "PROJ-1")
        self.store.unlink_ticket(self.a.id)
        self.store.unlink_ticket(self.a.id)
        self.assertIsNone(self.store.get(self.a.id).ticket_key)
        self.store.link_ticket(self.b.id, "jira", "PROJ-1")

    def test_item_for_ticket_finds_nothing_when_unlinked(self) -> None:
        self.assertIsNone(self.store.item_for_ticket("jira", "PROJ-1"))

    def test_linked_refs_lists_only_linked_changes(self) -> None:
        self.store.link_ticket(self.a.id, "jira", "PROJ-1")
        self.assertEqual(self.store.linked_ticket_refs(), [("jira", "PROJ-1")])

    def test_a_link_survives_a_reload(self) -> None:
        self.store.link_ticket(self.a.id, "jira", "PROJ-1")
        reloaded = self.roadmap_module.RoadmapStore()
        self.assertEqual(reloaded.get(self.a.id).ticket_key, "PROJ-1")

    def test_roadmap_json_written_before_this_feature_still_loads(self) -> None:
        """The fields are additive with None defaults precisely so no migration is needed."""
        path = self.roadmap_module.ROADMAP_DIR / "app.json"
        raw = json.loads(path.read_text())
        for entry in raw:
            entry.pop("tracker_id", None)
            entry.pop("ticket_key", None)
        path.write_text(json.dumps(raw))
        reloaded = self.roadmap_module.RoadmapStore()
        self.assertEqual(reloaded.get(self.a.id).title, "Fix login")
        self.assertIsNone(reloaded.get(self.a.id).ticket_key)

    def test_link_requires_both_parts(self) -> None:
        with self.assertRaises(ValueError):
            self.store.link_ticket(self.a.id, "jira", "")

    def test_pm_context_reports_the_linked_ticket(self) -> None:
        self.store.link_ticket(self.b.id, "jira", "PROJ-1")
        described = self.store.describe_own_product(
            "web",
            ticket_lookup=lambda t, k: {"raw_type": "Bug", "state": "In Progress"},
        )
        self.assertIn("tracked as Bug PROJ-1 (In Progress)", described)

    def test_pm_context_degrades_without_a_catalog_entry(self) -> None:
        """The PM should still be told a link exists even when the ticket isn't synced."""
        self.store.link_ticket(self.b.id, "jira", "PROJ-1")
        described = self.store.describe_own_product("web", ticket_lookup=lambda t, k: None)
        self.assertIn("linked to PROJ-1", described)


class TrackerConfigParsingTest(unittest.TestCase):
    PATH = Path("/tmp/config.toml")

    def test_env_var_token_is_preferred(self) -> None:
        import os

        os.environ["TEST_TRACKER_TOKEN"] = "from-env"
        try:
            trackers = _parse_trackers(
                {
                    "trackers": [
                        {
                            "provider": "jira",
                            "base_url": "https://acme.atlassian.net",
                            "projects": ["A"],
                            "token_env": "TEST_TRACKER_TOKEN",
                        }
                    ]
                },
                self.PATH,
            )
        finally:
            del os.environ["TEST_TRACKER_TOKEN"]
        self.assertEqual(trackers[0].token, "from-env")

    def test_ado_base_url_is_derived_from_the_organization(self) -> None:
        trackers = _parse_trackers(
            {"trackers": [{"provider": "ado", "organization": "acme", "projects": ["P"]}]},
            self.PATH,
        )
        self.assertEqual(trackers[0].base_url, "https://dev.azure.com/acme")

    def test_a_single_project_may_be_given_as_a_string(self) -> None:
        trackers = _parse_trackers(
            {"trackers": [{"provider": "ado", "organization": "a", "projects": "Solo"}]},
            self.PATH,
        )
        self.assertEqual(trackers[0].projects, ("Solo",))

    def test_an_unknown_provider_is_skipped_rather_than_fatal(self) -> None:
        """A typo must not stop the studio booting - the board is useful without a tracker."""
        trackers = _parse_trackers({"trackers": [{"provider": "bugzilla"}]}, self.PATH)
        self.assertEqual(trackers, ())

    def test_a_duplicate_id_is_skipped(self) -> None:
        """Ids identify a tracker on every linked item, so two trackers sharing one would
        make existing links ambiguous."""
        entry = {"provider": "jira", "id": "same", "base_url": "https://x", "projects": ["A"]}
        trackers = _parse_trackers({"trackers": [entry, dict(entry)]}, self.PATH)
        self.assertEqual(len(trackers), 1)

    def test_the_sync_interval_is_floored(self) -> None:
        """Hammering someone else's tracker is worse than syncing a little less often."""
        trackers = _parse_trackers(
            {
                "trackers": [
                    {
                        "provider": "jira",
                        "base_url": "https://x",
                        "projects": ["A"],
                        "sync_interval_minutes": 0.01,
                    }
                ]
            },
            self.PATH,
        )
        self.assertEqual(trackers[0].sync_interval_minutes, config_module.MIN_SYNC_INTERVAL_MINUTES)

    def test_no_trackers_table_means_the_feature_is_dormant(self) -> None:
        self.assertEqual(_parse_trackers({}, self.PATH), ())

    def test_unusable_reason_explains_each_missing_piece(self) -> None:
        self.assertIn("token", dataclasses.replace(JIRA, token="").unusable_reason)
        self.assertIn("projects", dataclasses.replace(JIRA, projects=()).unusable_reason)
        self.assertIn("base_url", dataclasses.replace(JIRA, base_url="").unusable_reason)
        self.assertEqual(JIRA.unusable_reason, "")


class TicketDataclassTest(unittest.TestCase):
    def test_from_dict_tolerates_an_extra_key(self) -> None:
        """The cache is written by a possibly-older version of this code."""
        ticket = Ticket.from_dict(
            {
                "tracker_id": "jira",
                "provider": "jira",
                "key": "A-1",
                "type": "bug",
                "raw_type": "Bug",
                "title": "t",
                "state": "s",
                "url": "u",
                "synced_at": 1.0,
                "some_future_field": "ignored",
            }
        )
        self.assertEqual(ticket.key, "A-1")


if __name__ == "__main__":
    unittest.main()
