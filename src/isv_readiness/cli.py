"""Public command line interface for the ISV readiness journey."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from isv_readiness.journey import cmd_qualify, cmd_validate
from isv_readiness.publish import PublishError, publish_project
from isv_readiness.simple import cmd_init, find_project


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
        description="Qualify and validate an ISV-owned infrastructure scope against NVIDIA ai-cloud-validation.",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    init_parser = subparsers.add_parser(
        "init",
        help="Create a pinned workspace and import the qualification context",
    )
    init_parser.add_argument("provider_name", help="Provider name, for example acme-cloud")
    init_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path("."),
        help="Workspace to create (default: current directory)",
    )
    init_parser.add_argument(
        "--domains",
        required=True,
        help="Comma-separated infrastructure domains the ISV owns",
    )
    init_parser.add_argument("--api", dest="api_url", default=None, help="Provider API base URL")
    init_parser.add_argument(
        "--api-spec",
        default=None,
        help="Local path or URL for the provider API specification",
    )
    init_parser.add_argument(
        "--auth",
        dest="auth_envs",
        action="append",
        default=[],
        help="Credential environment-variable name; repeat when needed",
    )
    init_parser.add_argument(
        "--validation-ref",
        default="main",
        help="ai-cloud-validation branch or tag to pin (default: main)",
    )
    init_parser.set_defaults(handler=_init)

    qualify_parser = subparsers.add_parser(
        "qualify",
        help="Draft, review, and approve the ISV-owned validation scope",
    )
    qualify_parser.add_argument(
        "--generator",
        choices=("codex", "claude"),
        default="codex",
        help="Profile generator (default: codex)",
    )
    qualify_parser.set_defaults(handler=_qualify)

    validate_parser = subparsers.add_parser(
        "validate",
        help="Generate reviewed fixes and run every owned validation domain",
    )
    validate_parser.add_argument(
        "--generator",
        choices=("codex", "claude"),
        default="codex",
        help="Provider implementation generator (default: codex)",
    )
    validate_parser.set_defaults(handler=_validate)

    publish_parser = subparsers.add_parser(
        "publish",
        help="Publish the latest successful evidence for every owned domain",
    )
    publish_parser.add_argument("--lab-id", type=int, required=True, help="NVIDIA-assigned lab ID")
    publish_parser.add_argument("--isv-software-version", default=None)
    publish_parser.add_argument("--tag", action="append", default=[])
    publish_parser.set_defaults(handler=_publish)
    return parser


def _init(args: argparse.Namespace) -> int:
    domains = [domain.strip() for domain in args.domains.split(",") if domain.strip()]
    return cmd_init(
        args.provider_name,
        workspace=args.workspace,
        domains=domains,
        api_url=args.api_url,
        auth_envs=args.auth_envs,
        api_spec=args.api_spec,
        validation_ref=args.validation_ref,
    )


def _qualify(args: argparse.Namespace) -> int:
    return cmd_qualify(generator=args.generator)


def _validate(args: argparse.Namespace) -> int:
    return cmd_validate(generator=args.generator)


def _publish(args: argparse.Namespace) -> int:
    try:
        publish_project(
            find_project(),
            lab_id=args.lab_id,
            isv_software_version=args.isv_software_version,
            tags=args.tag,
        )
    except (OSError, PublishError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0
