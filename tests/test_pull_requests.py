"""Tests for pull requests opened for reviewers after a task passes the judge.

No test here touches the network. Pushes go to a local bare repository: each clone's
origin is a real-looking Bitbucket / Azure DevOps URL (so host detection is exercised)
with `pushInsteadOf` pointing the push at the bare repo, and setUp refuses to run
unless git resolves the push URL locally. Every API call goes through
pull_requests._request, which the tests replace.
"""

import dataclasses
import os
import subprocess
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests._claude_stub import patch_claude
from tests.test_gitflow_judge import _FakeClaude, _git, _init_repo

from pm_studio import agent as agent_module
from pm_studio import judge as judge_module
from pm_studio import pull_requests as pr_module
from pm_studio import roadmap as roadmap_module
from pm_studio import tasks as tasks_module
from pm_studio.config import PullRequestConfig, SystemSpec, load_config
from pm_studio.pull_requests import Remote, parse_remote
from pm_studio.tasks import TaskRegistry
from pm_studio.trackers import TrackerError

BB_ORIGIN = "https://bitbucket.org/acme/rides.git"
ADO_ORIGIN = "https://acme@dev.azure.com/acme/Big%20Project/_git/Rides.Api"
CONFIG = PullRequestConfig(
    enabled=True, bitbucket_email="bot@example.com", bitbucket_token="bb-secret-token",
    ado_token="ado-secret-pat",
)
SPEC = SystemSpec(
    label="Rides", path="src/rides", gitflow="GITFLOW.md",
    pr_targets=(("*-rc", "release-candidate/next-release"),),
)


class ParseRemoteTest(unittest.TestCase):
    def test_bitbucket_spellings(self) -> None:
        expected = Remote("bitbucket", "acme", "rides")
        for url in (
            "https://bitbucket.org/acme/rides.git",
            "https://someone@bitbucket.org/acme/rides.git",
            "git@bitbucket.org:acme/rides.git",
            "ssh://git@bitbucket.org/acme/rides.git",
            # A browser URL pasted as the remote still names the repository.
            "https://bitbucket.org/acme/rides/src/main/",
        ):
            self.assertEqual(parse_remote(url), expected, url)

    def test_azure_devops_spellings(self) -> None:
        expected = Remote("ado", "acme", "Rides.Api", project="Big Project")
        for url in (
            "https://dev.azure.com/acme/Big%20Project/_git/Rides.Api",
            "https://acme@dev.azure.com/acme/Big%20Project/_git/Rides.Api",
            "git@ssh.dev.azure.com:v3/acme/Big%20Project/Rides.Api",
            "https://acme.visualstudio.com/DefaultCollection/Big%20Project/_git/Rides.Api",
        ):
            self.assertEqual(parse_remote(url), expected, url)

    def test_other_hosts_are_not_recognized(self) -> None:
        self.assertIsNone(parse_remote("https://github.com/acme/rides.git"))
        self.assertIsNone(parse_remote("/srv/git/rides.git"))


class ConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "pm_studio_local").mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _load(self, config: str):
        (self.root / "pm_studio_local" / "config.toml").write_text(textwrap.dedent(config))
        return load_config(self.root)

    def test_absent_table_is_disabled(self) -> None:
        self.assertFalse(self._load("").pull_requests.enabled)

    def test_credentials_are_read_from_the_named_variables(self) -> None:
        env = {"T_BB_EMAIL": "bot@example.com", "T_BB_TOKEN": "bbtok", "T_ADO": "adotok"}
        with mock.patch.dict(os.environ, env):
            cfg = self._load("""\
                [pull_requests]
                enabled = true
                [pull_requests.bitbucket]
                email_env = "T_BB_EMAIL"
                token_env = "T_BB_TOKEN"
                [pull_requests.ado]
                token_env = "T_ADO"
            """)
        self.assertEqual(
            cfg.pull_requests,
            PullRequestConfig(True, "bot@example.com", "bbtok", "adotok"),
        )

    def test_pr_targets_first_match_wins_and_default_is_main(self) -> None:
        cfg = self._load("""\
            [systems.rides]
            label = "Rides"
            pr_targets = { "*-rc" = "release-candidate/next-release", "hotfix/*" = "prod" }
        """)
        spec = cfg.systems["rides"]
        self.assertEqual(spec.pr_target("feature/NDT-1-x-rc"), "release-candidate/next-release")
        self.assertEqual(spec.pr_target("hotfix/NDT-2"), "prod")
        self.assertEqual(spec.pr_target("feature/NDT-1-x"), "main")

    def test_an_array_of_reviewers_applies_to_every_repo(self) -> None:
        cfg = self._load("""\
            [systems.rides]
            label = "Rides"
            path = "src/rides"
            reviewers = ["{ana}", " "]
        """)
        self.assertEqual(cfg.systems["rides"].reviewers_for("src/rides/backend"), ("{ana}",))

    def test_per_repo_reviewers_add_up(self) -> None:
        cfg = self._load("""\
            [systems.rides]
            label = "Rides"
            path = "src/rides"
            [systems.rides.reviewers]
            "*" = ["{ana}", "{bo}"]
            "db-migrations" = ["{dba}", "{bo}"]
        """)
        spec = cfg.systems["rides"]
        self.assertEqual(spec.reviewers_for("src/rides/backend"), ("{ana}", "{bo}"))
        self.assertEqual(spec.reviewers_for("src/rides/db-migrations"), ("{ana}", "{bo}", "{dba}"))

    def test_a_bare_string_of_reviewers_is_fatal(self) -> None:
        with self.assertRaises(SystemExit):
            self._load("""\
                [systems.rides]
                label = "Rides"
                reviewers = "{ana}"
            """)

    def test_invalid_pr_targets_is_fatal(self) -> None:
        with self.assertRaises(SystemExit):
            self._load("""\
                [systems.rides]
                label = "Rides"
                pr_targets = ["main"]
            """)


class _Api:
    """Stands in for pull_requests._request: records calls, answers by URL substring."""

    def __init__(self, routes: list[tuple[str, str, object]]):
        self.routes = routes
        self.calls: list[dict] = []

    def __call__(self, url, *, headers, method="GET", body=None, timeout=20):
        self.calls.append({"url": url, "method": method, "body": body, "headers": headers})
        for route_method, fragment, answer in self.routes:
            if route_method == method and fragment in url:
                if isinstance(answer, Exception):
                    raise answer
                return answer(url) if callable(answer) else answer
        raise AssertionError(f"unexpected request {method} {url}")

    def posts(self) -> list[dict]:
        return [c for c in self.calls if c["method"] == "POST"]


class _RepoFixture(unittest.TestCase):
    """A system repo at src/rides/backend whose origin pushes to a local bare repo."""

    origin_url = BB_ORIGIN

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.bare = self.root / "remote.git"
        _git(self.root, "init", "-q", "--bare", "-b", "main", str(self.bare))
        self.repo = self.root / "src" / "rides" / "backend"
        self.repo.mkdir(parents=True)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "test@example.com")
        _git(self.repo, "config", "user.name", "Test")
        (self.repo / "README.md").write_text("seed\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "seed")
        _git(self.repo, "remote", "add", "origin", self.origin_url)
        # The whole URL, not a prefix: insteadOf rewrites by prefix, so a prefix would
        # append the rest of the URL to the bare repo's path.
        _git(self.repo, "config", f"url.{self.bare.as_posix()}.pushInsteadOf", self.origin_url)
        # The guard: a push from these tests may only ever land in the bare repo.
        push_url = _git(self.repo, "remote", "get-url", "--push", "origin")
        if push_url != self.bare.as_posix():
            self.fail(f"push URL resolves to {push_url!r}, not the local bare repo")
        _git(self.repo, "push", "-q", "origin", "main")
        # What a fetch would leave - without one: only pushes are rewritten, so a fetch
        # would go to the real host.
        _git(self.repo, "update-ref", "refs/remotes/origin/main", "main")
        self.api = _Api(self.routes())
        self._orig_request = pr_module._request
        pr_module._request = self.api

    def tearDown(self) -> None:
        pr_module._request = self._orig_request
        self._tmp.cleanup()

    def routes(self) -> list:
        return []

    def _branches(self) -> dict[str, str]:
        out = _git(self.repo, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads")
        return dict(line.split(" ") for line in out.splitlines())

    def _work_on(self, branch: str, *files: str, base: str = "main") -> None:
        _git(self.repo, "switch", "-q", "-c", branch, base)
        for name in files:
            (self.repo / name).write_text(f"{name}\n")
            _git(self.repo, "add", "-A")
            _git(self.repo, "commit", "-q", "-m", f"feat(rides): NDT-7 add {name}")

    def _open(
        self, before: dict[str, str], config: PullRequestConfig = CONFIG, spec: SystemSpec = SPEC,
    ) -> list[dict]:
        rel = "src/rides/backend"
        return pr_module.open_for_task(
            self.root, spec, config,
            {rel: {"branches": before}}, {rel: {"branches": self._branches()}},
            task_id="t1", description="Add the rides thing.",
        )

    def _pushed(self, branch: str) -> str:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "-q", f"refs/heads/{branch}"],
            cwd=self.bare, capture_output=True, text=True,
        )
        return proc.stdout.strip()


