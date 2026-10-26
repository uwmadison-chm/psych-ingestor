"""The HTTP surface, as docs/api.md describes it."""

from fastapi.testclient import TestClient

BASELINE = {"participant_id": "10351", "session": "baseline"}


def test_the_worked_example_from_the_docs(client: TestClient):
    started = client.post("/v1/task/stroop/run", json={"parameters": BASELINE})
    assert started.status_code == 201
    run_id = started.json()["run_id"]
    assert started.json()["run_number"] == 1

    sent = client.post(
        f"/v1/task/stroop/run/{run_id}",
        json={
            "events": {
                "1": {
                    "data": {
                        "timestamp": "2026-07-26T18:25:43.511-05:00",
                        "type": "task_start",
                        "ts": 0,
                    }
                },
                "2": {
                    "data": {
                        "timestamp": "2026-07-26T18:25:47.204-05:00",
                        "type": "trial",
                        "word": "GREEN",
                        "rt": 843,
                    }
                },
            }
        },
    )
    assert sent.status_code == 201
    assert sent.json() == {"status": "in_progress", "stored": ["1", "2"]}

    finalized = client.post(f"/v1/task/stroop/run/{run_id}/finalize")
    assert finalized.status_code == 200
    assert finalized.json()["status"] == "finalizing"


def test_checking_on_a_run(client: TestClient):
    run_id = client.post("/v1/task/stroop/run", json={"parameters": BASELINE}).json()[
        "run_id"
    ]
    client.post(
        f"/v1/task/stroop/run/{run_id}", json={"events": {"1": {"data": {"trial": 1}}}}
    )

    checked = client.get(f"/v1/task/stroop/run/{run_id}")
    assert checked.status_code == 200
    assert checked.json() == {"status": "in_progress", "stored": ["1"]}


def test_an_unknown_task_is_a_404(client: TestClient):
    assert (
        client.post("/v1/task/nope/run", json={"parameters": BASELINE}).status_code
        == 404
    )


def test_reading_a_tasks_settings(client: TestClient):
    described = client.get("/v1/task/stroop")
    assert described.status_code == 200
    assert described.json() == {
        "task_code": "stroop",
        "open": True,
        "parameters": ["participant_id", "session"],
        "path_parameters": ["participant_id", "session"],
        "expires_after_sec": 86400,
        "max_event_size_bytes": 1048576,
        "media": None,
        "encryption": None,
        "parameter_signing": None,
    }


def test_a_media_tasks_settings_give_its_part_size(client: TestClient):
    described = client.get("/v1/task/interview").json()
    assert described["media"] == {"max_part_size_bytes": 1024}


def test_a_closed_task_gives_its_settings_too(client: TestClient):
    """Closing refuses new runs only. The runs still going need the rest."""
    described = client.get("/v1/task/balloons")
    assert described.status_code == 200
    assert described.json()["open"] is False
    assert described.json()["parameters"] == ["participant_id"]


def test_settings_for_an_unknown_task_are_a_404(client: TestClient):
    assert client.get("/v1/task/nope").status_code == 404


def test_an_unknown_run_is_a_404(client: TestClient):
    assert client.get("/v1/task/stroop/run/no-such-run").status_code == 404


def test_a_retry_is_a_200(client: TestClient):
    run_id = client.post("/v1/task/stroop/run", json={"parameters": BASELINE}).json()[
        "run_id"
    ]
    batch = {"1": {"data": {"trial": 1}}}
    assert (
        client.post(f"/v1/task/stroop/run/{run_id}", json={"events": batch}).status_code
        == 201
    )
    assert (
        client.post(f"/v1/task/stroop/run/{run_id}", json={"events": batch}).status_code
        == 200
    )


def test_a_repeated_id_with_new_content_is_a_422(client: TestClient):
    run_id = client.post("/v1/task/stroop/run", json={"parameters": BASELINE}).json()[
        "run_id"
    ]
    client.post(
        f"/v1/task/stroop/run/{run_id}", json={"events": {"1": {"data": {"trial": 1}}}}
    )
    collision = client.post(
        f"/v1/task/stroop/run/{run_id}", json={"events": {"1": {"data": {"trial": 9}}}}
    )

    assert collision.status_code == 422
    assert collision.json()["errors"]["1"]["can_retry"] is False


