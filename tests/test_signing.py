"""Parameter signing: signing keys, `pigsty sign`, and runs that only start from a link
whose parameters were signed. Also the text parameters signing makes possible. See
issues #15 and #44."""

import csv
import io
import json
import shutil
import sqlite3
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pyrage import x25519

from psych_ingestor import bech32, db, keys, signing, storage, sweep
from psych_ingestor.admit import RUN_FILE, admit
from psych_ingestor.app import create_app
from psych_ingestor.config import ConfigurationError, load_config
from psych_ingestor.organize import organize
from psych_ingestor.runs import RequestProblem
from psych_ingestor.service import Pig
from psych_ingestor.sty_cli import app as pigsty

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
MODULES = "questions:1,2,3|reels:03,22|sentences:05,52|videolog:1,2"
SIGNED = {"id": "10235", "event": "base", "modules": MODULES}

CONFIG = """
data_root = "./data"
database = "./pig.db"
public_keys_file = "public_keys.txt"

[task.teddy]
path_parameters = ["id", "event"]
run_key_parameters = ["id", "event"]
signed_parameters = ["id", "event", "modules"]
signed_by = ["lucida/signing"]

[task.stroop]
path_parameters = ["participant_id"]
run_key_parameters = ["participant_id"]
"""


@pytest.fixture
def private_keys_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "secure" / "private_keys"
    keys.new_signing_key(directory, "lucida/signing", NOW)
    keys.new_private_key(directory, "lucida/2026a", NOW)
    return directory


@pytest.fixture
def signing_key(private_keys_dir: Path) -> keys.SigningKey:
    return keys.read_signing_keys(private_keys_dir)[0]


@pytest.fixture
def web(tmp_path: Path, private_keys_dir: Path) -> Path:
    directory = tmp_path / "web"
    directory.mkdir()
    (directory / "public_keys.txt").write_text(
        keys.public_keys_text(
            keys.read_all_keys(private_keys_dir), private_keys_dir, NOW
        )
    )
    (directory / "pig.toml").write_text(CONFIG)
    return directory / "pig.toml"


@pytest.fixture
def pig(web: Path) -> Pig:
    config = load_config(web)
    config.in_progress_root.mkdir(parents=True, exist_ok=True)
    return Pig(config, db.connect(config.database))


@pytest.fixture
def secure(private_keys_dir: Path) -> Path:
    path = private_keys_dir.parent / "pigsty.toml"
    path.write_text(
        'private_keys_dir = "./private_keys"\n'
        "[task.teddy]\n"
        'datastore_base = "./teddy"\n'
        'organized = "./organized/teddy/{id}/{event}_{run_number}"\n'
        'signed_parameters = ["id", "event", "modules"]\n'
    )
    return path


def signature_for(signing_key: keys.SigningKey, values: dict = SIGNED) -> str:
    return signing.sign(signing_key, "teddy", values)


def refusal(pig: Pig, submitted: dict, signature: object = None) -> RequestProblem:
    with pytest.raises(RequestProblem) as raised:
        pig.start_run("teddy", submitted, signature)
    return raised.value


# ------------------------------------------------------------------ keys


def test_bech32_reads_and_writes_age_keys_too():
    """The encoding is age's, so it has to agree with age about age's own keys."""
    identity = x25519.Identity.generate()
    public = str(identity.to_public())
    prefix, raw = bech32.decode(public)
    assert (prefix, len(raw)) == ("age", 32)
    assert bech32.encode(prefix, raw) == public
    prefix, raw = bech32.decode(str(identity))
    assert bech32.encode(prefix, raw).upper() == str(identity)


def test_a_mistyped_key_doesnt_decode(signing_key: keys.SigningKey):
    public = signing_key.public_key
    assert public.startswith("pigpk1")
    changed = public[:-1] + ("q" if public[-1] != "q" else "p")
    with pytest.raises(ValueError):
        keys.check_public_key(changed)
    with pytest.raises(ValueError):
        keys.check_public_key(public[:-4])