class BitbucketTest(_RepoFixture):
    def routes(self) -> list:
        return [
            ("GET", "/pullrequests?", {"values": []}),
            ("GET", "/2.0/user", {"uuid": "{me}"}),
            ("GET", "effective-default-reviewers?pagelen=100", {
                "values": [{"user": {"uuid": "{ana}"}}, {"user": {"uuid": "{me}"}}],
                "next": "https://api.bitbucket.org/2.0/page2",
            }),
            ("GET", "/page2", {"values": [{"user": {"uuid": "{bo}"}}, {"user": {"uuid": "{ana}"}}]}),
            ("POST", "/repositories/acme/rides/pullrequests", lambda url: {
                "id": 42,
                "links": {"html": {"href": "https://bitbucket.org/acme/rides/pull-requests/42"}},
                "reviewers": [{"display_name": "Ana"}, {"display_name": "Bo"}],
            }),
        ]

    def test_pushes_and_opens_with_default_reviewers_minus_the_author(self) -> None:
        before = self._branches()
        self._work_on("feature/NDT-7-rides-thing", "svc.ts")
        (record,) = self._open(before)
        self.assertEqual(record["status"], "opened", record)
        self.assertEqual(record["url"], "https://bitbucket.org/acme/rides/pull-requests/42")
        self.assertEqual(record["target"], "main")
        self.assertEqual(self._pushed("feature/NDT-7-rides-thing"), self._branches()["feature/NDT-7-rides-thing"])
        (post,) = self.api.posts()
        body = post["body"]
        # Every page followed, the author dropped, duplicates collapsed.
        self.assertEqual(body["reviewers"], [{"uuid": "{ana}"}, {"uuid": "{bo}"}])
        self.assertEqual(body["source"], {"branch": {"name": "feature/NDT-7-rides-thing"}})
        self.assertEqual(body["destination"], {"branch": {"name": "main"}})
        self.assertEqual(body["title"], "feat(rides): NDT-7 add svc.ts")
        self.assertIn("Add the rides thing.", body["description"])
        self.assertIn("GITFLOW.md", body["description"])

    def test_system_reviewers_are_added_on_top_of_the_defaults(self) -> None:
        """Duplicates of a default collapse, the author is still dropped, and a bare
        Atlassian account id is sent as one."""
        spec = dataclasses.replace(SPEC, reviewers=(
            ("*", ("{bo}", "{me}", "557058:abc")),
            ("db-*", ("{dba}",)),  # another repo of the system: not this one
        ))
        before = self._branches()
        self._work_on("feature/NDT-7-x", "svc.ts")
        self._open(before, spec=spec)
        self.assertEqual(
            self.api.posts()[0]["body"]["reviewers"],
            [{"uuid": "{ana}"}, {"uuid": "{bo}"}, {"account_id": "557058:abc"}],
        )

    def test_several_commits_take_the_title_from_the_branch(self) -> None:
        before = self._branches()
        self._work_on("feature/NDT-7-rides-thing", "a.ts", "b.ts")
        self._open(before)
        self.assertEqual(self.api.posts()[0]["body"]["title"], "NDT-7 rides thing")

    def test_rc_branch_targets_the_release_candidate(self) -> None:
        _git(self.repo, "branch", "release-candidate/next-release", "main")
        before = self._branches()
        self._work_on("feature/NDT-7-x", "svc.ts")
        self._work_on("feature/NDT-7-x-rc", "svc.ts", base="release-candidate/next-release")
        records = self._open(before)
        self.assertEqual(
            [(r["branch"], r["target"]) for r in records],
            [("feature/NDT-7-x", "main"), ("feature/NDT-7-x-rc", "release-candidate/next-release")],
        )

    def test_a_commit_on_a_target_branch_opens_nothing(self) -> None:
        before = self._branches()
        (self.repo / "x.ts").write_text("x\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "on main")
        self.assertEqual(self._open(before), [])
        self.assertEqual(self.api.calls, [])

    def test_an_open_pull_request_is_updated_not_duplicated(self) -> None:
        self.api.routes.insert(0, ("GET", "/pullrequests?", {"values": [{
            "id": 9, "links": {"html": {"href": "https://bitbucket.org/acme/rides/pull-requests/9"}},
        }]}))
        before = self._branches()
        self._work_on("feature/NDT-7-x", "svc.ts")
        (record,) = self._open(before)
        self.assertEqual(record["status"], "updated")
        self.assertEqual(record["id"], 9)
        self.assertEqual(self.api.posts(), [])
        self.assertTrue(self._pushed("feature/NDT-7-x"))

    def test_no_credential_fails_without_pushing(self) -> None:
        before = self._branches()
        self._work_on("feature/NDT-7-x", "svc.ts")
        (record,) = self._open(before, PullRequestConfig(enabled=True))
        self.assertEqual(record["status"], "failed")
        self.assertIn("Bitbucket credential", record["error"])
        self.assertEqual(self._pushed("feature/NDT-7-x"), "")

    def test_api_errors_are_recorded_and_scrubbed(self) -> None:
        self.api.routes.insert(0, ("POST", "/pullrequests", TrackerError("HTTP 400: bad bb-secret-token")))
        before = self._branches()
        self._work_on("feature/NDT-7-x", "svc.ts")
        (record,) = self._open(before)
        self.assertEqual(record["status"], "failed")
        self.assertNotIn("bb-secret-token", record["error"])

    def test_the_token_is_not_on_the_push_command_line(self) -> None:
        before = self._branches()
        self._work_on("feature/NDT-7-x", "svc.ts")
        real_run = subprocess.run
        commands: list[list[str]] = []

        def spy(cmd, **kwargs):
            commands.append(list(cmd))
            return real_run(cmd, **kwargs)

        with mock.patch.object(pr_module.subprocess, "run", spy):
            self._open(before)
        self.assertTrue(any("push" in c for c in commands))
        self.assertFalse(any("bb-secret-token" in " ".join(c) for c in commands))


class AzureDevOpsTest(_RepoFixture):
    origin_url = ADO_ORIGIN

    def routes(self) -> list:
        return [
            ("GET", "/pullrequests?", {"value": []}),
            ("POST", "/pullrequests?", {"pullRequestId": 77, "reviewers": [{"displayName": "Leads"}]}),
        ]

    def test_opens_against_the_target_ref(self) -> None:
        before = self._branches()
        self._work_on("feature/NDT-7-x", "svc.ts")
        (record,) = self._open(before)
        self.assertEqual(record["status"], "opened", record)
        self.assertEqual(
            record["url"], "https://dev.azure.com/acme/Big%20Project/_git/Rides.Api/pullrequest/77"
        )
        self.assertEqual(record["reviewers"], ["Leads"])
        (post,) = self.api.posts()
        self.assertIn("/acme/Big%20Project/_apis/git/repositories/Rides.Api/pullrequests", post["url"])
        self.assertEqual(post["body"]["sourceRefName"], "refs/heads/feature/NDT-7-x")
        self.assertEqual(post["body"]["targetRefName"], "refs/heads/main")
        # Policy reviewers are the service's job; nothing extra declared, nothing sent.
        self.assertEqual(post["body"]["reviewers"], [])
        self.assertTrue(self._pushed("feature/NDT-7-x"))

    def test_system_reviewers_are_sent_as_identity_ids(self) -> None:
        spec = dataclasses.replace(SPEC, reviewers=(("*", ("1111-guid",)),))
        before = self._branches()
        self._work_on("feature/NDT-7-x", "svc.ts")
        self._open(before, spec=spec)
        self.assertEqual(self.api.posts()[0]["body"]["reviewers"], [{"id": "1111-guid"}])


class TaskWiringTest(unittest.TestCase):
    """The registry only reaches for pull requests when enabled AND the judge passed."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        _init_repo(self.repo)
        (self.repo / "GITFLOW.md").write_text("rules")
        self.child = self.repo / "src" / "rides" / "backend"
        self.child.mkdir(parents=True)
        _init_repo(self.child)
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "seed")
        (self.repo / "pm_studio_local").mkdir()
        (self.repo / "pm_studio_local" / "config.toml").write_text("")

        self._orig_config = tasks_module.CONFIG
        self._orig_systems = roadmap_module.SYSTEMS
        self._orig_run_judge = judge_module.run_judge
        self._orig_open = pr_module.open_for_task
        self.set_enabled(True)
        roadmap_module.SYSTEMS = {"rides": SPEC}
        self.verdict = "pass"
        judge_module.run_judge = lambda **kw: {
            "verdict": self.verdict, "summary": "s", "model": "m", "agent_usage": {},
            "violations": [{"rule": "r", "evidence": "e"}] if self.verdict == "violation" else [],
        }
        self.opened: list[dict] = []

        def fake_open(repo_root, spec, config, before, after, **kw):
            self.opened.append({"before": before, "after": after, **kw})
            return [{"repo": "src/rides/backend", "branch": "feature/NDT-7-x", "target": "main",
                     "status": "opened", "url": "https://example.test/pr/1", "reviewers": ["Ana"]}]

        pr_module.open_for_task = fake_open
        self.registry = TaskRegistry("sess", self.repo / "ws", self.repo, threading.Lock())

    def tearDown(self) -> None:
        tasks_module.CONFIG = self._orig_config
        roadmap_module.SYSTEMS = self._orig_systems
        judge_module.run_judge = self._orig_run_judge
        pr_module.open_for_task = self._orig_open
        self._tmp.cleanup()

    def set_enabled(self, enabled: bool) -> None:
        tasks_module.CONFIG = dataclasses.replace(
            load_config(self.repo), pull_requests=PullRequestConfig(enabled=enabled)
        )

    def _run(self) -> dict:
        def work():
            _git(self.child, "switch", "-q", "-c", "feature/NDT-7-x")
            (self.child / "svc.ts").write_text("x\n")
            _git(self.child, "add", "-A")
            _git(self.child, "commit", "-q", "-m", "feat: x")

        rels = self.registry._system_repo_dirs("rides")
        with self.registry.git_lock:
            head_before = self.registry._git_head()
            nested_before = self.registry._repo_states(rels)
        with patch_claude(_FakeClaude(self.repo, effect=work)):
            self.registry._run_and_record("t1", "Do it", 1.0, "rides", head_before, nested_before)
        return self.registry.get_task("t1")

    def test_a_passed_task_carries_its_pull_requests(self) -> None:
        task = self._run()
        self.assertEqual(task["pull_requests"][0]["url"], "https://example.test/pr/1")
        (call,) = self.opened
        after = call["after"]["src/rides/backend"]["branches"]
        self.assertIn("feature/NDT-7-x", after)
        self.assertNotIn("feature/NDT-7-x", call["before"]["src/rides/backend"]["branches"])

    def test_a_violation_opens_nothing(self) -> None:
        self.verdict = "violation"
        self.assertNotIn("pull_requests", self._run())
        self.assertEqual(self.opened, [])

    def test_disabled_opens_nothing(self) -> None:
        self.set_enabled(False)
        self.assertNotIn("pull_requests", self._run())
        self.assertEqual(self.opened, [])


class CompletionNoteTest(unittest.TestCase):
    def test_no_pull_requests_no_note(self) -> None:
        self.assertEqual(agent_module._pull_request_completion_note({}), "")

    def test_links_and_failures_are_spelled_out(self) -> None:
        note = agent_module._pull_request_completion_note({"pull_requests": [
            {"repo": "r", "branch": "b", "target": "main", "status": "opened",
             "url": "https://x/pr/1", "reviewers": ["Ana"]},
            {"repo": "r", "branch": "b-rc", "target": "rc", "status": "failed", "error": "HTTP 403"},
        ]})
        self.assertIn("https://x/pr/1", note)
        self.assertIn("Ana", note)
        self.assertIn("FAILED (HTTP 403)", note)


if __name__ == "__main__":
    unittest.main()
