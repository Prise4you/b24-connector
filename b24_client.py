"""
Тонкий клиент Bitrix24 REST поверх входящего вебхука.
Только чтение для коннектора знаний. Без внешних зависимостей (urllib).
"""
import json
import time
import urllib.request
import urllib.error
import urllib.parse
from typing import Any, Iterator, Optional


class B24Error(Exception):
    pass


class B24Client:
    def __init__(self, webhook_url: str, *, pause: float = 0.4, timeout: int = 30):
        # webhook_url вида https://<portal>.bitrix24.ru/rest/<uid>/<token>/
        self.base = webhook_url.rstrip("/") + "/"
        self.pause = pause          # антифлуд: B24 ~2 запроса/сек
        self.timeout = timeout

    def call(self, method: str, params: Optional[dict] = None) -> Any:
        """Один вызов метода REST. Возвращает поле result."""
        url = self.base + method + ".json"
        data = json.dumps(params or {}).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")
                # 503 QUERY_LIMIT_EXCEEDED — притормозить и повторить
                if e.code == 503 and attempt < 3:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise B24Error(f"{method}: HTTP {e.code} {body}") from e
            except (urllib.error.URLError, OSError) as e:
                if attempt < 3:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                raise B24Error(f"{method}: {e}") from e
        if "error" in payload:
            raise B24Error(f"{method}: {payload.get('error')} {payload.get('error_description')}")
        time.sleep(self.pause)
        return payload.get("result")

    def batch(self, commands: dict, halt: int = 0) -> dict:
        """
        Пакетный вызов: до 50 команд за один HTTP-запрос.
        commands — {ключ: (method, params)} или {ключ: "method?query"}.
        Возвращает {ключ: result} (только успешные; ошибки в result_error).
        """
        cmd_payload = {}
        for key, val in commands.items():
            if isinstance(val, tuple):
                method, params = val
                query = urllib.parse.urlencode(self._flatten(params or {}))
                cmd_payload[key] = f"{method}?{query}"
            else:
                cmd_payload[key] = val
        result = self.call("batch", {"halt": halt, "cmd": cmd_payload})
        return result or {}

    @staticmethod
    def _flatten(d, parent=""):
        """Преобразовать вложенный dict/list в плоские ключи для query-строки batch."""
        items = []
        if isinstance(d, dict):
            for k, v in d.items():
                key = f"{parent}[{k}]" if parent else str(k)
                items.extend(B24Client._flatten(v, key))
        elif isinstance(d, (list, tuple)):
            for i, v in enumerate(d):
                key = f"{parent}[{i}]"
                items.extend(B24Client._flatten(v, key))
        else:
            items.append((parent, d))
        return items

    def call_list(self, method: str, params: Optional[dict] = None,
                  *, result_key: Optional[str] = None) -> Iterator[dict]:
        """
        Постраничный обход list-методов (start/next).
        result_key — если result обёрнут (напр. tasks.task.list → result['tasks']).
        Отдаёт элементы по одному.
        """
        params = dict(params or {})
        start = 0
        while True:
            params["start"] = start
            url = self.base + method + ".json"
            data = json.dumps(params).encode("utf-8")
            req = urllib.request.Request(
                url, data=data, headers={"Content-Type": "application/json"}
            )
            payload = None
            for attempt in range(4):
                try:
                    with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                        payload = json.loads(resp.read().decode("utf-8"))
                    break
                except urllib.error.HTTPError as e:
                    body = e.read().decode("utf-8", "replace")
                    if e.code == 503 and attempt < 3:
                        time.sleep(1.5 * (attempt + 1))
                        continue
                    raise B24Error(f"{method}: HTTP {e.code} {body}") from e
                except (urllib.error.URLError, OSError) as e:
                    if attempt < 3:
                        time.sleep(1.0 * (attempt + 1))
                        continue
                    raise B24Error(f"{method}: {e}") from e
            if payload is None:
                break
            if "error" in payload:
                raise B24Error(f"{method}: {payload.get('error')} {payload.get('error_description')}")

            result = payload.get("result")
            items = result[result_key] if result_key and isinstance(result, dict) else result
            if not items:
                break
            for it in items:
                yield it

            nxt = payload.get("next")
            if nxt is None:
                break
            start = nxt
            time.sleep(self.pause)


if __name__ == "__main__":
    # Быстрая проверка доступа: python b24_client.py <webhook_url>
    import sys
    if len(sys.argv) < 2:
        print("usage: python b24_client.py <webhook_url>")
        sys.exit(1)
    c = B24Client(sys.argv[1])
    me = c.call("profile")
    print("OK, авторизован как:", me.get("NAME"), me.get("LAST_NAME"), "ID", me.get("ID"))
