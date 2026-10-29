"""Pull requests for reviewers: the last step of a dev task whose work passed its
system's git workflow rules.

Dev agents commit locally on a correctly named branch and never push (the systems'
gitflow rules say so, and nothing here changes that). Once the compliance judge rules
`pass` on the task (see tasks.TaskRegistry._judge), this module takes each branch the
task created or advanced in one of the system's OWN repositories, pushes it to that
repository's `origin`, and opens a pull request against the branch the system's
`pr_targets` name for it - so the work lands in front of human reviewers without
anyone pushing by hand. Nothing happens unless [pull_requests] enabled = true.

Two hosts, chosen from the repository's origin URL:

- **Bitbucket Cloud.** A pull request created through the REST API does NOT get the
  repository's default reviewers (the web UI adds them client-side), so they are read
  from `effective-default-reviewers` and sent with the create call - minus the PR's
  author, whom Bitbucket refuses as a reviewer.
- **Azure DevOps.** Required reviewers come from branch policies, which the service
  applies to every new pull request on its own; nothing to add here.

A branch that already has an open pull request to the same target is only pushed:
the push is what updates that PR. Every failure (no credential, push rejected, an API
error) is recorded on the task as a failed pull request and never fails the task -
the work itself is done and judged; only its delivery to reviewers is missing.

Tokens never leave this module: they travel in an Authorization header (for git, via
GIT_CONFIG_* environment variables, so they are not on any command line) and every
error is scrubbed before it is stored.
"""
from __future__ import annotations

import os
import re
import subprocess
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from .config import DEFAULT_PR_TARGET, PullRequestConfig, SystemSpec
from .trackers import TrackerError, _basic_auth, _scrub
from .trackers import _request as _tracker_request

# The single network seam for this module; tests replace it wholesale.
_request = _tracker_request

BITBUCKET_API = "https://api.bitbucket.org/2.0"
# Bitbucket's documented git username for authenticating with an Atlassian API token.
BITBUCKET_GIT_USER = "x-bitbucket-api-token-auth"
ADO_API_VERSION = "7.1"
# A pre-push hook may run a repository's whole test suite.
PUSH_TIMEOUT_SECONDS = 600
# Azure DevOps refuses longer descriptions; Bitbucket gets the same bound.
MAX_DESCRIPTION_CHARS = 4000
MAX_LISTED_COMMITS = 30


class PullRequestError(Exception):
    """Opening one pull request failed. Message is scrubbed before it is stored."""


@dataclass(frozen=True)
class Remote:
    kind: str  # "bitbucket" | "ado"
    # Bitbucket: workspace + repo slug. ADO: organization + project + repository.
    owner: str
    repo: str
    project: str = ""


def parse_remote(url: str) -> Remote | None:
    """The host a git remote URL points at, or None for anything but Bitbucket Cloud or
    Azure DevOps. Accepts the HTTPS and SSH spellings of both, with or without userinfo
    and `.git`, and percent-encoded names (ADO projects often have spaces)."""
    url = url.strip()
    # scp-like `git@host:path` or `ssh://git@host/path`; an https URL never matches,
    # since its scheme's `//` stops the userinfo part.
    ssh = re.match(r"^(?:ssh://)?[^@/]+@([^:/]+)[:/](.+)$", url)
    if ssh:
        host, path = ssh.group(1).lower(), ssh.group(2)
    else:
        split = urllib.parse.urlsplit(url)
        host, path = (split.hostname or "").lower(), split.path
    parts = [urllib.parse.unquote(p) for p in path.strip("/").split("/") if p]
    if parts:
        parts[-1] = parts[-1].removesuffix(".git")
    if host == "bitbucket.org" and len(parts) >= 2:
        # Only the first two segments count: a browser URL such as
        # https://bitbucket.org/ws/repo/src/main/ still names the repository.
        return Remote("bitbucket", parts[0], parts[1].removesuffix(".git"))
    if host == "ssh.dev.azure.com" and len(parts) == 4 and parts[0] == "v3":
        return Remote("ado", parts[1], parts[3], project=parts[2])
    if host == "dev.azure.com" and len(parts) == 4 and parts[2] == "_git":
        return Remote("ado", parts[0], parts[3], project=parts[1])
    if host.endswith(".visualstudio.com") and "_git" in parts:
        at = parts.index("_git")
        project_parts = [p for p in parts[:at] if p.lower() != "defaultcollection"]
        if len(project_parts) == 1 and at + 1 < len(parts):
            return Remote("ado", host.split(".")[0], parts[at + 1], project=project_parts[0])
    return None


def changed_branches(before: dict[str, str], after: dict[str, str], spec: SystemSpec) -> list[str]:
    """The local branches a task created or moved, minus any branch the system's pull
    requests target - a commit landing on a target is a rule violation for the judge to
    catch, never something to open a pull request FROM."""
    targets = {DEFAULT_PR_TARGET, *(target for _, target in spec.pr_targets)}
    return sorted(
        branch for branch, sha in after.items()
        if before.get(branch) != sha and branch not in targets
    )


