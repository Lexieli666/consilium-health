"""Interface: the ASGI entry point.

``uvicorn consilium.api.main:app``.

The application lives at ``consilium/api/`` and not at a root-level ``api/``, which is what the
build brief writes.  A top-level package outside ``consilium/`` would sit outside
``--cov=consilium`` and its request validation -- the part of an HTTP interface most worth testing
-- would go uncovered.  Recorded in CLAUDE.md section 6.  This is the opposite call from ``eval/``,
and for the opposite reason: the API ships in the wheel, the harness does not.

Importing this module builds no runtime and loads no corpus; the runtime is assembled during
startup.  Building it at import time would mean that generating the OpenAPI schema needed an
embedding model.
"""

from __future__ import annotations

from consilium.api.app import create_app

app = create_app()
