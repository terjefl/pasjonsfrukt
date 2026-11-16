import asyncio
import hmac
import html
import logging
import os
import time
from datetime import datetime, timezone
import xml.etree.ElementTree as ET
from functools import lru_cache
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from . import __version__
from .config import Config, User, config_from_stream
from .logging_utils import LogRedactSecretFilter
from .main import (
    as_int,
    build_podcast_feed_path,
    build_podcast_episode_file_path,
    get_secret_query_parameter,
    read_harvest_status,
)

api = FastAPI()

MAX_DESC_LEN = 200


class RSSResponse(FileResponse):
    media_type = "application/xml"
    charset = "utf-8"


CONFIG_ENV = "PASJONSFRUKT_CONFIG_FILE"
STATUS_TTL_S = 60  # /status result is reused this long, so N podcasts x stat() on the share is not repeated per probe
# /status answers 503 after this long instead of waiting on a hanging share
# (20 s: the share is 5-11 s slow during the nightly backup window, which must not give 503)
STATUS_TIMEOUT_S = 20

_started = time.monotonic()
_clock = time.monotonic  # replaced in tests
_status_cache: Optional[tuple[float, int, dict]] = None
_status_pending: Optional[asyncio.Future] = None


@lru_cache()
def api_config() -> Optional[Config]:
    """
    The configuration for this process.

    `pasjonsfrukt serve` overrides this dependency with the config it parsed. Workers
    that uvicorn starts itself (--workers N) are new processes without that override,
    so they read the file named in PASJONSFRUKT_CONFIG_FILE, which `serve` sets.
    """
    path = os.environ.get(CONFIG_ENV)
    if not path or not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return config_from_stream(f)


