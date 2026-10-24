"""Run with:  python -m fireweather [--host 127.0.0.1] [--port 8000]"""
import argparse
import logging

import uvicorn


def main():
    ap = argparse.ArgumentParser(description="RDPS fire-weather forecast viewer")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("pika").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    uvicorn.run("fireweather.server:app", host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
