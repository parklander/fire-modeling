"""Run with:  python -m fireweather [--host 127.0.0.1] [--port 8000]

Logs go to the console and to logs/fireweather.log (rotated at 5 MB, 5 kept).
"""
import argparse
import atexit
import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path

import uvicorn

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
FMT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def _setup_logging(debug):
    LOG_DIR.mkdir(exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(LOG_DIR / "fireweather.log", maxBytes=5_000_000,
                                              backupCount=5, encoding="utf-8")
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO, format=FMT,
                        handlers=[logging.StreamHandler(), fh])
    logging.getLogger("pika").setLevel(logging.WARNING)
    # "Connection pool is full" warnings are harmless noise from the parallel CWFIS fetches
    logging.getLogger("urllib3").setLevel(logging.ERROR)
    log = logging.getLogger("fireweather")

    # Anything that would otherwise only hit stderr (and vanish) goes to the log file too
    def excepthook(et, ev, tb):
        log.critical("uncaught exception", exc_info=(et, ev, tb))
    sys.excepthook = excepthook
    threading.excepthook = lambda a: log.critical("uncaught exception in thread %s",
                                                  a.thread.name if a.thread else "?",
                                                  exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    atexit.register(lambda: log.info("process exiting (pid %d)", os.getpid()))
    return log


def main():
    ap = argparse.ArgumentParser(description="RDPS fire-weather forecast viewer")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    log = _setup_logging(args.debug)
    # the mailer prints the report PDF from this server
    host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    os.environ.setdefault("FW_SELF_URL", f"http://{host}:{args.port}")
    log.info("starting on http://%s:%d (pid %d)", args.host, args.port, os.getpid())
    try:
        # log_config=None: uvicorn's own errors go through the handlers above
        uvicorn.run("fireweather.server:app", host=args.host, port=args.port,
                    log_level="warning", log_config=None)
    except BaseException:
        log.critical("server stopped by exception", exc_info=True)
        raise
    log.info("server stopped")


if __name__ == "__main__":
    main()
