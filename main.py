"""Application entry point.

Lives at the project root, outside the `app/` package. Exposes the FastAPI
application for ASGI servers (`main:app`) and supports direct execution.

Production runs Uvicorn workers under Gunicorn:

    gunicorn main:app -k uvicorn.workers.UvicornWorker -c gunicorn.conf.py
"""

import uvicorn

from app.core.config import settings
from app.main import app

__all__ = ["app"]


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.reload,
    )
