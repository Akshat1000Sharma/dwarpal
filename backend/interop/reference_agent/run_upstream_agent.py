#!/usr/bin/env python3
"""Run the published AP2 reference shopping agent against a running Dwarpal.

One command. It fetches the upstream samples at the pinned commit, installs Dwarpal in place of
the sample merchant, starts the agent and the two upstream credential services, and sends the agent
a shopping instruction over A2A. What comes back is whatever actually happened.

    python interop/reference_agent/run_upstream_agent.py
    python interop/reference_agent/run_upstream_agent.py --prepare-only
    python interop/reference_agent/run_upstream_agent.py --base https://your-tunnel.ngrok-free.dev

Why a substitution rather than a plugin: `shopping_agent_v2` hard-codes the path of the merchant it
spawns, `roles/merchant_agent_mcp/server.py`, and runs it over MCP stdio. So Dwarpal takes that
file's place. The shim is four lines and runs `interop.reference_agent.merchant_mcp_server`, which
needs nothing beyond the standard library and `mcp` and therefore works inside the upstream agent's
own virtual environment. The original file is kept beside it as `server.upstream.py`.

Everything this touches lives under a working directory that is gitignored. Nothing in the Dwarpal
tree is modified.

Prerequisites, all checked before anything is downloaded:

    git, a Google API key in GOOGLE_API_KEY or GEMINI_API_KEY, and uv. uv is installed into this
    interpreter with --install-uv if it is missing.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent

# The commit whose JSON Schemas this repository vendors, so the agent and the schemas agree.
AP2_REPO = "https://github.com/google-agentic-commerce/AP2.git"
AP2_COMMIT = "e1ea56db72a6385bce3e5c1112b3a56ce60acb43"

AGENT_PORT = 8080
CREDENTIALS_PROVIDER_PORT = 8082
PAYMENT_PROCESSOR_PORT = 8083

SHIM = '''"""Dwarpal, standing in for the sample merchant this agent expects to spawn.

