"""Run the scenario suite against a live merchant.

    python scenarios/run_suite.py                              standard, against localhost
    python scenarios/run_suite.py --profile smoke              a fast configuration check
    python scenarios/run_suite.py --profile demo               fill the dashboard with data
    python scenarios/run_suite.py --profile full               every case, at the quoted size
    python scenarios/run_suite.py --profile soak --minutes 20  the long one
    python scenarios/run_suite.py --suite s03 s06              just those suites

Exit status is 0 only when every case passed. Failures are printed and written to the report, not
summarised away.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from scenarios import report as reporting  # noqa: E402
from scenarios.harness import (  # noqa: E402
    DEFAULT_BASE,
    Client,
    Context,
    Scale,
    Suite,
    SuiteError,
    merchant_audience,
)
from scenarios.suites import ALL, SMOKE  # noqa: E402

PROFILES = ("smoke", "standard", "demo", "full", "soak")


def _environment(client: Client) -> dict[str, Any]:
    """What the merchant says about itself once the suite has finished with it."""
    out: dict[str, Any] = {}
    try:
        status, overview = client.get("/merchant/overview")
        if status == 200:
            verdicts = overview.get("verdicts", {})
            out.update(
                {
                    "verdicts_last_24h": verdicts.get("total"),
                    "approved": verdicts.get("allow"),
                    "refused": verdicts.get("deny"),
                    "escalated": verdicts.get("escalate"),
                    "challenged": verdicts.get("challenge"),
                    "captured": (overview.get("captured") or {}).get("display"),
                    "refunded": (overview.get("refunded") or {}).get("display"),
                    "agents_seen": overview.get("active_agents"),
                    "open_mandates": overview.get("open_mandates"),
                    "evidence_packets": overview.get("evidence_packets"),
                    "open_exceptions": overview.get("open_exceptions"),
                }
            )
        status, evidence = client.get("/merchant/evidence?limit=1")
        if status == 200:
            out["evidence_chain_valid"] = (evidence.get("chain") or {}).get("valid")
    except SuiteError:
        out["note"] = "the merchant became unreachable before the summary could be read"
    return out


def _restock(client: Client, *, announce: bool = False) -> bool:
    """Put the shelves back to their seeded levels. Stock only; nothing else is touched."""
    status, body = client.post("/merchant/catalog/restock", {})
    if status != 200:
        print(f"  warning: could not restock the catalog (HTTP {status}).")
        return False
    if announce:
        print(f"  restocked {body.get('restocked')} catalog items\n")
    return True


def run(
    base: str,
    *,
    profile: str,
    minutes: float,
    agents: int,
    only: list[str] | None,
    out_dir: Path,
    allow_live_whatsapp: bool = False,
) -> int:
    client = Client(base)

    status, health = client.get("/health")
    if status != 200:
        print(f"  the merchant is not reachable at {base} (HTTP {status})")
        print("  start it with: uvicorn main:app --port 8000")
        return 1

    # The suite drives hundreds of purchases and refusals. Against a merchant configured with real
    # Meta credentials, every one of them is a real WhatsApp message to a real person's phone.
    if health.get("whatsapp") == "live" and not allow_live_whatsapp:
        print(f"  refusing to run: {base} would send real WhatsApp messages.")
        print(f"  it reports environment={health.get('environment')}, whatsapp=live.")
        print("  start the merchant for testing instead:")
        print("    APP_ENV=testing uvicorn main:app --port 8000")
        print("  or pass --allow-live-whatsapp if you genuinely want the messages sent.")
        return 2

    # Put the shelves back before measuring anything. Several suites depend on a specific SKU being
    # buyable, and a run against a shop a previous run emptied fails everywhere at once with
    # sold-out errors that look like defects and are not.
    if not _restock(client, announce=True):
        print("  sold-out refusals in the report may be left over from an earlier run.")

    audience = merchant_audience(client)
    scale = Scale.for_profile(profile, minutes=minutes, agents=agents)
    ctx = Context(client=client, audience=audience, scale=scale, base=base)

    selected = ALL
    if only:
        wanted = set(only)
        selected = [fn for fn in ALL if fn.__module__.split(".")[-1][:3] in wanted]
    elif profile == "smoke":
        selected = [fn for fn in ALL if fn.__module__.split(".")[-1][:3] in SMOKE]

    print(f"Dwarpal scenario suite, profile {profile}, against {base}")
    print(f"audience {audience}, {len(selected)} suites\n")

    started = time.perf_counter()
    suites: list[Suite] = []
    for factory_fn in selected:
        name = factory_fn.__module__.split(".")[-1]
        # Restock between suites as well as before the first. A hundred purchases empty a real
        # shelf, and a suite that runs last should not be judged against what the ones before it
        # bought. Each suite then starts from the seeded catalog whatever order they run in.
        _restock(client)
        print(f"  running {name} ...", flush=True)
        try:
            suites.append(factory_fn(ctx))
        except SuiteError as exc:
            broken = Suite(name[:3], name, "the suite could not run")
            broken.skipped = str(exc)
            suites.append(broken)
    wall_ms = int((time.perf_counter() - started) * 1000)
    print()

    document = reporting.summarise(
        suites,
        profile=profile,
        base=base,
        wall_ms=wall_ms,
        environment=_environment(client),
    )
    paths = reporting.write(document, out_dir)
    reporting.print_console(document)
    print(f"  written to {paths['json']} and {paths['markdown']}")
    return 0 if document["totals"]["failed"] == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Drive the Dwarpal scenario suite.")
    parser.add_argument("--base", default=DEFAULT_BASE, help="the merchant's origin")
    parser.add_argument("--profile", default="standard", choices=PROFILES)
    parser.add_argument(
        "--minutes", type=float, default=0.0, help="override the soak duration, in minutes"
    )
    parser.add_argument(
        "--agents", type=int, default=0, help="override how many agents transact concurrently"
    )
    parser.add_argument("--suite", nargs="*", default=None, help="run only these suites, by id")
    parser.add_argument("--out", default="./reports", help="where the report is written")
    parser.add_argument(
        "--allow-live-whatsapp",
        action="store_true",
        help="permit a run against a merchant that sends real WhatsApp messages",
    )
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = (BACKEND_ROOT / out_dir).resolve()

    return run(
        args.base,
        profile=args.profile,
        minutes=args.minutes,
        agents=args.agents,
        only=args.suite,
        out_dir=out_dir,
        allow_live_whatsapp=args.allow_live_whatsapp,
    )


if __name__ == "__main__":
    sys.exit(main())
