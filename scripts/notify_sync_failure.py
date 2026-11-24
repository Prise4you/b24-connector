"""Send a Bitrix24 chat alert for a failed production sync attempt.

The GitHub workflow classifies the upstream run before exposing the Bitrix
webhook to the send step. Neither logs nor the message contain source data.
"""

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit


REPO = "Prise4you/b24-connector"
WORKFLOW = "B24 → NotebookLM Sync"
WORKFLOW_PATH = ".github/workflows/sync.yml"
CHAT_ID = "chat37500"
FAILED = {"failure", "timed_out", "cancelled"}
PHASES = {"обмен", "подготовка", "после обмена", "тест"}


def classify_sync(jobs: list[dict]) -> str | None:
    sync = next((job for job in jobs if job.get("name") == "sync"), None)
    if not sync or sync.get("conclusion") not in FAILED:
        return None
    steps = sync.get("steps") or []
    # Pending runs displaced by GitHub concurrency can have a cancelled job
    # with no runner or steps. Nothing actually failed in the connector.
    if not steps or not any(s.get("conclusion") not in (None, "skipped") for s in steps):
        return None
    connector = next((s for s in steps if s.get("name") == "Run connector"), None)
    if connector and connector.get("conclusion") in FAILED:
        return "обмен"
    if connector and connector.get("conclusion") == "success":
        return "после обмена"
    return "подготовка"


def trusted_source(env: dict[str, str]) -> bool:
    if env.get("ALERT_EVENT") == "workflow_dispatch":
        return env.get("GITHUB_REPOSITORY") == REPO and env.get("GITHUB_REF") == "refs/heads/main"
    return (
        env.get("ALERT_EVENT") == "workflow_run"
        and env.get("ALERT_SOURCE_REPO") == REPO
        and env.get("ALERT_HEAD_REPO") == REPO
        and env.get("ALERT_HEAD_BRANCH") == "main"
        and env.get("ALERT_WORKFLOW_NAME") == WORKFLOW
        and env.get("ALERT_WORKFLOW_PATH") == WORKFLOW_PATH
    )


def positive_int(value: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise ValueError("invalid run identifier")
    return int(value)


def fetch_attempt_jobs(run_id: int, attempt: int, token: str) -> list[dict]:
    url = (f"https://api.github.com/repos/{REPO}/actions/runs/{run_id}"
           f"/attempts/{attempt}/jobs?per_page=100")
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "b24-notebooklm-alert",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        raise ValueError("GitHub jobs response is invalid")
    return jobs


def write_output(send: bool, phase: str = "") -> None:
    path = Path(os.environ["GITHUB_OUTPUT"])
    with path.open("a", encoding="utf-8") as out:
        out.write(f"send={'true' if send else 'false'}\n")
        if send:
            out.write(f"phase={phase}\n")


def classify() -> None:
    env = os.environ
    if not trusted_source(env):
        write_output(False)
        return
    if env["ALERT_EVENT"] == "workflow_dispatch":
        write_output(True, "тест")
        return
    if env.get("ALERT_RUN_CONCLUSION") not in FAILED:
        write_output(False)
        return
    run_id = positive_int(env.get("ALERT_RUN_ID", ""))
    attempt = positive_int(env.get("ALERT_RUN_ATTEMPT", ""))
    jobs = fetch_attempt_jobs(run_id, attempt, env["GITHUB_TOKEN"])
    phase = classify_sync(jobs)
    write_output(phase is not None, phase or "")


def message_text(phase: str, run_id: int, attempt: int, test: bool) -> str:
    if phase not in PHASES:
        raise ValueError("invalid alert phase")
    now = datetime.now(timezone(timedelta(hours=10))).strftime("%d.%m.%Y %H:%M")
    if test:
        heading = "🧪 Тест уведомлений Б24 → NotebookLM"
    elif phase == "обмен":
        heading = "❌ Ошибка обмена Б24 → NotebookLM"
    elif phase == "подготовка":
        heading = "❌ Обмен Б24 → NotebookLM не стартовал"
    else:
        heading = "❌ Ошибка после обмена Б24 → NotebookLM (очередь дайджестов)"
    link = f"https://github.com/{REPO}/actions/runs/{run_id}"
    return (f"{heading}\nВремя: {now} (Владивосток)\n"
            f"Прогон: [URL={link}]#{run_id}[/URL], попытка {attempt}. "
            "Подробности — по ссылке в GitHub.")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def send_message(webhook: str, text: str) -> int:
    parts = urlsplit(webhook)
    if (parts.scheme != "https" or parts.hostname != "anit.bitrix24.ru"
            or parts.port is not None or parts.username or parts.password
            or parts.query or parts.fragment
            or not re.fullmatch(r"/rest/[0-9]+/[A-Za-z0-9_-]+/?", parts.path)):
        raise ValueError("invalid Bitrix24 alert webhook")
    url = webhook.rstrip("/") + "/im.message.add.json"
    body = json.dumps({"DIALOG_ID": CHAT_ID, "MESSAGE": text}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    opener = urllib.request.build_opener(NoRedirect)
    with opener.open(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    result = data.get("result")
    if not (isinstance(result, int) and not isinstance(result, bool) and result > 0):
        raise ValueError("Bitrix24 did not confirm a message ID")
    return result


def send() -> None:
    env = os.environ
    if not trusted_source(env):
        return
    run_id = positive_int(env["ALERT_RUN_ID"])
    attempt = positive_int(env["ALERT_RUN_ATTEMPT"])
    phase = env["ALERT_PHASE"]
    test = env["ALERT_EVENT"] == "workflow_dispatch"
    text = message_text(phase, run_id, attempt, test)
    message_id = send_message(env["B24_NLM_ALERT_WEBHOOK"], text)
    print(f"Bitrix24 alert delivered: message_id={message_id}")


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2 or sys.argv[1] not in {"classify", "send"}:
            raise ValueError("expected classify or send")
        {"classify": classify, "send": send}[sys.argv[1]]()
    except Exception as exc:
        # Exception text can contain an HTTP URL (and thus a webhook token).
        print(f"Alert step failed: {type(exc).__name__}", file=sys.stderr)
        sys.exit(1)
