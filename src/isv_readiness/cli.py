from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from isv_readiness.onboarding import (
    OnboardingError,
    build_provider_onboarding_plan,
    execute_provider_onboarding,
)
from isv_readiness.scan.k8s_dynamic import K8sDynamicArtifacts, scan_k8s_artifacts
from isv_readiness.scan.k8s_onboard import build_k8s_onboarding_plan, write_k8s_onboarding_files
from isv_readiness.scan.k8s_scope import load_k8s_scope
from isv_readiness.scan.models import GapReport
from isv_readiness.scan.profile import enrich_report_with_profile
from isv_readiness.scan.report import load_report, render_report
from isv_readiness.scan.scanner import ScanOptions, scan_provider
from isv_readiness.solution_profile import SolutionProfile, SolutionProfileError, load_solution_profile
from isv_readiness.validation_adapter import IsvctlAdapter, ValidationAdapterError


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
    scan_parser.add_argument("--domains", help="Comma-separated domains; defaults to covered/test domains in --profile")
    scan_parser.add_argument("--profile", type=Path, default=None, help="Solution profile for responsibility routing")
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

    profile_parser = subparsers.add_parser("profile", help="Validate and summarize a solution profile")
    profile_parser.add_argument("--in", dest="input_path", type=Path, required=True)
    profile_parser.add_argument("--format", choices=["summary", "json"], default="summary")
    profile_parser.set_defaults(handler=_profile)

    plan_parser = subparsers.add_parser("plan", help="Export the merged, normalized isvctl validation plan")
    plan_parser.add_argument("-f", "--config", type=Path, action="append", required=True)
    plan_parser.add_argument("--validation-root", type=Path, required=True)
    plan_parser.add_argument("--out", type=Path, default=Path("validation-plan.json"))
    plan_parser.set_defaults(handler=_plan)

    fix_parser = subparsers.add_parser("fix", help="Reserved for v0.3 agent fixes")
    fix_parser.set_defaults(handler=_reserved("gapctl fix ships in v0.3."))

    loop_parser = subparsers.add_parser("loop", help="Reserved for v0.4 until-green loops")
    loop_parser.set_defaults(handler=_reserved("gapctl loop ships in v0.4."))

    onboard_parser = subparsers.add_parser("onboard", help="Prepare a provider for readiness scanning")
    onboard_parser.add_argument("--domain", default=None, help="One domain; --domain k8s keeps the lightweight K8s flow")
    onboard_parser.add_argument("--domains", default=None, help="Comma-separated domains for the full provider scaffold")
    onboard_parser.add_argument("--profile", type=Path, default=None, help="Derive domains and ISV inputs from a profile")
    onboard_parser.add_argument("--provider-name", required=True, help="Provider name, for example dsx-air")
    onboard_parser.add_argument("--validation-root", type=Path, required=True, help="Path to ai-cloud-validation checkout")
    onboard_parser.add_argument("--write", action="store_true", help="Create the wrapper/scripts/scope template")
    onboard_parser.add_argument("--overwrite", action="store_true", help="Overwrite existing generated files")
    onboard_parser.set_defaults(handler=_onboard)
    return parser


def _scan(args: argparse.Namespace) -> int:
    profile = _load_profile_arg(args.profile)
    if args.profile is not None and profile is None:
        return 2
    domains = _scan_domains(args.domains, profile)
    if not domains:
        print("--domains must include at least one domain or --profile must declare a covered/test domain", file=sys.stderr)
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
            rows=sorted(
                [*report.rows, *dynamic_rows],
                key=lambda row: (
                    row.domain,
                    row.step_name,
                    row.validation_class or "",
                    row.detection,
                    row.id,
                ),
            ),
        )

    if profile is not None:
        report = enrich_report_with_profile(report, profile)

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
    if args.domain and args.domains:
        print("Use either --domain or --domains, not both.", file=sys.stderr)
        return 2
    if args.domain == "k8s" and args.domains is None and args.profile is None:
        return _onboard_k8s(args)

    profile = _load_profile_arg(args.profile)
    if args.profile is not None and profile is None:
        return 2
    domains = _onboarding_domains(args.domain, args.domains, profile)
    if not domains:
        print("Onboarding requires --domain, --domains, or a profile with covered/test domains.", file=sys.stderr)
        return 2
    try:
        plan = build_provider_onboarding_plan(
            args.validation_root,
            args.provider_name,
            domains,
            profile=profile,
        )
        if args.write:
            written = execute_provider_onboarding(plan, overwrite=args.overwrite)
            print("Created provider onboarding files:")
            for path in written:
                print(f"- {path}")
        else:
            print("Provider onboarding plan:")
            for line in plan.summary_lines():
                print(line)
            print("\nPass --write to run the upstream scaffold and complete selected-domain wiring.")
    except (FileExistsError, OnboardingError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


def _onboard_k8s(args: argparse.Namespace) -> int:
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


def _profile(args: argparse.Namespace) -> int:
    profile = _load_profile_arg(args.input_path)
    if profile is None:
        return 2
    if args.format == "json":
        print(json.dumps(profile.to_dict(), indent=2, sort_keys=True))
        return 0

    summary = profile.qualification_summary()
    coverage = summary["coverage"]
    print(f"Solution: {profile.solution.name} ({profile.solution.version})")
    print(f"Profile: {profile.solution.profile_status}")
    print(f"Journey: {summary['journey_stage']} / {summary['journey_status']}")
    print(
        "Coverage: "
        f"covered={coverage['covered']} gap={coverage['gap']} "
        f"out_of_scope={coverage['out_of_scope']} unknown={coverage['unknown']}"
    )
    print(f"Full validation ready: {'yes' if summary['full_validation_ready'] else 'no'}")
    print("Blocking domains: " + (", ".join(summary["blocking_domains"]) or "none"))
    print("Blocking capabilities: " + (", ".join(summary["blocking_capabilities"]) or "none"))
    return 0


def _plan(args: argparse.Namespace) -> int:
    try:
        plan = IsvctlAdapter(args.validation_root).plan(args.config)
        args.out.write_text(
            json.dumps(plan.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except (OSError, ValidationAdapterError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Wrote {args.out}")
    return 0


def _load_profile_arg(path: Path | None) -> SolutionProfile | None:
    if path is None:
        return None
    try:
        return load_solution_profile(path)
    except (OSError, SolutionProfileError) as exc:
        print(str(exc), file=sys.stderr)
        return None


def _scan_domains(raw_domains: str | None, profile: SolutionProfile | None) -> list[str]:
    if raw_domains:
        return [domain.strip() for domain in raw_domains.split(",") if domain.strip()]
    if profile is None:
        return []
    return [
        domain.domain
        for domain in profile.domains
        if domain.coverage == "covered" and domain.validation_mode == "test"
    ]


def _onboarding_domains(
    single_domain: str | None,
    raw_domains: str | None,
    profile: SolutionProfile | None,
) -> list[str]:
    if single_domain:
        return [single_domain]
    return _scan_domains(raw_domains, profile)


def _reserved(message: str):
    def handler(_args: argparse.Namespace) -> int:
        print(message, file=sys.stderr)
        return 2

    return handler
