import asyncio
import contextlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

from podme_api import (
    PodMeDefaultAuthClient,
    PodMeUserCredentials,
    PodMeClient,
    PodMeEpisode,
)
from podme_api.models import PodMeDownloadProgressTask
from podme_api.const import PodMeRegion
from podme_api.models import PodMeLanguage
from rfeed import Item, Guid, Enclosure, Feed, Image, iTunesItem, iTunes
from yarl import URL

from .config import ApiConfig, Config, User


def log(msg: str):
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


# --- Harvest status (read by GET /health) ---


def harvest_status_path(config: Config) -> Path:
    return Path(config.yield_dir) / ".harvest-status.json"


def write_harvest_status(
    config: Config, started: datetime, failed: list[str], error: Optional[str] = None
):
    finished = datetime.now(timezone.utc)
    status = {
        "started": started.isoformat(timespec="seconds"),
        "finished": finished.isoformat(timespec="seconds"),
        "duration_seconds": int((finished - started).total_seconds()),
        "failed": failed,
        "error": error,
    }
    path = harvest_status_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(status, indent=2))


def read_harvest_status(config: Config) -> Optional[dict]:
    try:
        return json.loads(harvest_status_path(config).read_text())
    except (FileNotFoundError, ValueError):
        return None


@contextlib.asynccontextmanager
async def get_podme_client(email: str, password: str, api_config: ApiConfig = None):
    api_config = api_config or ApiConfig()
    auth_client = PodMeDefaultAuthClient(
        user_credentials=PodMeUserCredentials(email=email, password=password),
    )
    client = PodMeClient(
        auth_client=auth_client,
        request_timeout=api_config.request_timeout,
        disable_credentials_storage=api_config.disable_credentials_storage,
    )
    if api_config.region is not None:
        auth_client.region = PodMeRegion[api_config.region.upper()]
    client.region = auth_client.region
    if api_config.language is not None:
        client.language = PodMeLanguage[api_config.language.upper()]
    try:
        await client.__aenter__()
        yield client
    finally:
        await client.__aexit__(None, None, None)


async def harvest_podcast(client: PodMeClient, config: Config, slug: str):
    if slug not in config.podcasts:
        log(f"[FAIL] The slug '{slug}' did not match any podcasts in the config file")
        return
    most_recent_episodes_limit = config.podcasts[slug].most_recent_episodes_limit
    if most_recent_episodes_limit is None:
        episodes = await client.get_episode_list(slug)
    else:
        episodes = await client.get_latest_episodes(slug, most_recent_episodes_limit)

    if len(episodes) == 0:
        log(f"[WARN] Could not find any published episodes for '{slug}'")
        return

    published_ids = [e.id for e in episodes]
    harvested_ids = await harvested_episode_ids(client, config, slug)
    to_harvest = [e for e in published_ids if e not in harvested_ids]
    if len(to_harvest) == 0:
        log(
            f"[INFO] Nothing new from '{slug}', all available episodes already harvested"
            f"{f' (only looking at {most_recent_episodes_limit} most recent)' if most_recent_episodes_limit is not None else ''}"
        )
        return
    log(
        f"[INFO] Found {len(to_harvest)} new episode{'s' if len(to_harvest) > 1 else ''} of '{slug}' ready to harvest"
        f"{f' (only looking at {most_recent_episodes_limit} most recent)' if most_recent_episodes_limit is not None else ''}"
    )
    podcast_dir = build_podcast_dir(config, slug)
    podcast_dir.mkdir(parents=True, exist_ok=True)

    for f in podcast_dir.glob("*_interim*"):
        log(f"[INFO] Removing stale interim file: {f.name}")
        f.unlink()

    # harvest each missing episode
    download_urls = await client.get_episode_download_url_bulk(to_harvest)
    download_infos = [
        (url, podcast_dir / f"{episode_id}.mp3") for episode_id, url in download_urls
    ]

    def log_progress(_: PodMeDownloadProgressTask, url: str, progress: int, total: int):
        log(f"[INFO] Downloading from {url}: {progress}/{total}.")

    def log_finished(url: str, path: str):
        log(f"[INFO] Finished downloading {url} to {path}.")

    placeholder = Path(__file__).parent.parent / "episode_not_available.mp3"
    sem = asyncio.Semaphore(config.api.max_concurrent_downloads)

    async def download_one(url, path):
        async with sem:
            try:
                url = await resolve_redirects(client, url)
                if is_hls_url(url):
                    await download_hls_audio(client, url, path)
                else:
                    await client.download_file(
                        url, path, on_progress=log_progress, on_finished=log_finished
                    )
                    if is_hls_playlist_file(path):
                        # Served as a playlist without .m3u8 in the URL
                        path.unlink()
                        await download_hls_audio(client, url, path)
            except Exception as e:
                log(f"[WARN] Download failed for {path.name}: {e}")
                if path.exists():
                    path.unlink()
                if placeholder.exists():
                    shutil.copy2(placeholder, path)
                    path.with_suffix(".unavailable").touch()
                    log(f"[INFO] Placed unavailable-episode placeholder at {path.name}")

    await asyncio.gather(*[download_one(url, path) for url, path in download_infos])

    await sync_slug_feed(client, config, slug, harvested_ids=harvested_ids + to_harvest)


