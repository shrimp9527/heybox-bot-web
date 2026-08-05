"""
小黑盒 API 签名算法
完整翻译自 heybox-bot-main/src/heybox/api/sign.go
"""

import hashlib
import os
import time


ALPHABET = "AB45STUVWZEFGJ6CH01D237IXYPQRKLMN89"


def _vm(e: int) -> int:
    if (128 & e) != 0:
        return 255 & ((e << 1) ^ 27)
    return e << 1


def _qm(e: int) -> int:
    return _vm(e) ^ e


def _dm(e: int) -> int:
    return _qm(_vm(e))


def _ym(e: int) -> int:
    return _dm(_qm(_vm(e)))


def _gm(e: int) -> int:
    return _ym(e) ^ _dm(e) ^ _qm(e)


def _km(e: list) -> list:
    r = list(e)
    t = [0, 0, 0, 0]
    t[0] = _gm(r[0]) ^ _ym(r[1]) ^ _dm(r[2]) ^ _qm(r[3])
    t[1] = _qm(r[0]) ^ _gm(r[1]) ^ _ym(r[2]) ^ _dm(r[3])
    t[2] = _dm(r[0]) ^ _qm(r[1]) ^ _gm(r[2]) ^ _ym(r[3])
    t[3] = _ym(r[0]) ^ _dm(r[1]) ^ _qm(r[2]) ^ _gm(r[3])
    r[0], r[1], r[2], r[3] = t[0], t[1], t[2], t[3]
    return r


def _av(s: str, table: str, n: int) -> str:
    base = table[:n] if n >= 0 else table[: len(table) + n]
    result = []
    for ch in s:
        result.append(base[ord(ch) % len(base)])
    return "".join(result)


def _sv(s: str, table: str) -> str:
    result = []
    for ch in s:
        result.append(table[ord(ch) % len(table)])
    return "".join(result)


def _interleave_limit20(*parts: str) -> str:
    max_len = max((len(p) for p in parts), default=0)
    result = []
    for i in range(max_len):
        for p in parts:
            if i < len(p):
                result.append(p[i])
    out = "".join(result)
    return out[:20]


def _normalize_path(path: str) -> str:
    parts = [p for p in path.split("/") if p]
    return "/" + "/".join(parts) + "/"


def _md5_lower(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def _md5_upper(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest().upper()


def _make_nonce(ts: int) -> str:
    random_bytes = os.urandom(16)
    raw = str(ts) + random_bytes.hex()
    return _md5_upper(raw)


def _calc_hkey(path: str, ts: int, nonce: str) -> str:
    sign_time = ts + 1
    e = _normalize_path(path)

    mix = _interleave_limit20(
        _av(str(sign_time), ALPHABET, -2),
        _sv(e, ALPHABET),
        _sv(nonce, ALPHABET),
    )

    h = _md5_lower(mix)
    last6 = h[-6:]

    arr = [ord(c) for c in last6]
    total = sum(_km(arr))

    suffix = str(total % 100)
    if len(suffix) < 2:
        suffix = "0" + suffix

    prefix = _av(h[:5], ALPHABET, -4)
    return prefix + suffix


def make_heybox_sign(path: str) -> tuple:
    """
    为指定路径生成小黑盒请求签名参数。
    返回: (hkey, timestamp, nonce)
    """
    ts = int(time.time())
    nonce = _make_nonce(ts)
    hkey = _calc_hkey(path, ts, nonce)
    return hkey, ts, nonce