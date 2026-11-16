"""Harvest isolation: local staging with atomic publish to yield_dir, stale cleanup, and the entrypoint modes."""

import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pasjonsfrukt import main
from pasjonsfrukt.config import Auth, Config, Podcast

ENTRYPOINT = Path(__file__).resolve().parents[1] / "docker" / "entrypoint.sh"

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    monkeypatch.setenv(main.STAGING_ENV, str(staging))
    cfg = Config(
        host="h",
        auth=Auth("a@b.c", "pw"),
        podcasts={"alpha": Podcast()},
        yield_dir=str(tmp_path / "yield"),
    )

    async def no_redirects(client, url, max_hops=10):
        return url

    async def no_feed(*a, **kw):
        return None

    monkeypatch.setattr(main, "resolve_redirects", no_redirects)
    monkeypatch.setattr(main, "sync_slug_feed", no_feed)
    return staging, cfg, tmp_path / "yield" / "alpha"


class FakeClient:
    """Just enough of PodMeClient for harvest_podcast: one new episode (id 101)."""

    def __init__(self, fail_midway: bool, seen_final: list):
        self.fail_midway, self.seen_final = fail_midway, seen_final

    async def get_episode_list(self, slug):
        return [SimpleNamespace(id=101)]

    async def get_episode_ids(self, slug):
        return [101]

    async def get_episode_download_url_bulk(self, ids):
        return [(i, f"https://cdn.example/{i}.mp3") for i in ids]

    async def download_file(self, url, path, on_progress=None, on_finished=None):
        path = Path(path)
        with path.open("wb") as f:
            f.write(b"a" * 1000)
            # while the download is running, nothing with the final name exists on the share
            self.seen_final.append(sorted(p.name for p in path.parents[0].iterdir()))
            if self.fail_midway:
                raise ConnectionError("connection reset mid-download")
            f.write(b"b" * 1000)


async def test_interrupted_download_leaves_no_file_in_yield_dir(setup):
    staging, cfg, podcast_dir = setup
    seen = []
    await main.harvest_podcast(
        FakeClient(fail_midway=True, seen_final=seen), cfg, "alpha"
    )
    assert list(podcast_dir.iterdir()) == []  # no final name and no .part on the share
    assert list(staging.iterdir()) == []  # staging is cleaned up too
    assert seen == [
        ["alpha__101.mp3"]
    ]  # the download itself went to staging, not the share


async def test_completed_download_appears_in_one_step(setup, monkeypatch):
    staging, cfg, podcast_dir = setup
    replaces = []
    real_replace = os.replace

    def watching_replace(src, dst):
        replaces.append(
            (
                Path(src).name,
                Path(dst).name,
                Path(dst).exists(),
                Path(src).stat().st_size,
            )
        )
        return real_replace(src, dst)

    monkeypatch.setattr(main.os, "replace", watching_replace)
    await main.harvest_podcast(
        FakeClient(fail_midway=False, seen_final=[]), cfg, "alpha"
    )
    assert ("101.mp3.part", "101.mp3", False, 2000) in replaces
    assert sorted(p.name for p in podcast_dir.iterdir()) == ["101.mp3"]  # no .part left
    assert (podcast_dir / "101.mp3").stat().st_size == 2000
    assert list(staging.iterdir()) == []


def test_publish_failure_removes_part(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.write_bytes(b"data")
    dest_dir = tmp_path / "yield"

    def failing_replace(a, b):
        raise OSError("share went away")

    monkeypatch.setattr(main.os, "replace", failing_replace)
    with pytest.raises(OSError):
        main.publish_file(src, dest_dir / "ep.mp3")
    assert list(dest_dir.iterdir()) == []


def test_feed_and_status_writes_are_atomic(tmp_path):
    feed = tmp_path / "feed.xml"
    main.write_text_atomic(feed, "<rss>1</rss>")
    main.write_text_atomic(feed, "<rss>2</rss>")
    assert feed.read_text() == "<rss>2</rss>"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["feed.xml"]


def test_stale_cleanup_ignores_files_newer_than_an_hour(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    monkeypatch.setenv(main.STAGING_ENV, str(staging))
    staging.mkdir()
    old, new = staging / "old", staging / "new"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    two_hours_ago = time.time() - 7200
    os.utime(old, (two_hours_ago, two_hours_ago))
    main.clean_staging()
    assert (
        not old.exists() and new.exists()
    )  # a newer file may belong to a running harvest


# --- entrypoint modes --------------------------------------------------------------------------


def _run_entrypoint(tmp_path, mode):
    """Run docker/entrypoint.sh with stub commands on PATH; each stub records that it was started."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    for name in ("pasjonsfrukt", "cron", "crontab", "tail"):
        stub = bin_dir / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> {calls}\n')
        stub.chmod(0o755)
    crontab_src = tmp_path / "crontab"
    crontab_src.write_text("37 5-20 * * * pasjonsfrukt harvest\n")
    script = ENTRYPOINT.read_text().replace(
        "/etc/cron.d/pasjonsfrukt-crontab", str(crontab_src)
    )
    ep = tmp_path / "entrypoint.sh"
    ep.write_text(script)
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "STAGING_DIR": str(tmp_path / "staging"),
        **({"PASJONSFRUKT_MODE": mode} if mode else {}),
    }
    r = subprocess.run(
        ["bash", str(ep), "--host", "0.0.0.0"],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    time.sleep(0.2)  # the background tail stub
    raw = calls.read_text() if calls.exists() else ""
    return r, [line.split()[0] for line in raw.splitlines()], raw


@pytest.mark.parametrize("mode", [None, "web"])
def test_web_mode_starts_only_the_server(tmp_path, mode):
    r, started, raw = _run_entrypoint(tmp_path, mode)
    assert r.returncode == 0, r.stderr
    assert started == ["pasjonsfrukt"] and "pasjonsfrukt serve --host 0.0.0.0" in raw


def test_harvest_mode_starts_only_cron(tmp_path):
    r, started, raw = _run_entrypoint(tmp_path, "harvest")
    assert r.returncode == 0, r.stderr
    assert "pasjonsfrukt" not in started  # no server
    assert "cron" in started and "cron -f" in raw and "crontab" in started


def test_unknown_mode_fails(tmp_path):
    r, started, _ = _run_entrypoint(tmp_path, "both")
    assert r.returncode == 64 and started == []