def test_signing_keys_and_encryption_keys_share_a_directory(private_keys_dir: Path):
    assert [key.name for key in keys.read_private_keys(private_keys_dir)] == [
        "lucida/2026a"
    ]
    assert [key.name for key in keys.read_signing_keys(private_keys_dir)] == [
        "lucida/signing"
    ]
    key_file = private_keys_dir / "lucida" / "signing.key"
    assert key_file.stat().st_mode & 0o077 == 0
    assert "PIG-SIGNING-KEY-1" in key_file.read_text()


def test_a_signing_key_reads_back_the_same(private_keys_dir: Path):
    made = keys.new_signing_key(private_keys_dir, "another", NOW)
    (read,) = [
        k for k in keys.read_signing_keys(private_keys_dir) if k.name == "another"
    ]
    assert read.public_key == made.public_key


def test_a_signing_key_cant_be_used_to_decrypt(private_keys_dir: Path):
    with pytest.raises(ConfigurationError, match="signing key"):
        keys.read_private_key_file(private_keys_dir / "lucida" / "signing.key")


def test_the_public_keys_file_has_both_kinds(web: Path, signing_key: keys.SigningKey):
    public = keys.read_public_keys(web.parent / "public_keys.txt")
    assert public["lucida/signing"] == signing_key.public_key
    assert public["lucida/2026a"].startswith("age1")


def test_pigsty_makes_signing_keys(tmp_path: Path, capsys):
    path = tmp_path / "pigsty.toml"
    path.write_text('private_keys_dir = "./keys"\n')
    pigsty(
        ["keys", "new", "--signing", "lucida/signing", "--config", str(path)],
        result_action="return_value",
    )
    assert "pigpk1" in capsys.readouterr().out
    pigsty(["keys", "export", "--config", str(path)], result_action="return_value")
    assert "lucida/signing pigpk1" in capsys.readouterr().out


# ------------------------------------------------------------------ configuration


def test_a_task_gives_what_it_signs(web: Path):
    described = TestClient(create_app(web)).get("/v1/task/teddy").json()
    assert described["parameters"] == ["id", "event", "modules"]
    assert described["path_parameters"] == ["id", "event"]
    assert described["parameter_signing"] == {
        "signed_parameters": ["id", "event", "modules"]
    }


@pytest.mark.parametrize(
    "old, new, complaint",
    [
        ('signed_by = ["lucida/signing"]\n', "", "needs signed_by"),
        (
            'signed_parameters = ["id", "event", "modules"]\n',
            "",
            "needs signed_parameters",
        ),
        ('["id", "event", "modules"]', '["event", "modules"]', "leaves out id"),
        ('["id", "event", "modules"]', '["id", "event", "id"]', "more than once"),
        ('["lucida/signing"]', '["lucida/2026a"]', "that's an encryption key"),
        ('["lucida/signing"]', '["lucida/nope"]', "no key by that name"),
    ],
)
def test_signing_settings_that_cant_work_are_refused(web: Path, old, new, complaint):
    web.write_text(CONFIG.replace(old, new))
    with pytest.raises(ConfigurationError, match=complaint):
        load_config(web)


# ------------------------------------------------------------------ starting runs


def test_a_signed_link_starts_a_run(pig: Pig, signing_key: keys.SigningKey):
    started = pig.start_run(
        "teddy", {**SIGNED, "nextURL": "https://x"}, signature_for(signing_key)
    )
    assert started["run_number"] == 1

    pig.finalize_run("teddy", started["run_id"])
    sweep.sweep(pig.config, pig.connection)
    manifest = storage.read_manifest(pig.config.done_root / "teddy" / started["run_id"])
    assert manifest["signed_by"] == "lucida/signing"
    assert manifest["parameters"] == SIGNED
    assert manifest["path_parameters"] == ["id", "event"]
    assert manifest["signed_parameters"] == ["id", "event", "modules"]
    # The signature isn't kept; the key's name says what it proved.
    assert manifest["extra_parameters"] == {"nextURL": "https://x"}


