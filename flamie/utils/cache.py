"""Cachés temporales de consultas frecuentes a Telegram."""
import threading
import time


class TTLCache:
    def __init__(self, ttl_seconds=30, max_items=4096):
        self.ttl_seconds = ttl_seconds
        self.max_items = max_items
        self._values = {}
        self._lock = threading.Lock()

    def get_or_load(self, key, loader):
        now = time.monotonic()
        with self._lock:
            cached = self._values.get(key)
            if cached and cached[0] > now:
                return cached[1]
        value = loader()
        with self._lock:
            if len(self._values) >= self.max_items:
                self._values = {k: v for k, v in self._values.items() if v[0] > now}
            if len(self._values) >= self.max_items:
                self._values.pop(next(iter(self._values)))
            self._values[key] = (now + self.ttl_seconds, value)
        return value

    def invalidate(self, key):
        with self._lock:
            self._values.pop(key, None)


chat_member_cache = TTLCache(ttl_seconds=30)
bot_identity_cache = TTLCache(ttl_seconds=3600, max_items=1)


def get_chat_member_cached(bot, chat_id, user_id):
    return chat_member_cache.get_or_load(
        (chat_id, user_id), lambda: bot.get_chat_member(chat_id, user_id)
    )


def get_bot_id_cached(bot):
    return bot_identity_cache.get_or_load("bot_id", lambda: bot.get_me().id)