Written by interop/reference_agent/run_upstream_agent.py. The file it replaced is beside it as
server.upstream.py.
"""

import os
import runpy
import sys

sys.path.insert(0, os.environ["DWARPAL_BACKEND"])
runpy.run_module("interop.reference_agent.merchant_mcp_server", run_name="__main__")
'''

# The reference agent is not a general shopper: it buys on the user's behalf when an awaited item
# appears, under mandates signed in advance, and its own prompt rejects anything else. So this is
# its workflow rather than an approximation of one.
CONVERSATION = [
    (
        "intent",
        "I want you to buy me a pack of Nilgiri black tea from this merchant as soon as it is "
        "available. It is a limited drop. Can you watch for it and buy it for me?",
    ),
    ("budget", "Yes, please buy it for me. A budget of 500 rupees is fine."),
    ("mandate approved", "mandate_approved"),
    ("check now", "check_product_now"),
]


class Step:
    """Reports what happened rather than what was supposed to happen."""

    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.results.append((name, bool(ok), detail))
        mark = "ok" if ok else "FAIL"
        print(f"  [{mark}] {name}" + (f": {detail}" if detail else ""), flush=True)
        return bool(ok)

    @property
    def failed(self) -> int:
        return sum(1 for _n, ok, _d in self.results if not ok)


def _run(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            env={**os.environ, **(env or {})},
            capture_output=True,
            text=True,
            timeout=1800,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return completed.returncode, (completed.stdout + completed.stderr)[-4000:]


def _uv(step: Step, install: bool) -> list[str] | None:
    found = shutil.which("uv")
    if found:
        step.add("uv is available", True, found)
        return [found]

    probe = subprocess.run(
        [sys.executable, "-m", "uv", "--version"], capture_output=True, text=True
    )
    if probe.returncode == 0:
        step.add("uv is available", True, f"{sys.executable} -m uv")
        return [sys.executable, "-m", "uv"]

    if not install:
        step.add(
            "uv is available",
            False,
            "not found. Re-run with --install-uv, or install it from https://docs.astral.sh/uv/",
        )
        return None

    code, output = _run([sys.executable, "-m", "pip", "install", "uv"], BACKEND_ROOT)
    if code != 0:
        step.add("uv installed", False, output[-300:])
        return None
    step.add("uv installed", True, "pip install uv")
    return [sys.executable, "-m", "uv"]


def _clone(step: Step, work_dir: Path) -> Path | None:
    repo = work_dir / "AP2"
    if (repo / ".git").exists():
        code, output = _run(["git", "rev-parse", "HEAD"], repo)
        if code == 0 and output.strip().startswith(AP2_COMMIT[:12]):
            step.add("upstream samples present at the pinned commit", True, AP2_COMMIT[:12])
            return repo
        shutil.rmtree(repo, ignore_errors=True)

    repo.mkdir(parents=True, exist_ok=True)
    for command in (
        ["git", "init", "--quiet"],
        ["git", "remote", "add", "origin", AP2_REPO],
        ["git", "fetch", "--quiet", "--depth", "1", "origin", AP2_COMMIT],
        ["git", "checkout", "--quiet", "FETCH_HEAD"],
    ):
        code, output = _run(command, repo)
        if code != 0:
            step.add(f"upstream samples fetched ({' '.join(command[:2])})", False, output[-300:])
            return None
    step.add("upstream samples fetched at the pinned commit", True, AP2_COMMIT[:12])
    return repo


def _install_shim(step: Step, repo: Path) -> bool:
    merchant = repo / "code" / "samples" / "python" / "src" / "roles" / "merchant_agent_mcp"
    server = merchant / "server.py"
    if not server.exists():
        step.add("the sample merchant this agent spawns was found", False, str(server))
        return False

    preserved = merchant / "server.upstream.py"
    if not preserved.exists():
        shutil.copy2(server, preserved)
    server.write_text(SHIM, encoding="utf-8")
    step.add(
        "Dwarpal installed in place of the sample merchant",
        True,
        f"{server.relative_to(repo)} (original kept as server.upstream.py)",
    )
    return True


def _sync(step: Step, repo: Path, uv: list[str]) -> bool:
    samples = repo / "code" / "samples" / "python"
    code, output = _run([*uv, "sync", "--quiet"], samples)
    step.add("upstream dependencies installed", code == 0, "" if code == 0 else output[-500:])
    return code == 0


def _wait_for(url: str, timeout: int) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1)
    return False


def _start(name: str, directory: Path, command: list[str], env: dict[str, str], logs: Path):
    handle = (logs / f"{name}.log").open("w", encoding="utf-8")
    return subprocess.Popen(
        command,
        cwd=str(directory),
        env={**os.environ, **env},
        stdout=handle,
        stderr=subprocess.STDOUT,
    )


MERCHANT_TOOLS = (
    "search_inventory",
    "check_product",
    "assemble_cart",
    "create_checkout",
    "complete_checkout",
)


def _turn(agent_base: str, session: str, index: int, text: str) -> str:
    payload = {
        "jsonrpc": "2.0",
        "id": f"dwarpal-interop-{index}",
        "method": "message/stream",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": text}],
                "messageId": f"dwarpal-interop-msg-{index}",
            },
            "metadata": {"sessionId": session},
        },
    }
    request = urllib.request.Request(
        f"{agent_base}/a2a/shopping_agent",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
    )
    collected: list[str] = []
    with urllib.request.urlopen(request, timeout=600) as response:
        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if line:
                collected.append(line)
    return "\n".join(collected)


def _log_size(logs: Path) -> int:
    """Where the adapter's log ends before this run appends to it."""
    try:
        return (logs / "dwarpal-merchant-mcp.log").stat().st_size
    except OSError:
        return 0


def _tools_the_merchant_actually_ran(logs: Path, since: int) -> list[str]:
    """Which tools the merchant served during this run.

    Read from an offset rather than truncating the file, because a previous adapter process may
    still hold it open and Windows will not let it be replaced while it does.
    """
    try:
        with (logs / "dwarpal-merchant-mcp.log").open("rb") as handle:
            handle.seek(since)
            served = handle.read().decode("utf-8", "replace")
    except OSError:
        return []
    return [tool for tool in MERCHANT_TOOLS if f"- {tool} " in served]