def test_a_changed_value_is_refused(pig: Pig, signing_key: keys.SigningKey):
    submitted = {**SIGNED, "modules": "questions:1"}
    problem = refusal(pig, submitted, signature_for(signing_key))
    assert problem.status_code == 403
    assert "doesn't match" in problem.message


@pytest.mark.parametrize(
    "signature, complaint",
    [
        (None, "no signature"),
        ("", "key name"),
        ("lucida/signing.not-a-signature", "can't be read"),
        ("lucida/other." + "A" * 86, "doesn't accept"),
        (12, "text"),
    ],
)
def test_a_signature_that_cant_be_checked_is_refused(
    pig: Pig, signing_key: keys.SigningKey, signature, complaint
):
    problem = refusal(pig, SIGNED, signature)
    assert problem.status_code == 403
    assert complaint in problem.message


def test_a_link_signed_for_another_task_is_refused(
    pig: Pig, signing_key: keys.SigningKey
):
    signature = signing.sign(signing_key, "other_task", SIGNED)
    assert refusal(pig, SIGNED, signature).status_code == 403


def test_a_missing_signed_parameter_is_a_422(pig: Pig, signing_key: keys.SigningKey):
    submitted = {name: SIGNED[name] for name in ("id", "event")}
    problem = refusal(pig, submitted, signature_for(signing_key))
    assert problem.status_code == 422
    assert "'modules'" in problem.message


def test_text_parameters_have_rules_of_their_own(pig: Pig, signing_key):
    for value in ["two\nlines", "x" * 2049]:
        values = {**SIGNED, "modules": value}
        problem = refusal(pig, values, signature_for(signing_key, values))
        assert problem.status_code == 422
    # Path parameters are still held to the safe-value rule, signed or not.
    values = {**SIGNED, "id": "../10235"}
    problem = refusal(pig, values, signature_for(signing_key, values))
    assert problem.status_code == 422
    assert "file name" in problem.message


def test_text_is_kept_exactly_as_it_arrives(pig: Pig, signing_key: keys.SigningKey):
    values = {**SIGNED, "modules": "Café | ünïcode: 1,2"}
    run_id = pig.start_run("teddy", values, signature_for(signing_key, values))[
        "run_id"
    ]
    run = pig._run_for_task("teddy", run_id)
    assert run.parameters == values


def test_a_task_that_doesnt_sign_ignores_a_signature(pig: Pig):
    run_id = pig.start_run("stroop", {"participant_id": "1"}, "x.y")["run_id"]
    assert pig._run_for_task("stroop", run_id).extra_parameters == {}


def test_a_signature_among_the_parameters_doesnt_count(pig: Pig, signing_key):
    """It's only another parameter there, recorded like any extra."""
    submitted = {**SIGNED, "sig": signature_for(signing_key)}
    assert refusal(pig, submitted).status_code == 403


def test_the_api_answers_a_bad_signature_with_403(web: Path, signing_key):
    client = TestClient(create_app(web))
    reply = client.post("/v1/task/teddy/run", json={"parameters": SIGNED})
    assert reply.status_code == 403
    assert "no signature" in reply.json()["message"]
    body = {"parameters": SIGNED, "signature": "lucida/x.AAAA"}
    reply = client.post("/v1/task/teddy/run", json=body)
    assert reply.status_code == 403
    assert "Use the link you were sent" in reply.json()["message"]
    body = {"parameters": SIGNED, "signature": signature_for(signing_key)}
    assert client.post("/v1/task/teddy/run", json=body).status_code == 201


# ------------------------------------------------------------------ pigsty sign


def test_pigsty_sign_signs_what_pigsty_toml_lists(pig: Pig, secure: Path, capsys):
    pigsty(
        ["sign", "teddy", "--key", "lucida/signing", "--config", str(secure)]
        + [f"{name}={value}" for name, value in SIGNED.items()],
        result_action="return_value",
    )
    signature = capsys.readouterr().out.strip()
    assert signature.startswith("lucida/signing.")
    assert pig.start_run("teddy", SIGNED, signature)["run_number"] == 1


