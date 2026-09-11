"""Read-only relay exports using temporary, synthetic auth files only."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from web.routes import cliproxy as relay


def auth(email="relay-only@example.test", workspace="team-one", **extra):
    return {
        "email": email, "account_id": workspace, "plan_type": "team", "type": "codex",
        "access_token": "synthetic-access", "refresh_token": "synthetic-refresh",
        "unknown_extension": {"flag": True, "nested": [1, "原样保留"]}, **extra,
    }


@pytest.fixture
def environment(tmp_path, monkeypatch):
    directory = tmp_path / "auths"
    directory.mkdir()
    monkeypatch.setattr(relay, "AUTHS_DIR", directory)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("export must not refresh authentication or quota")

    for name in ("_fetch_usage", "_refresh_tokens", "_apply_token_refresh", "_quota_for_file"):
        monkeypatch.setattr(relay, name, forbidden)
    app = FastAPI()
    app.include_router(relay.router)
    with TestClient(app) as client:
        yield directory, client


def put(directory, name, record=None, raw=None):
    content = raw if raw is not None else json.dumps(record or auth(), ensure_ascii=False, indent=3).encode("utf-8") + b"\r\n"
    (directory / name).write_bytes(content)
    return content


def preview(client, emails=None, all_files=False):
    response = client.post("/api/cliproxy/export-preview", json={"emails": emails or [], "all": all_files})
    assert response.status_code == 200
    return response.json()


def track_temporary_exports(monkeypatch):
    created = []
    original = relay.tempfile.TemporaryDirectory

    def tracked(*args, **kwargs):
        temporary = original(*args, **kwargs)
        created.append(Path(temporary.name))
        return temporary

    monkeypatch.setattr(relay.tempfile, "TemporaryDirectory", tracked)
    return created


def test_preview_matches_relay_only_email_exactly_and_retains_separate_workspaces(environment):
    directory, client = environment
    put(directory, "one.json", auth("Person@Example.Test", "workspace-one"))
    put(directory, "two.json", auth("person@example.test", "workspace-two"))
    put(directory, "other.json", auth("other-person@example.test", "other"))
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    data = preview(client, [" PERSON@example.test ", "person@example.test", "missing@example.test"])
    assert data["configured"] is True
    assert data["total_files"] == 3
    assert data["matched_emails"] == 1
    assert data["matched_files"] == 2
    assert data["not_found"] == ["missing@example.test"]
    assert [item["file"] for item in data["items"]] == ["one.json", "two.json"]
    assert {item["account_id"] for item in data["items"]} == {"workspace-one", "workspace-two"}
    assert all(item["valid"] and item["plan_type"] == "team" for item in data["items"])
    assert all(item["revision"] == hashlib.sha256(before[item["file"]]).hexdigest() for item in data["items"])
    encoded = json.dumps(data)
    assert "synthetic-access" not in encoded
    assert "synthetic-refresh" not in encoded
    assert "unknown_extension" not in encoded
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before


def test_empty_selection_is_empty_and_only_explicit_all_lists_all(environment):
    directory, client = environment
    put(directory, "one.json")
    put(directory, "broken.json", raw=b'{"access_token":"synthetic-private-fragment", invalid')
    data = preview(client, [" ", ""])
    assert data["total_files"] == 2
    assert data["items"] == []
    assert data["matched_files"] == 0
    all_data = preview(client, all_files=True)
    assert len(all_data["items"]) == 2
    assert all_data["matched_files"] == 1
    assert all_data["invalid_files"] == 1


@pytest.mark.parametrize("raw", [
    b'{"access_token":"synthetic-private-fragment", invalid',
    b"[1, 2]", b"null", b"{\"value\": NaN}", b"\xffsynthetic-private-fragment",
])
def test_invalid_json_is_visible_and_diagnostics_never_contain_source_fragments(environment, raw):
    directory, client = environment
    put(directory, "broken.json", raw=raw)
    data = preview(client, ["missing@example.test"])
    assert data["not_found"] == ["missing@example.test"]
    assert data["items"][0]["valid"] is False
    assert data["items"][0]["file"] == "broken.json"
    assert data["items"][0]["email"] == ""
    assert "synthetic-private-fragment" not in json.dumps(data)
    response = client.post("/api/cliproxy/export-files", json={"files": ["broken.json"], "format": "original"})
    assert response.status_code == 422
    assert "synthetic-private-fragment" not in response.text


def test_original_single_preserves_exact_bytes_filename_and_cleans_private_snapshot(environment, monkeypatch):
    directory, client = environment
    raw = b"\xef\xbb\xbf" + json.dumps(auth(), ensure_ascii=False, indent=4).encode("utf-8") + b"\r\n\r\n"
    put(directory, "original.json", raw=raw)
    revision = preview(client, all_files=True)["items"][0]["revision"]
    created = track_temporary_exports(monkeypatch)
    response = client.post("/api/cliproxy/export-files", json={
        "files": ["original.json"], "format": "original", "revisions": {"original.json": revision},
    })
    assert response.status_code == 200
    assert response.content == raw
    assert response.headers["content-type"].startswith("application/json")
    assert "original.json" in response.headers["content-disposition"]
    assert response.headers["x-exported-count"] == "1"
    assert response.headers["cache-control"] == "no-store"
    assert created and all(not path.exists() for path in created)
    assert (directory / "original.json").read_bytes() == raw


def test_original_multiple_is_a_zip_with_each_workspace_and_exact_original_bytes(environment, monkeypatch):
    directory, client = environment
    originals = {
        "team-one.json": put(directory, "team-one.json", auth(workspace="one")),
        "team-two.json": put(directory, "team-two.json", auth(workspace="two")),
    }
    created = track_temporary_exports(monkeypatch)
    response = client.post("/api/cliproxy/export-files", json={"files": list(originals), "format": "original"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["x-exported-count"] == "2"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.namelist() == list(originals)
        assert {name: archive.read(name) for name in archive.namelist()} == originals
    assert created and all(not path.exists() for path in created)


def test_merged_preserves_unknown_fields_and_sub2_uses_existing_converter(environment):
    directory, client = environment
    record = auth(custom_nested={"unexpected": [False, {"value": "保留"}]})
    put(directory, "one.json", record)
    merged = client.post("/api/cliproxy/export-files", json={"files": ["one.json"], "format": "merged"})
    assert merged.status_code == 200
    assert merged.json() == [record]
    converted = client.post("/api/cliproxy/export-files", json={"files": ["one.json"], "format": "sub2"})
    assert converted.status_code == 200
    data = converted.json()
    assert data["proxies"] == []
    assert len(data["accounts"]) == 1
    credentials = data["accounts"][0]["credentials"]
    assert credentials["email"] == record["email"]
    assert credentials["access_token"] == record["access_token"]
    assert credentials["chatgpt_account_id"] == record["account_id"]


@pytest.mark.parametrize("name", ["../outside.json", "/outside.json", "nested/a.json", "nested\\a.json", "C:\\outside.json", "a.json:stream", "\x00.json", "", "plain.txt"])
def test_unsafe_filenames_are_rejected_without_partial_export(environment, name):
    directory, client = environment
    put(directory, "valid.json")
    response = client.post("/api/cliproxy/export-files", json={"files": ["valid.json", name], "format": "original"})
    assert response.status_code == 400
    assert "content-disposition" not in response.headers


def test_empty_duplicate_and_unknown_format_requests_never_expand_to_all(environment):
    directory, client = environment
    put(directory, "one.json")
    assert client.post("/api/cliproxy/export-files", json={"files": [], "format": "original"}).status_code == 422
    assert client.post("/api/cliproxy/export-files", json={"files": ["one.json", "one.json"], "format": "original"}).status_code == 400
    assert client.post("/api/cliproxy/export-files", json={"files": ["one.json"], "format": "all"}).status_code == 422


def test_missing_selected_file_aborts_everything_and_cleans_snapshots(environment, monkeypatch):
    directory, client = environment
    put(directory, "exists.json")
    created = track_temporary_exports(monkeypatch)
    response = client.post("/api/cliproxy/export-files", json={"files": ["exists.json", "deleted.json"], "format": "original"})
    assert response.status_code == 409
    assert "本次未导出" in response.json()["detail"]
    assert "content-disposition" not in response.headers
    assert created and all(not path.exists() for path in created)


def test_file_changed_after_preview_rejects_stale_selection(environment, monkeypatch):
    directory, client = environment
    put(directory, "selected.json", auth(workspace="old"))
    original_revision = preview(client, all_files=True)["items"][0]["revision"]
    put(directory, "selected.json", auth(workspace="different"))
    created = track_temporary_exports(monkeypatch)
    response = client.post("/api/cliproxy/export-files", json={
        "files": ["selected.json"], "format": "original", "revisions": {"selected.json": original_revision},
    })
    assert response.status_code == 409
    assert "文件已变化" in response.json()["detail"]
    assert created and all(not path.exists() for path in created)
    missing_revision = client.post("/api/cliproxy/export-files", json={"files": ["selected.json"], "revisions": {}})
    assert missing_revision.status_code == 409


def test_symlink_is_visible_as_unsafe_and_never_reads_or_exports_the_target(environment, tmp_path):
    directory, client = environment
    target = tmp_path / "outside.json"
    target.write_text(json.dumps(auth("outside@example.test")), encoding="utf-8")
    try:
        os.symlink(target, directory / "linked.json")
    except (OSError, NotImplementedError):
        pytest.skip("This OS does not grant symlink creation permission")
    data = preview(client, all_files=True)
    assert data["items"][0]["valid"] is False
    assert data["items"][0]["email"] == ""
    assert "outside@example.test" not in json.dumps(data)
    response = client.post("/api/cliproxy/export-files", json={"files": ["linked.json"], "format": "original"})
    assert response.status_code == 400


def test_profile_claim_email_is_metadata_only_and_still_exactly_matched(environment):
    directory, client = environment
    claims = {"https://api.openai.com/profile": {"email": "CLAIM@example.test"}, "https://api.openai.com/auth": {"chatgpt_account_id": "claim-team", "chatgpt_plan_type": "team"}}
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    put(directory, "claim.json", {"access_token": f"synthetic.{encoded}.signature"})
    data = preview(client, ["claim@example.test"])
    assert data["matched_emails"] == 1
    assert data["items"][0]["account_id"] == "claim-team"
    assert data["items"][0]["email"] == "claim@example.test"
    assert "access_token" not in json.dumps(data)


def test_missing_auth_directory_reports_configuration_without_creating_it(environment, monkeypatch, tmp_path):
    _directory, client = environment
    missing = tmp_path / "not-configured"
    monkeypatch.setattr(relay, "AUTHS_DIR", missing)
    data = preview(client, ["missing@example.test"])
    assert data["configured"] is False
    assert data["items"] == []
    assert data["not_found"] == ["missing@example.test"]
    assert client.post("/api/cliproxy/export-files", json={"files": ["one.json"]}).status_code == 503
    assert not missing.exists()


def test_client_disconnect_also_cleans_the_temporary_export(environment):
    directory, _client = environment
    put(directory, "one.json")
    response = relay._export_files_sync(relay.RelayExportFilesRequest(files=["one.json"]))
    temporary = Path(response._temporary.name)
    assert temporary.exists()

    async def receive():
        return {"type": "http.disconnect"}

    async def send(_message):
        raise RuntimeError("synthetic client disconnect")

    with pytest.raises(RuntimeError, match="synthetic client disconnect"):
        asyncio.run(response({"type": "http", "method": "GET", "headers": []}, receive, send))
    assert not temporary.exists()


def test_legacy_export_routes_keep_their_existing_response_shape(environment):
    directory, client = environment
    record = auth()
    put(directory, "one.json", record)
    assert client.get("/api/cliproxy/export-cpa", params={"files": "one.json"}).json() == [record]
    assert len(client.get("/api/cliproxy/export-sub2", params={"files": "one.json"}).json()["accounts"]) == 1
