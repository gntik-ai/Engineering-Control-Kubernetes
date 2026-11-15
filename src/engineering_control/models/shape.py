"""The fixed shape of an answer: reading it out of what a model wrote, and checking it.

A model is always asked for one JSON object of a shape the platform states. Whatever the
provider promises, the platform checks the answer itself: what does not fit is not used.
The shapes are the small part of JSON Schema the platform's own answers need.
"""

from __future__ import annotations

import json
from typing import Any

_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "boolean": (bool,),
    "integer": (int,),
    "number": (int, float),
}


def read_object(text: str) -> dict[str, Any] | None:
    """The JSON object in what a model wrote: the whole text, or the object inside a
    code fence or a sentence around it. None when there is none."""
    said = (text or "").strip()
    candidates = [said]
    start, end = said.find("{"), said.rfind("}")
    if 0 <= start < end:
        candidates.append(said[start : end + 1])
    for candidate in candidates:
        try:
            found = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(found, dict):
            return found
    return None


def problem(schema: dict[str, Any], value: Any, where: str = "the answer") -> str | None:
    """Why the value does not have the shape, in a sentence, or None when it has."""
    kind = schema.get("type")
    if isinstance(kind, str):
        allowed = _TYPES.get(kind, ())
        # In JSON true is not a number, whatever Python thinks of it.
        if not isinstance(value, allowed) or (kind != "boolean" and isinstance(value, bool)):
            return f"{where} is not of the kind {kind}"
    choices = schema.get("enum")
    if isinstance(choices, list) and value not in choices:
        return f"{where} is not one of {', '.join(map(str, choices))}"
    if kind == "object":
        properties = schema.get("properties") or {}
        for name in schema.get("required") or []:
            if name not in value:
                return f"{where} has no {name}"
        for name, entry in value.items():
            if name not in properties:
                if schema.get("additionalProperties") is False:
                    return f"{where} has {name}, which is not asked for"
                continue
            wrong = problem(properties[name], entry, f"{name}")
            if wrong:
                return wrong
    if kind == "array" and isinstance(schema.get("items"), dict):
        for index, entry in enumerate(value, start=1):
            wrong = problem(schema["items"], entry, f"entry {index} of {where}")
            if wrong:
                return wrong
    return None


def words(schema: dict[str, Any]) -> str:
    """The shape as it is put before a model."""
    return json.dumps(schema, indent=1, sort_keys=True)
