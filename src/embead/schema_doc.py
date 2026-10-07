"""Print the packaged JSON report schemas as one line per field, so callers need not guess."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_PACKAGED = Path(__file__).resolve().parent / "schemas" / "v1"
_SOURCE_TREE = Path(__file__).resolve().parents[2] / "schemas" / "v1"


def schema_directory() -> Path:
    for candidate in (_PACKAGED, _SOURCE_TREE):
        if candidate.is_dir():
            return candidate
    raise RuntimeError("the packaged report schemas are missing from this installation")


def report_types() -> list[str]:
    return sorted(
        path.name.removesuffix(".schema.json") for path in schema_directory().glob("*.json")
    )


def load_schema(report_type: str) -> dict[str, Any]:
    path = schema_directory() / f"{report_type}.schema.json"
    if not path.is_file():
        raise ValueError(
            f"unknown report type {report_type!r}; choose from {', '.join(report_types())}"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve(node: Any, root: dict[str, Any]) -> dict[str, Any]:
    """Inline $ref and merge allOf so a field's properties read as one object."""

    if not isinstance(node, dict):
        return {}
    if "$ref" in node:
        name = node["$ref"].rsplit("/", 1)[-1]
        return _resolve(
            {
                **root.get("$defs", {}).get(name, {}),
                **{k: v for k, v in node.items() if k != "$ref"},
            },
            root,
        )
    if "allOf" in node:
        merged: dict[str, Any] = {"properties": {}, "required": []}
        for part in node["allOf"]:
            resolved = _resolve(part, root)
            merged["properties"].update(resolved.get("properties", {}))
            merged["required"].extend(resolved.get("required", []))
            for key, value in resolved.items():
                if key not in {"properties", "required"}:
                    merged.setdefault(key, value)
        return merged
    return node


def _type_text(node: dict[str, Any]) -> str:
    if "const" in node:
        return f"= {json.dumps(node['const'])}"
    if "enum" in node:
        return "one of " + ", ".join(json.dumps(value) for value in node["enum"])
    kind = node.get("type", "object" if "properties" in node else "any")
    return "|".join(kind) if isinstance(kind, list) else str(kind)


def field_lines(report_type: str, *, max_depth: int = 4) -> list[str]:
    """Return ``path  type  description`` lines for every documented field."""

    root = load_schema(report_type)
    lines = [f"# {root.get('title', report_type)}", "# * marks a required field"]

    def walk(node: Any, path: str, depth: int) -> None:
        resolved = _resolve(node, root)
        required = set(resolved.get("required", []))
        for name, child in resolved.get("properties", {}).items():
            child_resolved = _resolve(child, root)
            field_path = f"{path}.{name}" if path else name
            marker = "*" if name in required else " "
            description = child_resolved.get("description", "")
            line = f"{marker} {field_path}  ({_type_text(child_resolved)})"
            lines.append(f"{line}  {description}".rstrip())
            if depth >= max_depth:
                continue
            if child_resolved.get("type") == "array" or "items" in child_resolved:
                walk(child_resolved.get("items", {}), f"{field_path}[]", depth + 1)
            elif child_resolved.get("properties"):
                walk(child_resolved, field_path, depth + 1)

    walk(root, "", 0)
    return lines
