"""End-to-end encryption: public keys on the web side, ciphertext refused or taken, and
`pigsty admit` decrypting on the way into `runs/`. See issue #10."""

import base64
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pyrage
import pytest
from fastapi.testclient import TestClient
from pyrage import x25519

from psych_ingestor import db, keys, storage, sweep
from psych_ingestor.admit import RUN_FILE, admit
from psych_ingestor.app import create_app
from psych_ingestor.config import ConfigSource, ConfigurationError, load_config
from psych_ingestor.service import Pig
from psych_ingestor.sty_cli import app as pigsty

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
PARTICIPANT = {"participant_id": "10351"}

CONFIG = """
data_root = "./data"
database = "./pig.db"
public_keys_file = "public_keys.txt"

[task.secret]
path_parameters = ["participant_id"]
run_key_parameters = ["participant_id"]
media = true
encrypt_to = "lucida/2026a"

[task.plain]
path_parameters = ["participant_id"]
run_key_parameters = ["participant_id"]
"""


@pytest.fixture
def private_keys_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "secure" / "private_keys"
    keys.new_private_key(directory, "lucida/2026a", NOW)
    return directory


@pytest.fixture
def private_key(private_keys_dir: Path) -> keys.PrivateKey:
    return keys.read_private_keys(private_keys_dir)[0]


@pytest.fixture
def web(tmp_path: Path, private_keys_dir: Path) -> Path:
    """A web side whose public keys file was exported from `private_keys_dir`."""
    directory = tmp_path / "web"
    directory.mkdir()
    (directory / "public_keys.txt").write_text(
        keys.public_keys_text(
            keys.read_private_keys(private_keys_dir), private_keys_dir, NOW
        )
    )
    (directory / "pig.toml").write_text(CONFIG)
    return directory / "pig.toml"


@pytest.fixture
def pig(web: Path) -> Pig:
    config = load_config(web)
    config.in_progress_root.mkdir(parents=True, exist_ok=True)
    return Pig(config, db.connect(config.database))


def encrypted(plaintext: bytes, private_key: keys.PrivateKey) -> bytes:
    return pyrage.encrypt(
        plaintext, [x25519.Recipient.from_str(private_key.public_key)]
    )


def encrypted_data(data: object, private_key: keys.PrivateKey) -> str:
    """An event's `data` as the browser sends it for an encrypted task."""
    plaintext = json.dumps(data).encode("utf-8")
    return base64.b64encode(encrypted(plaintext, private_key)).decode("ascii")


def downloaded(pig: Pig, run_id: str, datastore: Path) -> Path:
    """Finish a run on the web side and copy it into the datastore's incoming/."""
    pig.finalize_run("secret", run_id)
    sweep.sweep(pig.config, pig.connection)
    incoming = datastore / "incoming" / run_id
    shutil.copytree(pig.config.done_root / "secret" / run_id, incoming)
    return incoming


# ------------------------------------------------------------------ the web side


def test_a_task_gives_the_public_key_it_encrypts_to(
    web: Path, private_key: keys.PrivateKey
):
    client = TestClient(create_app(web))
    assert client.get("/v1/task/secret").json()["encryption"] == {
        "public_key": private_key.public_key
    }
    assert client.get("/v1/task/plain").json()["encryption"] is None


def test_encrypt_to_needs_a_public_keys_file(tmp_path: Path):
    path = tmp_path / "pig.toml"
    path.write_text(CONFIG.replace('public_keys_file = "public_keys.txt"\n', ""))
    with pytest.raises(ConfigurationError, match="no public_keys_file"):
        load_config(path)


def test_encrypt_to_has_to_name_a_key_in_the_file(web: Path):
    web.write_text(CONFIG.replace('"lucida/2026a"', '"lucida/2027"'))
    with pytest.raises(ConfigurationError, match="no key by that name"):
        load_config(web)


def test_a_public_key_cut_short_is_refused(web: Path):
    keys_file = web.parent / "public_keys.txt"
    text = keys_file.read_text()
    keys_file.write_text(text[:-6] + "\n")
    with pytest.raises(ConfigurationError, match="cut short"):
        load_config(web)


