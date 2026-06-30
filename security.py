"""
Security layer для коннектора Bitrix24 → KB.
Фильтрует секреты перед отправкой в любой RAG.
Логирует каждый прогон с аудит-трейлом.
"""
import re
import os
import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ─── паттерны секретов ────────────────────────────────────────────────────────

_SECRET_PATTERNS = [
    # Менеджеры паролей
    (r'https?://passwork\.\w+/\S+',                        "passwork-link"),
    (r'https?://\w+\.1password\.com/\S+',                  "1password-link"),
    (r'https?://\w+\.bitwarden\.\w+/\S+',                  "bitwarden-link"),
    (r'https?://\w+\.keepass\.\w+/\S+',                    "keepass-link"),

    # Токены и ключи в URL / тексте
    (r'(?i)(?:token|secret|api[_\-]?key|access[_\-]?key)'
     r'\s*[=:]\s*[\w\-]{16,}',                             "api-token"),
    (r'(?i)(?:password|пароль|passwd|pwd)\s*[=:]\s*\S+',  "password-field"),
    (r'Bearer\s+[A-Za-z0-9\-._~+/]{20,}',                 "bearer-token"),
    (r'Basic\s+[A-Za-z0-9+/=]{20,}',                      "basic-auth"),

    # SSH/PGP/cert ключи
    (r'-----BEGIN [A-Z ]+-----',                            "pem-key"),
    (r'(?i)ssh-(?:rsa|ed25519|ecdsa)\s+[A-Za-z0-9+/=]{40,}', "ssh-key"),

    # Bitrix-специфичные токены в URL
    (r'/rest/\d+/[a-z0-9]{10,}/',                          "bitrix-webhook-token"),

    # Кредитные карты (16 цифр с разделителями)
    (r'\b(?:\d{4}[\s\-]?){3}\d{4}\b',                      "card-number"),

    # Российские паспортные данные (серия + номер)
    (r'\b\d{4}\s\d{6}\b',                                   "passport-number"),

    # СНИЛС
    (r'\b\d{3}-\d{3}-\d{3}\s\d{2}\b',                      "snils"),
]

_COMPILED = [(re.compile(p), label) for p, label in _SECRET_PATTERNS]

# ─── публичный API ────────────────────────────────────────────────────────────

class SecretFound:
    def __init__(self, label: str, match: str, context: str):
        self.label   = label
        self.match   = match[:60]          # не хранить полный секрет
        self.context = context[:80]        # 80 символов контекста вокруг
        self.hash    = hashlib.sha256(match.encode()).hexdigest()[:16]

    def __repr__(self):
        return f"[{self.label}] hash={self.hash} ctx=«{self.context}»"


def scan(text: str) -> list[SecretFound]:
    """Найти секреты в тексте. Возвращает список SecretFound (не изменяет текст)."""
    found = []
    for pattern, label in _COMPILED:
        for m in pattern.finditer(text):
            start = max(0, m.start() - 30)
            end   = min(len(text), m.end() + 30)
            ctx   = text[start:end].replace("\n", " ")
            found.append(SecretFound(label, m.group(), ctx))
    return found


def redact(text: str) -> tuple[str, list[SecretFound]]:
    """
    Удалить секреты из текста.
    Возвращает (очищенный текст, список того что было найдено).
    """
    found = []
    for pattern, label in _COMPILED:
        def _replace(m, _label=label):
            start = max(0, m.start() - 30)
            end   = min(len(text), m.end() + 30)
            found.append(SecretFound(_label, m.group(), text[start:end]))
            return f"[REDACTED:{_label}]"
        text = pattern.sub(_replace, text)
    return text, found


def redact_doc(content: str, source_label: str = "") -> tuple[str, int]:
    """
    Очистить целый документ.
    Возвращает (очищенный документ, кол-во замен).
    """
    cleaned, found = redact(content)
    if found:
        _audit_log_secret_found(source_label, found)
    return cleaned, len(found)

# ─── проверка прав администратора ─────────────────────────────────────────────

def require_admin(c, strict: bool = True) -> bool:
    """
    Проверить, что текущий пользователь вебхука — администратор портала.
    strict=True → поднимает исключение если нет прав.
    """
    from b24_client import B24Error
    try:
        is_admin = c.call("user.admin")
    except B24Error as e:
        if strict:
            raise PermissionError(f"Не удалось проверить права администратора: {e}")
        return False
    if not is_admin and strict:
        raise PermissionError(
            "Коннектор требует прав администратора портала.\n"
            "Убедитесь, что вебхук создан пользователем-администратором.\n"
            "Запустите с --skip-admin-check для принудительного запуска (не рекомендуется)."
        )
    return bool(is_admin)

# ─── проверка конфига ──────────────────────────────────────────────────────────

def validate_config(cfg: dict) -> list[str]:
    """Вернуть список предупреждений о небезопасном конфиге."""
    warnings = []
    webhook = cfg.get("webhook_url", "")
    if not webhook or webhook == "ВСТАВИТЬ_НОВЫЙ_ВЕБХУК":
        warnings.append("CRITICAL: webhook_url не задан")
    if "ТОКЕН" in webhook.upper() or len(webhook) < 40:
        warnings.append("CRITICAL: webhook_url выглядит как placeholder")
    # Проверить что config.json не world-readable
    cfg_path = Path("config.json")
    if cfg_path.exists():
        mode = oct(cfg_path.stat().st_mode)[-3:]
        if mode[2] != '0':
            warnings.append(f"config.json доступен для чтения другим пользователям (mode={mode}). "
                            f"Исправить: chmod 600 config.json")
    return warnings


def harden_config_file(path: str = "config.json"):
    """Установить права 600 на config.json."""
    p = Path(path)
    if p.exists():
        p.chmod(0o600)

# ─── аудит-лог ────────────────────────────────────────────────────────────────

_LOG_DIR = Path("logs")

def _audit_log_secret_found(source_label: str, found: list[SecretFound]):
    _LOG_DIR.mkdir(exist_ok=True)
    entry = {
        "ts":     datetime.now(timezone.utc).isoformat(),
        "event":  "secrets_redacted",
        "source": source_label,
        "count":  len(found),
        "items":  [{"label": f.label, "hash": f.hash} for f in found],
    }
    log_path = _LOG_DIR / "security_audit.jsonl"
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def audit_run_start(cfg: dict, mode: str):
    _LOG_DIR.mkdir(exist_ok=True)
    entry = {
        "ts":       datetime.now(timezone.utc).isoformat(),
        "event":    "run_start",
        "mode":     mode,
        "group_id": cfg.get("group_id"),
        "targets":  list(cfg.get("targets", {}).keys()),
    }
    with open(_LOG_DIR / "runs.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def audit_run_end(docs: dict, secrets_total: int):
    entry = {
        "ts":            datetime.now(timezone.utc).isoformat(),
        "event":         "run_end",
        "docs":          {k: len(v) for k, v in docs.items()},
        "secrets_found": secrets_total,
    }
    with open(_LOG_DIR / "runs.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