def test_pigsty_sign_can_print_a_whole_link(pig: Pig, secure: Path, capsys):
    pigsty(
        ["sign", "teddy", "--key", "lucida/signing", "--config", str(secure)]
        + ["--url", "https://teddy.example.edu/"]
        + [f"{name}={value}" for name, value in SIGNED.items()],
        result_action="return_value",
    )
    link = urllib.parse.urlsplit(capsys.readouterr().out.strip())
    parameters = dict(urllib.parse.parse_qsl(link.query))
    assert parameters["modules"] == MODULES
    signature = parameters.pop("sig")
    assert pig.start_run("teddy", parameters, signature)["run_number"] == 1


@pytest.mark.parametrize(
    "values, complaint",
    [
        (["id=1", "event=base"], "modules"),
        (["id=1", "event=base", "modules=x", "nextURL=y"], "doesn't sign nextURL"),
        (["id"], "a name and a value"),
    ],
)
def test_pigsty_sign_wants_exactly_the_signed_parameters(
    secure: Path, capsys, values, complaint
):
    with pytest.raises(SystemExit):
        pigsty(
            ["sign", "teddy", "--key", "lucida/signing", "--config", str(secure)]
            + values,
            result_action="return_value",
        )
    assert complaint in capsys.readouterr().err


def test_pigsty_sign_needs_a_signing_key_by_name(secure: Path, capsys):
    with pytest.raises(SystemExit):
        pigsty(
            ["sign", "teddy", "--key", "lucida/2026a", "--config", str(secure), "id=1"],
            result_action="return_value",
        )
    assert "There is: lucida/signing" in capsys.readouterr().err


def test_pigsty_sign_signs_a_csv(pig: Pig, secure: Path, tmp_path: Path, capsys):
    roster = tmp_path / "roster.csv"
    roster.write_text(
        "﻿record_id,id,event,modules\n"
        f'1,10235,base,"{MODULES}"\n'
        "2,10236,base,reels:01\n",
        encoding="utf-8",
    )
    pigsty(
        ["sign", "teddy", "--key", "lucida/signing", "--config", str(secure)]
        + ["--csv", str(roster)],
        result_action="return_value",
    )
    rows = list(csv.DictReader(io.StringIO(capsys.readouterr().out)))
    assert [row["record_id"] for row in rows] == ["1", "2"]
    for row in rows:
        values = {name: row[name] for name in ("id", "event", "modules")}
        pig.start_run("teddy", values, row["sig"])


def test_pigsty_sign_can_name_the_signature(secure: Path, tmp_path: Path, capsys):
    roster = tmp_path / "roster.csv"
    roster.write_text("id,event,modules\n10235,base,reels:01\n")
    pigsty(
        ["sign", "teddy", "--key", "lucida/signing", "--config", str(secure)]
        + ["--csv", str(roster), "--name", "signature"],
        result_action="return_value",
    )
    (row,) = csv.DictReader(io.StringIO(capsys.readouterr().out))
    assert row["signature"].startswith("lucida/signing.")


def test_pigsty_sign_refuses_a_csv_without_a_column(
    secure: Path, tmp_path: Path, capsys
):
    roster = tmp_path / "roster.csv"
    roster.write_text("id,event\n1,base\n")
    with pytest.raises(SystemExit):
        pigsty(
            ["sign", "teddy", "--key", "lucida/signing", "--config", str(secure)]
            + ["--csv", str(roster)],
            result_action="return_value",
        )
    captured = capsys.readouterr()
    assert "no column for modules" in captured.err
    assert captured.out == ""


def test_pigsty_check_warns_when_the_signed_lists_differ(
    secure: Path, monkeypatch, capsys
):
    from psych_ingestor import sty_cli

    published = {"teddy": {"parameter_signing": {"signed_parameters": ["id", "event"]}}}
    monkeypatch.setattr(sty_cli, "_task_settings", lambda url, code: published[code])
    secure.write_text('pig_url = "https://pig.example.edu"\n' + secure.read_text())
    pigsty(["check", "--config", str(secure)], result_action="return_value")
    out = capsys.readouterr().out
    assert "Warning: teddy: the web server checks signatures over id, event" in out


