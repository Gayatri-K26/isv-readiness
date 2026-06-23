from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from isv_readiness.scan.k8s_dynamic import K8sDynamicArtifacts, scan_k8s_artifacts
from isv_readiness.scan.k8s_onboard import build_k8s_onboarding_plan, write_k8s_onboarding_files
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
    scan_parser.add_argument("--artifacts-dir", type=Path, default=None, help="Directory for --run JUnit/log artifacts")
    scan_parser.add_argument("--run", action="store_true", help="K8s dynamic scan: execute isvctl test run and parse artifacts")
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

    onboard_parser = subparsers.add_parser("onboard", help="Prepare a provider for readiness scanning")
    onboard_parser.add_argument("--domain", choices=["k8s"], default="k8s")
    onboard_parser.add_argument("--provider-name", required=True, help="Provider name, for example dsx-air")
    onboard_parser.add_argument("--validation-root", type=Path, required=True, help="Path to ai-cloud-validation checkout")
    onboard_parser.add_argument("--write", action="store_true", help="Create the wrapper/scripts/scope template")
    onboard_parser.add_argument("--overwrite", action="store_true", help="Overwrite existing generated files")
    onboard_parser.set_defaults(handler=_onboard)
    return parser


def _scan(args: argparse.Namespace) -> int:
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

    config_path = _first_config_path(report, args.provider_repo)
    if args.run:
        if "k8s" not in domains and "kubernetes" not in domains:
            print("--run currently supports only --domains k8s.", file=sys.stderr)
            return 2
        if config_path is None or not config_path.exists():
            args.out.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(f"Wrote {args.out}")
            print("No runnable K8s provider config found; fix onboarding gaps first.", file=sys.stderr)
            return 1
        args.junit, args.log = _run_k8s_validation(args, config_path)

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
                config_path=config_path,
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


def _run_k8s_validation(args: argparse.Namespace, config_path: Path) -> tuple[Path | None, Path]:
    if args.validation_root is None:
        raise SystemExit("--run requires --validation-root so gapctl knows where to execute ai-cloud-validation.")
    validation_root = args.validation_root.resolve()
    artifacts_dir = args.artifacts_dir or (args.out.parent / f"{args.out.stem}-artifacts")
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    junit_path = artifacts_dir / "junit-validation.xml"
    log_path = artifacts_dir / "isvctl.log"
    try:
        config_arg = str(config_path.resolve().relative_to(validation_root))
    except ValueError:
        config_arg = str(config_path)

    command = [
        "uv",
        "run",
        "isvctl",
        "test",
        "run",
        "-f",
        config_arg,
        "--no-upload",
        "--junitxml",
        str(junit_path),
    ]
    env = os.environ.copy()
    result = subprocess.run(
        command,
        cwd=validation_root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    log_path.write_text(result.stdout or "", encoding="utf-8")
    print(f"Ran {' '.join(command)}")
    print(f"Wrote {log_path}")
    if junit_path.exists():
        print(f"Wrote {junit_path}")
    else:
        print(f"JUnit artifact was not produced at {junit_path}; log output will still be preserved.", file=sys.stderr)
    return junit_path if junit_path.exists() else None, log_path


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


def _onboard(args: argparse.Namespace) -> int:
    if args.domain != "k8s":
        print("Only K8s onboarding is implemented in v1.", file=sys.stderr)
        return 2
    try:
        plan = build_k8s_onboarding_plan(args.validation_root, args.provider_name)
        if args.write:
            written = write_k8s_onboarding_files(plan, overwrite=args.overwrite)
            print("Created K8s provider onboarding files:")
            for path in written:
                print(f"- {path}")
        else:
            print("K8s provider onboarding plan:")
            for line in plan.summary_lines():
                print(line)
            print("\nPass --write to create these files.")
    except (FileExistsError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
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
