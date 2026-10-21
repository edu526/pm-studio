"""Tests for the single-writer guard over one workspace.

The bug this exists to prevent: two servers against the same repo. Each store loads its
JSON once and writes whole files back from memory, so a second instance silently
overwrites the first's edits and re-imports tracker tickets the first already linked.
"""

import dataclasses
import json
import os
import tempfile
import unittest
from pathlib import Path

from pm_studio import config as config_module
from pm_studio import workspace_lock as lock_module
from pm_studio.workspace_lock import WorkspaceBusy, WorkspaceLock, holder, warn_if_held


class WorkspaceLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config_module.CONFIG
        config_module.CONFIG = dataclasses.replace(
            config_module.CONFIG,
            repo_root=Path(self._tmp.name),
            workspace_root="ws",
            host="127.0.0.1",
            port=8000,
        )
        lock_module.CONFIG = config_module.CONFIG
        config_module.CONFIG.workspace_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        config_module.CONFIG = self._orig
        lock_module.CONFIG = self._orig
        self._tmp.cleanup()

    def _write_lock(self, **fields: object) -> Path:
        path = lock_module.lock_path()
        payload = {"pid": os.getpid(), "host": "127.0.0.1", "port": 8000, "token": "x"}
        payload.update(fields)
        path.write_text(json.dumps(payload))
        return path

    def test_acquire_then_release_leaves_no_file(self) -> None:
        lock = WorkspaceLock().acquire()
        self.assertTrue(lock_module.lock_path().exists())
        self.assertEqual(holder().pid, os.getpid())
        lock.release()
        self.assertFalse(lock_module.lock_path().exists())
        self.assertIsNone(holder())

    def test_a_second_instance_is_refused_while_the_first_lives(self) -> None:
        first = WorkspaceLock().acquire()
        try:
            with self.assertRaises(WorkspaceBusy) as caught:
                WorkspaceLock().acquire()
        finally:
            first.release()
        # The message has to name the process to stop - "already running" with no pid
        # sends somebody hunting through ps.
        self.assertIn(str(os.getpid()), str(caught.exception))
        self.assertEqual(caught.exception.holder.pid, os.getpid())

    def test_a_lock_from_a_dead_process_is_taken_over(self) -> None:
        """A crash or kill -9 must not need manual cleanup before the next start."""
        self._write_lock(pid=_dead_pid())
        lock = WorkspaceLock().acquire()
        self.addCleanup(lock.release)
        self.assertEqual(holder().pid, os.getpid())

    def test_an_unreadable_lock_is_not_a_holder(self) -> None:
        lock_module.lock_path().write_text("{ this is not json")
        self.assertIsNone(holder())
        lock = WorkspaceLock().acquire()
        self.addCleanup(lock.release)
        self.assertEqual(holder().pid, os.getpid())

    def test_release_never_deletes_a_successors_lock(self) -> None:
        """Two starts around one crash: the loser of a race must not unlink the lock the
        winner is holding."""
        first = WorkspaceLock().acquire()
        # Somebody else takes over the file (same pid here, different token).
        self._write_lock(token="somebody-else")
        first.release()
        self.assertTrue(lock_module.lock_path().exists())
        self.assertEqual(json.loads(lock_module.lock_path().read_text())["token"], "somebody-else")

    def test_warn_if_held_is_quiet_for_the_holder_itself(self) -> None:
        lock = WorkspaceLock().acquire()
        self.addCleanup(lock.release)
        self.assertIsNone(warn_if_held())

    def test_warn_if_held_names_a_live_other_process(self) -> None:
        """What a script editing the workspace by hand gets: not a block, but the pid to
        stop first."""
        self._write_lock(pid=_other_live_pid())
        held = warn_if_held()
        self.assertIsNotNone(held)
        self.assertIn("127.0.0.1:8000", held.describe())


def _dead_pid() -> int:
    """A pid nothing can be using: fork a child and reap it."""
    pid = os.fork()
    if pid == 0:  # pragma: no cover - the child never returns to the test
        os._exit(0)
    os.waitpid(pid, 0)
    return pid


def _other_live_pid() -> int:
    """pid 1 is always alive and is never this process."""
    return 1


if __name__ == "__main__":
    unittest.main()
