"""Follow-ups from review: missing config gives 503 on /status, --workers > 1 needs a config file."""

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from pasjonsfrukt import api as api_module
from pasjonsfrukt import cli
from pasjonsfrukt.api import api, api_config


def test_status_without_config_is_503_not_500(monkeypatch):
    monkeypatch.setattr(api_module, "_status_cache", None)
    api.dependency_overrides[api_config] = lambda: None
    try:
        r = TestClient(api).get("/status")
    finally:
        api.dependency_overrides.clear()
    assert r.status_code == 503 and r.json()["problems"] == ["no configuration loaded"]


@pytest.mark.parametrize(
    "args, n",
    [
        ([], 1),
        (["--port", "1"], 1),
        (["--workers", "2"], 2),
        (["--workers=3"], 3),
        (["--workers", "x"], 1),
    ],
)
def test_workers_requested(args, n):
    assert cli.workers_requested(args) == n


def test_serve_refuses_workers_without_config_file(monkeypatch):
    started = []
    monkeypatch.setattr(cli.uvicorn.main, "main", lambda args: started.append(args))
    r = CliRunner().invoke(
        cli.cli,
        ["serve", "-c", "-", "--workers", "2"],
        input="host: http://h\nauth: {email: a@b.c, password: pw}\nyield_dir: y\npodcasts:\n  alpha: {}\n",
    )
    flat = "".join(
        ch for ch in r.output if ch.isalnum() or ch in "->"
    )  # rich wraps the message in a box
    assert r.exit_code == 2 and "needstheconfigurationinafile" in flat
    assert started == []
    r = CliRunner().invoke(
        cli.cli,
        ["serve", "-c", "-"],
        input="host: http://h\nauth: {email: a@b.c, password: pw}\nyield_dir: y\npodcasts:\n  alpha: {}\n",
    )  # one worker from stdin is fine
    assert r.exit_code == 0 and len(started) == 1


def test_harvest_skips_while_another_harvest_holds_the_lock(monkeypatch, tmp_path):
    import fcntl
    import hashlib
    import os
    import tempfile
    from pathlib import Path

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        f"host: http://h\nauth: {{email: a@b.c, password: pw}}\nyield_dir: {tmp_path / 'y'}\npodcasts:\n  alpha: {{}}\n"
    )

    def no_client(*a, **kw):
        raise AssertionError("a skipped harvest must not log in to PodMe")

    monkeypatch.setattr(cli, "get_podme_client", no_client)
    name = hashlib.sha256(os.path.abspath(str(tmp_path / "y")).encode()).hexdigest()[
        :16
    ]
    with (Path(tempfile.gettempdir()) / f"pasjonsfrukt-harvest-{name}.lock").open(
        "w"
    ) as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r = CliRunner().invoke(cli.cli, ["harvest", "-c", str(cfg_file)])
    assert r.exit_code == 0 and "Another harvest is already running" in r.output
