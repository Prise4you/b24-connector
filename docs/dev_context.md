# b24-connector: Контекст разработки (сессия 2026-06-26)

## Что это и зачем

Инструмент для выгрузки данных проекта Bitrix24 (задачи+комментарии, чат, файлы)
в AI-базу знаний. Пилот на проекте 506 «Сайты АНИТ» (173 задачи).
Рассматривается как коммерческий продукт для Маркетплейса Битрикс24 (АНИТ — партнёр).

## Два продукта

**Продукт A — внутренний** (NotebookLM, уже ~80% готов):
- ЦА: команда АНИТ
- Движок: NotebookLM (Google) — только несекретные данные
- Ограничение: открытые линии + CRM (клиентские ПДн) → ЗАПРЕЩЕНО
- Монетизация: продуктивность + платные интеграции под ключ

**Продукт B — Маркетплейс** (~20% готов, после A):
- ЦА: администраторы/интеграторы порталов Б24
- Движок: нативный BitrixGPT или Yandex/GigaChat (данные в РФ)
- Комплаенс: нужен DPA, политика, OAuth-приложение, модерация 1С-Битрикс
- Монетизация: freemium + подписка + внедрение

## Текущая архитектура (5 модулей)

### b24_client.py (146 строк)
REST-клиент Bitrix24. Ключевые методы:
- `call(method, params)` — одиночный вызов с retry (4 попытки, timeout=30s)
- `batch(commands, halt=0)` — до 50 команд в одном HTTP-запросе
- `call_list(method, params, result_key)` — автопагинация (limit/offset)
Важно: Python 3.9 совместимость — ТОЛЬКО `Optional[X]` из typing (не `X | None`).
Retry ловит `(urllib.error.URLError, OSError)` — включает `socket.timeout`.

### connector.py (601 строка)
Основной оркестратор. Флаги: `--dry-run`, `--skip-admin-check`, `--bind`, `--poll`.

Extract задач: `extract_tasks_full()` — batches по 20 через `tasks.task.get select=*,UF_*`.
Только этот метод возвращает embedded-объекты creator/responsible/auditorsData/accomplicesData.
Extract комментариев: batches по 40 через `task.commentitem.getlist`.
Extract чата: `im.dialog.messages.get` с DIALOG_ID=SG{group_id}.
Extract файлов: `disk.storage.getlist` + рекурсивный обход `disk.folder.getchildren`.

BBcode-парсинг: `[URL=...]`, `[DISK FILE ID=...]`, `[USER=id]Name[/USER]`.
Disk-кэш `_DISK_CACHE` — избегает дублированных `disk.file.get` вызовов.

ПДн-guard: если `targets.notebooklm.enabled=True` И (`include.crm.enabled=True` ИЛИ `include.open_lines` непустое) → sys.exit(1) с объяснением 152-ФЗ.

Transform: build_tasks_doc включает все поля (постановщик, ответственный, соисполнители,
наблюдатели, статус, приоритет, даты, дедлайн, кто закрыл, родительская задача, теги,
CRM-ссылка, контроль задачи, ссылки из описания, файловые референсы, чеклист, комментарии
с авторами, датами и ссылками).

### security.py (187 строк)
Фильтрует 11 типов секретов перед отправкой в RAG:
passwork-link, 1password-link, bitwarden-link, keepass-link,
api-token, password-field, bearer-token, basic-auth,
pem-key, ssh-key, bitrix-webhook-token, card-number,
passport-number, snils.

`redact_doc(content, label)` — возвращает (очищенный текст, кол-во замен).
Аудит-лог: `logs/security_audit.jsonl` (каждый найденный секрет — hash, контекст).
`audit_run_start/end` — `logs/runs.jsonl`.
`harden_config_file()` — chmod 600.
`require_admin(c)` — проверяет `user.admin` через REST.

### events.py (70 строк)
Offline-события Bitrix24 (без публичного сервера!):
- `event.bind` с `event_type=offline` — события копятся в очереди портала
- `event.offline.get` — отдаёт И удаляет из очереди (drain)

События задач: ONTASKADD, ONTASKUPDATE, ONTASKCOMMENTADD.
`bind_offline(c)` — идемпотентна, игнорирует "already bound".
`poll_offline(c)` — дренирует всю очередь постранично.
`has_task_changes(events)` — фильтрует только задачные события.

### load_notebooklm.py (95 строк)
CLI-загрузчик в NotebookLM. CLI-бинарь: `/Users/prise4you/claudenewinst/notebooklm-py/.venv/bin/notebooklm`.
Цикл: `source delete-by-title -y` (если есть) → `source add --type file`.
`ensure_notebook()` — создаёт если notebook_id пуст, возвращает id.
Маппинг имён: tasks.md → "Задачи и обсуждения", chat.md → "Чат группы", files.md → "Файлы на диске".