def open_for_task(
    repo_root: Path,
    spec: SystemSpec,
    config: PullRequestConfig,
    nested_before: dict[str, dict],
    nested_after: dict[str, dict],
    *,
    task_id: str,
    description: str,
) -> list[dict]:
    """One record per branch the task moved in each of the system's repositories:
    {repo, branch, target, status: opened|updated|failed, url?, id?, reviewers?, error?}.
    The caller has already established that the task passed the judge."""
    results: list[dict] = []
    for rel in sorted(nested_after):
        before = (nested_before.get(rel) or {}).get("branches") or {}
        after = nested_after[rel].get("branches") or {}
        for branch in changed_branches(before, after, spec):
            target = spec.pr_target(branch)
            record = {"repo": rel, "branch": branch, "target": target}
            try:
                record.update(_open_one(
                    repo_root / rel, branch, target, config,
                    spec=spec, reviewers=spec.reviewers_for(rel),
                    task_id=task_id, description=description,
                ))
            except (PullRequestError, TrackerError) as exc:
                secrets = (config.bitbucket_token, config.ado_token)
                record.update(status="failed", error=_scrub(str(exc), *secrets)[:500])
            results.append(record)
    return results


def _open_one(
    repo: Path, branch: str, target: str, config: PullRequestConfig,
    *, spec: SystemSpec, reviewers: tuple[str, ...], task_id: str, description: str,
) -> dict:
    origin = _git(repo, "remote", "get-url", "origin")
    remote = parse_remote(origin)
    if remote is None:
        raise PullRequestError(
            f"origin ({_scrub(origin)}) is neither Bitbucket Cloud nor Azure DevOps"
        )
    host = _BitbucketHost(config) if remote.kind == "bitbucket" else _AdoHost(config)
    _push(repo, branch, host.git_auth)
    existing = host.find_open(remote, branch, target)
    if existing:
        return {"status": "updated", **existing}
    title, body = _compose(repo, branch, target, spec, task_id, description)
    return {"status": "opened", **host.create(remote, branch, target, title, body, reviewers)}


# ---- git -------------------------------------------------------------------------


def _git(repo: Path, *args: str, env: dict | None = None, timeout: int = 60) -> str:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(repo), capture_output=True, text=True,
            env=env, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise PullRequestError(f"git {args[0]} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise PullRequestError(f"git {args[0]} failed: {(proc.stderr or proc.stdout).strip()[:400]}")
    return proc.stdout.strip()


def _push(repo: Path, branch: str, auth: str) -> None:
    """Pushes `branch` to origin under its own name. Hooks run (a failing pre-push hook
    is a failed pull request, not something to bypass), and git may not prompt: this
    runs on a server thread with nobody at a terminal."""
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "never",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: {auth}",
    }
    ref = f"refs/heads/{branch}"
    _git(repo, "push", "origin", f"{ref}:{ref}", env=env, timeout=PUSH_TIMEOUT_SECONDS)


def _branch_commits(repo: Path, branch: str, target: str) -> list[str]:
    """`<short sha> <subject>` for the branch's commits not on its target, oldest first -
    against the remote-tracking target when this clone has one, else the local one."""
    for base in (f"origin/{target}", target):
        try:
            out = _git(repo, "log", "--reverse", "--format=%h %s", f"{base}..refs/heads/{branch}")
        except PullRequestError:
            continue
        return [line for line in out.splitlines() if line.strip()]
    return []


def _compose(
    repo: Path, branch: str, target: str, spec: SystemSpec, task_id: str, description: str,
) -> tuple[str, str]:
    commits = _branch_commits(repo, branch, target)
    if len(commits) == 1:
        title = commits[0].split(" ", 1)[-1]
    else:
        # feature/NDT-123-short-description -> "NDT-123 short description"
        name = branch.rsplit("/", 1)[-1]
        ticket = re.match(r"^([A-Z][A-Z0-9]+-\d+)-?(.*)$", name)
        title = f"{ticket.group(1)} {ticket.group(2).replace('-', ' ')}".strip() if ticket else name
    listed = "\n".join(f"- {c}" for c in commits[-MAX_LISTED_COMMITS:]) or "- (none listed)"
    footer = (
        # The blank line after the heading is what makes Bitbucket render a list.
        f"\n\n**Commits**\n\n{listed}\n\n"
        f"Opened by PM Studio for dev task {task_id} after it passed the {spec.label} "
        f"git workflow rules ({spec.gitflow})."
    )
    room = MAX_DESCRIPTION_CHARS - len(footer)
    body = description if len(description) <= room else description[: room - 1] + "…"
    return title[:255], body + footer


# ---- hosts -----------------------------------------------------------------------


