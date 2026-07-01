from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from isv_readiness.fixes import FixGuardrailError, build_fix_proposal
from isv_readiness.loop import LoopStateError, advance_loop, load_loop_state
from isv_readiness.onboarding import (
    OnboardingError,
    build_provider_onboarding_plan,
    execute_provider_onboarding,
)
from isv_readiness.scan.dynamic import DynamicArtifacts, scan_dynamic_artifacts
from isv_readiness.scan.k8s_dynamic import K8sDynamicArtifacts, scan_k8s_artifacts
from isv_readiness.scan.k8s_onboard import build_k8s_onboarding_plan, write_k8s_onboarding_files
from isv_readiness.scan.k8s_scope import load_k8s_scope
from isv_readiness.scan.models import GapReport
from isv_readiness.scan.profile import enrich_report_with_profile
from isv_readiness.scan.report import load_report, render_report
from isv_readiness.scan.scanner import ScanOptions, scan_provider
from isv_readiness.solution_profile import SolutionProfile, SolutionProfileError, load_solution_profile
from isv_readiness.validation_adapter import IsvctlAdapter, ValidationAdapterError
from isv_readiness.verification import (
    VerificationError,
    apply_verified_candidate,
    load_verification_manifest,
    verify_fix_candidate,
)


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
    scan_parser.add_argument("--junit", type=Path, default=None, help="Dynamic scan: JUnit XML from isvctl test run")
    scan_parser.add_argument("--log", type=Path, default=None, help="Dynamic scan: captured isvctl log")
    scan_parser.add_argument(
        "--setup-json", type=Path, default=None, help="K8s dynamic scan: captured setup inventory JSON"
    )
    scan_parser.add_argument("--scope", type=Path, default=None, help="K8s dynamic scan: ISV ownership/scope JSON")
    scan_parser.add_argument("--artifacts-dir", type=Path, default=None, help="Directory for --run JUnit/log artifacts")
    scan_parser.add_argument(
        "--run", action="store_true", help="Execute one domain with isvctl and parse its artifacts"
    )
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
    plan_parser.add_argument(
        "--validation-root", type=Path, default=None, help="Checkout root; omit for isvctl on PATH"
    )
    plan_parser.add_argument("--out", type=Path, default=Path("validation-plan.json"))
    plan_parser.set_defaults(handler=_plan)

    fix_parser = subparsers.add_parser("fix", help="Validate a candidate provider-script edit and emit a patch")
    fix_parser.add_argument("--in", dest="input_path", type=Path, required=True, help="Profile-enriched gaps.json")
    fix_parser.add_argument("--gap-id", required=True, help="One deterministic gap ID to propose a fix for")
    fix_parser.add_argument("--provider-repo", type=Path, required=True, help="Approved provider repository root")
    fix_parser.add_argument("--candidate", type=Path, required=True, help="Candidate replacement file")
    fix_parser.add_argument("--out", type=Path, required=True, help="Unified diff output path")
    fix_parser.set_defaults(handler=_fix)

    verify_parser = subparsers.add_parser("verify", help="Verify one candidate in an isolated static rescan")
    verify_parser.add_argument("--in", dest="input_path", type=Path, required=True, help="Profile-enriched gaps.json")
    verify_parser.add_argument("--gap-id", required=True, help="One deterministic static gap ID")
    verify_parser.add_argument("--provider-repo", type=Path, required=True, help="Approved provider repository root")
    verify_parser.add_argument("--candidate", type=Path, required=True, help="Candidate replacement file")
    verify_parser.add_argument("--validation-root", type=Path, default=None, help="ai-cloud-validation checkout root")
    verify_parser.add_argument("--out", type=Path, required=True, help="Verification manifest JSON")
    verify_parser.set_defaults(handler=_verify)

    apply_parser = subparsers.add_parser("apply", help="Apply a successfully verified candidate atomically")
    apply_parser.add_argument("--in", dest="input_path", type=Path, required=True, help="Profile-enriched gaps.json")
    apply_parser.add_argument("--gap-id", required=True, help="Verified deterministic gap ID")
    apply_parser.add_argument("--provider-repo", type=Path, required=True, help="Approved provider repository root")
    apply_parser.add_argument("--candidate", type=Path, required=True, help="Verified candidate replacement file")
    apply_parser.add_argument("--verification", type=Path, required=True, help="Successful verification manifest")
    apply_parser.add_argument("--backup-dir", type=Path, required=True, help="Backup directory for an existing target")
    apply_parser.add_argument("--out", type=Path, required=True, help="Application result JSON")
    apply_parser.add_argument("--apply", action="store_true", help="Required explicit authorization to change source")
    apply_parser.set_defaults(handler=_apply)

    loop_parser = subparsers.add_parser("loop", help="Advance deterministic one-gap-at-a-time loop state")
    loop_parser.add_argument("--in", dest="input_path", type=Path, required=True, help="Latest gaps.json")
    loop_parser.add_argument("--domain", required=True, help="One domain controlled by this loop")
    loop_parser.add_argument("--state", type=Path, required=True, help="Persistent loop state JSON")
    loop_parser.add_argument("--attempted-gap", default=None, help="Previously selected gap just attempted")
    loop_parser.add_argument("--max-attempts", type=int, default=3, help="Retry budget per gap")
    loop_parser.set_defaults(handler=_loop)

    onboard_parser = subparsers.add_parser("onboard", help="Prepare a provider for readiness scanning")
    onboard_parser.add_argument(
        "--domain", default=None, help="One domain; --domain k8s keeps the lightweight K8s flow"
    )
    onboard_parser.add_argument(
        "--domains", default=None, help="Comma-separated domains for the full provider scaffold"
    )
    onboard_parser.add_argument(
        "--profile", type=Path, default=None, help="Derive domains and ISV inputs from a profile"
    )
    onboard_parser.add_argument("--provider-name", required=True, help="Provider name, for example dsx-air")
    onboard_parser.add_argument(
        "--validation-root", type=Path, required=True, help="Path to ai-cloud-validation checkout"
    )
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
        print(
            "--domains must include at least one domain or --profile must declare a covered/test domain",
            file=sys.stderr,
        )
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
        if len(domains) != 1:
            print("--run requires exactly one domain per invocation.", file=sys.stderr)
            return 2
        if config_path is None or not config_path.exists():
            args.out.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(f"Wrote {args.out}")
            print("No runnable provider config found; fix onboarding gaps first.", file=sys.stderr)
            return 1
        args.junit, args.log = _run_validation(args, config_path, domains[0])

    if _has_dynamic_artifacts(args):
        if len(domains) != 1:
            print("Dynamic artifact ingestion requires exactly one domain per invocation.", file=sys.stderr)
            return 2
        domain = domains[0]
        if domain in {"k8s", "kubernetes"}:
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
        else:
            if args.setup_json is not None or args.scope is not None:
                print("--setup-json and --scope are Kubernetes-specific.", file=sys.stderr)
                return 2
            if args.junit is None:
                print("Non-Kubernetes dynamic ingestion requires --junit.", file=sys.stderr)
                return 2
            dynamic_rows = scan_dynamic_artifacts(
                DynamicArtifacts(
                    provider_repo=args.provider_repo,
                    domain=domain,
                    junit_path=args.junit,
                    log_path=args.log,
                    config_path=config_path,
                    static_rows=tuple(report.rows),
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


def _run_validation(
    args: argparse.Namespace,
    config_path: Path,
    domain: str,
) -> tuple[Path | None, Path]:
    if args.validation_root is None:
        raise SystemExit("--run requires --validation-root so gapctl knows where to execute ai-cloud-validation.")
    validation_root = args.validation_root.resolve()
    artifacts_dir = args.artifacts_dir or (args.out.parent / f"{args.out.stem}-artifacts")
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    artifact_domain = domain.replace("_", "-")
    junit_path = artifacts_dir / f"junit-{artifact_domain}.xml"
    log_path = artifacts_dir / f"isvctl-{artifact_domain}.log"
    try:
        config_arg = str(config_path.resolve().relative_to(validation_root))
    except ValueError:
        config_arg = str(config_path)

    command = [
        *IsvctlAdapter(validation_root).command_prefix,
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


def _fix(args: argparse.Namespace) -> int:
    try:
        proposal = build_fix_proposal(
            load_report(args.input_path),
            gap_id=args.gap_id,
            provider_repo=args.provider_repo,
            candidate_path=args.candidate,
        )
        args.out.write_text(proposal.patch, encoding="utf-8")
    except (OSError, json.JSONDecodeError, FixGuardrailError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Wrote guarded patch proposal: {args.out}")
    print(f"Gap: {proposal.gap_id}")
    print(f"Target: {proposal.target}")
    print(f"Patch SHA-256: {proposal.patch_sha256}")
    print("Source files were not modified; review and apply the patch separately.")
    return 0


def _verify(args: argparse.Namespace) -> int:
    try:
        manifest = verify_fix_candidate(
            load_report(args.input_path),
            gap_id=args.gap_id,
            provider_repo=args.provider_repo,
            candidate_path=args.candidate,
            validation_root=args.validation_root,
        )
        args.out.write_text(json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, json.JSONDecodeError, FixGuardrailError, VerificationError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Verification success: {'yes' if manifest.success else 'no'}")
    print(f"Gap: {manifest.gap_id}")
    print(f"Target: {manifest.target}")
    print(f"Selected status: {manifest.selected_status_before} -> {manifest.selected_status_after or 'missing'}")
    print(f"Regressions: {len(manifest.regressions)}")
    print(f"Manifest: {args.out}")
    return 0 if manifest.success else 1


def _apply(args: argparse.Namespace) -> int:
    if not args.apply:
        print("Refusing to change source without the explicit --apply flag.", file=sys.stderr)
        return 2
    try:
        result = apply_verified_candidate(
            load_report(args.input_path),
            gap_id=args.gap_id,
            provider_repo=args.provider_repo,
            candidate_path=args.candidate,
            manifest=load_verification_manifest(args.verification),
            backup_dir=args.backup_dir,
        )
        args.out.write_text(json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, json.JSONDecodeError, FixGuardrailError, VerificationError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Applied verified candidate: {result.target}")
    print(f"Backup: {result.backup_path or 'not required (new file)'}")
    print(f"Result: {args.out}")
    print("Run a fresh gapctl scan and record the attempt in gapctl loop.")
    return 0


def _loop(args: argparse.Namespace) -> int:
    try:
        report = load_report(args.input_path)
        previous = load_loop_state(args.state) if args.state.exists() else None
        state = advance_loop(
            report,
            domain=args.domain,
            previous=previous,
            attempted_gap_id=args.attempted_gap,
            max_attempts=args.max_attempts,
        )
        args.state.write_text(json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, json.JSONDecodeError, LoopStateError, TypeError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Loop status: {state.status}")
    print(f"Domain: {state.domain}")
    print(f"Unresolved rows: {state.unresolved_count}")
    print(f"Selected gap: {state.selected_gap_id or 'none'}")
    print(f"Route: {state.route or 'none'}")
    print(f"Reason: {state.reason}")
    print(f"State: {args.state}")
    return 1 if state.status == "blocked" else 0


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
        domain.domain for domain in profile.domains if domain.coverage == "covered" and domain.validation_mode == "test"
    ]


def _onboarding_domains(
    single_domain: str | None,
    raw_domains: str | None,
    profile: SolutionProfile | None,
) -> list[str]:
    if single_domain:
        return [single_domain]
    return _scan_domains(raw_domains, profile)
