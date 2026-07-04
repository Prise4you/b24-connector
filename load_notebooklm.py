"""
Load-таргет: NotebookLM (через CLI notebooklm-py).
Только для Продукта A (внутренний инструмент). НЕ для клиентских ПДн.

Стратегия пересборки: для каждого документа delete-by-title → add — так
источник всегда актуален (не растёт бесконечно, не накапливает старый+новый
контент вперемешку). На практике delete-by-title не всегда успевает удалить
предыдущую версию (таймаут, сетевая ошибка, либо уже есть дубли — CLI в этом
случае возвращает AMBIGUOUS_TITLE и отказывается удалять без явного ID), из-за
чего дубли всё равно могут появляться — см. dedupe_sources() ниже, которая
находит (и, если явно включено, удаляет) такие дубли и «ghost»-источники
(остались от чатов/файлов, убранных из конфига). Лимит источников на ноутбук
у NotebookLM — 100.
"""
import os
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

NOTEBOOKLM_BIN = os.environ.get("NLM_BIN", "/Users/prise4you/claudenewinst/notebooklm-py/.venv/bin/notebooklm")
DEFAULT_STORAGE = os.path.expanduser(
    "~/.notebooklm/profiles/default/storage_state.json")


class NotebookLMError(Exception):
    pass


def _run(args: list, timeout: int = 180) -> str:
    """Запустить notebooklm CLI, вернуть stdout. Поднять при ошибке."""
    cmd = [NOTEBOOKLM_BIN] + args
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise NotebookLMError(f"timeout ({timeout}s): {' '.join(args[:3])}")
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "(пустой вывод CLI)"
        raise NotebookLMError(f"CLI error ({proc.returncode}): {detail[:300]}")
    return proc.stdout


