import base64
import hashlib
import hmac
import secrets
import time


def hash_pin(pin):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(pin.encode(), salt=salt, n=16384, r=8, p=1)
    return base64.b64encode(salt + digest).decode()


def verify_pin(pin, value):
    try:
        raw = base64.b64decode(value)
        digest = hashlib.scrypt(pin.encode(), salt=raw[:16], n=16384, r=8, p=1)
        return hmac.compare_digest(digest, raw[16:])
    except (ValueError, TypeError):
        return False


class Sessions:
    def __init__(self):
        self.tokens = {}
        self.failures = 0
        self.blocked_until = 0

    def create(self):
        current = time.monotonic()
        self.tokens = {k: expiry for k, expiry in self.tokens.items() if expiry > current}
        if len(self.tokens) >= 128:
            self.tokens.pop(next(iter(self.tokens)))
        token = secrets.token_urlsafe(32)
        self.tokens[token] = time.monotonic() + 12 * 3600
        return token

    def valid(self, token):
        return bool(token and self.tokens.get(token, 0) > time.monotonic())

    def clear(self):
        self.tokens.clear()