def install_secret_filter(config: Optional[Config]):
    """Redact feed secrets from uvicorn's logs (query strings carry ?secret=). Idempotent."""
    if config is None:
        return
    secrets = ([config.secret] if config.secret else []) + [
        u.secret for u in config.users
    ]
    if not secrets:
        return
    for name in ("uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, LogRedactSecretFilter) for f in logger.filters):
            logger.addFilter(LogRedactSecretFilter(secrets))


@api.on_event("startup")
async def _startup():
    # runs in every worker process, also those started with --workers N
    install_secret_filter(api.dependency_overrides.get(api_config, api_config)())


def raise_for_secret(config: Config, secret: Optional[str]) -> Optional[User]:
    if config.users:
        for user in config.users:
            if hmac.compare_digest(secret or "", user.secret):
                return user
        raise HTTPException(
            status_code=401,
            detail=(
                "Authorization failed, missing secret"
                if secret is None
                else "Authorization failed, incorrect secret"
            ),
        )
    elif config.secret is not None:
        if not hmac.compare_digest(secret or "", config.secret):
            raise HTTPException(
                status_code=401,
                detail=(
                    "Authorization failed, missing secret"
                    if secret is None
                    else "Authorization failed, incorrect secret"
                ),
            )
    return None


def raise_for_podcast_slug(config: Config, slug):
    if slug not in config.podcasts.keys():
        raise HTTPException(status_code=404, detail="Requested resource not found")


def _existing_file(file_path) -> Optional[str]:
    return str(file_path.resolve()) if file_path.is_file() else None


async def file_response_if_exists(file_path, response_class=FileResponse):
    # the lookup touches the share (CIFS), so it runs in a thread and never blocks the event loop
    resolved = await run_in_threadpool(_existing_file, file_path)
    if resolved is None:
        raise HTTPException(status_code=404, detail="Requested resource not found")
    return response_class(resolved)


def get_feed_meta(feed_path):
    try:
        tree = ET.parse(feed_path)
        root = tree.getroot()
        channel = root.find("channel")
        if channel is None:
            return None, None
        title_el = channel.find("title")
        desc_el = channel.find("description")
        title = title_el.text if title_el is not None else None
        description = desc_el.text if desc_el is not None else None
        return title, description
    except Exception as e:
        print(f"[WARN] Could not read feed metadata from {feed_path}: {e}")
        return None, None


def render_description(desc, slug):
    if not desc:
        return ""
    escaped = html.escape(desc)
    if len(desc) <= MAX_DESC_LEN:
        return f"<p>{escaped}</p>"
    short = html.escape(desc[:MAX_DESC_LEN])
    uid = slug.replace("-", "_")
    return (
        f'<span id="desc-short-{uid}">{short}… '
        f'<a href="#" onclick="toggleDesc(\'{uid}\'); return false;">Vis mer</a></span>'
        f'<span id="desc-full-{uid}" style="display:none">{escaped} '
        f'<a href="#" onclick="toggleDesc(\'{uid}\'); return false;">Vis mindre</a></span>'
    )


@api.get("/", response_class=HTMLResponse)
async def get_index(secret: Optional[str] = None, config: Config = Depends(api_config)):
    if config.disable_index:
        raise HTTPException(status_code=404, detail="Not found")
    user = raise_for_secret(config, secret)

    alias = user.alias if user else None
    secret_param = (
        f"?secret={user.secret}" if user else get_secret_query_parameter(config)
    )

    slugs = list(config.podcasts)
    # parsing the feeds reads from the share; do it in threads, not in the event loop
    metas = await asyncio.gather(
        *(
            run_in_threadpool(
                get_feed_meta, build_podcast_feed_path(config, slug, alias=alias)
            )
            for slug in slugs
        )
    )
    podcasts = [
        (title or slug, slug, description or "")
        for slug, (title, description) in zip(slugs, metas)
    ]

    podcasts.sort(key=lambda x: x[0].lower())

    cards = []
    for title, slug, description in podcasts:
        feed_url = f"{config.host}/{slug}{secret_param}"
        feed_url_encoded = quote(feed_url, safe="")
        feed_url_no_proto = feed_url.removeprefix("https://").removeprefix("http://")
        desc_html = render_description(description, slug)
        cards.append(
            f"""
        <div class="card">
            <div class="card-title">{html.escape(title)}</div>
            <div class="card-slug">{html.escape(slug)}</div>
            {desc_html}
            <div class="card-links">
                <a class="rss-link" href="{html.escape(feed_url)}">RSS-feed</a>
                <a class="btn btn-overcast" href="overcast://x-callback-url/add?url={feed_url_encoded}">Overcast</a>
                <a class="btn btn-pocketcasts" href="pktc://subscribe/{html.escape(feed_url_no_proto)}">Pocket Casts</a>
            </div>
        </div>"""
        )

    cards_html = "\n".join(cards)

    page = f"""<!DOCTYPE html>
<html lang="no">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pasjonsfrukt</title>
<style>
  body {{ margin: 0; padding: 16px; background: #f5f5f7; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #1d1d1f; }}
  .container {{ max-width: 680px; margin: 0 auto; }}
  h1 {{ font-size: 1.6rem; font-weight: 700; margin-bottom: 20px; color: #e85d26; }}
  .card {{ background: #fff; border-radius: 14px; padding: 18px 20px; margin-bottom: 14px; box-shadow: 0 1px 4px rgba(0,0,0,0.07); }}
  .card-title {{ font-size: 1.1rem; font-weight: 600; margin-bottom: 2px; }}
  .card-slug {{ font-family: monospace; font-size: 0.82rem; color: #888; margin-bottom: 10px; }}
  .card p {{ font-size: 0.9rem; color: #444; margin: 0 0 12px; line-height: 1.5; }}
  .card span {{ font-size: 0.9rem; color: #444; line-height: 1.5; }}
  .card-links {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 10px; }}
  .rss-link {{ font-size: 0.85rem; color: #e85d26; text-decoration: none; margin-right: 4px; }}
  .rss-link:hover {{ text-decoration: underline; }}
  .btn {{ display: inline-block; padding: 6px 16px; border-radius: 20px; font-size: 0.85rem; font-weight: 500; color: #fff; text-decoration: none; }}
  .btn-overcast {{ background: #fc7e0f; }}
  .btn-pocketcasts {{ background: #f43e37; }}
</style>
</head>
<body>
<div class="container">
  <h1>Pasjonsfrukt</h1>
{cards_html}
</div>
<script>
function toggleDesc(uid) {{
  var s = document.getElementById('desc-short-' + uid);
  var f = document.getElementById('desc-full-' + uid);
  if (s.style.display === 'none') {{ s.style.display = ''; f.style.display = 'none'; }}
  else {{ s.style.display = 'none'; f.style.display = ''; }}
}}
</script>
</body>
</html>"""

    return HTMLResponse(content=page)


@api.get("/health")
async def get_health():
    """
    Liveness: the process is up and answering. Unauthenticated, for the Docker
    healthcheck and monitoring (InfraDash, Uptime Kuma).

    Does no file I/O and has no dependencies, so it answers 200 even when the share
    with yield_dir hangs or is missing. The deep check is GET /status.
    """
    return {
        "status": "ok",
        "version": __version__,
        "uptime_seconds": int(time.monotonic() - _started),
    }


def compute_status(config: Config) -> tuple[int, dict]:
    """
    The deep check behind GET /status: last harvest age, failed podcasts and missing
    feed files. Reads from yield_dir (the share), so call it in a thread.
    """
    problems = []
    warnings = []
    now = datetime.now(timezone.utc)

    status = read_harvest_status(config)
    last_harvest = None
    harvest_age = None
    if status is None:
        problems.append("no harvest has completed yet")
    else:
        try:
            last_harvest = datetime.fromisoformat(status["finished"])
            harvest_age = int((now - last_harvest).total_seconds())
            if harvest_age > config.api.health_max_harvest_age:
                problems.append(f"last harvest is {harvest_age // 3600}h old")
        except (KeyError, ValueError):
            problems.append("harvest status file is unreadable")
        if status.get("error"):
            problems.append(f"last harvest aborted: {status['error']}")
        failed = status.get("failed") or []
        streak = status.get("failed_streak")
        if isinstance(streak, dict):
            # A podcast that failed only in the last run is most likely a transient
            # network error: report it as a warning. Two runs in a row is a real fault.
            persistent = [s for s in failed if as_int(streak.get(s), default=2) >= 2]
            warnings = [
                f"podcast failed in last harvest only: {s}"
                for s in failed
                if s not in persistent
            ]
        else:
            # Status file written before streaks were recorded: keep the old behaviour.
            persistent = failed
        if persistent:
            problems.append(f"podcasts failed in last harvest: {', '.join(persistent)}")

    aliases = [u.alias for u in config.users] or [None]
    missing_feeds = [
        slug
        for slug in config.podcasts
        if not all(
            build_podcast_feed_path(config, slug, alias=a).is_file() for a in aliases
        )
    ]
    if missing_feeds:
        problems.append(f"feeds missing for: {', '.join(missing_feeds)}")

    body = {
        "status": "ok" if not problems else "error",
        "problems": problems,
        "podcasts": len(config.podcasts),
        "last_harvest": status["finished"] if status and "finished" in status else None,
        "last_harvest_age_seconds": harvest_age,
        "last_harvest_duration_seconds": (
            status.get("duration_seconds") if status else None
        ),
        "warnings": warnings,
    }
    return (200 if not problems else 503), body


async def _refresh_status(config: Config) -> tuple[int, dict]:
    global _status_cache
    code, body = await run_in_threadpool(compute_status, config)
    _status_cache = (_clock(), code, body)
    return code, body


async def cached_status(config: Config) -> tuple[int, dict]:
    """
    compute_status at most once per STATUS_TTL_S. While a lookup is running, other
    callers wait for the same one instead of starting their own (a hanging share would
    otherwise pile up threads). After STATUS_TIMEOUT_S the caller gets 503; the lookup
    keeps running and its result is cached when it finishes.
    """
    global _status_pending
    if _status_cache is not None and _clock() - _status_cache[0] < STATUS_TTL_S:
        return _status_cache[1], _status_cache[2]
    if _status_pending is None or _status_pending.done():
        _status_pending = asyncio.ensure_future(_refresh_status(config))
    try:
        return await asyncio.wait_for(asyncio.shield(_status_pending), STATUS_TIMEOUT_S)
    except asyncio.TimeoutError:
        return 503, {
            "status": "error",
            "problems": [
                f"status check did not finish within {STATUS_TIMEOUT_S}s (is the storage slow?)"
            ],
            "podcasts": len(config.podcasts),
            "warnings": [],
        }


@api.get("/status")
async def get_status(config: Config = Depends(api_config)):
    """
    Deep health check, unauthenticated: 200 when every configured podcast has a feed
    file and the last full harvest finished recently without persistent failures;
    otherwise 503 with the reasons in "problems". A podcast that failed only in the
    last full harvest is listed under "warnings" and does not cause a 503; one that
    failed in two full harvests in a row does. The result is cached for STATUS_TTL_S
    seconds. Never includes secrets or episode data, and makes no external calls.
    """
    if config is None:
        # no override and no readable config file: say so instead of failing with 500
        body = {
            "status": "error",
            "problems": ["no configuration loaded"],
            "podcasts": 0,
            "warnings": [],
        }
        return JSONResponse(body, status_code=503)
    code, body = await cached_status(config)
    return JSONResponse(body, status_code=code)


@api.get(f"/{{slug}}")
async def get_feed(
    slug: str, secret: Optional[str] = None, config: Config = Depends(api_config)
):
    user = raise_for_secret(config, secret)
    raise_for_podcast_slug(config, slug)
    alias = user.alias if user else None
    return await file_response_if_exists(
        build_podcast_feed_path(config, slug, alias=alias), RSSResponse
    )


@api.get(f"/{{podcast_slug}}/{{episode_id}}")
async def get_episode(
    podcast_slug: str,
    episode_id: int,
    secret: Optional[str] = None,
    config: Config = Depends(api_config),
):
    raise_for_secret(config, secret)
    raise_for_podcast_slug(config, podcast_slug)
    return await file_response_if_exists(
        build_podcast_episode_file_path(config, podcast_slug, episode_id)
    )
