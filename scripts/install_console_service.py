#!/usr/bin/env python3
"""Install or inspect the macOS Dev Mesh Console LaunchAgent."""

from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path


LABEL = "com.glenzli.dev-mesh.observer-console"
DEFAULT_DATABASE = Path("~/.local/state/dev-mesh/observer-20260812.1.sqlite3").expanduser()


def launch_agent_payload(
    *,
    root: Path,
    python: Path,
    database: Path,
    registry: Path,
    port: int,
    collect_interval: float,
) -> dict[str, object]:
    logs = Path("~/Library/Logs").expanduser()
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(python),
            "-m",
            "dev_mesh_console",
            "--db",
            str(database),
            "--registry",
            str(registry),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--collect-interval",
            str(collect_interval),
        ],
        "EnvironmentVariables": {"PYTHONPATH": str(root / "runtime")},
        "WorkingDirectory": str(root),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": 10,
        "Umask": 0o077,
        "StandardOutPath": str(logs / "dev-mesh-observer-console.log"),
        "StandardErrorPath": str(logs / "dev-mesh-observer-console.error.log"),
    }


def _run(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def install(arguments: argparse.Namespace) -> int:
    root = arguments.root.expanduser().resolve()
    python = arguments.python.expanduser().resolve()
    database = arguments.database.expanduser().resolve()
    registry = (arguments.registry or database.with_suffix(database.suffix + ".roots.json")).expanduser().resolve()
    if not (root / "runtime/dev_mesh_console/__main__.py").is_file():
        raise ValueError(f"Dev Mesh Console runtime is missing under {root}")
    if not python.is_file():
        raise ValueError(f"Python executable is missing: {python}")
    database.parent.mkdir(parents=True, exist_ok=True)
    Path("~/Library/Logs").expanduser().mkdir(parents=True, exist_ok=True)
    launch_agents = Path("~/Library/LaunchAgents").expanduser()
    launch_agents.mkdir(parents=True, exist_ok=True)
    target = launch_agents / f"{LABEL}.plist"
    payload = launch_agent_payload(
        root=root,
        python=python,
        database=database,
        registry=registry,
        port=arguments.port,
        collect_interval=arguments.collect_interval,
    )
    encoded = plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass

    domain = f"gui/{os.getuid()}"
    _run("launchctl", "bootout", f"{domain}/{LABEL}", check=False)
    _run("launchctl", "bootstrap", domain, str(target))
    _run("launchctl", "kickstart", "-k", f"{domain}/{LABEL}")
    print(target)
    return 0


def status(_arguments: argparse.Namespace) -> int:
    completed = _run(
        "launchctl",
        "print",
        f"gui/{os.getuid()}/{LABEL}",
        check=False,
    )
    output = completed.stdout if completed.returncode == 0 else completed.stderr
    print(output.rstrip())
    return completed.returncode


def uninstall(_arguments: argparse.Namespace) -> int:
    domain = f"gui/{os.getuid()}"
    _run("launchctl", "bootout", f"{domain}/{LABEL}", check=False)
    target = Path("~/Library/LaunchAgents").expanduser() / f"{LABEL}.plist"
    try:
        target.unlink()
    except FileNotFoundError:
        pass
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subparsers = result.add_subparsers(dest="command", required=True)
    service = subparsers.add_parser("install")
    service.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    service.add_argument("--python", type=Path, default=Path(sys.executable))
    service.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    service.add_argument("--registry", type=Path)
    service.add_argument("--port", type=int, default=8765)
    service.add_argument("--collect-interval", type=float, default=15.0)
    service.set_defaults(handler=install)
    inspect = subparsers.add_parser("status")
    inspect.set_defaults(handler=status)
    remove = subparsers.add_parser("uninstall")
    remove.set_defaults(handler=uninstall)
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        return int(arguments.handler(arguments))
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        print(f"console service error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
