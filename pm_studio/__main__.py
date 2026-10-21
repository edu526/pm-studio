"""Entry point: `python -m pm_studio` (or the `pm-studio` script) from the target
repo's root.

    python -m pm_studio                 run the PM Studio server for the repo at cwd
    python -m pm_studio --port 8005      ... on port 8005 (overrides [server] port)
    python -m pm_studio --host 0.0.0.0   ... bound to a specific host interface
    python -m pm_studio init            scaffold pm_studio_local/ + seed docs into the repo
    python -m pm_studio version          print the installed package version

The server run path accepts `--host`/`--port` flags that override the corresponding
`[server]` values in `pm_studio_local/config.toml` for this process only, without
editing the file. Precedence for the port is: `--port` flag > `[server] port` in
config.toml > the built-in default (8000). An invalid or missing flag value is a hard
error (clear message on stderr, non-zero exit) - there is no silent fallback.
"""

import argparse
import dataclasses
import sys
import threading
import time
import webbrowser

from . import __version__
from . import config


def _open_browser_when_ready(server) -> None:
    """Opens the browser once uvicorn reports the app started (lifespan done, socket
    bound) - not after a fixed delay. Loading the server takes several seconds, and a
    browser opened before that just sits on a blank page. Watching uvicorn's own flag,
    rather than probing the port, also never opens an older server still holding it."""
    while not server.started:
        if server.should_exit:
            return  # it failed to start (port taken, bad config): nothing to open
        time.sleep(0.2)
    # Read the live module attribute so a `--port`/`--host` override is reflected.
    url = config.CONFIG.base_url
    print(f"[pm_studio] ready at {url}")
    webbrowser.open(url)


def _serve() -> None:
    import uvicorn

    from .workspace_lock import WorkspaceBusy, WorkspaceLock

    cfg = config.CONFIG
    print(f"PM Studio v{__version__} - project '{cfg.project_name}' at {cfg.repo_root} (starting...)")
    # Before the server module is imported (and with it the stores that would start
    # reading the workspace): one writer per workspace, or this process does not run.
    # See workspace_lock - two servers against one workspace silently overwrite each
    # other's edits and double-import tracker tickets.
    try:
        lock = WorkspaceLock().acquire()
    except WorkspaceBusy as busy:
        print(f"\n{busy}\n", file=sys.stderr)
        raise SystemExit(1)
    # timeout_graceful_shutdown: without it, Ctrl+C waits on every open browser tab's
    # websockets and every in-flight request indefinitely ("Shutting down" forever).
    server = uvicorn.Server(uvicorn.Config(
        "pm_studio.server:app", host=cfg.host, port=cfg.port, reload=False,
        timeout_graceful_shutdown=5,
    ))
    threading.Thread(target=_open_browser_when_ready, args=(server,), daemon=True).start()
    try:
        server.run()
    except KeyboardInterrupt:
        # Ctrl+C after shutdown already completed: uvicorn.run() swallows this too, and a
        # traceback here would read as a crash when the server stopped cleanly.
        pass
    finally:
        # Released however we leave - clean stop, Ctrl+C or crash. A lock left behind by
        # a kill -9 is taken over by the next start rather than needing cleanup.
        lock.release()


def _apply_overrides(host: str | None, port: int | None) -> None:
    """Rebuild the frozen config with any CLI overrides and reassign the module-level
    `config.CONFIG`. This must run BEFORE `pm_studio.server` is imported, since server
    binds `from .config import CONFIG` at import time (`_serve` triggers that import)."""
    updates: dict[str, object] = {}
    if host is not None:
        updates["host"] = host
    if port is not None:
        updates["port"] = port
    if updates:
        config.CONFIG = dataclasses.replace(config.CONFIG, **updates)


def _run(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="pm-studio",
        description="Run the PM Studio server for the repo at the current directory.",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="host interface to bind (overrides [server] host in config.toml)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="port to bind (overrides [server] port in config.toml)",
    )
    args = parser.parse_args(argv)
    _apply_overrides(args.host, args.port)
    _serve()


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else None
    if command in ("version", "--version"):
        print(f"pm-studio {__version__}")
    elif command == "init":
        from .scaffold import run_init

        run_init(config.CONFIG.repo_root)
    else:
        # No subcommand (or leading flags): the plain server run path, which parses
        # `--host`/`--port`. argparse reports bad/missing values on stderr and exits
        # non-zero; an unknown positional/flag is likewise a non-zero error.
        _run(sys.argv[1:])


if __name__ == "__main__":
    main()