# ------------------------------------------------------------------ the secure side


def finished(pig: Pig, signing_key: keys.SigningKey, datastore: Path) -> str:
    run_id = pig.start_run("teddy", SIGNED, signature_for(signing_key))["run_id"]
    pig.store_events("teddy", run_id, {"1": {"data": {"trial": 1}}})
    pig.finalize_run("teddy", run_id)
    sweep.sweep(pig.config, pig.connection)
    shutil.copytree(
        pig.config.done_root / "teddy" / run_id, datastore / "incoming" / run_id
    )
    assert admit("teddy", datastore).admitted == [run_id]
    return run_id


def test_admit_keeps_the_parameter_lists(pig: Pig, signing_key, tmp_path: Path):
    datastore = tmp_path / "teddy"
    run_id = finished(pig, signing_key, datastore)
    run = json.loads((datastore / "runs" / run_id / RUN_FILE).read_text())
    assert run["run_file_version"] == 2
    assert run["path_parameters"] == ["id", "event"]
    assert run["signed_parameters"] == ["id", "event", "modules"]
    assert run["signed_by"] == "lucida/signing"


def test_organize_wont_put_text_in_a_path(pig: Pig, signing_key, tmp_path: Path):
    datastore = tmp_path / "teddy"
    finished(pig, signing_key, datastore)
    tree = tmp_path / "tree"
    report = organize("teddy", datastore, tree, "{id}/{modules}_{run_number}")
    (why,) = report.errors.values()
    assert "isn't one of this task's path_parameters" in why
    report = organize("teddy", datastore, tree, "{id}/{event}_{run_number}")
    assert report.errors == {}


def test_admit_reads_a_manifest_from_before_signing(
    pig: Pig, signing_key, tmp_path: Path
):
    """A version 3 manifest has `run_key`, and every parameter is a path parameter."""
    run_id = pig.start_run("stroop", {"participant_id": "10351"})["run_id"]
    pig.finalize_run("stroop", run_id)
    sweep.sweep(pig.config, pig.connection)
    incoming = tmp_path / "stroop" / "incoming" / run_id
    shutil.copytree(pig.config.done_root / "stroop" / run_id, incoming)
    manifest = storage.read_manifest(incoming)
    for name in ("path_parameters", "signed_parameters", "signed_by"):
        del manifest[name]
    manifest["run_key"] = manifest.pop("run_key_parameters")
    manifest["manifest_version"] = 3
    (incoming / storage.MANIFEST_FILE).write_text(json.dumps(manifest))

    assert admit("stroop", tmp_path / "stroop").admitted == [run_id]
    run = json.loads((tmp_path / "stroop" / "runs" / run_id / RUN_FILE).read_text())
    assert run["run_key_parameters"] == ["participant_id"]
    assert run["path_parameters"] == ["participant_id"]
    assert run["signed_parameters"] == []
    assert run["signed_by"] is None


def test_migrating_gives_old_runs_their_path_parameters(tmp_path: Path):
    path = tmp_path / "pig.db"
    connection = db.connect(path)
    connection.execute("ALTER TABLE runs DROP COLUMN signed_by")
    for column in ("path_parameter_names", "signed_parameter_names"):
        connection.execute(f"ALTER TABLE run_identifiers DROP COLUMN {column}")
    connection.execute(
        "INSERT INTO runs (run_id, task_code, run_key_hash, run_number, phase, "
        "started_at) VALUES ('r1', 'stroop', 'h', 1, 'collecting', '2026-10-24')"
    )
    connection.execute(
        "INSERT INTO run_identifiers VALUES ('r1', ?, '{}', ?)",
        (json.dumps({"participant_id": "1", "session": "a"}), '["participant_id"]'),
    )
    connection.execute("PRAGMA user_version = 3")
    connection.close()

    db.migrate(path)
    row = (
        sqlite3.connect(path)
        .execute(
            "SELECT path_parameter_names, signed_parameter_names FROM run_identifiers"
        )
        .fetchone()
    )
    assert json.loads(row[0]) == ["participant_id", "session"]
    assert json.loads(row[1]) == []
