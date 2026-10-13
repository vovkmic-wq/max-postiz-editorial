"""Generate versioned JSON schemas and frozen migration DDL in this workspace only."""
import json
from pathlib import Path

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from max_postiz_agent import hub_models  # noqa: F401
from max_postiz_agent.db import Base


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


statements = []
for table in Base.metadata.sorted_tables:
    if table.name.startswith("hub_"):
        statements.append(str(CreateTable(table).compile(dialect=postgresql.dialect())).strip())
        statements.extend(str(CreateIndex(i).compile(dialect=postgresql.dialect())) for i in table.indexes)
statements.append("CREATE INDEX hub_material_search ON materials USING gin (to_tsvector('russian', title || ' ' || text))")
ddl = Path("migrations/versions/0003_schema.sql")
if not ddl.exists():
    ddl.write_text(";\n".join(statements) + ";\n", encoding="utf-8")
schema = json.loads(Path("schemas/sources.schema.json").read_text(encoding="utf-8"))
source = schema["$defs"]["source"]
if "json_api" not in source["properties"]["type"]["enum"]:
    source["properties"]["type"]["enum"].append("json_api")
source["properties"]["api"] = {
    "type": "object", "additionalProperties": False, "required": ["items_path", "fields"],
    "properties": {
        "items_path": {"type": "string"},
        "fields": {"type": "object", "additionalProperties": False, "required": ["id", "text", "url"],
                   "properties": {k: {"type": "string", "minLength": 1} for k in ("id", "title", "text", "url", "published_at")}},
        "next_path": {"type": "string"}, "cursor_param": {"type": "string"},
        "max_pages": {"type": "integer", "minimum": 1, "maximum": 20},
        "auth_header": {"enum": ["Authorization", "X-API-Key"]},
        "auth_prefix": {"type": "string", "maxLength": 32}}}
condition = {"if": {"properties": {"type": {"const": "json_api"}}, "required": ["type"]},
             "then": {"required": ["api"], "properties": {"endpoint": {"type": "string", "format": "uri", "pattern": "^https?://"}}}}
if condition not in source["allOf"]:
    source["allOf"].append(condition)
write("schemas/sources.schema.json", schema)
target = {"type": "object", "additionalProperties": False,
          "required": ["id", "profile_id", "platform", "route", "destination", "enabled"],
          "properties": {
              "id": {"type": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}$"},
              "profile_id": {"type": "string"},
              "platform": {"enum": ["telegram", "max", "vk", "pikabu"]},
              "route": {"enum": ["postiz", "max_browser", "max_api", "manual", "disabled"]},
              "destination": {"type": "string", "maxLength": 200}, "enabled": {"type": "boolean"},
              "options": {"type": "object", "additionalProperties": False,
                          "properties": {"max_chars": {"type": "integer", "minimum": 1, "maximum": 40000},
                                         "settings": {"type": "object"}}}}}
task = {"type": "object", "additionalProperties": False,
        "required": ["id", "profile_id", "interval_seconds", "lookback_hours", "enabled"],
        "properties": {"id": {"type": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}$"},
                       "profile_id": {"type": "string"}, "interval_seconds": {"type": "integer", "minimum": 60},
                       "lookback_hours": {"type": "integer", "minimum": 1, "maximum": 8760}, "enabled": {"type": "boolean"}}}
write("schemas/publishing.schema.json", {
    "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object", "additionalProperties": False,
    "required": ["schema_version", "postiz", "targets", "tasks"],
    "properties": {
        "schema_version": {"const": "1.0"},
        "postiz": {"type": "object", "additionalProperties": False, "properties": {
            "base_url_env": {"type": "string"}, "api_key_env": {"type": "string"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120},
            "max_video_verified": {"type": "boolean"}, "approval_guard_verified": {"type": "boolean"}}},
        "targets": {"type": "array", "items": target}, "tasks": {"type": "array", "items": task}}})
write("config/publishing.example.json", {"schema_version": "1.0", "postiz": {
    "base_url_env": "POSTIZ_BASE_URL", "api_key_env": "POSTIZ_API_KEY", "approval_guard_verified": False,
    "max_video_verified": False}, "targets": [{"id": p, "profile_id": "test", "platform": p,
    "route": "manual" if p == "pikabu" else "max_browser" if p == "max" else "disabled", "destination": "",
    "enabled": False, "options": {"max_chars": 15000 if p == "pikabu" else 3500}}
    for p in ("max", "pikabu", "telegram", "vk")], "tasks": []})
print("Schemas and frozen DDL prepared")
