"""GET /status: harvest age, failed podcasts with the failed_streak tolerance, missing feeds."""

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from pasjonsfrukt import api as api_module
from pasjonsfrukt.api import api, api_config
from pasjonsfrukt.config import ApiConfig, Auth, Config, Podcast
from pasjonsfrukt.main import (
    build_podcast_feed_path,
    harvest_status_path,
    write_harvest_status,
)

SLUGS = ["alpha", "beta"]


@pytest.fixture
def config(tmp_path):
    cfg = Config(
        host="http://localhost",
        auth=Auth(email="x@example.com", password="pw"),
        podcasts={slug: Podcast() for slug in SLUGS},
        yield_dir=str(tmp_path / "yield"),
        api=ApiConfig(),
    )
    for slug in SLUGS:
        feed = build_podcast_feed_path(cfg, slug)
        feed.parent.mkdir(parents=True, exist_ok=True)
        feed.write_text("<rss/>")
    return cfg


@pytest.fixture(autouse=True)
def fresh_status_cache(monkeypatch):
    monkeypatch.setattr(api_module, "_status_cache", None)
    monkeypatch.setattr(api_module, "_status_pending", None)


@pytest.fixture
def health(config):
    """The deep check (GET /status), which carries the harvest semantics that /health used to have."""
    api.dependency_overrides[api_config] = lambda: config
    client = TestClient(api)

    def get():
        api_module._status_cache = None  # every call in these tests is a fresh lookup
        return client.get("/status")

    yield get
    api.dependency_overrides.clear()


def full_run(config, failed, error=None):
    write_harvest_status(config, datetime.now(timezone.utc), failed, error=error)


def test_clean_harvest_is_ok(config, health):
    full_run(config, [])
    r = health()
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["warnings"] == []


def test_one_failed_run_is_200_with_warning(config, health):
    full_run(config, [])
    full_run(config, ["alpha"])
    r = health()
    assert r.status_code == 200, r.json()
    assert r.json()["problems"] == []
    assert any("alpha" in w for w in r.json()["warnings"])


def test_same_slug_failing_twice_in_a_row_is_503(config, health):
    full_run(config, ["alpha"])
    full_run(config, ["alpha"])
    r = health()
    assert r.status_code == 503
    assert any("podcasts failed" in p and "alpha" in p for p in r.json()["problems"])
    assert json.loads(harvest_status_path(config).read_text())["failed_streak"] == {
        "alpha": 2
    }


def test_streak_resets_after_a_successful_run(config, health):
    full_run(config, ["alpha"])
    full_run(config, [])
    full_run(config, ["alpha"])
    assert health().status_code == 200


def test_different_slugs_failing_do_not_add_up(config, health):
    full_run(config, ["alpha"])
    full_run(config, ["beta"])
    r = health()
    assert r.status_code == 200
    assert any("beta" in w for w in r.json()["warnings"])


def test_missing_feed_file_is_still_503(config, health):
    full_run(config, [])
    build_podcast_feed_path(config, "beta").unlink()
    r = health()
    assert r.status_code == 503
    assert any("feeds missing for: beta" in p for p in r.json()["problems"])


def test_aborted_harvest_is_still_503(config, health):
    full_run(config, [], error="login failed")
    r = health()
    assert r.status_code == 503
    assert any("aborted" in p for p in r.json()["problems"])


def test_old_status_file_without_streak_does_not_crash(config, health):
    path = harvest_status_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path.write_text(
        json.dumps(
            {
                "started": now,
                "finished": now,
                "duration_seconds": 1,
                "failed": ["alpha"],
                "error": None,
            }
        )
    )
    r = health()
    # Same as before the change until a new full harvest rewrites the file.
    assert r.status_code == 503
    assert r.json()["warnings"] == []
    # The next full run counts the old file's failures as the previous run.
    full_run(config, ["alpha"])
    assert json.loads(path.read_text())["failed_streak"] == {"alpha": 2}
    assert health().status_code == 503


def test_garbage_streak_values_do_not_crash(config, health):
    full_run(config, ["alpha"])
    path = harvest_status_path(config)
    status = json.loads(path.read_text())
    status["failed_streak"] = {"alpha": "nonsense"}
    path.write_text(json.dumps(status))
    assert health().status_code == 503  # unreadable streak counts as persistent


def test_slug_missing_from_streak_counts_as_persistent(config, health):
    full_run(config, ["alpha"])
    path = harvest_status_path(config)
    status = json.loads(path.read_text())
    status["failed_streak"] = {}
    path.write_text(json.dumps(status))
    assert health().status_code == 503


def test_garbage_previous_streak_does_not_stop_status_write(config, health):
    full_run(config, ["alpha"])
    path = harvest_status_path(config)
    status = json.loads(path.read_text())
    status["failed_streak"] = {"alpha": "nonsense"}
    path.write_text(json.dumps(status))
    full_run(config, ["alpha", "beta"])  # must not raise
    assert json.loads(path.read_text())["failed_streak"] == {"alpha": 2, "beta": 1}
