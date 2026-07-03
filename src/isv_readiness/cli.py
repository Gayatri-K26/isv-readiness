from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from isv_readiness.agent import AgentWorkflowError, run_agent_turn
from isv_readiness.auto import AutoWorkflowError, run_auto
from isv_readiness.bundle import BundleError, build_bundle
from isv_readiness.change_verification import (
    apply_verified_change_set,
    load_change_application,
    load_change_verification,
    rollback_change_application,
    verify_change_set,
)
from isv_readiness.changes import build_change_proposal, load_change_set
from isv_readiness.context import (
    ContextError,
    build_context_pack,
    import_context_source,
    sync_context_sources,
)
from isv_readiness.fixes import FixGuardrailError
from isv_readiness.generation import run_generator
from isv_readiness.live import LiveRunError, run_live_domain
from isv_readiness.loop import LoopStateError, advance_loop, load_loop_state
from isv_readiness.onboarding import (
    OnboardingError,
    build_provider_onboarding_plan,
    execute_provider_onboarding,
)
from isv_readiness.project import (
    DEFAULT_VALIDATION_URL,
    ProjectError,
    build_bootstrap_plan,
    execute_bootstrap,
    load_project,
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
from isv_readiness.verification import VerificationError


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        parser.print_help()
        return 2
    return args.handler(args)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gapctl",
        description=(
            "ISV readiness for ai-cloud-validation, in two phases: "
            "qualify (assess & scope the ISV-owned domains: bootstrap, profile) and "
            "validate (test the owned scope: plan, onboard, scan, generate, verify, apply, loop, live, bundle)."
        ),
    )
    subparsers = parser.add_subparsers(dest="command")

    bootstrap_parser = subparsers.add_parser(
        "bootstrap",
        help="Qualify phase: create a pinned workspace scoped to the ISV-owned domains and optionally clone ai-cloud-validation",
    )
    bootstrap_parser.add_argument("--workspace", type=Path, required=True, help="Workspace directory")
    bootstrap_parser.add_argument("--provider-name", required=True, help="Provider name, for example acme-cloud")
    bootstrap_parser.add_argument("--domains", required=True, help="Comma-separated ISV-owned domains (one or many)")
    bootstrap_parser.add_argument("--validation-root", type=Path, default=None, help="Existing checkout to use")
    bootstrap_parser.add_argument("--validation-url", default=DEFAULT_VALIDATION_URL)
    bootstrap_parser.add_argument("--validation-ref", default="main") # Default to main branch for validation checkout
    bootstrap_parser.add_argument("--profile", type=Path, default=None)
    bootstrap_parser.add_argument("--api-base-url", default=None, help="Provider API endpoint (never a credential)")
    bootstrap_parser.add_argument(
        "--api-base-url-env", default=None, help="Environment variable name used by provider scripts for the API URL"
    )
    bootstrap_parser.add_argument("--api-spec", default=None, help="Local path or URL to the provider API spec")
    bootstrap_parser.add_argument(
        "--auth-env",
        action="append",
        default=[],
        help="Credential environment variable name; repeat for multiple inputs",
    )
    bootstrap_parser.add_argument(
        "--runtime-env",
        action="append",
        default=[],
        help="Non-secret runtime environment variable name to pass to isvctl; repeat as needed",
    )
    bootstrap_parser.add_argument("--write", action="store_true", help="Clone if needed and write isv-project.yaml")
    bootstrap_parser.add_argument("--overwrite", action="store_true", help="Replace an existing project manifest")
    bootstrap_parser.set_defaults(handler=_bootstrap)

    context_sync_parser = subparsers.add_parser(
        "context-sync", help="Normalize project context sources into a redacted local cache"
    )
    context_sync_parser.add_argument("--project", type=Path, required=True)
    context_sync_parser.add_argument("--cache-dir", type=Path, default=None)
    context_sync_parser.add_argument(
        "--allow-network", action="store_true", help="Fetch declared HTTP and GitHub issue sources"
    )
    context_sync_parser.set_defaults(handler=_context_sync)

    context_import_parser = subparsers.add_parser(
        "context-import", help="Import a host-fetched MCP or other declared context export"
    )
    context_import_parser.add_argument("--project", type=Path, required=True)
    context_import_parser.add_argument("--source-id", required=True)
    context_import_parser.add_argument("--in", dest="input_path", type=Path, required=True)
    context_import_parser.add_argument("--cache-dir", type=Path, default=None)
    context_import_parser.set_defaults(handler=_context_import)

    context_pack_parser = subparsers.add_parser(
        "context-pack", help="Build one bounded, redacted context pack for a selected gap"
    )
    context_pack_parser.add_argument("--project", type=Path, required=True)
    context_pack_parser.add_argument("--in", dest="input_path", type=Path, required=True, help="gaps.json")
    context_pack_parser.add_argument("--gap-id", required=True)
    context_pack_parser.add_argument("--cache-dir", type=Path, default=None)
    context_pack_parser.add_argument("--max-chars", type=int, default=48_000)
    context_pack_parser.add_argument("--out", type=Path, required=True)
    context_pack_parser.set_defaults(handler=_context_pack)

    generate_parser = subparsers.add_parser(
        "generate", help="Run an explicit generator adapter and require a schema-valid, hash-bound change set"
    )
    generate_parser.add_argument("--context-pack", type=Path, required=True)
    generate_parser.add_argument("--generator", required=True, help="Generator adapter executable (no shell)")
    generate_parser.add_argument("--generator-arg", action="append", default=[])
    generate_parser.add_argument("--generator-env", action="append", default=[])
    generate_parser.add_argument("--cwd", type=Path, default=Path.cwd())
    generate_parser.add_argument("--timeout", type=int, default=300)
    generate_parser.add_argument("--out", type=Path, required=True)
    generate_parser.set_defaults(handler=_generate)

    change_propose_parser = subparsers.add_parser(
        "change-propose", help="Guard a generated multi-file change set and emit an auditable proposal"
    )
    change_propose_parser.add_argument("--in", dest="input_path", type=Path, required=True, help="gaps.json")
    change_propose_parser.add_argument("--provider-repo", type=Path, required=True)
    change_propose_parser.add_argument("--changes", type=Path, required=True)
    change_propose_parser.add_argument("--out", type=Path, required=True)
    change_propose_parser.add_argument("--patch-out", type=Path, default=None)
    change_propose_parser.set_defaults(handler=_change_propose)

    change_verify_parser = subparsers.add_parser(
        "change-verify", help="Verify a guarded multi-file change set in an isolated static rescan"
    )
    change_verify_parser.add_argument("--in", dest="input_path", type=Path, required=True, help="gaps.json")
    change_verify_parser.add_argument("--provider-repo", type=Path, required=True)
    change_verify_parser.add_argument("--changes", type=Path, required=True)
    change_verify_parser.add_argument("--validation-root", type=Path, default=None)
    change_verify_parser.add_argument("--out", type=Path, required=True)
    change_verify_parser.set_defaults(handler=_change_verify)

    change_apply_parser = subparsers.add_parser(
        "change-apply", help="Explicitly apply a verified multi-file transaction with backups"
    )
    change_apply_parser.add_argument("--in", dest="input_path", type=Path, required=True, help="gaps.json")
    change_apply_parser.add_argument("--provider-repo", type=Path, required=True)
    change_apply_parser.add_argument("--changes", type=Path, required=True)
    change_apply_parser.add_argument("--verification", type=Path, required=True)
    change_apply_parser.add_argument("--backup-dir", type=Path, required=True)
    change_apply_parser.add_argument("--out", type=Path, required=True)
    change_apply_parser.add_argument("--apply", action="store_true")
    change_apply_parser.set_defaults(handler=_change_apply)

    change_rollback_parser = subparsers.add_parser(
        "change-rollback", help="Explicitly restore a recorded multi-file application transaction"
    )
    change_rollback_parser.add_argument("--application", type=Path, required=True)
    change_rollback_parser.add_argument("--provider-repo", type=Path, required=True)
    change_rollback_parser.add_argument("--out", type=Path, required=True)
    change_rollback_parser.add_argument("--rollback", action="store_true")
    change_rollback_parser.set_defaults(handler=_change_rollback)

    live_parser = subparsers.add_parser(
        "live-run", help="Run one policy-authorized domain or validation selection and preserve artifacts"
    )
    live_parser.add_argument("--project", type=Path, required=True)
    live_parser.add_argument("--domain", required=True)
    live_parser.add_argument("--selection", default=None, help="Validation class to pass to pytest -k")
    live_parser.add_argument("--scope", type=Path, default=None, help="Kubernetes ownership scope")
    live_parser.add_argument("--artifacts-dir", type=Path, required=True)
    live_parser.add_argument("--timeout", type=int, default=3600)
    live_parser.add_argument("--out", type=Path, required=True)
    live_parser.add_argument("--run-live", action="store_true", help="Required explicit infrastructure-run authorization")
    live_parser.set_defaults(handler=_live_run)

    agent_parser = subparsers.add_parser(
        "agent-run",
        help="Validate phase (advanced): per-gap-gated scan/generate/review/apply/live turns; prefer 'auto' for the single-review-gate flow",
    )
    agent_parser.add_argument("--project", type=Path, required=True)
    agent_parser.add_argument("--domain", required=True)
    agent_parser.add_argument("--work-dir", type=Path, required=True)
    agent_parser.add_argument("--generator", default=None, help="Explicit generator adapter executable")
    agent_parser.add_argument("--generator-arg", action="append", default=[])
    agent_parser.add_argument("--generator-env", action="append", default=[])
    agent_parser.add_argument("--approve-patch", default=None, help="Exact reviewed patch SHA-256")
    agent_parser.add_argument("--apply", action="store_true", help="Apply only the exact --approve-patch transaction")
    agent_parser.add_argument("--run-live", action="store_true", help="Run policy-authorized targeted validation")
    agent_parser.add_argument("--onboard", action="store_true", help="Scaffold a missing provider before scanning")
    agent_parser.set_defaults(handler=_agent_run)

    auto_parser = subparsers.add_parser(
        "auto",
        help="Validate phase: autonomously fill/fix every owned auto-fixable gap, then stop at one review gate",
    )
    auto_parser.add_argument("--project", type=Path, required=True)
    auto_parser.add_argument("--domain", required=True)
    auto_parser.add_argument("--work-dir", type=Path, required=True)
    auto_parser.add_argument("--generator", required=True, help="Explicit generator adapter executable")
    auto_parser.add_argument("--generator-arg", action="append", default=[])
    auto_parser.add_argument("--generator-env", action="append", default=[])
    auto_parser.add_argument("--max-iterations", type=int, default=50)
    auto_parser.add_argument("--apply", action="store_true", help="Apply the reviewed combined patch (needs --approve-patch)")
    auto_parser.add_argument("--approve-patch", default=None, help="Exact combined patch SHA-256 printed at the review gate")
    auto_parser.set_defaults(handler=_auto)

    bundle_parser = subparsers.add_parser(
        "bundle", help="Validate phase: assemble a sanitized, hash-inventoried owned-scope validation evidence bundle"
    )
    bundle_parser.add_argument("--project", type=Path, required=True)
    bundle_parser.add_argument("--agent-work-dir", type=Path, action="append", required=True)
    bundle_parser.add_argument("--out-dir", type=Path, required=True)
    bundle_parser.set_defaults(handler=_bundle)

    scan_parser = subparsers.add_parser(
        "scan", help="Validate phase: build a deterministic static/dynamic gaps.json report over owned domains"
    )
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

    profile_parser = subparsers.add_parser(
        "profile", help="Qualify phase: validate and summarize the solution profile and owned-scope readiness"
    )
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

    loop_parser = subparsers.add_parser(
        "loop", help="Validate phase: advance deterministic one-gap-at-a-time loop state"
    )
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


