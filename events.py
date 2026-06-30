"""
Автопополнение через offline-события Bitrix24.
event.bind(event_type=offline) — без публичного сервера: события копятся в
очереди на стороне Bitrix24, забираются методом event.offline.get (он же
удаляет их из очереди).

Запуск по расписанию — через launchd (см. com.anit.b24kb.plist).
"""
from b24_client import B24Client, B24Error

# События, на которые подписываемся для проекта (задачи).
TASK_EVENTS = ["ONTASKADD", "ONTASKUPDATE", "ONTASKCOMMENTADD"]


def bind_offline(c: B24Client, events=None) -> list:
    """
    Зарегистрировать offline-обработчики. Идемпотентно: повторный bind
    того же события вернёт ошибку «уже привязано» — глотаем её.
    Возвращает список успешно привязанных событий.
    """
    events = events or TASK_EVENTS
    bound = []
    for ev in events:
        try:
            c.call("event.bind", {"event": ev, "event_type": "offline"})
            bound.append(ev)
            print(f"  ✅ привязано: {ev}")
        except B24Error as e:
            msg = str(e)
            if "ERROR_EVENT_BINDING_EXISTS" in msg or "уже" in msg.lower() or "exist" in msg.lower():
                print(f"  • {ev} уже привязано")
                bound.append(ev)
            else:
                print(f"  ❌ {ev}: {e}")
    return bound


def unbind_offline(c: B24Client, events=None) -> None:
    """Снять offline-обработчики (для отката)."""
    events = events or TASK_EVENTS
    for ev in events:
        try:
            c.call("event.unbind", {"event": ev, "event_type": "offline"})
            print(f"  отвязано: {ev}")
        except B24Error as e:
            print(f"  {ev}: {e}")


def poll_offline(c: B24Client, limit: int = 50) -> list:
    """
    Забрать накопленные offline-события (event.offline.get удаляет их из очереди).
    Возвращает список dict событий: [{EVENT_NAME, MESSAGE_ID, ...}, ...].
    Вычерпывает очередь полностью (пагинация по limit).
    """
    all_events = []
    while True:
        result = c.call("event.offline.get", {"limit": limit})
        events = (result or {}).get("events", []) if isinstance(result, dict) else []
        if not events:
            break
        all_events.extend(events)
        if len(events) < limit:
            break
    return all_events


def has_task_changes(events: list) -> bool:
    """Есть ли среди событий изменения задач/комментариев?"""
    names = {str(e.get("EVENT_NAME", "")).upper() for e in events}
    return bool(names & set(TASK_EVENTS))
