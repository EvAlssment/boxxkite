"""`boxkite images build/get/ls/rm` — hosted-mode declarative custom image
builds (control-plane/src/control_plane/routers/images.py,
docs/DECLARATIVE-BUILDER-DESIGN.md).

Hosted-only, authenticated with the same long-lived API key `boxkite exec`/
`session`/`secrets` use. Gated server-side by `BOXKITE_IMAGE_BUILDER_ENABLED`
(off by default) -- this module does no client-side guessing about whether
the feature is enabled; a deployment that hasn't opted in surfaces the
control-plane's own 404 via `hosted_request`'s existing error handling.

Not a Dockerfile-passthrough: `base` is one of a small pre-approved set, and
every package list must be exact-version pinned (`name==version`, or
`@scope/name==version` for npm) -- an unpinned entry is rejected by the
control-plane with a 400, surfaced the same way.
"""

from __future__ import annotations

import typer

from .client import hosted_request
from .context import Context, resolve_context
from .errors import CliError


def _require_hosted(ctx: Context, command: str) -> None:
    if ctx.mode != "hosted":
        raise CliError(
            f"`boxkite {command}` needs a hosted control-plane. Local docker-compose mode "
            "(`boxkite up`) has no declarative image builder of its own -- run `boxkite signup` "
            "or `boxkite config set-url`/`set-key` to target a hosted control-plane."
        )


def build(
    base: str = typer.Option(
        "boxkite-default",
        "--base",
        help=(
            "Pre-approved base image: boxkite-default, boxkite-minimal, boxkite-node, "
            "boxkite-go, boxkite-nextjs, or boxkite-rust."
        ),
    ),
    python_package: list[str] = typer.Option(
        None,
        "--python-package",
        help="Exact-version-pinned pip package, e.g. --python-package polars==1.9.0. Repeatable.",
    ),
    apt_package: list[str] = typer.Option(
        None,
        "--apt-package",
        help="Exact-version-pinned apt/apk package, e.g. --apt-package ripgrep==14.1.0-1. Repeatable.",
    ),
    npm_package: list[str] = typer.Option(
        None,
        "--npm-package",
        help="Exact-version-pinned npm package, e.g. --npm-package typescript==5.6.0. Repeatable.",
    ),
    label: str | None = typer.Option(None, "--label", help="Optional label for your own reference."),
) -> None:
    """Queue a custom image build. Always asynchronous -- poll `boxkite images get <id>` for progress."""
    ctx = resolve_context()
    _require_hosted(ctx, "images build")
    body: dict = {"base": base}
    if python_package:
        body["python_packages"] = python_package
    if apt_package:
        body["apt_packages"] = apt_package
    if npm_package:
        body["npm_packages"] = npm_package
    if label is not None:
        body["label"] = label
    result = hosted_request(ctx, "POST", "/v1/images", json=body)
    typer.echo(f"Queued image build {result['id']} (status={result['status']})")


def get(
    image_id: str = typer.Argument(..., help="Image ID to look up (see `boxkite images ls`)."),
) -> None:
    """Get a single custom image's build status."""
    ctx = resolve_context()
    _require_hosted(ctx, "images get")
    image = hosted_request(ctx, "GET", f"/v1/images/{image_id}")
    typer.echo(f"{image['id']}  {image['status']:<10} base={image['base']}  label={image.get('label') or '-'}")
    if image.get("digest"):
        typer.echo(f"  digest={image['digest']}  registry_ref={image.get('registry_ref')}")
    if image.get("failure_reason"):
        typer.echo(f"  failure_reason={image['failure_reason']}")


def ls() -> None:
    """List this account's custom images."""
    ctx = resolve_context()
    _require_hosted(ctx, "images ls")
    result = hosted_request(ctx, "GET", "/v1/images")
    if not result:
        typer.echo("No custom images.")
        return
    for image in result:
        typer.echo(
            f"{image['id']}  {image['status']:<10} base={image['base']}  label={image.get('label') or '-'}"
        )


def rm(
    image_id: str = typer.Argument(..., help="Image ID to delete (see `boxkite images ls`)."),
) -> None:
    """Delete a custom image's control-plane bookkeeping row.

    Does NOT retroactively tear down any already-running sandbox session
    created from this image's digest -- those keep running unaffected.
    """
    ctx = resolve_context()
    _require_hosted(ctx, "images rm")
    hosted_request(ctx, "DELETE", f"/v1/images/{image_id}")
    typer.echo(f"Deleted image {image_id}")