### launchd/
`com.anit.b24kb.poll.plist` — каждые 600 секунд, `python connector.py --poll`.
`install.sh` — bind + mkdir logs + cp plist + launchctl load.

## Конфигурация (config.json, в .gitignore, chmod 600)

```json
{
  "webhook_url": "<НОВЫЙ ВЕБХУК — НЕ КОММИТИТЬ>",
  "group_id": 506,
  "output_dir": "out",
  "include": {
    "tasks": true,
    "task_comments": true,
    "group_chat": true,
    "disk_files": true,
    "crm": {"enabled": false},
    "open_lines": []
  },
  "targets": {
    "notebooklm": {
      "enabled": true,
      "notebook_id": ""
    }
  }
}
```

## Инциденты безопасности и принятые решения

### Инцидент S1 (2026-06-26)
В пилотной выгрузке задач был passwork.me-линк с кредами → ушёл в ноутбук 06c54069
(Google-сервер, США). Кей в ноутбуке: ноутбук удалён, `out/` удалён, пароли в passwork
сменены, старый вебхук скомпрометирован (был в чате с Claude).

### Инцидент S2
Старый вебхук `uww6pcdlajxvykk3` (uid 958, Александр Хан) — был передан в чат → попал
в Anthropic-логи. Решение: заменён плейсхолдером в config.json, пользователь перегенерирует.

### Принятые архитектурные решения
1. Фильтр секретов — обязателен на ВСЕХ путях, не только NotebookLM
2. ПДн-guard — зашит в код (не только в документацию)
3. Offline-события выбраны над онлайн-вебхуками — нет нужды в публичном сервере
4. Python 3.9 — нет walrus-operator и X|None union syntax
5. Batch по 20 задач — баланс между скоростью и нагрузкой на API
6. `tasks.task.get select=*` — единственный способ получить embedded creator/responsible
7. NotebookLM для Маркетплейса НЕПУБЛИКУЕМ (152-ФЗ трансграничная передача)

## Что ещё не сделано (Продукт A, остаток)

**A4 (организационное):**
- Согласие сотрудников на обработку данных в NotebookLM (GDPR-аналог внутри)
- Проверить, подано ли уведомление в РКН (АНИТ как оператор ПДн)
- Проверить, кто является admin-пользователем для нового вебхука

**Блокер:**
- Пользователь должен перегенерировать вебхук (VPN мешал зайти на страницу Bitrix24)
- Вписать новый URL в config.json
- Запустить `python3 connector.py --dry-run --skip-admin-check`
- Убедиться что tasks.md содержит все поля и секреты маскируются

## Нерешённые технические вопросы

1. **Инкрементальный extract**: сейчас `--poll` делает ПОЛНУЮ пересборку всего проекта
   при любом изменении. При 173+ задачах — 9 batch-запросов по 20 задач каждый раз.
   Нужен: дата последнего прогона → `filter: {CHANGED_DATE_FROM: last_run}` только для изменённых задач.

2. **Содержимое файлов**: сейчас выгружаются только метаданные файлов (имя, размер, дата).
   Текстовое содержимое docx/pdf/txt через DOWNLOAD_URL + экстракция — не реализовано.

3. **Разрешение имён для closed_by**: для пользователей, не являющихся creator/responsible/auditor,
   имя получается как string(id) — нужен `user.get` или batch lookup.

4. **Чат имена**: в `build_chat_doc` авторы — только ID (`ID{author}`), не имена.
   Нужен batch-lookup user.get для авторов сообщений.

5. **Многострочные описания задач**: обрезаются до 2000 символов — может терять важный контекст.

6. **NotebookLM source_wait**: после `source add` файл обрабатывается асинхронно.
   Текущий код не ждёт завершения обработки перед следующей загрузкой.

7. **Нет тестов**: нет unit-тестов для парсеров BBcode, фильтра секретов, person_label и т.д.
   Разработка велась с проверкой через `python3 -c "import ..."` и ручным тестом.

## Метаинформация о самом процессе разработки

Этот проект разрабатывается через Claude Code (AI-ассистент). Ключевые ограничения процесса:
- **Контекстное окно**: Claude Code теряет контекст при достижении лимита токенов
- **Память**: автосохранение в `/Users/prise4you/.claude/projects/-Users-prise4you-postmaker/memory/`
- **Верификация без портала**: все модули прошли проверку синтаксиса, но не прогонялись с живым Bitrix24
- **Итеративная разработка**: сначала определялась архитектура, потом поочерёдно реализовывались модули

Паттерн ошибок в этой сессии:
- Python 3.9 syntax: `dict | None` → нужно `Optional[dict]`
- Regex too strict: webhook token pattern `{20,}` не поймал 16-символьный токен → `{10,}`
- `socket.timeout` не является подклассом `urllib.error.URLError` → ловить как `OSError`
- `call_list` не имела retry-логики в отличие от `call` — исправлено
