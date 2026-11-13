"""What pig-prepare makes from a small organized tree, built here the way `pigsty
organize` lays one out: pig-run.json, events.jsonl, and media/{event_id}/ parts."""

import csv
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from pig_prepare.prepare import main, media_name

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def make_run(
    root: Path,
    run_id: str,
    events: list[dict],
    media: list[dict] | None = None,
    parts: dict[str, list[bytes]] | None = None,
    run_number: int = 1,
    participant_id: str = "10351",
    session: str = "baseline",
) -> Path:
    run_dir = root / "circe" / session / f"{participant_id}_{run_number:04d}"
    run_dir.mkdir(parents=True)
    run_file = {
        "type": "pig_run",
        "run_file_version": 2,
        "run_id": run_id,
        "task_code": "circe",
        "run_number": run_number,
        "run_key_parameters": ["participant_id", "session"],
        "path_parameters": ["participant_id", "session"],
        "signed_parameters": [],
        "disposition": "finalized",
        "started_at": "2026-11-13T15:00:00+00:00",
        "closed_at": "2026-11-13T15:10:00+00:00",
        "done_at": "2026-11-13T15:11:00+00:00",
        "parameters": {"participant_id": participant_id, "session": session},
        "extra_parameters": {"utm_source": "email"},
        "media": media or [],
        "signed_by": None,
        "encrypted_to": None,
        "decrypted_with": [],
    }
    (run_dir / "pig-run.json").write_text(json.dumps(run_file))
    with open(run_dir / "events.jsonl", "w") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")
    for event_id, contents in (parts or {}).items():
        directory = run_dir / "media" / event_id
        directory.mkdir(parents=True)
        for number, content in enumerate(contents, 1):
            (directory / f"{number:06d}.part").write_bytes(content)
    return run_dir


def event(event_id: str, data, media_id: int | None = None) -> dict:
    metadata = {"stored_at": "2026-11-13T15:01:00+00:00"}
    if media_id is not None:
        metadata["media_id"] = media_id
    return {"event_id": event_id, "data": data, "metadata": metadata}


def read_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_events_become_rows_with_the_run_and_its_parameters(tmp_path):
    organized = tmp_path / "organized"
    make_run(
        organized,
        "run-a",
        [
            event("1", {"type": "trial", "rt_ms": 512, "stimulus": {"word": "owl"}}),
            event(
                "0",
                {
                    "type": "started",
                    "_client": {"wall_time": "w", "performance_now": 1.5},
                },
            ),
            event("1", {"type": "trial", "rt_ms": 512, "stimulus": {"word": "owl"}}),
        ],
    )

    assert main([str(organized), str(tmp_path / "prepared")]) == 0

    rows = read_csv(tmp_path / "prepared/circe/10351/baseline/events.csv")
    assert [r["event_id"] for r in rows] == ["0", "1"]  # in order, the repeat dropped
    assert rows[0]["_client.performance_now"] == "1.5"
    assert rows[1]["stimulus.word"] == "owl"
    assert rows[1]["participant_id"] == "10351"
    assert rows[1]["utm_source"] == "email"
    assert rows[1]["stored_at"] == "2026-11-13T15:01:00+00:00"

    index = read_csv(tmp_path / "prepared/runs.csv")
    assert [(r["run_id"], r["path"], r["disposition"]) for r in index] == [
        ("run-a", "circe/10351/baseline", "finalized")
    ]
    combined = read_csv(tmp_path / "prepared/events.csv")
    assert len(combined) == 2