async def resolve_redirects(client: PodMeClient, url, max_hops: int = 10) -> URL:
    """
    Follow redirects without re-quoting the Location headers.

    Acast (e.g. E24-podden) redirects to a signed stitcher URL. aiohttp/yarl
    re-quote that URL when following redirects, which breaks the signature and
    gives 403, so every download failed and got a placeholder. Resolving the
    final URL with encoded=True and passing that to download_file avoids it.
    """
    client._ensure_session()
    url = URL(str(url), encoded=True)
    for _ in range(max_hops):
        async with client.session.get(url, allow_redirects=False) as resp:
            location = resp.headers.get("Location")
            if resp.status not in (301, 302, 303, 307, 308) or not location:
                return url
            url = url.join(URL(location, encoded=True))
    return url


def is_hls_url(url) -> bool:
    return ".m3u8" in URL(str(url), encoded=True).path


def is_hls_playlist_file(path: Path) -> bool:
    """True if the file is an HLS playlist (text) rather than audio."""
    try:
        if path.stat().st_size > 1024 * 1024:
            return False
        with path.open("rb") as f:
            return f.read(7) == b"#EXTM3U"
    except OSError:
        return False


def _hls_attrs(line: str) -> dict[str, str]:
    """Parse the attribute list of an HLS tag line (KEY=VALUE,KEY="VALUE")."""
    _, _, attrs = line.partition(":")
    return {
        k: v.strip('"') for k, v in re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', attrs)
    }


async def _hls_get(client: PodMeClient, url: str) -> bytes:
    # encoded=True: the Akamai URLs are signed and must be sent byte for byte
    async with client.session.get(URL(url, encoded=True)) as resp:
        resp.raise_for_status()
        return await resp.read()


def _hls_audio_playlist(master_url: str, master: str) -> str:
    """Pick the audio-only media playlist from an HLS master playlist."""
    lines = master.splitlines()
    # Audio-only variant: #EXT-X-STREAM-INF with only an mp4a codec
    for i, line in enumerate(lines[:-1]):
        if line.startswith("#EXT-X-STREAM-INF"):
            codecs = _hls_attrs(line).get("CODECS", "")
            if codecs and all(c.strip().startswith("mp4a") for c in codecs.split(",")):
                return urljoin(master_url, lines[i + 1].strip())
    # Separate audio rendition
    for line in lines:
        if line.startswith("#EXT-X-MEDIA") and "TYPE=AUDIO" in line:
            uri = _hls_attrs(line).get("URI")
            if uri:
                return urljoin(master_url, uri)
    raise RuntimeError("No audio playlist found in HLS master playlist")


async def _aes128_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        "openssl", "enc", "-d", "-aes-128-cbc", "-K", key.hex(), "-iv", iv.hex(),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    out, err = await proc.communicate(data)
    if proc.returncode != 0:
        raise RuntimeError(f"AES-128 decryption failed: {err.decode(errors='replace')}")
    return out


