import html
import xml.etree.ElementTree as ET
from functools import lru_cache
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import FileResponse, HTMLResponse

from .config import Config
from .main import (
    build_podcast_feed_path,
    build_podcast_episode_file_path,
    get_secret_query_parameter,
)

api = FastAPI()

MAX_DESC_LEN = 200


class RSSResponse(FileResponse):
    media_type = "application/xml"
    charset = "utf-8"


@lru_cache()
def api_config() -> Optional[Config]:
    return None


def raise_for_secret(config: Config, secret):
    if config.secret is not None and secret != config.secret:
        if secret is None:
            raise HTTPException(
                status_code=401, detail="Authorization failed, missing secret"
            )
        raise HTTPException(
            status_code=401, detail="Authorization failed, incorrect secret"
        )


def raise_for_podcast_slug(config: Config, slug):
    if slug not in config.podcasts.keys():
        raise HTTPException(status_code=404, detail="Requested resource not found")


def file_response_if_exists(file_path):
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Requested resource not found")
    return FileResponse(str(file_path.resolve()))


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
    except Exception:
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
    raise_for_secret(config, secret)

    secret_param = get_secret_query_parameter(config)

    podcasts = []
    for slug in config.podcasts:
        feed_path = build_podcast_feed_path(config, slug)
        title, description = get_feed_meta(feed_path)
        podcasts.append((title or slug, slug, description or ""))

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


@api.get(f"/{{slug}}")
async def get_feed(
    slug: str, secret: Optional[str] = None, config: Config = Depends(api_config)
):
    raise_for_secret(config, secret)
    raise_for_podcast_slug(config, slug)
    return file_response_if_exists(build_podcast_feed_path(config, slug))


@api.get(f"/{{podcast_slug}}/{{episode_id}}")
async def get_episode(
    podcast_slug: str,
    episode_id: int,
    secret: Optional[str] = None,
    config: Config = Depends(api_config),
):
    raise_for_secret(config, secret)
    raise_for_podcast_slug(config, podcast_slug)
    return file_response_if_exists(
        build_podcast_episode_file_path(config, podcast_slug, episode_id)
    )