class _BitbucketHost:
    def __init__(self, config: PullRequestConfig) -> None:
        if not (config.bitbucket_email and config.bitbucket_token):
            raise PullRequestError(
                "no Bitbucket credential - set [pull_requests.bitbucket] email_env and "
                "token_env to environment variables that hold them"
            )
        self.headers = {
            "Authorization": _basic_auth(config.bitbucket_email, config.bitbucket_token),
            "Accept": "application/json",
        }
        self.git_auth = _basic_auth(BITBUCKET_GIT_USER, config.bitbucket_token)

    def _repo_url(self, remote: Remote) -> str:
        return (
            f"{BITBUCKET_API}/repositories/{urllib.parse.quote(remote.owner)}"
            f"/{urllib.parse.quote(remote.repo)}"
        )

    @staticmethod
    def _summary(pr: dict) -> dict:
        return {
            "id": pr.get("id"),
            "url": ((pr.get("links") or {}).get("html") or {}).get("href", ""),
            "reviewers": [r.get("display_name", "") for r in pr.get("reviewers") or []],
        }

    def find_open(self, remote: Remote, branch: str, target: str) -> dict | None:
        query = (
            f'source.branch.name="{branch}" AND destination.branch.name="{target}" '
            'AND state="OPEN"'
        )
        url = f"{self._repo_url(remote)}/pullrequests?{urllib.parse.urlencode({'q': query})}"
        values = _request(url, headers=self.headers).get("values") or []
        return self._summary(values[0]) if values else None

    def _reviewers(self, remote: Remote, extra: tuple[str, ...]) -> list[dict]:
        """Every effective default reviewer (repository and project level), followed
        across pages, then the system's own `reviewers` - minus the token's own user,
        the PR's author, whom Bitbucket refuses as a reviewer."""
        me = _request(f"{BITBUCKET_API}/user", headers=self.headers)
        mine = {me.get("uuid", ""), me.get("account_id", "")} - {""}
        ids: list[str] = []
        url = f"{self._repo_url(remote)}/effective-default-reviewers?pagelen=100"
        while url:
            page = _request(url, headers=self.headers)
            ids += [(entry.get("user") or {}).get("uuid", "") for entry in page.get("values") or []]
            url = page.get("next", "")
        ids += extra
        unique = [i for n, i in enumerate(ids) if i and i not in mine and i not in ids[:n]]
        # A `{...}` id is a uuid; anything else is an Atlassian account id.
        return [{"uuid": i} if i.startswith("{") else {"account_id": i} for i in unique]

    def create(
        self, remote: Remote, branch: str, target: str, title: str, body: str,
        extra_reviewers: tuple[str, ...] = (),
    ) -> dict:
        pr = _request(
            f"{self._repo_url(remote)}/pullrequests",
            headers=self.headers,
            method="POST",
            body={
                "title": title,
                "description": body,
                "source": {"branch": {"name": branch}},
                "destination": {"branch": {"name": target}},
                "reviewers": self._reviewers(remote, extra_reviewers),
            },
        )
        return self._summary(pr)


class _AdoHost:
    def __init__(self, config: PullRequestConfig) -> None:
        if not config.ado_token:
            raise PullRequestError(
                "no Azure DevOps credential - set [pull_requests.ado] token_env to an "
                "environment variable holding a PAT with Code (read & write)"
            )
        self.headers = {"Authorization": _basic_auth("", config.ado_token), "Accept": "application/json"}
        self.git_auth = self.headers["Authorization"]

    @staticmethod
    def _repo_path(remote: Remote) -> str:
        return "/".join(urllib.parse.quote(p) for p in (remote.owner, remote.project))

    def _api(self, remote: Remote, params: dict | None = None) -> str:
        query = urllib.parse.urlencode({**(params or {}), "api-version": ADO_API_VERSION})
        return (
            f"https://dev.azure.com/{self._repo_path(remote)}/_apis/git/repositories/"
            f"{urllib.parse.quote(remote.repo)}/pullrequests?{query}"
        )

    def _summary(self, remote: Remote, pr: dict) -> dict:
        pr_id = pr.get("pullRequestId")
        return {
            "id": pr_id,
            "url": f"https://dev.azure.com/{self._repo_path(remote)}/_git/"
                   f"{urllib.parse.quote(remote.repo)}/pullrequest/{pr_id}",
            "reviewers": [r.get("displayName", "") for r in pr.get("reviewers") or []],
        }

    def find_open(self, remote: Remote, branch: str, target: str) -> dict | None:
        url = self._api(remote, {
            "searchCriteria.sourceRefName": f"refs/heads/{branch}",
            "searchCriteria.targetRefName": f"refs/heads/{target}",
            "searchCriteria.status": "active",
        })
        values = _request(url, headers=self.headers).get("value") or []
        return self._summary(remote, values[0]) if values else None

    def create(
        self, remote: Remote, branch: str, target: str, title: str, body: str,
        extra_reviewers: tuple[str, ...] = (),
    ) -> dict:
        # Policy reviewers are added by the service; only the system's own go here.
        pr = _request(
            self._api(remote),
            headers=self.headers,
            method="POST",
            body={
                "sourceRefName": f"refs/heads/{branch}",
                "targetRefName": f"refs/heads/{target}",
                "title": title,
                "description": body,
                "reviewers": [{"id": r} for r in extra_reviewers],
            },
        )
        return self._summary(remote, pr)
