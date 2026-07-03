"""
Достаёт список group_id проектов из ответа config.php и пишет его прямо
в GITHUB_OUTPUT (для matrix workflow). Не полагается на захват stdout
через $(...) в bash — там наблюдалась ненадёжность на GH Actions runner'е.
"""
import json
import os
import sys
import traceback

try:
    path = sys.argv[1]
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    data = json.loads(raw)
    groups = [int(p["group_id"]) for p in data.get("projects", [])]
    result = json.dumps(groups)
    print(f"Проекты: {result}")

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"groups={result}\n")
except Exception:
    print("DISCOVER_GROUPS_ERROR", file=sys.stderr)
    traceback.print_exc(file=sys.stderr)
    sys.exit(1)