def _bootstrap(args: argparse.Namespace) -> int:
    try:
        plan = build_bootstrap_plan(
            args.workspace,
            provider_name=args.provider_name,
            domains=[item.strip() for item in args.domains.split(",") if item.strip()],
            validation_root=args.validation_root,
            validation_url=args.validation_url,
            validation_ref=args.validation_ref,
            profile=args.profile,
            api_base_url=args.api_base_url,
            api_base_url_env=args.api_base_url_env,
            api_spec=args.api_spec,
            auth_env=args.auth_env,
            pass_env=args.runtime_env,
        )
        if not args.write:
            print("Bootstrap plan:")
            for line in plan.summary_lines():
                print(line)
            print("\nPass --write to clone when needed and create the project manifest.")
            return 0
        project = execute_bootstrap(plan, overwrite=args.overwrite)
    except (OSError, ProjectError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Wrote project: {plan.manifest_path}")
    print(f"Pinned validation commit: {project.validation.resolved_commit}")
    print(f"Provider state: {project.provider.state}")
    print("Live infrastructure runs remain disabled until explicitly authorized in the project.")
    return 0


def _context_cache_dir(project_path: Path, override: Path | None) -> Path:
    return override or (project_path.resolve().parent / ".gapctl" / "context-cache")


def _context_sync(args: argparse.Namespace) -> int:
    try:
        project = load_project(args.project)
        records = sync_context_sources(
            project,
            args.project,
            _context_cache_dir(args.project, args.cache_dir),
            allow_network=args.allow_network,
        )
    except (OSError, ProjectError, ContextError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    for record in records:
        detail = f" ({record.error})" if record.error else ""
        print(f"{record.source_id}: {record.status}{detail}")
    required_failures = [record for record in records if record.status == "error"]
    return 1 if required_failures else 0


def _context_import(args: argparse.Namespace) -> int:
    try:
        record = import_context_source(
            load_project(args.project),
            args.source_id,
            args.input_path,
            _context_cache_dir(args.project, args.cache_dir),
        )
    except (OSError, ProjectError, ContextError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Imported {record.source_id}: {record.sha256}")
    return 0


def _context_pack(args: argparse.Namespace) -> int:
    try:
        pack = build_context_pack(
            load_project(args.project),
            args.project,
            load_report(args.input_path),
            gap_id=args.gap_id,
            cache_dir=_context_cache_dir(args.project, args.cache_dir),
            max_chars=args.max_chars,
        )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(pack.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, json.JSONDecodeError, ProjectError, ContextError, TypeError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Wrote context pack: {args.out}")
    print(f"Context items: {len(pack.items)}")
    print(f"Budget: {pack.budget['used_chars']}/{pack.budget['max_chars']} characters")
    return 0


def _generate(args: argparse.Namespace) -> int:
    try:
        context_pack = json.loads(args.context_pack.read_text(encoding="utf-8"))
        change_set = run_generator(
            context_pack,
            command=[args.generator, *args.generator_arg],
            cwd=args.cwd,
            pass_env=args.generator_env,
            timeout_seconds=args.timeout,
        )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(change_set.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, json.JSONDecodeError, subprocess.SubprocessError, FixGuardrailError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Wrote generated change set: {args.out}")
    print(f"Gap: {change_set.gap_id}")
    print(f"Files: {len(change_set.changes)}")
    print("No provider source was changed; run change-propose and review the patch next.")
    return 0


def _change_propose(args: argparse.Namespace) -> int:
    try:
        proposal = build_change_proposal(
            load_report(args.input_path),
            provider_repo=args.provider_repo,
            change_set=load_change_set(args.changes),
        )
        args.out.write_text(json.dumps(proposal.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if args.patch_out:
            args.patch_out.write_text(proposal.patch, encoding="utf-8")
    except (OSError, json.JSONDecodeError, FixGuardrailError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Wrote guarded change proposal: {args.out}")
    if args.patch_out:
        print(f"Wrote review patch: {args.patch_out}")
    print(f"Files: {len(proposal.files)}")
    print("Provider source was not modified.")
    return 0


def _change_verify(args: argparse.Namespace) -> int:
    try:
        manifest = verify_change_set(
            load_report(args.input_path),
            provider_repo=args.provider_repo,
            change_set=load_change_set(args.changes),
            validation_root=args.validation_root,
        )
        args.out.write_text(json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, json.JSONDecodeError, FixGuardrailError, VerificationError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Verification success: {'yes' if manifest.success else 'no'}")
    print(f"Files: {len(manifest.files)}")
    print(f"Regressions: {len(manifest.regressions)}")
    print(f"Manifest: {args.out}")
    return 0 if manifest.success else 1


def _change_apply(args: argparse.Namespace) -> int:
    if not args.apply:
        print("Refusing to change source without the explicit --apply flag.", file=sys.stderr)
        return 2
    try:
        result = apply_verified_change_set(
            load_report(args.input_path),
            provider_repo=args.provider_repo,
            change_set=load_change_set(args.changes),
            manifest=load_change_verification(args.verification),
            backup_dir=args.backup_dir,
        )
        args.out.write_text(json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, json.JSONDecodeError, FixGuardrailError, VerificationError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Applied verified change set: {len(result.files)} file(s)")
    print(f"Result: {args.out}")
    print("Run a fresh static scan and the reviewed targeted live validation next.")
    return 0


def _change_rollback(args: argparse.Namespace) -> int:
    if not args.rollback:
        print("Refusing to restore source without the explicit --rollback flag.", file=sys.stderr)
        return 2
    try:
        result = rollback_change_application(
            load_change_application(args.application),
            provider_repo=args.provider_repo,
        )
        args.out.write_text(json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, json.JSONDecodeError, VerificationError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Rolled back change set: {result.gap_id}")
    print(f"Files restored or removed: {len(result.files)}")
    print(f"Result: {args.out}")
    return 0


def _live_run(args: argparse.Namespace) -> int:
    try:
        result = run_live_domain(
            load_project(args.project),
            args.project,
            domain=args.domain,
            artifacts_dir=args.artifacts_dir,
            explicit_authorization=args.run_live,
            selection=args.selection,
            scope_path=args.scope,
            timeout_seconds=args.timeout,
        )
        args.out.write_text(json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, json.JSONDecodeError, ProjectError, LiveRunError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Live verification success: {'yes' if result.success else 'no'}")
    print(f"Selection: {result.selection or 'entire domain'}")
    print(f"Statuses: {', '.join(result.selected_statuses) or 'no JUnit rows'}")
    print(f"Artifacts: {args.artifacts_dir}")
    print(f"Result: {args.out}")
    return 0 if result.success else 1


def _agent_run(args: argparse.Namespace) -> int:
    command = [args.generator, *args.generator_arg] if args.generator else None
    try:
        state = run_agent_turn(
            args.project,
            domain=args.domain,
            work_dir=args.work_dir,
            generator_command=command,
            generator_pass_env=args.generator_env,
            approval_patch_sha256=args.approve_patch,
            apply_changes=args.apply,
            run_live=args.run_live,
            onboard_if_missing=args.onboard,
        )
    except (
        OSError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
        AgentWorkflowError,
        ContextError,
        ProjectError,
        FixGuardrailError,
        VerificationError,
        LiveRunError,
        OnboardingError,
        LoopStateError,
        SolutionProfileError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Agent status: {state.status}")
    print(f"Domain: {state.domain}")
    print(f"Selected gap: {state.selected_gap_id or 'none'}")
    print(f"Reason: {state.reason}")
    if state.patch_sha256:
        print(f"Review patch SHA-256: {state.patch_sha256}")
    print(f"State: {args.work_dir.resolve() / 'agent-state.json'}")
    return 1 if state.status == "blocked" else 0


def _auto(args: argparse.Namespace) -> int:
    try:
        review = run_auto(
            args.project,
            domain=args.domain,
            work_dir=args.work_dir,
            generator_command=[args.generator, *args.generator_arg],
            generator_pass_env=args.generator_env,
            max_iterations=args.max_iterations,
            apply=args.apply,
            approval_patch_sha256=args.approve_patch,
        )
    except (
        OSError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
        AutoWorkflowError,
        ContextError,
        ProjectError,
        FixGuardrailError,
        SolutionProfileError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Auto status: {review.status}")
    print(f"Domain: {review.domain}")
    print(f"Staged fixes: {len(review.staged)}")
    for fix in review.staged:
        print(f"- fixed {fix.target or fix.gap_id} ({fix.validation_class or 'n/a'})")
    print(f"Parked gaps: {len(review.parked)}")
    for gap in review.parked:
        flag = " [MASKED FAILURE]" if gap.masked_failure else ""
        print(f"- {gap.gap_id} [{gap.status}] route={gap.action}{flag}: {gap.reason}")
    print(f"Reason: {review.reason}")
    if review.changed_files:
        print(f"Combined patch: {args.work_dir.resolve() / 'auto-review.patch'}")
        print(f"Approve with: --apply --approve-patch {review.patch_sha256}")
    return 0


def _bundle(args: argparse.Namespace) -> int:
    try:
        manifest = build_bundle(
            args.project,
            agent_work_dirs=args.agent_work_dir,
            output_dir=args.out_dir,
        )
    except (OSError, json.JSONDecodeError, ProjectError, AgentWorkflowError, BundleError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Bundle outcome: {manifest.outcome}")
    print(f"Domains: {len(manifest.domains)}")
    print(f"Included artifacts: {len(manifest.files)}")
    print(f"Bundle: {args.out_dir.resolve()}")
    return 0 if manifest.outcome != "incomplete" else 1


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

    summary = profile.scope_summary()
    coverage = summary["coverage"]
    print(f"Solution: {profile.solution.name} ({profile.solution.version})")
    print(f"Profile: {profile.solution.profile_status}")
    print(f"Journey: {summary['journey_stage']} / {summary['journey_status']}")
    print("Owned domains: " + (", ".join(summary["owned_domains"]) or "none"))
    print(
        "Owned coverage: "
        f"covered={coverage['covered']} gap={coverage['gap']} "
        f"out_of_scope={coverage['out_of_scope']} unknown={coverage['unknown']}"
    )
    print(f"Validation-ready (owned scope): {'yes' if summary['validation_ready'] else 'no'}")
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