def _drive(step: Step, agent_base: str, session: str, logs: Path, since: int) -> str:
    """Walk the agent through its own workflow and collect everything it streamed back."""
    transcript: list[str] = []
    for index, (label, text) in enumerate(CONVERSATION, start=1):
        try:
            events = _turn(agent_base, session, index, text)
        except (urllib.error.URLError, OSError) as exc:
            step.add(f"the agent answered turn {index} ({label})", False, str(exc))
            break
        transcript.append(f"### turn {index}: {label}\n{events}")
        count = events.count("data:")
        step.add(f"the agent answered turn {index} ({label})", True, f"{count} events")

    joined = "\n".join(transcript)
    # Count a tool as reached only when the merchant's own log shows it running. The name
    # appears in the transcript merely from being offered to the model, so matching on that
    # would claim every tool was called on every run.
    reached = _tools_the_merchant_actually_ran(logs, since)
    step.add(
        "the agent called Dwarpal's merchant tools",
        bool(reached),
        ", ".join(reached) or "none reached",
    )
    return joined


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="http://127.0.0.1:8000", help="the Dwarpal origin")
    parser.add_argument(
        "--work-dir",
        default=str(BACKEND_ROOT.parent / ".reference-agent"),
        help="where the upstream samples are fetched to",
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="fetch and install the substitution, then stop without starting anything",
    )
    parser.add_argument("--install-uv", action="store_true", help="pip install uv if it is missing")
    args = parser.parse_args(argv)

    step = Step()
    work_dir = Path(args.work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    print(f"Running the AP2 reference shopping agent against {args.base}\n")

    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY", "")
    step.add(
        "a Google API key is configured",
        bool(api_key),
        "GOOGLE_API_KEY or GEMINI_API_KEY" if api_key else "the ADK agent cannot run without one",
    )
    step.add(
        "Dwarpal is reachable",
        _wait_for(args.base.rstrip("/") + "/health", timeout=5),
        args.base,
    )
    if step.failed:
        print("\nStopping: the prerequisites above are not met.")
        return 1

    uv = _uv(step, args.install_uv)
    if uv is None:
        return 1

    repo = _clone(step, work_dir)
    if repo is None:
        return 1
    if not _install_shim(step, repo):
        return 1
    if not _sync(step, repo, uv):
        return 1

    if args.prepare_only:
        print("\nPrepared. Nothing was started, as --prepare-only was given.")
        return 0

    logs = work_dir / "logs"
    temp_db = work_dir / "temp-db"
    for directory in (logs, temp_db):
        directory.mkdir(parents=True, exist_ok=True)

    roles = repo / "code" / "samples" / "python" / "src" / "roles"
    environment = {
        "GOOGLE_API_KEY": api_key,
        "FLOW": "card",
        "DWARPAL_BACKEND": str(BACKEND_ROOT),
        "DWARPAL_BASE_URL": args.base,
        "TEMP_DB_DIR": str(temp_db),
        "LOGS_DIR": str(logs),
        "AP2_TOKEN_STORE_PATH": str(temp_db / "ap2_token_store.json"),
        "MERCHANT_INVENTORY_PATH": str(temp_db / "merchant_inventory.json"),
        "AGENT_PUBLIC_KEY_PATH": str(temp_db / "agent_signing_key.pub"),
        "MERCHANT_SIGNING_KEY_PATH": str(temp_db / "merchant_signing_key.pem"),
    }

    # The adapter appends to its log, so this run is read from where the previous one ended.
    log_offset = _log_size(logs)

    # uv takes a lock on the project environment, so starting three processes at once makes them
    # queue behind each other's resolution. Warming it first means the timeouts below measure the
    # agent starting rather than uv thinking.
    _run([*uv, "run", "python", "-c", "pass"], roles / "shopping_agent_v2", environment)

    processes: list[subprocess.Popen] = []
    try:
        for name, directory in (
            ("credentials-provider", roles / "credentials_provider_mcp"),
            ("merchant-payment-processor", roles / "merchant_payment_processor_mcp"),
        ):
            if (directory / "trigger_server.py").exists():
                processes.append(
                    _start(
                        name,
                        directory,
                        [*uv, "run", "python", "trigger_server.py"],
                        environment,
                        logs,
                    )
                )
        step.add("upstream credential services started", True, f"{len(processes)} process(es)")

        agent_dir = roles / "shopping_agent_v2"
        processes.append(
            _start("agent", agent_dir, [*uv, "run", "python", "run_server.py"], environment, logs)
        )
        # The agent binds 127.0.0.1 only, and localhost resolves to ::1 first on Windows.
        agent_base = f"http://127.0.0.1:{AGENT_PORT}"
        card = f"{agent_base}/a2a/shopping_agent/.well-known/agent-card.json"
        if not step.add("the reference shopping agent is serving", _wait_for(card, 300), card):
            print(f"\nThe agent did not come up. Its log is at {logs / 'agent.log'}.")
            return 1

        transcript = _drive(step, agent_base, "dwarpal-interop", logs, log_offset)
        (logs / "a2a-transcript.txt").write_text(transcript, encoding="utf-8")
        print(f"\n  the full A2A transcript is at {logs / 'a2a-transcript.txt'}")
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()

    print()
    passed = len(step.results) - step.failed
    print(f"{passed}/{len(step.results)} steps succeeded. Logs are under {logs}.")
    print("What Dwarpal decided is in its own verdict log and evidence browser.")
    return 0 if step.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
