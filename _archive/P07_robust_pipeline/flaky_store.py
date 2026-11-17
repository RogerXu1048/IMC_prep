"""Simulated remote object store (PROVIDED INFRASTRUCTURE — treat it as a black box).

It behaves like S3/GCS over a slow, unreliable network:
  * every request has 20-120 ms latency
  * ~15% of requests fail with TransientStoreError ("503 SlowDown")
  * ~3% of requests hang ~0.5 s and then raise TimeoutError
  * more than MAX_IN_FLIGHT concurrent requests -> TransientStoreError ("429 Too Many Requests")
  * missing objects raise NotFoundError (retrying will not help)

API
  list_objects(root)            -> list[str]   relative POSIX paths of every object under root (cheap, reliable)
  stat_object(root, rel)        -> dict        {"size": int, "etag": str} (cheap, reliable; like an HTTP HEAD)
  fetch_bytes_sync(root, rel)   -> bytes       blocking download
  await fetch_bytes(root, rel)  -> bytes       asyncio download
  configure(...)                               change failure rates / latency / seed (used by tests)
  STATS                                        request counters
"""
import asyncio
import hashlib
import random
import time
from pathlib import Path


class TransientStoreError(Exception):
    """Retryable: throttling or a temporary server-side error."""


class NotFoundError(Exception):
    """Permanent: the object does not exist."""


MAX_IN_FLIGHT = 10
_cfg = {"fail_rate": 0.15, "timeout_rate": 0.03, "min_latency": 0.02, "max_latency": 0.12, "hang": 0.5}
_rng = random.Random(7)
_in_flight = 0
STATS = {"requests": 0, "transient": 0, "timeouts": 0, "throttled": 0, "bytes": 0}


def configure(seed=None, **kwargs):
    unknown = set(kwargs) - set(_cfg)
    if unknown:
        raise ValueError(f"unknown settings: {unknown}")
    _cfg.update(kwargs)
    if seed is not None:
        _rng.seed(seed)
    for k in STATS:
        STATS[k] = 0


def list_objects(root):
    root = Path(root)
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def stat_object(root, rel):
    p = Path(root) / rel
    if not p.exists():
        raise NotFoundError(rel)
    data = p.read_bytes()
    return {"size": len(data), "etag": hashlib.md5(data).hexdigest()}


def _plan(root, rel):
    STATS["requests"] += 1
    latency = _rng.uniform(_cfg["min_latency"], _cfg["max_latency"])
    r = _rng.random()
    if not (Path(root) / rel).exists():
        return latency, "notfound"
    if r < _cfg["fail_rate"]:
        return latency, "transient"
    if r < _cfg["fail_rate"] + _cfg["timeout_rate"]:
        return _cfg["hang"], "timeout"
    return latency, "ok"


def _finish(root, rel, outcome):
    if outcome == "notfound":
        raise NotFoundError(rel)
    if outcome == "transient":
        STATS["transient"] += 1
        raise TransientStoreError(f"503 SlowDown: {rel}")
    if outcome == "timeout":
        STATS["timeouts"] += 1
        raise TimeoutError(f"read timed out: {rel}")
    data = (Path(root) / rel).read_bytes()
    STATS["bytes"] += len(data)
    return data


def fetch_bytes_sync(root, rel):
    latency, outcome = _plan(root, rel)
    time.sleep(latency)
    return _finish(root, rel, outcome)


async def fetch_bytes(root, rel):
    global _in_flight
    if _in_flight >= MAX_IN_FLIGHT:
        STATS["throttled"] += 1
        await asyncio.sleep(0.005)
        raise TransientStoreError("429 Too Many Requests")
    _in_flight += 1
    try:
        latency, outcome = _plan(root, rel)
        await asyncio.sleep(latency)
        return _finish(root, rel, outcome)
    finally:
        _in_flight -= 1