def test_a_field_outside_data_is_refused(client: TestClient):
    """Pig stores only `data`. A top-level `timestamp` — the shape from before issue #3 —
    would be dropped silently otherwise, and the task would never know."""
    run_id = client.post("/v1/task/stroop/run", json={"parameters": BASELINE}).json()[
        "run_id"
    ]
    refused = client.post(
        f"/v1/task/stroop/run/{run_id}",
        json={
            "events": {"1": {"timestamp": "2026-07-26T18:25:43.511-05:00", "data": {}}}
        },
    )
    assert refused.status_code == 422
    assert refused.json()["stored"] == []
    assert refused.json()["errors"]["1"]["can_retry"] is False
    assert "'data'" in refused.json()["errors"]["1"]["message"]


def test_a_bad_parameter_says_what_is_allowed(client: TestClient):
    refused = client.post(
        "/v1/task/stroop/run",
        json={"parameters": {"participant_id": "10 351", "session": "baseline"}},
    )
    assert refused.status_code == 422
    assert "letters, digits" in refused.json()["message"]


def test_cross_origin_requests_are_allowed(client: TestClient):
    replied = client.post(
        "/v1/task/stroop/run",
        json={"parameters": BASELINE},
        headers={"Origin": "https://tasks.example.edu"},
    )
    assert replied.headers["access-control-allow-origin"] == "*"


def test_a_public_task_page_can_preflight_a_localhost_pig(client: TestClient):
    """A task hosted on a public https page counts as a "public" origin to Chrome,
    while a `pig serve` on localhost is a "private" one. Chrome's Private Network
    Access check adds this header to the preflight and refuses the real request
    unless we answer it — see docs/security.md.
    """
    preflight = client.options(
        "/v1/task/stroop/run",
        headers={
            "Origin": "https://tasks.example.edu",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
            "Access-Control-Request-Private-Network": "true",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-private-network"] == "true"


def test_health_reports_each_task(client: TestClient):
    report = client.get("/health").json()
    assert report["ok"] is True
    assert report["tasks"]["stroop"]["open"] is True
    assert report["tasks"]["balloons"]["open"] is False
    assert report["tasks"]["stroop"]["runs"]["in_progress"] == 0


def test_the_api_is_under_v1_but_health_isnt(client: TestClient):
    assert client.get("/task/stroop").status_code == 404
    assert client.post("/task/stroop/run", json=BASELINE).status_code == 404
    assert client.get("/health").status_code == 200
    assert client.get("/v1/health").status_code == 404


def test_bare_parameters_or_events_are_refused(client: TestClient):
    """The shapes from before v1. Refused, so a task still sending them finds out."""
    refused = client.post("/v1/task/stroop/run", json=BASELINE)
    assert refused.status_code == 422
    assert "'parameters'" in refused.json()["message"]

    run_id = client.post("/v1/task/stroop/run", json={"parameters": BASELINE}).json()[
        "run_id"
    ]
    refused = client.post(
        f"/v1/task/stroop/run/{run_id}", json={"1": {"data": {"trial": 1}}}
    )
    assert refused.status_code == 422
    assert "'events'" in refused.json()["message"]


def test_a_field_beside_parameters_or_events_is_refused(client: TestClient):
    refused = client.post(
        "/v1/task/stroop/run", json={"parameters": BASELINE, "utm_source": "email"}
    )
    assert refused.status_code == 422
    assert "'utm_source'" in refused.json()["message"]

    run_id = client.post("/v1/task/stroop/run", json={"parameters": BASELINE}).json()[
        "run_id"
    ]
    refused = client.post(
        f"/v1/task/stroop/run/{run_id}", json={"events": {}, "2": {"data": {}}}
    )
    assert refused.status_code == 422
    assert "belong inside 'events'" in refused.json()["message"]