def ensure_notebook(notebook_id: str, title: str) -> str:
    """
    Вернуть валидный notebook_id. Если пуст — создать ноутбук и вернуть новый id.
    """
    if notebook_id:
        return notebook_id
    print(f"  NotebookLM: создаю ноутбук «{title}»...")
    out = _run(["create", title, "--json"])
    import re
    try:
        data = json.loads(out)
        # CLI возвращает {"notebook": {"id": "..."}} или {"notebook_id": "..."} или {"id": "..."}
        nb = data.get("notebook") or {}
        new_id = (nb.get("id") or data.get("notebook_id") or data.get("id", ""))
    except json.JSONDecodeError:
        nb = None
    # fallback: вытащить UUID из любого вывода (текст или битый JSON)
    if not new_id:
        m = re.search(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', out)
        new_id = m.group(0) if m else ""
    if not new_id:
        raise NotebookLMError(f"не удалось получить notebook_id из вывода: {out[:200]}")
    print(f"  NotebookLM: ноутбук создан, id={new_id}")
    return new_id


_NOT_FOUND_MARKER = "No source found with title"


def upload_doc(notebook_id: str, file_path: str, title: str) -> bool:
    """
    Пересобрать источник: удалить старый по title (если есть) → добавить новый файл.
    Возвращает True, если после этого прогона возможен дубль (delete-by-title
    упал НЕ по причине "источника не было" — например AMBIGUOUS_TITLE, когда
    таких источников уже несколько, или таймаут/сетевая ошибка CLI). В этом
    случае мы всё равно продолжаем добавление (fail-open — лучше свежий дубль,
    чем вообще потерять обновление), но сигнализируем наружу для дальнейшего
    GC через dedupe_sources().
    """
    possible_duplicate = False
    # 1. Удалить старую версию (молча, если её реально не было)
    try:
        _run(["source", "delete-by-title", title, "-n", notebook_id, "-y"], timeout=60)
        print(f"    ↻ старый источник «{title}» удалён")
    except NotebookLMError as e:
        if _NOT_FOUND_MARKER in str(e):
            pass  # источника не было — норм, первая загрузка
        else:
            # Реальная проблема (AMBIGUOUS_TITLE — уже есть дубли; таймаут;
            # сетевая ошибка) — не молчим, но и не блокируем прогон.
            print(f"    ⚠️  delete-by-title для «{title}» не удалось ({e}) — возможен дубль")
            time.sleep(2.0)
            try:
                _run(["source", "delete-by-title", title, "-n", notebook_id, "-y"], timeout=60)
                print(f"    ↻ старый источник «{title}» удалён (со второй попытки)")
            except NotebookLMError as e2:
                if _NOT_FOUND_MARKER not in str(e2):
                    print(f"    ⚠️  повтор тоже не удался ({e2}) — источник «{title}» пойдёт в отчёт dedupe_sources")
                    possible_duplicate = True

    # 2. Добавить новый
    _run(["source", "add", str(file_path),
          "-n", notebook_id, "--type", "file", "--title", title], timeout=180)
    print(f"    ✅ загружен «{title}»")
    return possible_duplicate


# карта имя_файла → заголовок источника в NotebookLM
_DOC_TITLES = {
    "tasks.md":         "Задачи и обсуждения",
    "chat.md":          "Чат группы",
    "files.md":         "Файлы на диске",
    "crm_companies.md": "Компании CRM",
    "crm_contacts.md":  "Контакты CRM",
}


def _doc_title(group_name: str, filename: str) -> str:
    label = _DOC_TITLES.get(filename)
    if not label:
        label = filename.replace("_", " ").replace(".md", "").title()
    return f"{group_name} — {label}"


def upload_files(notebook_id: str, file_list: list) -> None:
    """Загрузить файлы в NotebookLM. file_list: [(file_path, title), ...]."""
    for file_path, title in file_list:
        if not Path(file_path).exists():
            continue
        upload_doc(notebook_id, file_path, title)


def _list_sources(notebook_id: str) -> list:
    """Сырой список источников ноутбука: [{id, title, created_at}, ...]."""
    try:
        out = _run(["source", "list", "-n", notebook_id, "--json"], timeout=60)
        data = json.loads(out)
        srcs = data.get("sources") if isinstance(data, dict) else data
        return srcs if isinstance(srcs, list) else []
    except (NotebookLMError, json.JSONDecodeError):
        return []


def _count_sources(notebook_id: str) -> Optional[int]:
    """Число источников в ноутбуке. None — если не удалось определить."""
    srcs = _list_sources(notebook_id)
    return len(srcs) if srcs else (0 if srcs == [] else None)


def dedupe_sources(notebook_id: str, expected_titles: set, group_prefix: str,
                    dry_run: bool = True) -> dict:
    """
    Найти и (если dry_run=False) удалить:
    - дубли: несколько источников с одинаковым title — оставляем самый свежий
      (по created_at, если есть; иначе последний в списке от CLI);
    - ghost-источники: title начинается с `group_prefix` (namespace проекта,
      например "РТЦ Б24 — "), но не входит в expected_titles — то есть
      относится к нашей выгрузке, но соответствующий чат/файл больше не
      сконфигурирован (был удалён из проекта).
    Возвращает {"duplicates": [...], "ghosts": [...], "dry_run": bool} —
    списки title, которые были (или были бы) затронуты.
    """
    sources = _list_sources(notebook_id)
    by_title = {}
    for s in sources:
        by_title.setdefault(s.get("title", ""), []).append(s)

    report = {"duplicates": [], "ghosts": [], "dry_run": dry_run}

    for title, items in by_title.items():
        if len(items) > 1:
            # Оставляем самый свежий по created_at (если он есть у всех),
            # иначе — последний элемент, который обычно и есть самый новый
            # в порядке выдачи CLI.
            def _key(it):
                return it.get("created_at") or ""
            items_sorted = sorted(items, key=_key)
            to_delete = items_sorted[:-1]
            report["duplicates"].append({"title": title, "count": len(items)})
            if not dry_run:
                for it in to_delete:
                    sid = it.get("id")
                    if not sid:
                        continue
                    try:
                        _run(["source", "delete", sid, "-n", notebook_id, "-y"], timeout=60)
                    except NotebookLMError as e:
                        print(f"    ⚠️  не удалось удалить дубль «{title}» ({sid[:12]}...): {e}")

    if group_prefix:
        for title in by_title:
            if title.startswith(group_prefix) and title not in expected_titles:
                report["ghosts"].append(title)
                if not dry_run:
                    try:
                        _run(["source", "delete-by-title", title, "-n", notebook_id, "-y"], timeout=60)
                    except NotebookLMError as e:
                        print(f"    ⚠️  не удалось удалить ghost-источник «{title}»: {e}")

    return report


def list_notebooks(with_source_counts: bool = True) -> list:
    """
    Список ВСЕХ ноутбуков NotebookLM в аккаунте: [{id, name, sources}].
    Best-effort: при любой ошибке (нет сессии/сети) возвращает [] и не валит прогон.

    ВНИМАНИЕ: перечисляет весь Google-аккаунт (включая личные/сторонние
    ноутбуки, не только проектные), и с with_source_counts=True делает
    отдельный CLI-вызов на КАЖДЫЙ найденный ноутбук — на аккаунте с
    десятками ноутбуков это медленно и ненадёжно (легко упереться в лимит
    времени прогона GitHub Actions). Для снимка в админ-панель используйте
    project_notebooks_snapshot() ниже — он опрашивает только известные
    ноутбуки проектов из config.json, а не весь аккаунт.
    """
    try:
        out = _run(["list", "--json"], timeout=60)
    except NotebookLMError:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    items = data.get("notebooks") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    result = []
    for it in items:
        if not isinstance(it, dict):
            continue
        nb_id = it.get("id") or it.get("notebook_id") or ""
        name  = it.get("title") or it.get("name") or ""
        if not nb_id:
            continue
        entry = {"id": nb_id, "name": name, "sources": None}
        if with_source_counts:
            entry["sources"] = _count_sources(nb_id)
        result.append(entry)
    return result


def project_notebooks_snapshot(known: list) -> list:
    """
    Снимок числа источников ТОЛЬКО по известным ноутбукам проектов —
    known: [{"id": notebook_id, "name": ...}, ...] (обычно из config.json:
    projects[].notebook_id + include.crm.notebook_id). В отличие от
    list_notebooks(), НЕ перечисляет весь аккаунт NotebookLM — только
    опрашивает число источников по уже известным id, что быстро (N
    CLI-вызовов на N реальных проектов, а не на весь аккаунт) и не тащит
    в админ-панель посторонние личные ноутбуки пользователя.
    """
    result = []
    seen = set()
    for item in known:
        nb_id = (item or {}).get("id") or ""
        if not nb_id or nb_id in seen:
            continue
        seen.add(nb_id)
        result.append({
            "id": nb_id,
            "name": item.get("name") or nb_id,
            "sources": _count_sources(nb_id),
        })
    return result


def session_status(storage_path: Optional[str] = None,
                   max_age_hours: int = 24) -> dict:
    """
    Свежесть сессии NotebookLM по mtime storage_state.json.
    Возвращает {ok: bool, date: iso|None}. Эвристика (не гарантирует валидность токена).
    """
    p = Path(storage_path or DEFAULT_STORAGE)
    if not p.exists():
        return {"ok": False, "date": None}
    mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
    age_h = (datetime.now(timezone.utc) - mtime).total_seconds() / 3600
    return {"ok": age_h <= max_age_hours,
            "date": mtime.strftime("%Y-%m-%dT%H:%M:%S")}


def load_to_notebooklm(docs_dir: str, notebook_id: str,
                       group_name: str, doc_names: list,
                       dedupe_mode: str = "dry_run") -> str:
    """
    Загрузить все документы проекта в NotebookLM.
    Возвращает актуальный notebook_id (важно если был создан новый).

    dedupe_mode: "off" — не проверять дубли/ghost-источники;
                 "dry_run" (по умолчанию) — только отчёт в лог, ничего не
                 удалять; "delete" — реально удалять найденные дубли и
                 ghost-источники (включать только после того, как отчёт из
                 dry_run был просмотрен и подтверждён пользователем).
    """
    nb_title = f"{group_name} — база знаний проекта"
    notebook_id = ensure_notebook(notebook_id, nb_title)

    expected_titles = set()
    any_possible_dup = False
    for name in doc_names:
        path = Path(docs_dir) / name
        if not path.exists():
            continue
        title = _doc_title(group_name, name)
        expected_titles.add(title)
        if upload_doc(notebook_id, path, title):
            any_possible_dup = True

    if dedupe_mode != "off":
        report = dedupe_sources(
            notebook_id, expected_titles, f"{group_name} — ",
            dry_run=(dedupe_mode == "dry_run"),
        )
        if report["duplicates"] or report["ghosts"]:
            action = "найдены (dry-run, не тронуты)" if report["dry_run"] else "удалены"
            if report["duplicates"]:
                print(f"    🔁 Дубли источников {action}: " +
                      ", ".join(f"{d['title']} (x{d['count']})" for d in report["duplicates"]))
            if report["ghosts"]:
                print(f"    👻 Ghost-источники {action}: " + ", ".join(report["ghosts"]))
        elif any_possible_dup:
            print("    (после повторных попыток delete-by-title дублей не обнаружено)")

    n = _count_sources(notebook_id)
    if n is not None:
        print(f"    Источников в ноутбуке сейчас: {n}")

    return notebook_id
