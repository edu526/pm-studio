"""One writer per workspace.

Every store in this package keeps its state in memory and writes whole files back
(roadmap.RoadmapStore._save_product, portfolio.PortfolioStore._save) on the premise
stated in their docstrings: ONE always-running process owns the workspace. Nothing
enforced that premise. A second server started against the same repo - trivially done
with `python -m pm_studio --port 8005`, or by forgetting one is already up - loads its
own copy, never re-reads the files, and overwrites the other's work on its next save.
Both instances also run their own tracker passes, so the same ticket imports twice,
each instance blind to the other's 1:1 link.

So: a lock file next to the state it protects. The server takes it at startup and
refuses to run if another live process holds it, naming that process and its port so
the answer is "go to the window you already have open" rather than a silent second
writer. A lock left behind by a dead process is taken over, not obeyed - a crash must
not need manual cleanup.

Scripts that edit the workspace directly (the stop-edit-restart procedure) are not
blocked, since they run exactly when the server is down. They get `warn_if_held()`
instead, which the stores call: a loud line when a live server holds the lock and is
about to have its in-memory copy clobber whatever the script writes.
"""

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .config import CONFIG

LOCK_NAME = "server.lock"


def lock_path() -> Path:
    return CONFIG.workspace_dir / LOCK_NAME


@dataclass(frozen=True)
class Holder:
    """Whoever wrote the lock file. `alive` is the only question that matters."""

    pid: int
    port: int | None
    host: str | None
    started_at: float | None

    @property
    def alive(self) -> bool:
        return _pid_alive(self.pid)

    def describe(self) -> str:
        where = f"http://{self.host}:{self.port}" if self.host and self.port else "unknown address"
        age = ""
        if self.started_at:
            minutes = max(0, int((time.time() - self.started_at) // 60))
            age = f", running {minutes} min" if minutes else ", just started"
        return f"pid {self.pid} at {where}{age}"


class WorkspaceBusy(RuntimeError):
    """Raised when a live process already owns this workspace."""

    def __init__(self, holder: Holder) -> None:
        self.holder = holder
        super().__init__(
            f"Another PM Studio is already running for this workspace ({holder.describe()}).\n"
            f"Two servers against one workspace overwrite each other's edits and import "
            f"tracker tickets twice, so this one will not start.\n"
            f"Use that server, or stop it first (kill {holder.pid}), then start again."
        )


def _pid_alive(pid: int) -> bool:
    """Signal 0 asks the kernel whether the pid exists without touching it. A pid the
    OS has since recycled onto an unrelated process reads as alive, which errs toward
    refusing to start - the safe direction for a lock whose whole job is preventing a
    second writer."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, owned by somebody else
    return True


def _read(path: Path) -> Holder | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    try:
        return Holder(
            pid=int(data["pid"]),
            port=data.get("port"),
            host=data.get("host"),
            started_at=data.get("started_at"),
        )
    except (KeyError, TypeError, ValueError):
        return None


def holder() -> Holder | None:
    """The live holder, or None. A stale file (dead pid, or unreadable) is not a holder."""
    path = lock_path()
    if not path.exists():
        return None
    held = _read(path)
    if held is None or not held.alive:
        return None
    return held


class WorkspaceLock:
    """Held for the life of the server process; released on shutdown."""

    def __init__(self) -> None:
        self._path = lock_path()
        self._token: str | None = None

    def acquire(self) -> "WorkspaceLock":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        existing = holder()
        if existing is not None:
            raise WorkspaceBusy(existing)
        stale = self._path.exists()
        token = os.urandom(8).hex()
        payload = {
            "pid": os.getpid(),
            "host": CONFIG.host,
            "port": CONFIG.port,
            "started_at": time.time(),
            "token": token,
        }
        # Last write wins between two processes racing here, which is the narrowest the
        # window gets without a cross-platform atomic primitive: both would have had to
        # pass the live-holder check microseconds apart. The token below is what keeps
        # the loser from later deleting the winner's lock.
        self._path.write_text(json.dumps(payload, indent=2))
        self._token = token
        if stale:
            print(f"[pm_studio] took over a stale {LOCK_NAME} (its process is gone)")
        return self

    def release(self) -> None:
        """Deletes the file only if it is still OURS - never a successor's."""
        if self._token is None:
            return
        try:
            data = json.loads(self._path.read_text())
        except (OSError, ValueError):
            self._token = None
            return
        if data.get("token") == self._token:
            try:
                self._path.unlink()
            except OSError:
                pass
        self._token = None

    def __enter__(self) -> "WorkspaceLock":
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()


def warn_if_held() -> Holder | None:
    """For anything editing the workspace outside the server: one loud line if a live
    server owns it. Not a block - a script that knows what it is doing (and a test
    harness) must still be able to run - but the warning names the pid to stop first."""
    held = holder()
    if held is None or held.pid == os.getpid():
        return None
    print(
        f"[pm_studio] WARNING: a PM Studio server is running ({held.describe()}). "
        f"It holds the workspace in memory and will overwrite direct edits to these "
        f"files on its next save. Stop it first (kill {held.pid})."
    )
    return held