def test_a_new_public_keys_file_takes_effect_without_a_restart(
    web: Path, tmp_path: Path
):
    source = ConfigSource(web)
    first = source.current().task["secret"].public_key
    other = keys.new_private_key(tmp_path / "other", "lucida/2026a", NOW)
    keys_file = web.parent / "public_keys.txt"
    keys_file.write_text(keys.public_keys_text([other], tmp_path / "other", NOW))
    stat = keys_file.stat()
    os.utime(keys_file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

    source.current()  # The first load didn't know about the keys file.
    assert source.current().task["secret"].public_key == other.public_key != first


def test_an_encrypted_run_refuses_plaintext_events(pig: Pig):
    run_id = pig.start_run("secret", PARTICIPANT)["run_id"]
    result = pig.store_events("secret", run_id, {"1": {"data": {"trial": 1}}})
    assert result.stored == []
    assert result.errors["1"]["can_retry"] is False
    assert "isn't" in result.errors["1"]["message"]


def test_an_encrypted_run_takes_ciphertext(pig: Pig, private_key: keys.PrivateKey):
    run_id = pig.start_run("secret", PARTICIPANT)["run_id"]
    data = encrypted_data({"trial": 1}, private_key)
    result = pig.store_events("secret", run_id, {"1": {"data": data}})
    assert result.stored == ["1"]


def test_a_run_stays_encrypted_whatever_the_task_does(
    pig: Pig, private_key: keys.PrivateKey
):
    run_id = pig.start_run("secret", PARTICIPANT)["run_id"]
    pig.config.task["secret"].encrypt_to = None
    pig.config.task["secret"].public_key = None
    result = pig.store_events("secret", run_id, {"1": {"data": {"trial": 1}}})
    assert result.stored == []


def test_an_encrypted_run_refuses_a_plaintext_media_start(pig: Pig):
    run_id = pig.start_run("secret", PARTICIPANT)["run_id"]
    refused = pig.start_media(
        "secret", run_id, {"event_id": "clip", "data": {"prompt": 3}}
    )
    assert refused.status_code == 422


def test_an_encrypted_run_refuses_plaintext_parts(
    web: Path, private_key: keys.PrivateKey
):
    client = TestClient(create_app(web))
    run_id = client.post(
        "/v1/task/secret/run", json={"parameters": PARTICIPANT}
    ).json()["run_id"]
    started = client.post(
        f"/v1/task/secret/run/{run_id}/media",
        json={"event_id": "clip", "data": encrypted_data({"p": 3}, private_key)},
    )
    assert started.status_code == 201
    media_id = started.json()["media_id"]
    part = f"/v1/task/secret/run/{run_id}/media/{media_id}"

    assert client.put(f"{part}/1", content=b"plain bytes").status_code == 422
    sent = client.put(f"{part}/1", content=encrypted(b"audio", private_key))
    assert sent.status_code == 201


def test_the_manifest_says_what_the_run_was_encrypted_to(
    pig: Pig, private_key: keys.PrivateKey
):
    run_id = pig.start_run("secret", PARTICIPANT)["run_id"]
    pig.finalize_run("secret", run_id)
    sweep.sweep(pig.config, pig.connection)
    manifest = storage.read_manifest(pig.config.done_root / "secret" / run_id)
    assert manifest["encrypted_to"] == "lucida/2026a"


# ------------------------------------------------------------------ keys


def test_a_new_key_is_private_and_age_can_read_it(private_key: keys.PrivateKey):
    assert private_key.path.stat().st_mode & 0o777 == 0o600
    text = private_key.path.read_text()
    assert f"# public key: {private_key.public_key}\n" in text
    assert "pyrage" in text
    assert text.splitlines()[-1].startswith("AGE-SECRET-KEY-1")


def test_a_key_is_never_replaced(private_keys_dir: Path):
    with pytest.raises(FileExistsError):
        keys.new_private_key(private_keys_dir, "lucida/2026a", NOW)


def test_a_key_others_can_read_is_refused(private_key: keys.PrivateKey):
    private_key.path.chmod(0o640)
    with pytest.raises(ConfigurationError, match="chmod 600"):
        keys.read_private_keys(private_key.path.parent.parent)


def test_export_lists_every_key_by_name(private_keys_dir: Path):
    keys.new_private_key(private_keys_dir, "other", NOW)
    exported = keys.public_keys_text(
        keys.read_private_keys(private_keys_dir), private_keys_dir, NOW
    )
    names = [line.split()[0] for line in exported.splitlines() if line[0] != "#"]
    assert names == ["lucida/2026a", "other"]


def test_pigsty_makes_and_exports_keys(tmp_path: Path, capsys):
    path = tmp_path / "pigsty.toml"
    path.write_text('private_keys_dir = "./keys"\n')
    pigsty(
        ["keys", "new", "lucida/2026b", "--config", str(path)],
        result_action="return_value",
    )
    assert (tmp_path / "keys" / "lucida" / "2026b.key").exists()
    capsys.readouterr()

    pigsty(["keys", "export", "--config", str(path)], result_action="return_value")
    exported = capsys.readouterr().out
    keys_file = tmp_path / "public_keys.txt"
    keys_file.write_text(exported)
    assert list(keys.read_public_keys(keys_file)) == ["lucida/2026b"]


# ------------------------------------------------------------------ admitting


def test_admit_decrypts_events_and_parts(
    pig: Pig, private_key: keys.PrivateKey, tmp_path: Path
):
    run_id = pig.start_run("secret", PARTICIPANT)["run_id"]
    pig.store_events(
        "secret",
        run_id,
        {"1": {"data": encrypted_data({"trial": 1, "rt": 523}, private_key)}},
    )
    started = pig.start_media(
        "secret",
        run_id,
        {"event_id": "clip", "data": encrypted_data({"prompt": 3}, private_key)},
    )
    media_id = started.body["media_id"]
    part = pig.config.in_progress_root / "secret" / run_id / "media"
    part = part / f"{media_id:05d}" / "000001.part"
    part.write_bytes(encrypted(b"audio bytes", private_key))
    datastore = tmp_path / "datastore"
    incoming = downloaded(pig, run_id, datastore)

    report = admit("secret", datastore, [private_key])

    assert report.problems == {}
    assert report.admitted == [run_id]
    admitted = datastore / "runs" / run_id
    lines = storage.read_lines(admitted / "events.jsonl")
    assert [line["data"] for line in lines] == [{"trial": 1, "rt": 523}, {"prompt": 3}]
    assert lines[1]["metadata"]["media_id"] == media_id
    assert (admitted / part.relative_to(part.parents[2])).read_bytes() == b"audio bytes"
    run = json.loads((admitted / RUN_FILE).read_text())
    assert run["encrypted_to"] == "lucida/2026a"
    assert run["decrypted_with"] == ["lucida/2026a"]
    # Nothing in incoming/ changes, and no scratch is left behind.
    assert (incoming / "manifest.json").exists()
    assert sorted(path.name for path in (datastore / "runs").iterdir()) == [run_id]


def test_admit_can_keep_the_ciphertext(
    pig: Pig, private_key: keys.PrivateKey, tmp_path: Path
):
    run_id = pig.start_run("secret", PARTICIPANT)["run_id"]
    pig.store_events(
        "secret", run_id, {"1": {"data": encrypted_data({"trial": 1}, private_key)}}
    )
    datastore = tmp_path / "datastore"
    incoming = downloaded(pig, run_id, datastore)
    kept = tmp_path / "ciphertext"

    admit("secret", datastore, [private_key], kept)

    assert (kept / run_id / "events.jsonl").read_bytes() == (
        incoming / "events.jsonl"
    ).read_bytes()
    assert (kept / run_id / "manifest.json").exists()
    assert [path.name for path in kept.iterdir()] == [run_id]


def test_a_run_no_key_opens_isnt_admitted(
    pig: Pig, private_key: keys.PrivateKey, tmp_path: Path
):
    run_id = pig.start_run("secret", PARTICIPANT)["run_id"]
    pig.store_events(
        "secret", run_id, {"1": {"data": encrypted_data({"trial": 1}, private_key)}}
    )
    datastore = tmp_path / "datastore"
    downloaded(pig, run_id, datastore)
    stranger = keys.new_private_key(tmp_path / "elsewhere", "stranger", NOW)

    report = admit("secret", datastore, [stranger], tmp_path / "ciphertext")

    assert report.admitted == []
    assert "no private key" in next(iter(report.problems.values()))
    assert list((datastore / "runs").iterdir()) == []
    assert list((tmp_path / "ciphertext").iterdir()) == []


def test_an_encrypted_run_needs_private_keys(pig: Pig, tmp_path: Path):
    run_id = pig.start_run("secret", PARTICIPANT)["run_id"]
    datastore = tmp_path / "datastore"
    downloaded(pig, run_id, datastore)

    report = admit("secret", datastore, [])

    assert "private_keys_dir" in next(iter(report.problems.values()))


def test_a_cut_short_line_is_kept_as_it_is(
    pig: Pig, private_key: keys.PrivateKey, tmp_path: Path
):
    run_id = pig.start_run("secret", PARTICIPANT)["run_id"]
    events = pig.config.in_progress_root / "secret" / run_id / "events.jsonl"
    events.write_text('{"event_id": "1", "da\n')
    pig.store_events(
        "secret", run_id, {"2": {"data": encrypted_data({"trial": 2}, private_key)}}
    )
    datastore = tmp_path / "datastore"
    downloaded(pig, run_id, datastore)

    admit("secret", datastore, [private_key])

    admitted = (datastore / "runs" / run_id / "events.jsonl").read_text()
    assert admitted.splitlines()[0] == '{"event_id": "1", "da'
    assert json.loads(admitted.splitlines()[1])["data"] == {"trial": 2}


def test_pigsty_check_warns_about_a_public_key_no_private_key_matches(
    private_keys_dir: Path, private_key: keys.PrivateKey, monkeypatch, capsys
):
    from psych_ingestor import sty_cli

    stranger = x25519.Identity.generate().to_public()
    published = {
        "secret": {"encryption": {"public_key": private_key.public_key}},
        "other": {"encryption": {"public_key": str(stranger)}},
    }
    monkeypatch.setattr(sty_cli, "_task_settings", lambda url, code: published[code])
    path = private_keys_dir.parent / "pigsty.toml"
    path.write_text(
        'pig_url = "https://pig.example.edu"\n'
        'private_keys_dir = "./private_keys"\n'
        '[task.secret]\ndatastore_base = "./a"\norganized = "./o/a/{run_number}"\n'
        '[task.other]\ndatastore_base = "./b"\norganized = "./o/b/{run_number}"\n'
    )

    pigsty(["check", "--config", str(path)], result_action="return_value")

    out = capsys.readouterr().out
    assert "secret: encrypted to lucida/2026a, which is here." in out
    assert "Warning: other:" in out
