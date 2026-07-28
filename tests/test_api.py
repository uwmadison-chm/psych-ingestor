"""The HTTP surface, as docs/api.md describes it."""

from fastapi.testclient import TestClient

BASELINE = {"participant_id": "10351", "session": "baseline"}


def test_the_worked_example_from_the_docs(client: TestClient):
    started = client.post("/task/stroop/run", json=BASELINE)
    assert started.status_code == 201
    run_id = started.json()["run_id"]
    assert started.json()["run_number"] == 1

    sent = client.post(
        f"/task/stroop/run/{run_id}",
        json={
            "1": {
                "timestamp": "2026-07-26T18:25:43.511-05:00",
                "data": {"type": "task_start", "ts": 0},
            },
            "2": {
                "timestamp": "2026-07-26T18:25:47.204-05:00",
                "data": {"type": "trial", "word": "GREEN", "rt": 843},
            },
        },
    )
    assert sent.status_code == 201
    assert sent.json() == {"status": "in_progress", "stored": ["1", "2"]}

    finalized = client.post(f"/task/stroop/run/{run_id}/finalize")
    assert finalized.status_code == 200
    assert finalized.json()["status"] == "finalizing"


def test_checking_on_a_run(client: TestClient):
    run_id = client.post("/task/stroop/run", json=BASELINE).json()["run_id"]
    client.post(f"/task/stroop/run/{run_id}", json={"1": {"data": {"trial": 1}}})

    checked = client.get(f"/task/stroop/run/{run_id}")
    assert checked.status_code == 200
    assert checked.json() == {"status": "in_progress", "stored": ["1"]}


def test_an_unknown_task_is_a_404(client: TestClient):
    assert client.post("/task/nope/run", json=BASELINE).status_code == 404


def test_an_unknown_run_is_a_404(client: TestClient):
    assert client.get("/task/stroop/run/no-such-run").status_code == 404


def test_a_retry_is_a_200(client: TestClient):
    run_id = client.post("/task/stroop/run", json=BASELINE).json()["run_id"]
    batch = {"1": {"data": {"trial": 1}}}
    assert client.post(f"/task/stroop/run/{run_id}", json=batch).status_code == 201
    assert client.post(f"/task/stroop/run/{run_id}", json=batch).status_code == 200


def test_a_repeated_id_with_new_content_is_a_422(client: TestClient):
    run_id = client.post("/task/stroop/run", json=BASELINE).json()["run_id"]
    client.post(f"/task/stroop/run/{run_id}", json={"1": {"data": {"trial": 1}}})
    collision = client.post(
        f"/task/stroop/run/{run_id}", json={"1": {"data": {"trial": 9}}}
    )

    assert collision.status_code == 422
    assert collision.json()["errors"]["1"]["can_retry"] is False


def test_a_bad_parameter_says_what_is_allowed(client: TestClient):
    refused = client.post(
        "/task/stroop/run", json={"participant_id": "10 351", "session": "baseline"}
    )
    assert refused.status_code == 422
    assert "letters, digits" in refused.json()["message"]


def test_cross_origin_requests_are_allowed(client: TestClient):
    replied = client.post(
        "/task/stroop/run",
        json=BASELINE,
        headers={"Origin": "https://tasks.example.edu"},
    )
    assert replied.headers["access-control-allow-origin"] == "*"


def test_health_reports_each_task(client: TestClient):
    report = client.get("/health").json()
    assert report["ok"] is True
    assert report["tasks"]["stroop"]["open"] is True
    assert report["tasks"]["balloons"]["open"] is False
    assert report["tasks"]["stroop"]["runs"]["in_progress"] == 0
