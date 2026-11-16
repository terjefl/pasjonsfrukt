import fcntl
import hashlib
import os
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Annotated
import typer
import uvicorn
import pprint

from . import api
from .api import api as api_app, api_config
from .async_cli import AsyncTyper
from .config import config_from_stream
from .main import (
    clean_staging,
    get_podme_client,
    sync_slug_feed,
    harvest_podcast,
    check_slug_compatibility,
    log,
    write_harvest_status,
)

# Never dump local variables (they include the parsed config with credentials) into tracebacks.
cli = AsyncTyper(pretty_exceptions_show_locals=False)


async def run_per_slug(slugs, action: str, func, client, config):
    """
    Run func for each slug, so one failing podcast (e.g. removed from PodMe)
    doesn't stop the rest. Exits non-zero afterwards if any slug failed.
    """
    failed = []
    for s in slugs:
        try:
            await func(client, config, s)
        except Exception as e:
            log(f"[FAIL] {action} of '{s}' failed: {type(e).__name__}: {e}")
            failed.append(s)
    if failed:
        log(f"[FAIL] {action} failed for {len(failed)} podcast(s): {', '.join(failed)}")
    return failed


@cli.command()
async def harvest(
    podcast_slugs: Annotated[
        Optional[list[str]],
        typer.Argument(
            metavar="[PODCAST_SLUG]...",
        ),
    ] = None,
    config_stream: Annotated[
        Optional[typer.FileText],
        typer.Option(
            "--config-file",
            "-c",
            encoding="utf-8",
            help="Configurations file",
        ),
    ] = "config.yaml",
):
    """
    Scrape podcast episodes
    """
    config = config_from_stream(config_stream)

    # Two overlapping runs would download the same episodes to the same staging file and
    # .part on the share, and could publish or remove each other's half-written files.
    # The lock lives on the local filesystem, not in yield_dir, because network shares
    # (CIFS/NFS) handle flock unreliably. Same lock as in podimo.
    lock_name = hashlib.sha256(os.path.abspath(config.yield_dir).encode()).hexdigest()[
        :16
    ]
    lock_path = Path(tempfile.gettempdir()) / f"pasjonsfrukt-harvest-{lock_name}.lock"
    lock_file = lock_path.open("w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, PermissionError):
        log("[WARN] Another harvest is already running, skipping this run")
        raise typer.Exit(code=0)

    log("[INFO] Harvest started")
    clean_staging()
    started = datetime.now(timezone.utc)
    full_run = podcast_slugs is None
    try:
        async with get_podme_client(
            config.auth.email, config.auth.password, config.api
        ) as client:
            to_harvest = config.podcasts.keys() if full_run else podcast_slugs
            failed = await run_per_slug(
                to_harvest, "Harvest", harvest_podcast, client, config
            )
    except Exception as exc:
        log(f"[FAIL] Harvest aborted: {type(exc).__name__}: {exc}")
        if full_run:
            write_harvest_status(
                config, started, [], error=f"{type(exc).__name__}: {exc}"
            )
        raise typer.Exit(code=1)
    # Only a full run (all podcasts) counts as "the last harvest" for /health.
    if full_run:
        write_harvest_status(config, started, failed)
    log("[INFO] Harvest finished")
    if failed:
        raise typer.Exit(code=1)


@cli.command("sync")
async def sync_feeds(
    podcast_slugs: Annotated[
        Optional[list[str]],
        typer.Argument(
            metavar="[PODCAST_SLUG]...",
        ),
    ] = None,
    config_stream: Annotated[
        Optional[typer.FileText],
        typer.Option(
            "--config-file",
            "-c",
            encoding="utf-8",
            help="Configurations file",
        ),
    ] = "config.yaml",
):
    """
    Update RSS podcast feeds to match scraped episodes
    """
    config = config_from_stream(config_stream)
    async with get_podme_client(
        config.auth.email, config.auth.password, config.api
    ) as client:
        to_sync = config.podcasts.keys() if podcast_slugs is None else podcast_slugs
        failed = await run_per_slug(to_sync, "Sync", sync_slug_feed, client, config)
    if failed:
        raise typer.Exit(code=1)


@cli.command(
    name="serve",
    context_settings={
        "allow_extra_args": True,
        "ignore_unknown_options": True,
    },  # Enabled to support uvicorn options
)
def serve_api(
    ctx: typer.Context,
    config_stream: Annotated[
        Optional[typer.FileText],
        typer.Option(
            "--config-file",
            "-c",
            encoding="utf-8",
            help="Configurations file",
        ),
    ] = "config.yaml",
):
    """
    Serve RSS podcast feeds

    Wrapper around uvicorn, and supports passing additional options to the underlying uvicorn.run() command.
    """
    ctx.args.insert(0, f"{api.__name__}:api")
    config = config_from_stream(config_stream)
    api_app.dependency_overrides[api_config] = lambda: config
    # Workers started with --workers N are new processes without the override above;
    # they read the same file through PASJONSFRUKT_CONFIG_FILE (see api.api_config).
    config_path = getattr(config_stream, "name", None)
    if isinstance(config_path, str) and os.path.isfile(config_path):
        os.environ[api.CONFIG_ENV] = os.path.abspath(config_path)
    elif workers_requested(ctx.args) > 1:
        # stdin or a path that is not a regular file: the workers could not load it and
        # every request would fail with 500
        raise typer.BadParameter(
            "--workers > 1 needs the configuration in a file (not stdin)",
            param_hint="--config-file",
        )
    # also installed on startup in every worker (api._startup)
    api.install_secret_filter(config)
    uvicorn.main.main(args=ctx.args)


@cli.command(name="config")
def print_config(
    config_stream: Annotated[
        Optional[typer.FileText],
        typer.Option(
            "--config-file",
            "-c",
            encoding="utf-8",
            help="Configurations file",
        ),
    ] = "config.yaml",
):
    """
    Print parsed config
    """
    pprint.pprint(config_from_stream(config_stream))


@cli.command(name="check")
async def check_slug(
    podcast_slug: Annotated[str, typer.Argument(metavar="PODCAST_SLUG")],
    episodes: Annotated[
        int,
        typer.Option(
            "--episodes",
            "-n",
            help="Number of recent episodes to check",
        ),
    ] = 20,
    config_stream: Annotated[
        Optional[typer.FileText],
        typer.Option(
            "--config-file",
            "-c",
            encoding="utf-8",
            help="Configurations file",
        ),
    ] = "config.yaml",
):
    """
    Check if a podcast slug is safe to add (all episodes on PodMe CDN, not Acast)
    """
    config = config_from_stream(config_stream)
    async with get_podme_client(
        config.auth.email, config.auth.password, config.api
    ) as client:
        await check_slug_compatibility(client, podcast_slug, episodes)


@cli.callback()
def callback():
    """
    Scrape PodMe podcast streams to mp3 and host with RSS feed
    """


def workers_requested(args: list[str]) -> int:
    """The value of uvicorn's --workers in the extra args (1 when not given or not a number)."""
    for i, a in enumerate(args):
        value = None
        if a == "--workers" and i + 1 < len(args):
            value = args[i + 1]
        elif a.startswith("--workers="):
            value = a.split("=", 1)[1]
        if value is not None:
            try:
                return int(value)
            except ValueError:
                return 1
    return 1
