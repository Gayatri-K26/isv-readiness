from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from isv_readiness.scan.k8s_dynamic import K8sDynamicArtifacts, scan_k8s_artifacts
from isv_readiness.scan.k8s_scope import load_k8s_scope
from isv_readiness.scan.models import GapReport
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

    scan_parser = subparsers.add_parser("scan", help="Build a deterministic static/dynamic gaps.json report")
    scan_parser.add_argument("-p", "--provider-repo", type=Path, required=True)
    scan_parser.add_argument("--domains", required=True, help="Comma-separated domains, for example vm,network")
    scan_parser.add_argument("--validation-root", type=Path, default=None)
    scan_parser.add_argument("--out", type=Path, default=Path("gaps.json"))
    scan_parser.add_argument("--junit", type=Path, default=None, help="K8s dynamic scan: JUnit XML from isvctl test run")
    scan_parser.add_argument("--log", type=Path, default=None, help="K8s dynamic scan: captured isvctl log")
    scan_parser.add_argument("--setup-json", type=Path, default=None, help="K8s dynamic scan: captured setup inventory JSON")
    scan_parser.add_argument("--scope", type=Path, default=None, help="K8s dynamic scan: ISV ownership/scope JSON")
    scan_parser.add_argument("--run", action="store_true", help="Reserved for later: execute isvctl directly")
    scan_parser.add_argument("--lab", default=None, help="Reserved for later: named lab/run environment")
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
        print("--run execution is reserved for a later v1 step; pass --junit/--log/--setup-json to ingest artifacts.", file=sys.stderr)
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

    if _has_dynamic_artifacts(args):
        if "k8s" not in domains and "kubernetes" not in domains:
            print("Dynamic artifact ingestion currently supports only --domains k8s.", file=sys.stderr)
            return 2
        dynamic_rows = scan_k8s_artifacts(
            K8sDynamicArtifacts(
                provider_repo=args.provider_repo,
                junit_path=args.junit,
                log_path=args.log,
                setup_json_path=args.setup_json,
                config_path=_first_config_path(report, args.provider_repo),
                scope=load_k8s_scope(args.scope),
            )
        )
        report = GapReport(
            schema_version=report.schema_version,
            provider_repo=report.provider_repo,
            domains=report.domains,
            isv_context=report.isv_context,
            rows=sorted([*report.rows, *dynamic_rows], key=lambda row: (row.domain, row.step_name, row.validation_class or "", row.detection, row.id)),
        )

    args.out.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {args.out}")
    return 0


def _has_dynamic_artifacts(args: argparse.Namespace) -> bool:
    return args.junit is not None or args.log is not None or args.setup_json is not None


def _first_config_path(report: GapReport, provider_repo: Path) -> Path | None:
    for row in report.rows:
        config_path = row.evidence.config_path
        if not config_path:
            continue
        path = Path(config_path)
        return path if path.is_absolute() else provider_repo / path
    return None


def _report(args: argparse.Namespace) -> int:
    report = load_report(args.input_path)
    print(render_report(report, args.format))
    return 0


def _reserved(message: str):
    def handler(_args: argparse.Namespace) -> int:
        print(message, file=sys.stderr)
        return 2

    return handler
