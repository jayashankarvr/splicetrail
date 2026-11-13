"""Where the dataset downloader finds a Kaggle token."""

import pytest

from splicetrail.bench import download


@pytest.fixture
def no_token_env(monkeypatch, tmp_path):
    monkeypatch.delenv("KAGGLE_API_TOKEN", raising=False)
    monkeypatch.delenv("KAGGLE_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(download.Path, "home", lambda: tmp_path)
    return tmp_path


def test_reads_access_token_file(no_token_env):
    (no_token_env / ".kaggle").mkdir()
    (no_token_env / ".kaggle" / "access_token").write_text("abc123\n")
    assert download._token() == "abc123"


def test_environment_wins_over_file(no_token_env, monkeypatch):
    (no_token_env / ".kaggle").mkdir()
    (no_token_env / ".kaggle" / "access_token").write_text("from-file")
    monkeypatch.setenv("KAGGLE_ACCESS_TOKEN", "from-env")
    assert download._token() == "from-env"


def test_no_token_anywhere(no_token_env):
    assert download._token() is None


class _FakeResponse:
    def __init__(self, status, body=b""):
        self.status_code = status
        self.ok = status < 400
        self.body = body
        self.closed = False

    def close(self):
        self.closed = True

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size):
        yield self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _zip_bytes():
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("img.png", b"x")
    return buf.getvalue()


def _fake_get(statuses, calls):
    def get(url, headers=None, **kwargs):
        calls.append(headers)
        status = statuses[len(calls) - 1]
        return _FakeResponse(status, _zip_bytes() if status == 200 else b"")

    return get


@pytest.mark.parametrize("status", [401, 403, 404])
def test_rejected_token_retries_without_header(monkeypatch, tmp_path, status, capsys):
    requests = pytest.importorskip("requests")
    calls = []
    monkeypatch.setattr(requests, "get", _fake_get([status, 200], calls))
    download._download_with_token("owner/data", tmp_path, "s3cret-token")
    assert calls[0] == {"Authorization": "Bearer s3cret-token"}
    assert calls[1] is None
    assert (tmp_path / "img.png").exists()
    assert not (tmp_path / "archive.zip").exists()
    out = capsys.readouterr()
    assert "s3cret-token" not in out.out + out.err


def test_rejected_token_and_anonymous_failure_exits(monkeypatch, tmp_path, capsys):
    requests = pytest.importorskip("requests")
    calls = []
    monkeypatch.setattr(requests, "get", _fake_get([404, 404], calls))
    with pytest.raises(SystemExit) as exc:
        download._download_with_token("owner/data", tmp_path, "s3cret-token")
    message = str(exc.value)
    assert "owner/data" in message and "Settings -> API" in message
    assert "KAGGLE_ACCESS_TOKEN" in message
    assert "s3cret-token" not in message
    out = capsys.readouterr()
    assert "s3cret-token" not in out.out + out.err
    assert len(calls) == 2


def test_accepted_token_does_not_retry(monkeypatch, tmp_path):
    requests = pytest.importorskip("requests")
    calls = []
    monkeypatch.setattr(requests, "get", _fake_get([200], calls))
    download._download_with_token("owner/data", tmp_path, "tok")
    assert len(calls) == 1
    assert (tmp_path / "img.png").exists()


def test_server_error_with_token_is_not_retried(monkeypatch, tmp_path):
    requests = pytest.importorskip("requests")
    calls = []
    monkeypatch.setattr(requests, "get", _fake_get([500], calls))
    with pytest.raises(RuntimeError):
        download._download_with_token("owner/data", tmp_path, "tok")
    assert len(calls) == 1