def test_media_is_named_by_data_name_then_event_id(tmp_path):
    organized = tmp_path / "organized"
    make_run(
        organized,
        "run-a",
        [
            event(
                "0", {"content_type": "image/png", "name": "drawing.png"}, media_id=1
            ),
            event("1", {"content_type": "image/png", "name": "drawing"}, media_id=2),
            event("2", {"content_type": "image/png"}, media_id=3),
        ],
        media=[
            {"media_id": 1, "event_id": "0", "finished": True, "parts": 2},
            {"media_id": 2, "event_id": "1", "finished": True, "parts": 1},
            {"media_id": 3, "event_id": "2", "finished": True, "parts": 1},
        ],
        parts={"0": [b"ab", b"cd"], "1": [b"ef"], "2": [b"gh"]},
    )

    assert main([str(organized), str(tmp_path / "prepared")]) == 0

    out = tmp_path / "prepared/circe/10351/baseline"
    assert (out / "drawing.png").read_bytes() == b"abcd"
    assert (out / "drawing_take2.png").read_bytes() == b"ef"
    assert (out / "media_2.png").read_bytes() == b"gh"
    rows = read_csv(out / "events.csv")
    assert [r["media_file"] for r in rows] == [
        "drawing.png",
        "drawing_take2.png",
        "media_2.png",
    ]
    combined = read_csv(tmp_path / "prepared/events.csv")
    assert combined[0]["media_file"] == "circe/10351/baseline/drawing.png"


def test_media_name_makes_any_name_safe_in_a_path():
    assert media_name("7", {"name": "Prompt 3: café"}) == "Prompt_3_caf_"
    assert media_name("7", {"name": "../../etc/passwd"}) == "_etc_passwd"
    assert media_name("7", {"name": ""}) == "media_7"
    assert media_name("7", ["not", "an", "object"]) == "media_7"


def test_a_second_run_gets_its_number_and_a_prepared_run_is_skipped(tmp_path):
    organized = tmp_path / "organized"
    make_run(organized, "run-a", [event("0", {"n": 1})])
    make_run(organized, "run-b", [event("0", {"n": 2})], run_number=2)
    prepared = tmp_path / "prepared"

    assert main([str(organized), str(prepared)]) == 0
    assert (prepared / "circe/10351/baseline_0002/events.csv").exists()

    (prepared / "circe/10351/baseline/events.csv").write_text("left alone\n")
    assert main([str(organized), str(prepared)]) == 0
    assert (prepared / "circe/10351/baseline/events.csv").read_text() == "left alone\n"


def test_a_parameter_that_collides_with_an_event_field_is_a_problem(tmp_path):
    organized = tmp_path / "organized"
    make_run(organized, "run-a", [event("0", {"utm_source": "x"})])

    with pytest.raises(SystemExit) as exited:
        main([str(organized), str(tmp_path / "prepared")])
    assert "1 problem" in str(exited.value)


def test_a_directory_without_pig_run_json_is_not_a_run(tmp_path):
    organized = tmp_path / "organized"
    (organized / "circe" / "stray").mkdir(parents=True)
    (organized / "circe" / "stray" / "manifest.json").write_text("{}")

    assert main([str(organized), str(tmp_path / "prepared")]) == 0
    assert read_csv(tmp_path / "prepared/events.csv") == []


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg and ffprobe")
def test_a_recording_in_parts_is_joined_and_remuxed(tmp_path):
    recording = tmp_path / "source.webm"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=d=1",
            "-c:a",
            "libopus",
            "-f",
            "webm",
            recording,
        ],
        check=True,
    )
    whole = recording.read_bytes()
    middle = len(whole) // 2

    organized = tmp_path / "organized"
    make_run(
        organized,
        "run-a",
        [
            event(
                "0",
                {"content_type": "audio/webm;codecs=opus", "name": "prompt3"},
                media_id=1,
            )
        ],
        media=[{"media_id": 1, "event_id": "0", "finished": True, "parts": 2}],
        parts={"0": [whole[:middle], whole[middle:]]},
    )

    assert main([str(organized), str(tmp_path / "prepared")]) == 0

    out = tmp_path / "prepared/circe/10351/baseline"
    assert (out / "prompt3.webm").exists()
    assert not (out / "prompt3.joined.webm").exists()
    rows = read_csv(out / "events.csv")
    assert rows[0]["media_file"] == "prompt3.webm"
    assert rows[0]["media_finished"] == "true"
    assert float(rows[0]["media_duration_s"]) == pytest.approx(1.0, abs=0.1)
