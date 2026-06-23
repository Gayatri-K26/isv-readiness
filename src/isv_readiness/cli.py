from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from isv_readiness.scan.report import load_report, render_report
from isv_readiness.scan.scanner import ScanOptions, scan_provider


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        parser.print_help()
        return 2
    return args.handler(args)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gapctl", description="ISV readiness gap scanner")
    subparsers = parser.add_subparsers(dest="command")

    scan_parser = subparsers.add_parser("scan", help="Build a deterministic static gaps.json report")
    scan_parser.add_argument("-p", "--provider-repo", type=Path, required=True)
    scan_parser.add_argument("--domains", required=True, help="Comma-separated domains, for example vm,network")
    scan_parser.add_argument("--validation-root", type=Path, default=None)
    scan_parser.add_argument("--out", type=Path, default=Path("gaps.json"))
    scan_parser.add_argument("--run", action="store_true", help="Reserved for v0.2 dynamic scans")
    scan_parser.add_argument("--lab", default=None, help="Reserved for v0.2 dynamic scans")
    scan_parser.set_defaults(handler=_scan)

    report_parser = subparsers.add_parser("report", help="Render a gaps.json report")
    report_parser.add_argument("--in", dest="input_path", type=Path, required=True)
    report_parser.add_argument("--format", choices=["scorecard", "tree", "md"], default="scorecard")
    report_parser.set_defaults(handler=_report)

    fix_parser = subparsers.add_parser("fix", help="Reserved for v0.3 agent fixes")
    fix_parser.set_defaults(handler=_reserved("gapctl fix ships in v0.3."))

    loop_parser = subparsers.add_parser("loop", help="Reserved for v0.4 until-green loops")
    loop_parser.set_defaults(handler=_reserved("gapctl loop ships in v0.4."))

    onboard_parser = subparsers.add_parser("onboard", help="Reserved for access-level readiness checks")
    onboard_parser.add_argument("--check", action="store_true")
    onboard_parser.set_defaults(handler=_reserved("gapctl onboard --check ships after the v0.1 static scanner."))
    return parser


def _scan(args: argparse.Namespace) -> int:
    if args.run:
        print("Dynamic --run scanning is reserved for v0.2; run without --run for the v0.1 static scanner.", file=sys.stderr)
        return 2
    domains = [domain.strip() for domain in args.domains.split(",") if domain.strip()]
    if not domains:
        print("--domains must include at least one domain", file=sys.stderr)
        return 2
    report = scan_provider(
        ScanOptions(
            provider_repo=args.provider_repo,
            domains=domains,
            validation_root=args.validation_root,
        )
    )
    args.out.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {args.out}")
    return 0


def _report(args: argparse.Namespace) -> int:
    report = load_report(args.input_path)
    print(render_report(report, args.format))
    return 0


def _reserved(message: str):
    def handler(_args: argparse.Namespace) -> int:
        print(message, file=sys.stderr)
        return 2

    return handler
