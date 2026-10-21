"""A server error must be readable by the browser, not disguised as a CORS failure.

## The bug this pins

`ServerErrorMiddleware` is the outermost layer of a Starlette app, so the bare
500 it returns for an unhandled exception is generated *outside*
`CORSMiddleware`. It therefore carries no `Access-Control-Allow-Origin`
header, and the browser — which may not read a response it was not granted
access to — reports:

    ... has been blocked by CORS policy: No 'Access-Control-Allow-Origin'
    header is present on the requested resource.

while the real fault was, say, `column businesses.town does not exist`. The
message sends the reader after the CORS configuration, which is not where the
problem was.

`VisibleErrorMiddleware` answers the exception from inside the CORS layer, so
the browser gets a legible 500.

No database is involved: the app under test raises on purpose, and the
middleware stack is the real one from `app.main`.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from starlette.middleware import Middleware

from app.core.errors import VisibleErrorMiddleware

ALLOWED = "http://localhost:3000"
DENIED = "http://evil.example"


def _app() -> FastAPI:
    """The real middleware order from `app.main`, on an app that raises."""
    app = FastAPI()
    app.add_middleware(VisibleErrorMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[ALLOWED],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/boom")
    async def boom():
        raise RuntimeError("column businesses.town does not exist")

    @app.get("/fine")
    async def fine():
        return {"ok": True}

    return app


def test_an_unhandled_error_carries_the_cors_header():
    """The whole point: a 500 the browser is allowed to read."""
    with TestClient(_app(), raise_server_exceptions=False) as client:
        response = client.get("/boom", headers={"Origin": ALLOWED})

    assert response.status_code == 500
    assert response.headers.get("access-control-allow-origin") == ALLOWED


def test_the_error_body_is_json_and_says_what_it_is():
    """`{"detail": ...}` is what the PWA's client knows how to read."""
    with TestClient(_app(), raise_server_exceptions=False) as client:
        response = client.get("/boom", headers={"Origin": ALLOWED})

    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"detail": "Internal server error"}


def test_the_internal_message_is_not_leaked_to_the_caller():
    """The traceback goes to the log, not to the browser."""
    with TestClient(_app(), raise_server_exceptions=False) as client:
        response = client.get("/boom", headers={"Origin": ALLOWED})

    assert "businesses.town" not in response.text


def test_an_untrusted_origin_still_gets_no_cors_header():
    """The middleware must not become a way around the allow-list."""
    with TestClient(_app(), raise_server_exceptions=False) as client:
        response = client.get("/boom", headers={"Origin": DENIED})

    assert response.status_code == 500
    assert "access-control-allow-origin" not in response.headers


def test_successful_responses_are_untouched():
    with TestClient(_app()) as client:
        response = client.get("/fine", headers={"Origin": ALLOWED})

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert response.headers.get("access-control-allow-origin") == ALLOWED


def test_the_real_app_registers_the_error_middleware_inside_cors():
    """Order is the fix; a reordering would silently undo it.

    `add_middleware` inserts at the front, so the list reads outermost-first.
    `VisibleErrorMiddleware` must come *after* `CORSMiddleware` in that list
    to sit inside it.
    """
    from app.main import app

    classes = [middleware.cls for middleware in app.user_middleware]

    assert CORSMiddleware in classes, "CORS middleware is missing from the app"
    assert VisibleErrorMiddleware in classes, "error middleware is missing from the app"
    assert classes.index(VisibleErrorMiddleware) > classes.index(CORSMiddleware), (
        "VisibleErrorMiddleware must be registered BEFORE CORSMiddleware so that it "
        "sits inside it — otherwise the 500 is still generated outside the CORS layer "
        "and the browser still reports a CORS policy failure"
    )


def test_the_error_middleware_is_not_registered_twice():
    """A double registration would shadow the first with the second."""
    from app.main import app

    classes = [middleware.cls for middleware in app.user_middleware]
    assert classes.count(VisibleErrorMiddleware) == 1
    assert isinstance(app.user_middleware[0], Middleware)
