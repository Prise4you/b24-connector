"""
Load-таргет: NotebookLM (через CLI notebooklm-py).
Только для Продукта A (внутренний инструмент). НЕ для клиентских ПДн.

Стратегия пересборки: для каждого документа delete-by-title → add.
Так источник всегда актуален, без накопления дублей.
"""
import os
import json
import subprocess
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
        raise NotebookLMError(f"CLI error ({proc.returncode}): {proc.stderr.strip()[:300]}")
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


def upload_doc(notebook_id: str, file_path: str, title: str) -> None:
    """
    Пересобрать источник: удалить старый по title (если есть) → добавить новый файл.
    """
    # 1. Удалить старую версию (молча, если её нет)
    try:
        _run(["source", "delete-by-title", title, "-n", notebook_id, "-y"], timeout=60)
        print(f"    ↻ старый источник «{title}» удалён")
    except NotebookLMError:
        pass  # источника не было — норм

    # 2. Добавить новый
    _run(["source", "add", str(file_path),
          "-n", notebook_id, "--type", "file", "--title", title], timeout=180)
    print(f"    ✅ загружен «{title}»")


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


def upload_audio_files(notebook_id: str, audio_list: list) -> None:
    """Загрузить аудиофайлы в NotebookLM. audio_list: [(file_path, title), ...]."""
    for file_path, title in audio_list:
        if not Path(file_path).exists():
            continue
        upload_doc(notebook_id, file_path, title)


def _count_sources(notebook_id: str) -> Optional[int]:
    """Число источников в ноутбуке. None — если не удалось определить."""
    try:
        out = _run(["source", "list", "-n", notebook_id, "--json"], timeout=60)
        data = json.loads(out)
        srcs = data.get("sources") if isinstance(data, dict) else data
        return len(srcs) if isinstance(srcs, list) else None
    except (NotebookLMError, json.JSONDecodeError):
        return None


def list_notebooks(with_source_counts: bool = True) -> list:
    """
    Список ноутбуков NotebookLM: [{id, name, sources}].
    Best-effort: при любой ошибке (нет сессии/сети) возвращает [] и не валит прогон.
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
                       group_name: str, doc_names: list) -> str:
    """
    Загрузить все документы проекта в NotebookLM.
    Возвращает актуальный notebook_id (важно если был создан новый).
    """
    nb_title = f"{group_name} — база знаний проекта"
    notebook_id = ensure_notebook(notebook_id, nb_title)

    for name in doc_names:
        path = Path(docs_dir) / name
        if not path.exists():
            continue
        title = _doc_title(group_name, name)
        upload_doc(notebook_id, path, title)

    return notebook_id
