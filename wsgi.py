"""Production entry point (gunicorn / any WSGI server).

    HARRY_HOSTED=1 HARRY_SESSION_SECRET=... gunicorn -w 2 -k gthread \
        --threads 8 -b 0.0.0.0:8000 wsgi:application

Desktop use is unchanged: ``python main.py --ui`` still serves on loopback.

Notes for hosting
-----------------
* The job queue is in-process. Run ONE worker process, or set
  ``HARRY_SINGLE_WORKER=1`` and scale with threads; multiple processes would
  each own a different in-memory job table.
* Generated documents and media live on disk (``data/``); mount that path as a
  persistent volume. Object storage adapters are a separate step.
"""

import logging
import os

os.environ.setdefault("HARRY_HOSTED", "1")

from harry_core.logging_setup import setup_logging  # noqa: E402
from harry_core.ui import create_app  # noqa: E402

setup_logging(os.environ.get("HARRY_LOG_LEVEL", "INFO"))
application = create_app()
app = application


if __name__ == "__main__":
    logging.getLogger("harry.wsgi").info("starting development server; use gunicorn in production")
    application.run(host=os.environ.get("HARRY_HOST", "0.0.0.0"),
                    port=int(os.environ.get("HARRY_PORT", "8000")),
                    threaded=True)
