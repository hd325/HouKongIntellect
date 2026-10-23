"""Bound password hashing and repeated login attempts in this single process."""
import asyncio
import time
from collections import OrderedDict, deque
from fastapi import HTTPException

HASH_SLOTS = asyncio.Semaphore(2)
_attempts = OrderedDict()


def admit(account: str, peer: str):
    now = time.monotonic()
    for key, maximum in ((('account', account), 10), (('peer', peer), 30)):
        bucket = _attempts.setdefault(key, deque())
        _attempts.move_to_end(key)
        while bucket and bucket[0] <= now - 60:
            bucket.popleft()
        if len(bucket) >= maximum:
            raise HTTPException(429, "登入嘗試過於頻繁，請一分鐘後重試。", headers={"Retry-After": "60"})
        bucket.append(now)
    while len(_attempts) > 4096:
        _attempts.popitem(last=False)
    if HASH_SLOTS.locked():
        raise HTTPException(429, "登入服務繁忙，請稍後重試。")
