"""Достаёт список group_id проектов из ответа config.php (для matrix workflow)."""
import json
import sys

path = sys.argv[1]
with open(path, encoding="utf-8") as f:
    raw = f.read().rsplit("HTTP_", 1)[0]
data = json.loads(raw)
groups = [int(p["group_id"]) for p in data.get("projects", [])]
print(json.dumps(groups))
