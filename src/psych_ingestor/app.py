"""The web service.

It accepts requests, checks them, appends events to files, and updates the database.
That's all: no background threads, no schedulers, no work that outlives a request. See
docs/design_assumptions.md.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Body, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import db, health, service
from .config import DEFAULT_CONFIG, ConfigSource
from .runs import RequestProblem
from .service import Pig


def create_app(config_path: Path) -> FastAPI:
    """Build the service around one configuration file.

    Every request asks the source for the current configuration, so editing a task
    definition takes effect on the next request — no restart, no signal. The file is
    read here once as well, so a service that starts at all has a configuration that
    loads.
    """
    source = ConfigSource(config_path)
    config = source.current()
    config.in_progress_root.mkdir(parents=True, exist_ok=True)
    # Open the database once now, so a file this Pig can't use fails at startup with a
    # readable message rather than on the first request.
    db.connect(config.database).close()

    app = FastAPI(title="Psych Ingestor", docs_url=None, redoc_url=None)
    app.state.config_source = source

    # Tasks are static pages hosted anywhere, so every request they make is cross-origin.
    # Permissive is the deliberate default; see docs/security.md. allow_private_network
    # answers Chrome's Private Network Access preflight check, which otherwise blocks a
    # public https task page from reaching a Pig running on localhost.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "PUT", "OPTIONS"],
        allow_headers=["*"],
        allow_private_network=True,
    )

    @app.exception_handler(RequestProblem)
    def _problem(request: Request, problem: RequestProblem) -> JSONResponse:
        return JSONResponse(
            status_code=problem.status_code, content={"message": problem.message}
        )

    # Every task endpoint is under /v1/, so a later version can live beside this one
    # for as long as an offline device might still turn up speaking it. /health isn't:
    # whatever watches the service shouldn't have to know about API versions.
    v1 = APIRouter(prefix="/v1")

    @v1.get("/task/{task_code}")
    def describe_task(task_code: str) -> JSONResponse:
        with _pig(source) as pig:
            return JSONResponse(content=pig.describe_task(task_code))

    @v1.post("/task/{task_code}/run")
    def start_run(task_code: str, submitted: Annotated[Any, Body()]) -> JSONResponse:
        parameters, signature = service.read_start_request(submitted)
        with _pig(source) as pig:
            return JSONResponse(
                status_code=201,
                content=pig.start_run(task_code, parameters, signature),
            )

    @v1.post("/task/{task_code}/run/{run_id}")
    def store_events(
        task_code: str, run_id: str, submitted: Annotated[Any, Body()]
    ) -> JSONResponse:
        events = service.read_events_request(submitted)
        with _pig(source) as pig:
            result = pig.store_events(task_code, run_id, events)
            return JSONResponse(status_code=result.status_code, content=result.body())

    @v1.post("/task/{task_code}/run/{run_id}/finalize")
    def finalize_run(task_code: str, run_id: str) -> JSONResponse:
        with _pig(source) as pig:
            result = pig.finalize_run(task_code, run_id)
            return JSONResponse(status_code=result.status_code, content=result.body())

    @v1.get("/task/{task_code}/run/{run_id}")
    def describe_run(task_code: str, run_id: str) -> JSONResponse:
        with _pig(source) as pig:
            return JSONResponse(content=pig.describe_run(task_code, run_id))

    @v1.post("/task/{task_code}/run/{run_id}/media")
    def start_media(
        task_code: str, run_id: str, event: Annotated[Any, Body()]
    ) -> JSONResponse:
        # `Body()` because the service checks the shape itself, with its own messages;
        # left to FastAPI, an untyped parameter is read from the query string.
        with _pig(source) as pig:
            reply = pig.start_media(task_code, run_id, event)
            return JSONResponse(status_code=reply.status_code, content=reply.body)

    @v1.put("/task/{task_code}/run/{run_id}/media/{media_id}/{part}")
    async def store_part(
        task_code: str, run_id: str, media_id: int, part: int, request: Request
    ) -> JSONResponse:
        # Async, unlike everything else here, because the body is streamed to disk
        # rather than read into memory. See `Pig.store_part`.
        with _pig(source) as pig:
            reply = await pig.store_part(
                task_code,
                run_id,
                media_id,
                part,
                request.stream(),
                _content_length(request),
            )
            return JSONResponse(status_code=reply.status_code, content=reply.body)

    @v1.post("/task/{task_code}/run/{run_id}/media/{media_id}/finish")
    def finish_media(
        task_code: str, run_id: str, media_id: int, declared: Annotated[Any, Body()]
    ) -> JSONResponse:
        with _pig(source) as pig:
            reply = pig.finish_media(task_code, run_id, media_id, declared)
            return JSONResponse(status_code=reply.status_code, content=reply.body)

    @v1.post("/task/{task_code}/run/{run_id}/purge")
    def purge_run(
        task_code: str, run_id: str, submitted: Annotated[Any, Body()]
    ) -> JSONResponse:
        # Asked for by `pigsty purge`, never by a task. See docs/api.md.
        with _pig(source) as pig:
            return JSONResponse(content=pig.purge_run(task_code, run_id, submitted))

    app.include_router(v1)

    @app.get("/health")
    def check_health() -> JSONResponse:
        with _pig(source) as pig:
            report = health.report(pig.config, pig.connection, source.problem)
            return JSONResponse(
                status_code=200 if report["ok"] else 503, content=report
            )

    return app


def _content_length(request: Request) -> int | None:
    """The body size the request claims, if it claims one. Only ever used to refuse
    early: the real limit is counted as the bytes arrive."""
    header = request.headers.get("content-length")
    return int(header) if header and header.isdigit() else None


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
