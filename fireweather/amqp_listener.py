"""Subscribe to MSC Datamart AMQP notifications and trigger downloads as RDPS files land.

Messages on exchange `xpublic` are Sarracenia v02 posts. The body is
"<timestamp> <base_url> <relpath>", e.g.
"20261022140243.873 https://dd1.weather.gc.ca/ 20261022/WXO-DD/model_rdps/10km/12/024/..._PT024H.grib2"
"""
import logging
import socket
import ssl
import threading
import uuid

import pika

from . import config
from .ingest import parse_filename

log = logging.getLogger("amqp")


def _queue_name():
    # Stable per machine so a restart re-attaches to the same (still-buffering) queue.
    host = socket.gethostname().lower().replace(".", "-")
    suffix = uuid.uuid5(uuid.NAMESPACE_DNS, host).hex[:8]
    return f"q_{config.AMQP_USER}.fireweather.rdps.{suffix}"


def listen(ingestor, stop: threading.Event):
    params = pika.ConnectionParameters(
        host=config.AMQP_HOST, port=config.AMQP_PORT,
        credentials=pika.PlainCredentials(config.AMQP_USER, config.AMQP_PASS),
        ssl_options=pika.SSLOptions(ssl.create_default_context(), config.AMQP_HOST),
        heartbeat=60, blocked_connection_timeout=300)
    queue = _queue_name()
    backoff = 5
    while not stop.is_set():
        conn = None
        try:
            conn = pika.BlockingConnection(params)
            ch = conn.channel()
            ch.queue_declare(queue, durable=False, auto_delete=False,
                             arguments={"x-expires": config.AMQP_QUEUE_EXPIRE_MS})
            ch.queue_bind(queue, config.AMQP_EXCHANGE, routing_key=config.AMQP_TOPIC)
            ch.basic_qos(prefetch_count=200)
            log.info("subscribed %s -> %s", config.AMQP_TOPIC, queue)
            backoff = 5
            # inactivity_timeout lets us notice `stop` and keeps heartbeats flowing
            for method, _props, body in ch.consume(queue, inactivity_timeout=1):
                if stop.is_set():
                    break
                if method is None:
                    continue
                _handle(ingestor, body)
                ch.basic_ack(method.delivery_tag)
            ch.cancel()
        except Exception as e:
            log.warning("AMQP connection lost (%s); retrying in %ds", e, backoff)
            stop.wait(backoff)
            backoff = min(backoff * 2, 300)
        finally:
            if conn and conn.is_open:
                try:
                    conn.close()
                except Exception:
                    pass


def _handle(ingestor, body):
    parts = body.decode("utf-8", "replace").split()
    if len(parts) < 3:
        return
    base, rel = parts[1], parts[2]
    parsed = parse_filename(rel)
    if not parsed:
        return  # one of the ~400 other RDPS fields
    run, var, fh = parsed
    if ingestor.submit(run, var, fh, url=base.rstrip("/") + "/" + rel):
        log.debug("notified %s %s f%03d", run, var, fh)