async def download_hls_audio(
    client: PodMeClient, url, path: Path, concurrency: int = 8
):
    """
    Download the audio track of an HLS stream.

    Video podcasts (e.g. Papaya) are delivered as an HLS master playlist with
    video variants and an audio-only playlist of AES-128 encrypted fMP4
    segments. Downloading the URL directly just saves the playlist text, and
    ffmpeg (5.1 in the image) stops after the first encrypted fMP4 segment.
    So fetch and decrypt the audio segments here, concatenate them after the
    init segment, and let ffmpeg remux the result (no re-encoding) into a
    regular MP4 container, like the other episodes.

    Relative URIs are resolved with urljoin on plain strings: yarl's join
    normalises the signed path (%2f -> /), and Akamai then answers 403.
    """
    client._ensure_session()
    master_url = str(url)
    master = (await _hls_get(client, master_url)).decode()
    if "#EXT-X-STREAM-INF" in master:
        playlist_url = _hls_audio_playlist(master_url, master)
        playlist = (await _hls_get(client, playlist_url)).decode()
    else:
        playlist_url, playlist = master_url, master

    init_url = None
    segments = []  # (url, key_info, sequence number)
    key_info = None
    sequence = 0
    for line in playlist.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            sequence = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-MAP:"):
            init_url = urljoin(playlist_url, _hls_attrs(line)["URI"])
        elif line.startswith("#EXT-X-KEY:"):
            attrs = _hls_attrs(line)
            method = attrs.get("METHOD", "NONE")
            if method == "NONE":
                key_info = None
            elif method == "AES-128":
                key_info = (
                    urljoin(playlist_url, attrs["URI"]),
                    attrs.get("IV"),
                )
            else:
                raise RuntimeError(f"Unsupported HLS encryption method {method}")
        elif line and not line.startswith("#"):
            segments.append((urljoin(playlist_url, line), key_info, sequence))
            sequence += 1
    if not segments:
        raise RuntimeError("HLS audio playlist has no segments")

    keys: dict[str, bytes] = {}
    for _, info, _ in segments:
        if info is not None and info[0] not in keys:
            keys[info[0]] = await _hls_get(client, info[0])

    async def fetch_segment(seg_url, info, seq):
        data = await _hls_get(client, seg_url)
        if info is None:
            return data
        key_url, iv = info
        iv_bytes = (
            bytes.fromhex(iv[2:] if iv.lower().startswith("0x") else iv)
            if iv
            else seq.to_bytes(16, "big")
        )
        return await _aes128_decrypt(keys[key_url], iv_bytes, data)

    log(f"[INFO] Downloading {len(segments)} HLS audio segments for {path.name}")
    fragments = path.with_name(f"{path.stem}_interim.frag.mp4")
    interim = path.with_stem(f"{path.stem}_interim")
    try:
        with fragments.open("wb") as f:
            if init_url is not None:
                f.write(await _hls_get(client, init_url))
            for i in range(0, len(segments), concurrency):
                batch = segments[i : i + concurrency]
                for data in await asyncio.gather(*(fetch_segment(*s) for s in batch)):
                    f.write(data)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(fragments),
            "-map", "0:a:0", "-vn", "-c:a", "copy",
            "-map_metadata", "-1",
            "-f", "mp4", "-brand", "isomiso2mp41",
            str(interim),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )  # fmt: skip
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not interim.exists() or interim.stat().st_size == 0:
            raise RuntimeError(
                f"ffmpeg failed ({proc.returncode}): {stderr.decode(errors='replace')[-500:]}"
            )
        interim.replace(path)
    finally:
        fragments.unlink(missing_ok=True)
        interim.unlink(missing_ok=True)
    log(f"[INFO] Finished downloading HLS audio to {path}")


async def harvested_episode_ids(client: PodMeClient, config: Config, slug: str):
    podcast_dir = build_podcast_dir(config, slug)
    if not podcast_dir.is_dir():
        # no directory, so clearly no harvested episodes
        return []
    episode_ids = await client.get_episode_ids(slug)
    harvested = []
    for f in podcast_dir.iterdir():
        if not f.is_file():
            continue
        m = re.match(r"^(\d+)\.mp3$", f.name)  # fix #1: only match numeric filenames
        if m is not None:
            episode_id = int(m.group(1))
            if is_hls_playlist_file(f):
                # Earlier versions saved the HLS playlist instead of the audio.
                # Remove it so the episode is harvested again.
                log(
                    f"[INFO] Removing broken playlist file {slug}/{f.name}, will re-harvest"
                )
                f.unlink()
                continue
            if episode_id in episode_ids:
                harvested.append(episode_id)
    return harvested


PODME_CDN_HOSTS = ("dd-podme.akamaized.net", "amd-podme.akamaized.net")
ACAST_CDN_HOST = "flex2.acast.com"


async def check_slug_compatibility(client: PodMeClient, slug: str, episode_count: int):
    episodes = await client.get_latest_episodes(slug, episode_count)
    if not episodes:
        log(f"[WARN] No episodes found for '{slug}'")
        return
    episode_ids = [e.id for e in episodes]
    download_urls = await client.get_episode_download_url_bulk(episode_ids)
    podme_count = 0
    acast_count = 0
    for episode_id, url in download_urls:
        if any(host in url for host in PODME_CDN_HOSTS):
            print(f"  ✅ {episode_id}: PODME  ({url[:60]}...)")
            podme_count += 1
        elif ACAST_CDN_HOST in url:
            print(f"  ❌ {episode_id}: ACAST  ({url[:60]}...)")
            acast_count += 1
        else:
            print(f"  ❓ {episode_id}: UNKNOWN ({url[:60]}...)")
    total = podme_count + acast_count
    print(f"\nResultat for '{slug}': {podme_count}/{total} på PodMe-CDN")
    if acast_count == 0:
        print(f"✅ Trygg å legge til – alle episoder er på PodMe-CDN")
    else:
        print(
            f"❌ Ikke trygg – {acast_count} episode{'r' if acast_count > 1 else ''} er på Acast-CDN og vil feile med 403"
        )


