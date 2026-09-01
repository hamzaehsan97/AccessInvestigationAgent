"""Command-line interface for the Access Investigation Agent."""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path
from typing import Optional

from agent.core.config import get_settings
from agent.core.db import get_db
from agent.playbooks import (
    access_explain_llm,
    offboarding_leakage_llm,
    offline_access_explain_report,
    offline_offboarding_report,
)
from agent.core.run_store import FileRunStore
from agent.llm.runtime import Agent
from agent.core.schemas import Investigation


def _bounded_limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("limit must be an integer") from exc
    if not 1 <= limit <= 100:
        raise argparse.ArgumentTypeError("limit must be from 1 to 100")
    return limit


def _print_report(inv: Investigation, verbose: bool = False) -> None:
    print(inv.report_md)
    print()
    print(f"— investigation_id: {inv.id}")
    print(f"— status: {inv.status}  verified: {inv.verified}")
    print(f"— snapshot: {inv.snapshot_time}  dataset: {inv.dataset_version}")
    if inv.unresolved_evidence:
        print(f"— unresolved IDs: {[e.key() for e in inv.unresolved_evidence]}")
    if inv.unobserved_evidence:
        print(f"— unobserved IDs: {[e.key() for e in inv.unobserved_evidence]}")
    if verbose:
        print("— trace:")
        for step in inv.trace:
            print("   ", json.dumps(step, default=str)[:220])


def _cmd_investigate(args: argparse.Namespace) -> int:
    settings = get_settings()
    db = get_db(settings.agent_db_path)
    store = FileRunStore(settings.run_store_dir)
    requester = getpass.getuser()

    if args.offline:
        if args.playbook == "offboarding_leakage" or (not args.playbook and not args.question):
            inv = offline_offboarding_report(limit=args.limit or 5, db=db)
        elif args.playbook == "access_explain":
            if not args.person:
                print("--person is required for the access_explain playbook", file=sys.stderr)
                return 2
            inv = offline_access_explain_report(
                person_query=args.person, resource_hint=args.resource, db=db
            )
        else:
            print(
                "--offline requires --playbook offboarding_leakage or --playbook access_explain",
                file=sys.stderr,
            )
            return 2
        store.save(inv)
        _print_report(inv, verbose=args.verbose)
        return 0

    agent = Agent(db=db, settings=settings, run_store=store)
    if args.playbook == "offboarding_leakage":
        inv = offboarding_leakage_llm(agent, limit=args.limit or 5)
    elif args.playbook == "access_explain":
        if not args.person:
            print("--person is required for the access_explain playbook", file=sys.stderr)
            return 2
        inv = access_explain_llm(agent, args.person, args.resource)
    else:
        if not args.question:
            print("Provide a question, or use --playbook.", file=sys.stderr)
            return 2
        inv = agent.investigate(question=args.question, requester=requester)
    _print_report(inv, verbose=args.verbose)
    return 0 if inv.status == "succeeded" else 1


def _cmd_runs_list(args: argparse.Namespace) -> int:
    settings = get_settings()
    store = FileRunStore(settings.run_store_dir)
    for rid in store.list_ids():
        inv = store.load(rid)
        if not inv:
            continue
        print(f"{rid}  {inv.status:12s}  {inv.snapshot_time}  {inv.question[:80]!r}")
    return 0


def _cmd_runs_show(args: argparse.Namespace) -> int:
    settings = get_settings()
    store = FileRunStore(settings.run_store_dir)
    inv = store.load(args.id)
    if not inv:
        print(f"no run with id {args.id}", file=sys.stderr)
        return 1
    if args.format == "md":
        print(inv.report_md)
    else:
        print(json.dumps(inv.model_dump(), indent=2, default=str))
    return 0


def _cmd_scanners(args: argparse.Namespace) -> int:
    from agent.tools.scanners import available_scanners

    for s in available_scanners():
        print(s)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="agent", description="Access Investigation Agent")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_inv = sub.add_parser("investigate", help="Run an investigation")
    p_inv.add_argument("question", nargs="?", help="Free-form question")
    p_inv.add_argument(
        "--playbook",
        choices=["offboarding_leakage", "access_explain"],
        help="Use a deterministic playbook",
    )
    p_inv.add_argument("--person", help="Person name / email / id (for access_explain)")
    p_inv.add_argument("--resource", help="Application or repo hint (for access_explain)")
    p_inv.add_argument(
        "--limit", type=_bounded_limit, help="Row limit for playbooks (1-100)", default=None
    )
    p_inv.add_argument(
        "--offline",
        action="store_true",
        help="Skip LLM calls; use deterministic playbooks only",
    )
    p_inv.add_argument("--verbose", action="store_true", help="Print full trace")
    p_inv.set_defaults(func=_cmd_investigate)

    p_runs = sub.add_parser("runs", help="Inspect stored investigations")
    p_runs_sub = p_runs.add_subparsers(dest="runs_cmd", required=True)
    p_runs_list = p_runs_sub.add_parser("list")
    p_runs_list.set_defaults(func=_cmd_runs_list)
    p_runs_show = p_runs_sub.add_parser("show")
    p_runs_show.add_argument("id")
    p_runs_show.add_argument("--format", choices=["md", "json"], default="md")
    p_runs_show.set_defaults(func=_cmd_runs_show)

    p_sc = sub.add_parser("scanners", help="List available scanners")
    p_sc.set_defaults(func=_cmd_scanners)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
