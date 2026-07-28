"""The web service.

It accepts requests, checks them, appends events to files, and updates the database.
That's all: no background threads, no schedulers, no work that outlives a request. See
docs/design_assumptions.md.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import db, health
from .config import DEFAULT_CONFIG, ConfigSource
from .runs import Pig, RequestProblem


def create_app(config_path: Path) -> FastAPI:
    """Build the service around one configuration file.

    Every request asks the source for the current configuration, so editing a task
    definition takes effect on the next request — no restart, no signal. The file is
    read here once as well, so a service that starts at all has a configuration that
    loads.
    """
    source = ConfigSource(config_path)
    source.current().in_progress_root.mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="Psych Ingestor", docs_url=None, redoc_url=None)
    app.state.config_source = source

    # Tasks are static pages hosted anywhere, so every request they make is cross-origin.
    # Permissive is the deliberate default; see docs/security.md.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.exception_handler(RequestProblem)
    def _problem(request: Request, problem: RequestProblem) -> JSONResponse:
        return JSONResponse(
            status_code=problem.status_code, content={"message": problem.message}
        )

    @app.post("/task/{task_code}/run")
    def start_run(task_code: str, parameters: dict[str, Any]) -> JSONResponse:
        with _pig(source) as pig:
            return JSONResponse(
                status_code=201, content=pig.start_run(task_code, parameters)
            )

    @app.post("/task/{task_code}/run/{run_id}")
    def store_events(
        task_code: str, run_id: str, events: dict[str, Any]
    ) -> JSONResponse:
        with _pig(source) as pig:
            result = pig.store_events(task_code, run_id, events)
            return JSONResponse(status_code=result.status_code, content=result.body())

    @app.post("/task/{task_code}/run/{run_id}/finalize")
    def finalize_run(task_code: str, run_id: str) -> JSONResponse:
        with _pig(source) as pig:
            result = pig.finalize_run(task_code, run_id)
            return JSONResponse(status_code=result.status_code, content=result.body())

    @app.get("/task/{task_code}/run/{run_id}")
    def describe_run(task_code: str, run_id: str) -> JSONResponse:
        with _pig(source) as pig:
            return JSONResponse(content=pig.describe_run(task_code, run_id))

    @app.get("/health")
    def check_health() -> JSONResponse:
        with _pig(source) as pig:
            report = health.report(pig, source.problem)
            return JSONResponse(
                status_code=200 if report["ok"] else 503, content=report
            )

    return app


class _pig:
    """The current configuration and one database connection, for one request.

    The connection is closed when the request is done; the configuration is whatever the
    file says right now.
    """

    def __init__(self, source: ConfigSource):
        self.source = source

    def __enter__(self) -> Pig:
        config = self.source.current()
        self.connection = db.connect(config.database)
        return Pig(config, self.connection)

    def __exit__(self, *exception: object) -> None:
        self.connection.close()


def app() -> FastAPI:
    """Entry point for `uvicorn psych_ingestor.app:app --factory`.

    Reads PIG_CONFIG for the configuration file; `pig serve` sets it for you.
    """
    return create_app(Path(os.environ.get("PIG_CONFIG", DEFAULT_CONFIG)))