def get_secret_query_parameter(config: Config):
    if config.secret is None:
        return ""  # no secret required, so don't append query parameter
    return f"?secret={config.secret}"


def build_podcast_dir(config: Config, slug: str):
    return Path(config.yield_dir) / slug


def build_podcast_feed_path(config: Config, slug: str, alias: Optional[str] = None):
    base = config.podcasts[slug].feed_name
    filename = f"{base}-{alias}.xml" if alias else f"{base}.xml"
    return build_podcast_dir(config, slug) / filename


def build_podcast_episode_file_path(config: Config, podcast_slug: str, episode_id: int):
    return build_podcast_dir(config, podcast_slug) / f"{episode_id}.mp3"


def build_feed(
    config: Config,
    episodes: list[PodMeEpisode],
    slug: str,
    title: str,
    description: str,
    image_url: str,
    unavailable_ids: set[int] = None,
    secret: Optional[str] = None,
):
    secret_query_param = (
        f"?secret={secret}"
        if secret is not None
        else get_secret_query_parameter(config)
    )
    unavailable_ids = unavailable_ids or set()
    items = []
    for e in episodes:
        episode_id = e.id
        episode_path = f"{slug}/{episode_id}"
        episode_title = (
            f"[Ikke tilgjengelig] {e.title}"
            if episode_id in unavailable_ids
            else e.title
        )
        items.append(
            Item(
                title=episode_title,
                description=e.description,
                guid=Guid(episode_id, isPermaLink=False),
                enclosure=Enclosure(
                    url=f"{config.host}/{episode_path}{secret_query_param}",
                    type="audio/mpeg",
                    length=build_podcast_episode_file_path(config, slug, episode_id)
                    .stat()
                    .st_size,
                ),
                pubDate=e.date_added,
                extensions=[
                    iTunesItem(
                        author=e.author_full_name,
                        duration=e.length,
                    )
                ],
            )
        )
    feed_link = f"{config.host}/{slug}{secret_query_param}"
    feed = Feed(
        title=title,
        link=feed_link,
        description=description,
        language="no",
        image=Image(url=image_url, title=title, link=feed_link),
        items=sorted(items, key=lambda i: i.pubDate, reverse=True),
        extensions=[iTunes(block="Yes")],
    )
    return feed.rss()


async def sync_slug_feed(
    client: PodMeClient,
    config: Config,
    slug: str,
    harvested_ids: list[int] = None,
):
    if slug not in config.podcasts:
        log(f"[FAIL] The slug '{slug}' did not match any podcasts in the config file")
        return
    log(f"[INFO] Syncing '{slug}' feed...")
    episode_ids = (
        harvested_ids
        if harvested_ids is not None
        else await harvested_episode_ids(client, config, slug)
    )
    episodes = await client.get_episodes_info(episode_ids)
    podcast_info = await client.get_podcast_info(slug)
    podcast_dir = build_podcast_dir(config, slug)
    unavailable_ids = {
        int(f.stem) for f in podcast_dir.glob("*.unavailable") if f.stem.isdigit()
    }
    build_podcast_dir(config, slug).mkdir(parents=True, exist_ok=True)
    if config.users:
        for user in config.users:
            feed = build_feed(
                config,
                episodes,
                slug,
                podcast_info.title,
                podcast_info.description,
                podcast_info.image_url,
                unavailable_ids=unavailable_ids,
                secret=user.secret,
            )
            with build_podcast_feed_path(config, slug, alias=user.alias).open(
                "w", encoding="utf-8"
            ) as feed_file:
                feed_file.write(feed)
        log(
            f"[INFO] '{slug}' feed written for {len(config.users)} user{'s' if len(config.users) != 1 else ''}"
            f" ({len(episodes)} episode{'s' if len(episodes) != 1 else ''})"
        )
    else:
        feed = build_feed(
            config,
            episodes,
            slug,
            podcast_info.title,
            podcast_info.description,
            podcast_info.image_url,
            unavailable_ids=unavailable_ids,
        )
        with build_podcast_feed_path(config, slug).open(
            "w", encoding="utf-8"
        ) as feed_file:
            feed_file.write(feed)
        log(
            f"[INFO] '{slug}' feed now serving {len(episodes)} episode{'s' if len(episodes) != 1 else ''}"
        )
