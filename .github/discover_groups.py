"""Достаёт список group_id проектов из ответа config.php (для matrix workflow)."""
import json
import sys
import traceback

try:
    path = sys.argv[1]
    with open(path, encoding="utf-8") as f:
        raw = f.read().rsplit("HTTP_", 1)[0]
    data = json.loads(raw)
    groups = [int(p["group_id"]) for p in data.get("projects", [])]
    print(json.dumps(groups))
except Exception:
    print("DISCOVER_GROUPS_ERROR", file=sys.stderr)
    traceback.print_exc(file=sys.stderr)
    sys.stderr.flush()
    sys.exit(1)
