"""Health contract: /health is liveness without file I/O, /status is the cached deep check run in a thread."""

import asyncio
import logging
import time
from pathlib import Path

import httpx
import pytest

from pasjonsfrukt import api as api_module
from pasjonsfrukt.api import api, api_config
from pasjonsfrukt.config import ApiConfig, Auth, Config, Podcast, User
from pasjonsfrukt.logging_utils import LogRedactSecretFilter
from pasjonsfrukt.main import build_podcast_feed_path

# async tests run with anyio's pytest plugin (anyio comes with starlette), on asyncio
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def config(tmp_path):
    cfg = Config(
        host="http://localhost",
        auth=Auth(email="x@example.com", password="pw"),
        podcasts={"alpha": Podcast(), "beta": Podcast()},
        yield_dir=str(tmp_path / "yield"),
        api=ApiConfig(),
    )
    for slug in cfg.podcasts:
        feed = build_podcast_feed_path(cfg, slug)
        feed.parent.mkdir(parents=True, exist_ok=True)
        feed.write_text("<rss><channel><title>T</title></channel></rss>")
    return cfg


@pytest.fixture(autouse=True)
def fresh(monkeypatch, config):
    monkeypatch.setattr(api_module, "_status_cache", None)
    monkeypatch.setattr(api_module, "_status_pending", None)
    api.dependency_overrides[api_config] = lambda: config
    yield
    api.dependency_overrides.clear()


def client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url="http://t"
    )


async def test_health_is_liveness_without_file_io(monkeypatch, tmp_path, config):
    def boom(*a, **kw):
        raise AssertionError("/health must not touch the file system")

    monkeypatch.setattr(api_module, "read_harvest_status", boom)
    monkeypatch.setattr(Path, "is_file", boom)
    config.yield_dir = str(tmp_path / "does-not-exist")
    async with client() as c:
        r = await c.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert (
        body["status"] == "ok"
        and body["version"]
        and isinstance(body["uptime_seconds"], int)
    )


async def test_health_answers_fast_while_status_waits_on_slow_storage(monkeypatch):
    slow_s = 5.0
    real = Path.is_file

    def slow_is_file(self):
        time.sleep(slow_s)  # a hanging CIFS stat()
        return real(self)

    monkeypatch.setattr(Path, "is_file", slow_is_file)
    async with client() as c:
        status_task = asyncio.create_task(c.get("/status"))
        await asyncio.sleep(0.2)  # /status is now blocked in its thread
        t0 = time.perf_counter()
        r = await c.get("/health")
        health_ms = (time.perf_counter() - t0) * 1000
        assert r.status_code == 200 and health_ms < 100, health_ms
        assert not status_task.done()  # /status is still waiting on the storage
        r = await status_task
    assert r.status_code == 503  # no harvest status in the tmp dir


async def test_status_lookup_is_cached_for_60_s(monkeypatch, config):
    calls = []
    real = api_module.compute_status

    def counting(cfg):
        calls.append(1)
        return real(cfg)

    now = [1000.0]
    monkeypatch.setattr(api_module, "compute_status", counting)
    monkeypatch.setattr(api_module, "_clock", lambda: now[0])
    async with client() as c:
        await c.get("/status")
        now[0] += 59
        await c.get("/status")
        assert len(calls) == 1  # two calls inside the window: one file-system lookup
        now[0] += 2
        await c.get("/status")
        assert len(calls) == 2  # after the window: a new lookup


async def test_concurrent_status_calls_share_one_lookup(monkeypatch):
    calls = []

    def slow(cfg):
        calls.append(1)
        time.sleep(0.3)
        return 200, {"status": "ok", "problems": [], "warnings": []}

    monkeypatch.setattr(api_module, "compute_status", slow)
    async with client() as c:
        rs = await asyncio.gather(*(c.get("/status") for _ in range(5)))
    assert [r.status_code for r in rs] == [200] * 5 and len(calls) == 1


async def test_status_times_out_with_503_instead_of_hanging(monkeypatch):
    monkeypatch.setattr(api_module, "STATUS_TIMEOUT_S", 0.2)

    def hangs(cfg):
        time.sleep(1.0)
        return 200, {"status": "ok", "problems": [], "warnings": []}

    monkeypatch.setattr(api_module, "compute_status", hangs)
    async with client() as c:
        t0 = time.perf_counter()
        r = await c.get("/status")
        assert time.perf_counter() - t0 < 0.8
        assert r.status_code == 503 and "did not finish" in r.json()["problems"][0]
        await asyncio.sleep(1.0)  # the lookup finishes in the background and is cached
        assert (await c.get("/status")).status_code == 200


async def test_feed_and_index_lookups_run_in_a_thread(monkeypatch, config):
    import threading

    main_thread = threading.get_ident()
    seen = []
    real = Path.is_file

    def is_file(self):
        seen.append(threading.get_ident())
        return real(self)

    monkeypatch.setattr(Path, "is_file", is_file)
    async with client() as c:
        assert (await c.get("/alpha")).status_code == 200
        assert (await c.get("/nope/1")).status_code == 404
    assert seen and main_thread not in seen


def test_workers_read_config_from_env(monkeypatch, tmp_path):
    """--workers N starts new processes without dependency_overrides; they load PASJONSFRUKT_CONFIG_FILE."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "host: http://h\nauth: {email: a@b.c, password: pw}\nyield_dir: y\npodcasts:\n  alpha: {}\n"
    )
    api_config.cache_clear()
    monkeypatch.setenv(api_module.CONFIG_ENV, str(cfg_file))
    try:
        cfg = api_config()
        assert cfg is not None and list(cfg.podcasts) == ["alpha"]
        assert cfg.yield_dir == "y"
    finally:
        api_config.cache_clear()
    monkeypatch.setenv(api_module.CONFIG_ENV, str(tmp_path / "missing.yaml"))
    assert api_config() is None
    api_config.cache_clear()


def test_secret_filter_is_installed_once(config):
    config.users = [User(alias="a", secret="s3cret-a")]
    logger = logging.getLogger("uvicorn.access")
    before = list(logger.filters)
    try:
        api_module.install_secret_filter(config)
        api_module.install_secret_filter(config)
        added = [
            f
            for f in logger.filters
            if isinstance(f, LogRedactSecretFilter) and f not in before
        ]
        assert len(added) <= 1 and any(
            isinstance(f, LogRedactSecretFilter) for f in logger.filters
        )
    finally:
        logger.filters = before
