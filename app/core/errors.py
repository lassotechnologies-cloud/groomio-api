"""Error responses a browser can actually read.

## Why this exists

Starlette builds its middleware stack like this:

    ServerErrorMiddleware → user middleware → ExceptionMiddleware → router

`ServerErrorMiddleware` is the **outermost** layer, so the bare `500 Internal
Server Error` it produces for an unhandled exception is generated *outside*
`CORSMiddleware`. That response carries no `Access-Control-Allow-Origin`
header, and a browser is not allowed to read a response it was not granted
access to — so the browser hides the body and reports a CORS failure instead:

    Access to fetch at 'http://localhost:8000/v1/onboarding/status' from origin
    'http://localhost:3000' has been blocked by CORS policy: No
    'Access-Control-Allow-Origin' header is present on the requested resource.

The message points at CORS configuration. The actual fault was three layers
down — a `ProgrammingError: column businesses.town does not exist`, because
the database schema was behind the code. A developer who trusts the browser
message spends the afternoon editing `ALLOWED_ORIGINS` and never finds it.

`VisibleErrorMiddleware` catches the exception first and returns a JSON 500
from *inside* the CORS layer, so:

* the browser receives a readable `500` with `{"detail": ...}` instead of an
  opaque `net::ERR_FAILED`, and
* the CORS headers are added by `CORSMiddleware` itself — the same allow-list
  that governs every other response, with no second copy of the policy to
  drift out of sync.

The traceback is still logged. Swallowing it here would trade one invisible
failure for another: a 500 nobody can see the cause of.

## Ordering

This middleware must be registered **before** `CORSMiddleware` (see
`app/main.py`): `add_middleware` inserts each new middleware at the front of
the stack, so the last one registered is the outermost. Registering it first
puts it directly around the router, which is the only place its behaviour is
correct — a middleware outside the CORS layer would have to add the
`Access-Control-Allow-Origin` header by hand, and a hand-written copy of the
allow-list is exactly the kind of thing that drifts.
"""

import logging

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

logger = logging.getLogger("groomio.errors")


class VisibleErrorMiddleware(BaseHTTPMiddleware):
    """Turn an unhandled exception into a JSON 500 the caller can read.

    Only exceptions that escape the router are handled here. `HTTPException`
    and request-validation errors are turned into responses by
    `ExceptionMiddleware`, which sits inside this one, so they keep their
    normal status codes and bodies.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        try:
            return await call_next(request)
        except Exception:
            # The traceback is the whole point: the caller gets a one-line
            # summary, the operator gets the stack.
            logger.exception(
                "Unhandled error handling %s %s", request.method, request.url.path
            )
            return JSONResponse(
                status_code=500,
                content={"detail": "Internal server error"},
            )
