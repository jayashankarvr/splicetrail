"""Tests for the API's exposure guards: auth, limits, timeouts, persistence."""

import asyncio
import io
import logging
import os
import subprocess
import sys
import threading
import time
from unittest import mock

import numpy as np
import pytest
from PIL import Image

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from splicetrail import Config, ForensicsEngine  # noqa: E402
import splicetrail.api as api  # noqa: E402
from splicetrail.api import create_app  # noqa: E402
from splicetrail.bench.synthetic import make_recolored_splice  # noqa: E402

KEY = "test-key-123"


@pytest.fixture(autouse=True)
def _plain_server(monkeypatch):
    for name in (
        "SPLICETRAIL_API_KEY",
        "SPLICETRAIL_INDEX_DIR",
        "SPLICETRAIL_MAX_TARGETS",
        "SPLICETRAIL_MAX_IMAGE_PIXELS",
        "SPLICETRAIL_LOCK_TIMEOUT",
        "SPLICETRAIL_PHOTODNA_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="module")
def sample():
    return make_recolored_splice(seed=0, image_size=96, fragment_size=24)


def _png(array):
    buf = io.BytesIO()
    Image.fromarray(array).save(buf, format="PNG")
    return ("image.png", buf.getvalue(), "image/png")


# --- 1. API key --------------------------------------------------------------


def test_api_key_required_when_set(monkeypatch):
    monkeypatch.setenv("SPLICETRAIL_API_KEY", KEY)
    client = TestClient(create_app())
    for path in ("/", "/health", "/targets", "/config", "/docs", "/openapi.json"):
        assert client.get(path).status_code == 401, path
        assert client.get(path, headers={"X-API-Key": "wrong"}).status_code == 401
        assert client.get(path, headers={"X-API-Key": KEY}).status_code == 200, path
    r = client.post("/index", files={"file": ("x.png", b"x", "image/png")})
    assert r.status_code == 401
    assert client.delete("/targets/x").status_code == 401
    # Probes stay open so container health checks work.
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200


def test_no_api_key_means_open():
    client = TestClient(create_app())
    assert client.get("/targets").status_code == 200


def test_api_key_uses_constant_time_compare(monkeypatch):
    monkeypatch.setenv("SPLICETRAIL_API_KEY", KEY)
    client = TestClient(create_app())
    with mock.patch.object(api.hmac, "compare_digest", return_value=True) as cmp:
        assert client.get("/targets", headers={"X-API-Key": "x"}).status_code == 200
    cmp.assert_called_once_with(b"x", KEY.encode())


# --- 2. bind -----------------------------------------------------------------


@pytest.mark.parametrize(
    "host,loopback",
    [("127.0.0.1", True), ("::1", True), ("localhost", True), ("0.0.0.0", False)],
)
def test_is_loopback(host, loopback):
    assert api._is_loopback(host) is loopback


def _run_server_logs(caplog, host):
    with mock.patch.dict(sys.modules, {"uvicorn": mock.MagicMock()}):
        with caplog.at_level(logging.WARNING, logger="splicetrail.api"):
            api.run_server(host=host)
    return caplog.text


def test_public_bind_without_key_warns(caplog):
    assert "without SPLICETRAIL_API_KEY" in _run_server_logs(caplog, "0.0.0.0")


def test_bind_warning_reaches_stderr_without_logging_setup():
    code = (
        "import sys\n"
        "from unittest import mock\n"
        "sys.modules['uvicorn'] = mock.MagicMock()\n"
        "import splicetrail.api as a\n"
        "a.run_server(host='0.0.0.0')\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("SPLICETRAIL_")}
    done = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "without SPLICETRAIL_API_KEY" in done.stderr


def test_loopback_or_keyed_bind_does_not_warn(caplog, monkeypatch):
    assert "without SPLICETRAIL_API_KEY" not in _run_server_logs(caplog, "127.0.0.1")
    monkeypatch.setenv("SPLICETRAIL_API_KEY", KEY)
    assert "without SPLICETRAIL_API_KEY" not in _run_server_logs(caplog, "0.0.0.0")


def test_serve_defaults_to_loopback(monkeypatch):
    pytest.importorskip("click")
    from click.testing import CliRunner

    from splicetrail.cli import cli

    with mock.patch.object(api, "run_server") as run:
        assert CliRunner().invoke(cli, ["serve"]).exit_code == 0
    assert run.call_args.kwargs["host"] == "127.0.0.1"

    from splicetrail import __main__ as entry

    monkeypatch.setattr(sys, "argv", ["splicetrail", "serve"])
    with mock.patch.object(api, "run_server") as run:
        entry.main()
    assert run.call_args.kwargs["host"] == "127.0.0.1"


# --- 3. no stalls ------------------------------------------------------------


def test_busy_engine_gives_503_and_health_stays_up(sample, monkeypatch):
    monkeypatch.setenv("SPLICETRAIL_LOCK_TIMEOUT", "0.2")
    client = TestClient(create_app())
    started, release = threading.Event(), threading.Event()

    def slow_index(self, *args, **kwargs):
        started.set()
        release.wait(10)
        return 1

    results = {}
    with mock.patch.object(ForensicsEngine, "index_target", slow_index):
        worker = threading.Thread(
            target=lambda: results.setdefault(
                "index", client.post("/index", files={"file": _png(sample.donor)})
            )
        )
        worker.start()
        try:
            assert started.wait(10)
            t0 = time.monotonic()
            assert client.get("/health/live").status_code == 200
            assert client.get("/health").status_code == 200
            assert time.monotonic() - t0 < 2
            r = client.post("/match", files={"file": _png(sample.tampered)})
            assert r.status_code == 503
            assert r.headers["Retry-After"] == "1"
        finally:
            release.set()
            worker.join(10)
    assert results["index"].status_code == 200


def test_pixel_cap(sample, monkeypatch):
    client = TestClient(create_app())
    big = np.zeros((800, 800, 3), dtype=np.uint8)  # 640k > default 500k
    r = client.post("/index", files={"file": _png(big)})
    assert r.status_code == 413 and "Maximum 500000 pixels" in r.json()["detail"]
    assert client.post("/match", files={"file": _png(big)}).status_code == 413

    monkeypatch.setenv("SPLICETRAIL_MAX_IMAGE_PIXELS", "1000")  # sample is 96x96
    client = TestClient(create_app())
    assert client.post("/index", files={"file": _png(sample.donor)}).status_code == 413


# --- 4. early size rejection -------------------------------------------------


def _drive(guard, headers, chunks):
    """Run an ASGI request through ``guard``; return (status, chunks pulled)."""
    pulled, sent = [], []
    queue = list(chunks)

    async def receive():
        body = queue.pop(0)
        pulled.append(body)
        return {"type": "http.request", "body": body, "more_body": bool(queue)}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/index", "headers": headers}
    asyncio.run(guard(scope, receive, send))
    return sent[0]["status"], pulled


async def _drain_app(scope, receive, send):
    while (await receive()).get("more_body"):
        pass
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def test_content_length_rejected_before_reading():
    inner = mock.AsyncMock()
    guard = api._RequestGuard(inner, api_key=None, body_limit=100)
    status, pulled = _drive(guard, [(b"content-length", b"101")], [b"x" * 101])
    assert status == 413 and pulled == [] and not inner.called


def test_streamed_body_cut_off_at_limit():
    guard = api._RequestGuard(_drain_app, api_key=None, body_limit=100)
    status, pulled = _drive(guard, [], [b"x" * 60] * 10)
    assert status == 413 and len(pulled) == 2  # stopped at the chunk past 100
    status, _ = _drive(guard, [], [b"x" * 50, b"x" * 50])
    assert status == 200


def test_chunked_upload_over_limit_is_413(monkeypatch):
    monkeypatch.setattr(api, "MULTIPART_OVERHEAD", 0)
    monkeypatch.setattr(api, "MAX_FILE_SIZE", 10_000)
    client = TestClient(create_app())

    def body():
        yield (
            b"--B\r\nContent-Disposition: form-data; name=file; filename=x.png\r\n"
            b"Content-Type: image/png\r\n\r\n"
        )
        for _ in range(100):
            yield b"x" * 1000
        yield b"\r\n--B--\r\n"

    r = client.post(
        "/index",
        content=body(),
        headers={"Content-Type": "multipart/form-data; boundary=B"},
    )
    assert r.status_code == 413


def test_upload_read_stops_at_limit_plus_one():
    class Upload:
        def __init__(self, size):
            self.left, self.read_total = size, 0

        async def read(self, n):
            n = min(n, self.left)
            self.left -= n
            self.read_total += n
            return b"x" * n

    upload = Upload(5_000_000)
    with pytest.raises(api.HTTPException) as e:
        asyncio.run(api.read_upload(upload, limit=1000))
    assert e.value.status_code == 413 and upload.read_total == 1001
    assert len(asyncio.run(api.read_upload(Upload(1000), limit=1000))) == 1000


# --- 5. bounded growth -------------------------------------------------------


def test_default_target_cap():
    client = TestClient(create_app())
    assert client.get("/health").json()["index"]["max_targets"] == 1000
    client = TestClient(create_app(Config(max_targets=None)))
    assert client.get("/health").json()["index"]["max_targets"] == 1000


def test_target_cap_evicts_oldest(sample, monkeypatch):
    monkeypatch.setenv("SPLICETRAIL_MAX_TARGETS", "2")
    client = TestClient(create_app())
    for name, image in (
        ("a", sample.donor),
        ("b", sample.tampered),
        ("c", sample.donor[::-1].copy()),
    ):
        r = client.post(
            "/index", params={"image_id": name}, files={"file": _png(image)}
        )
        assert r.status_code == 200
    ids = [t["image_id"] for t in client.get("/targets").json()["targets"]]
    assert ids == ["b", "c"]


# --- 6. PIL global -----------------------------------------------------------


def test_import_leaves_pil_limit_alone():
    code = (
        "from PIL import Image\n"
        "before = Image.MAX_IMAGE_PIXELS\n"
        "import splicetrail.api\n"
        "print(before == Image.MAX_IMAGE_PIXELS)"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert done.stdout.strip() == "True"


def test_decompression_bomb_is_413(monkeypatch):
    buf = io.BytesIO()
    Image.new("RGB", (100, 100)).save(buf, format="PNG")
    # PIL raises DecompressionBombError at open past twice its own limit.
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    with pytest.raises(api.HTTPException) as e:
        api.decode_image(buf.getvalue(), max_pixels=10**9)
    assert e.value.status_code == 413


def test_size_checked_before_decoding():
    buf = io.BytesIO()
    Image.new("RGB", (100, 100)).save(buf, format="PNG")
    with mock.patch.object(Image.Image, "convert") as convert:
        with pytest.raises(api.HTTPException) as e:
            api.decode_image(buf.getvalue(), max_pixels=9999)
    assert e.value.status_code == 413 and not convert.called


# --- 7. no leaking internals -------------------------------------------------


def test_500_is_generic_and_logged_with_request_id(sample, caplog):
    client = TestClient(create_app(), raise_server_exceptions=False)
    with mock.patch.object(
        ForensicsEngine, "match", side_effect=RuntimeError("secret /srv/path")
    ):
        with caplog.at_level(logging.ERROR, logger="splicetrail.api"):
            r = client.post("/match", files={"file": _png(sample.tampered)})
    assert r.status_code == 500
    body = r.json()
    assert body["detail"] == "Internal server error" and "secret" not in r.text
    assert r.headers["X-Request-ID"] == body["request_id"]
    assert body["request_id"] in caplog.text and "secret /srv/path" in caplog.text


def test_health_reports_counts_not_ids(sample):
    client = TestClient(create_app())
    client.post(
        "/index", params={"image_id": "hidden"}, files={"file": _png(sample.donor)}
    )
    r = client.get("/health")
    assert r.json()["index"]["target_count"] == 1 and "hidden" not in r.text


def test_targets_hide_server_paths(sample, tmp_path, monkeypatch):
    client = _persistent_app(monkeypatch, tmp_path)
    client.post(
        "/index", params={"image_id": "donor"}, files={"file": _png(sample.donor)}
    )
    r = client.get("/targets")
    (target,) = r.json()["targets"]
    assert set(target) == {"image_id", "shape"}
    assert target["shape"] == {"height": 96, "width": 96}
    assert str(tmp_path) not in r.text and ".png" not in r.text


def test_match_400_uses_our_message_only(sample):
    from splicetrail.exceptions import InvalidImageError

    client = TestClient(create_app(), raise_server_exceptions=False)
    with mock.patch.object(
        ForensicsEngine, "match", side_effect=InvalidImageError("Image is all black")
    ):
        r = client.post("/match", files={"file": _png(sample.tampered)})
    assert r.status_code == 400 and r.json()["detail"] == "Image is all black"

    # A bare ValueError is not a validation message: it never reaches the client.
    with mock.patch.object(
        ForensicsEngine, "match", side_effect=ValueError("secret /srv/path")
    ):
        r = client.post("/match", files={"file": _png(sample.tampered)})
    assert r.status_code == 500 and "secret" not in r.text


def test_hash_errors_are_fixed_messages(caplog):
    from splicetrail import hashing
    from splicetrail.exceptions import DependencyError

    client = TestClient(create_app())
    image = {"file": _png(np.zeros((8, 8, 3), np.uint8))}
    with mock.patch.object(
        hashing._ALGORITHMS["pdq"],
        "compute",
        side_effect=DependencyError("secret /opt/lib missing"),
    ):
        r = client.post("/hash", params={"algorithm": "pdq"}, files=image)
    assert r.status_code == 501 and "secret" not in r.text
    assert "pip install splicetrail[hash]" in r.json()["detail"]

    with mock.patch.object(
        hashing._ALGORITHMS["pdq"], "compute", side_effect=RuntimeError("secret boom")
    ):
        with caplog.at_level(logging.ERROR, logger="splicetrail.api"):
            r = client.post("/hash", params={"algorithm": "pdq"}, files=image)
    assert r.status_code == 502 and "secret" not in r.text
    request_id = r.headers["X-Request-ID"]
    assert request_id in r.json()["detail"] and "pdq" in r.json()["detail"]
    assert request_id in caplog.text and "secret boom" in caplog.text


def test_photodna_errors_are_fixed_messages(monkeypatch, caplog):
    from splicetrail import photodna

    monkeypatch.setenv("SPLICETRAIL_API_KEY", KEY)
    monkeypatch.setenv("SPLICETRAIL_PHOTODNA_KEY", "secret-key")
    client = TestClient(create_app())
    call = dict(
        params={"algorithm": "photodna"},
        files={"file": _png(np.zeros((8, 8, 3), np.uint8))},
        headers={"X-API-Key": KEY},
    )
    with mock.patch(
        "splicetrail.photodna.PhotoDnaClient",
        side_effect=photodna.PhotoDnaError("secret endpoint http://10.0.0.1"),
    ):
        r = client.post("/hash", **call)
    assert r.status_code == 501 and "secret" not in r.text
    assert "pip install splicetrail[hash]" in r.json()["detail"]

    with mock.patch("splicetrail.photodna.PhotoDnaClient") as client_cls:
        client_cls.return_value.match.side_effect = photodna.PhotoDnaError(
            "secret HTTP 401"
        )
        with caplog.at_level(logging.ERROR, logger="splicetrail.api"):
            r = client.post("/hash", **call)
    assert r.status_code == 502 and "secret" not in r.text
    detail = r.json()["detail"]
    assert "Upstream hashing service" in detail
    assert r.headers["X-Request-ID"] in detail
    assert "secret HTTP 401" in caplog.text


def test_lifespan_sets_start_time():
    with TestClient(create_app()) as client:
        assert client.get("/health").json()["uptime_seconds"] >= 0


# --- 8. PhotoDNA gated -------------------------------------------------------


def test_photodna_needs_api_key(monkeypatch):
    monkeypatch.setenv("SPLICETRAIL_PHOTODNA_KEY", "secret-key")
    client = TestClient(create_app())
    with mock.patch("splicetrail.photodna.PhotoDnaClient") as client_cls:
        r = client.post(
            "/hash",
            params={"algorithm": "photodna"},
            files={"file": _png(np.zeros((8, 8, 3), np.uint8))},
        )
    assert r.status_code == 403 and "SPLICETRAIL_API_KEY" in r.json()["detail"]
    client_cls.assert_not_called()


# --- 9. persistence ----------------------------------------------------------


def _persistent_app(monkeypatch, directory, **kwargs):
    monkeypatch.setenv("SPLICETRAIL_INDEX_DIR", str(directory))
    return TestClient(create_app(), **kwargs)


def test_index_survives_restart_and_delete_persists(sample, tmp_path, monkeypatch):
    first = _persistent_app(monkeypatch, tmp_path)
    r = first.post(
        "/index", params={"image_id": "donor"}, files={"file": _png(sample.donor)}
    )
    assert r.status_code == 200

    second = _persistent_app(monkeypatch, tmp_path)
    body = second.post("/match", files={"file": _png(sample.tampered)}).json()
    assert body["detected"] and body["coherent_regions"][0]["image_id"] == "donor"

    assert second.delete("/targets/donor").status_code == 200
    third = _persistent_app(monkeypatch, tmp_path)
    assert third.get("/targets").json()["count"] == 0
    assert os.listdir(tmp_path / "images") == []
    assert len([d for d in os.listdir(tmp_path) if d.startswith("snap-")]) == 1


def test_crash_mid_save_keeps_previous_state(sample, tmp_path, monkeypatch):
    client = _persistent_app(monkeypatch, tmp_path, raise_server_exceptions=False)
    client.post(
        "/index", params={"image_id": "donor"}, files={"file": _png(sample.donor)}
    )

    real_save = ForensicsEngine.save

    def crashing_save(self, path):
        real_save(self, path)
        with open(f"{path}.engine.json", "w") as f:
            f.write("{half")  # torn write, then the process dies
        raise OSError("disk went away")

    with mock.patch.object(ForensicsEngine, "save", crashing_save):
        r = client.post(
            "/index",
            params={"image_id": "other"},
            files={"file": _png(sample.tampered)},
        )
    assert r.status_code == 500
    # The failed id isn't left half-indexed in memory either.
    assert [t["image_id"] for t in client.get("/targets").json()["targets"]] == [
        "donor"
    ]

    restarted = _persistent_app(monkeypatch, tmp_path)
    assert [t["image_id"] for t in restarted.get("/targets").json()["targets"]] == [
        "donor"
    ]
    body = restarted.post("/match", files={"file": _png(sample.tampered)}).json()
    assert body["detected"]


def test_unreadable_saved_index_stops_startup(tmp_path, monkeypatch):
    (tmp_path / "CURRENT").write_text("snap-missing")
    monkeypatch.setenv("SPLICETRAIL_INDEX_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="saved index"):
        create_app()


def test_ready_checks_index_dir(tmp_path, monkeypatch):
    assert "storage_free_mb" not in TestClient(create_app()).get("/health/ready").json()

    client = _persistent_app(monkeypatch, tmp_path)
    assert client.get("/health/ready").json()["storage_free_mb"] >= 0
    with mock.patch.object(api.os, "access", return_value=False):
        assert client.get("/health/ready").status_code == 503
