"""NEW-120: deterministic, model-free inventory of a legacy web console.

Productises the research scripts (``falcone-console-inventory.data/{pages,xcheck,dead}.py``)
as library code. Given a source checkout and the project's ``ui_migration`` policy it
reports, read-only:

- routes -> page file -> imported components, LOC and a complexity tier;
- the endpoints each page reaches (method and path) through its import closure;
- a cross-check of every endpoint against the backend route tables
  (``OK``, ``ROUTED_NOT_PUBLISHED``, ``GATEWAY_ONLY``, ``NO_ROUTE``);
- dead modules (unreachable from the entry point) and orphan pages (not routed);
- the Vitest files and Playwright specs that reference each page.

The output is a versioned JSON document (schema version, source SHA and a hash of the
inventory itself) plus a Markdown summary. Nothing is executed, written or fetched: the
JS/TS sources are tokenised by a small scanner, never evaluated.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from engineering_control.engineering.models import UiMigrationPolicy
from engineering_control.security.redaction import SecretRedactor

# 2 (NEW-136): records `legacy_app_tree`, the Git tree id of the legacy app directory.
SCHEMA_VERSION = 2
# NEW-136 review: a console scan root that is (or traverses) a symlink; the plan blocks.
SYMLINK_ROOT = "UI_INVENTORY_SYMLINK_ROOT"
GENERATOR = "engineering_control.engineering.ui_inventory"
HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
# The call site's method could not be determined (indirect or computed options).
UNKNOWN_METHOD = "UNKNOWN"

# Size tiers by the page file's own LOC, as in the research report (S/M/L/XL).
TIER_THRESHOLDS: tuple[tuple[int, str], ...] = ((150, "small"), (500, "medium"), (1000, "large"))
TIER_XL = "xl"

# Closure traversal stops at the shell, context and primitive modules, so a page's closure
# measures the page and what it delegates to, not the whole console (research pages.py).
CLOSURE_STOP_PREFIXES = (
    "layouts/",
    "components/ui/",
    "router.tsx",
    "lib/console-context",
    "lib/console-session",
)
# Request helpers whose first argument is the URL (lib/http.ts, lib/console-session.ts and
# the local `request` wrappers of the service modules).
REQUEST_HELPERS = frozenset(
    {
        "fetch",
        "request",
        "requestJson",
        "requestConsoleSessionJson",
        "performAuthenticatedRequest",
    }
)
SOURCE_SUFFIXES = (".ts", ".tsx", ".mts", ".mjs")
MAX_SOURCE_BYTES = 2_000_000
MAX_SOURCE_FILES = 20_000
_RESOLVE_SUFFIXES = ("", ".ts", ".tsx", ".mts", ".mjs", ".d.mts", "/index.ts", "/index.tsx")
_QUERY_EXPRESSION = re.compile(r"(?i)(query|search|params|qs|suffix)")
_ENDPOINT = re.compile(r"^(?:\{\})?(/(?:v1|api)/[^\s'\"`]*)")
_IMPORT = re.compile(
    r"""(?:import|export)\s+(?:type\s+)?(?:[^'"]*?\sfrom\s+)?['"](@/[^'"]+|\.{1,2}/[^'"]+)['"]"""
    r"""|import\(\s*['"](@/[^'"]+|\.{1,2}/[^'"]+)['"]"""
)
_NAMED_IMPORT = re.compile(
    r"""import\s+(?:type\s+)?\{([^}]*)\}\s*from\s*['"]([^'"]+)['"]"""
    r"""|import\s+([A-Za-z_$][\w$]*)\s*(?:,\s*\{[^}]*\})?\s*from\s*['"]([^'"]+)['"]"""
)
_LAZY_IMPORT = re.compile(
    r"""(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=\s*lazy\(.{0,200}?import\(\s*['"]([^'"]+)['"]""",
    re.DOTALL,
)
_ROUTES_ARRAY = re.compile(
    r"(?:export\s+)?const\s+\w*[Rr]outes\s*(?::[^=]{0,200})?=\s*\[|createBrowserRouter\(\s*\["
)
_JSX_TAG = re.compile(r"<([A-Z][\w$]*)")
_IDENT = re.compile(r"[A-Za-z_$][\w$]*")


class EndpointClass(StrEnum):
    OK = "OK"
    ROUTED_NOT_PUBLISHED = "ROUTED_NOT_PUBLISHED"
    GATEWAY_ONLY = "GATEWAY_ONLY"
    NO_ROUTE = "NO_ROUTE"


class InventoryError(ValueError):
    """The checkout or a route table cannot be inventoried."""


# --------------------------------------------------------------------------------------
# A minimal JS/TS lexer: comments and string/template literals, nothing else.
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class JsLiteral:
    start: int
    end: int  # exclusive, after the closing quote
    value: str  # template expressions replaced by "{}"
    expressions: tuple[str, ...] = ()


@dataclass(frozen=True)
class JsSource:
    text: str
    code: str  # comments blanked; literals kept
    masked: str  # comments and literal bodies blanked (quotes kept)
    literals: tuple[JsLiteral, ...]
    by_start: dict[int, JsLiteral] = field(default_factory=dict, compare=False, repr=False)

    def literals_in(self, start: int, end: int) -> list[JsLiteral]:
        return [item for item in self.literals if start <= item.start and item.end <= end]

    def literal_at(self, position: int) -> JsLiteral | None:
        return self.by_start.get(position)


def scan_js(text: str) -> JsSource:
    code = list(text)
    masked = list(text)
    literals: list[JsLiteral] = []
    index = 0
    length = len(text)

    def blank(buffer: list[str], start: int, end: int) -> None:
        for position in range(start, end):
            if buffer[position] != "\n":
                buffer[position] = " "

    while index < length:
        char = text[index]
        pair = text[index : index + 2]
        if pair == "//":
            end = text.find("\n", index)
            end = length if end < 0 else end
            blank(code, index, end)
            blank(masked, index, end)
            index = end
            continue
        if pair == "/*":
            end = text.find("*/", index + 2)
            end = length if end < 0 else end + 2
            blank(code, index, end)
            blank(masked, index, end)
            index = end
            continue
        if char in {"'", '"'}:
            closing = _scan_quoted(text, index)
            if closing is None:  # unterminated on this line (JSX text, regex literal): skip
                index += 1
                continue
            literals.append(JsLiteral(index, closing, _unescape(text[index + 1 : closing - 1])))
            blank(masked, index + 1, closing - 1)
            index = closing
            continue
        if char == "`":
            end, value, expressions = _scan_template(text, index)
            literals.append(JsLiteral(index, end, value, tuple(expressions)))
            blank(masked, index + 1, end - 1)
            index = end
            continue
        index += 1
    return JsSource(
        text,
        "".join(code),
        "".join(masked),
        tuple(literals),
        {literal.start: literal for literal in literals},
    )


def _scan_quoted(text: str, start: int) -> int | None:
    quote = text[start]
    index = start + 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "\n":
            return None
        if char == quote:
            return index + 1
        index += 1
    return None


def _unescape(value: str) -> str:
    return re.sub(r"\\(.)", r"\1", value)


def _scan_template(text: str, start: int) -> tuple[int, str, list[str]]:
    index = start + 1
    parts: list[str] = []
    expressions: list[str] = []
    while index < len(text):
        char = text[index]
        if char == "\\":
            parts.append(text[index + 1 : index + 2])
            index += 2
            continue
        if char == "`":
            return index + 1, "".join(parts), expressions
        if text.startswith("${", index):
            end = _scan_expression(text, index + 2)
            expression = text[index + 2 : end - 1].strip()
            expressions.append(expression)
            parts.append("\x00" + str(len(expressions) - 1) + "\x00")
            index = end
            continue
        parts.append(char)
        index += 1
    return len(text), "".join(parts), expressions


def _scan_expression(text: str, index: int) -> int:
    depth = 1
    while index < len(text):
        char = text[index]
        if char in {"'", '"'}:
            end = _scan_quoted(text, index)
            index = end if end is not None else index + 1
            continue
        if char == "`":
            index = _scan_template(text, index)[0]
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return len(text)


def cooked_template(literal: JsLiteral) -> str:
    """The literal value with each `${expr}` replaced by "{}".

    A trailing expression glued to a complete segment (`/v1/plans${query}`) is a query
    string builder, not a path parameter, and is dropped; so is a trailing query-named one.
    """

    def replace(match: re.Match[str]) -> str:
        position = int(match.group(1))
        expression = literal.expressions[position]
        if literal.value[match.end() :]:
            return "{}"
        preceding = literal.value[: match.start()]
        if preceding and not preceding.endswith("/"):
            if preceding[-1].isalnum() or _QUERY_EXPRESSION.search(expression):
                return ""
        return "{}"

    return re.sub(r"\x00(\d+)\x00", replace, literal.value)


def _matching(masked: str, start: int) -> int:
    """Index of the bracket closing masked[start] (== opener), or -1."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack: list[str] = []
    for index in range(start, len(masked)):
        char = masked[index]
        if char in pairs:
            stack.append(pairs[char])
        elif char in pairs.values():
            if not stack or stack.pop() != char:
                return -1
            if not stack:
                return index
    return -1


def _top_level_split(
    masked: str, start: int, end: int, separator: str = ","
) -> list[tuple[int, int]]:
    parts: list[tuple[int, int]] = []
    depth = 0
    segment = start
    for index in range(start, end):
        char = masked[index]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == separator and depth == 0:
            parts.append((segment, index))
            segment = index + 1
    parts.append((segment, end))
    return parts


# --------------------------------------------------------------------------------------
# Endpoint extraction
# --------------------------------------------------------------------------------------


def endpoint_path(literal: JsLiteral, source: JsSource) -> str | None:
    """Normalised endpoint path template for a URL literal, or None."""
    value = cooked_template(literal) if literal.expressions else literal.value
    match = _ENDPOINT.match(value)
    if match is None:
        return None
    path = match.group(1).split("?", 1)[0].split("#", 1)[0]
    following = source.masked[literal.end : literal.end + 40]
    if path.endswith("/") and re.match(r"\s*\+\s*[A-Za-z_$(]", following):
        path += "{}"  # '/v1/tenants/' + tenantId
    path = path.rstrip("/") or "/"
    return normalize_path(path)


def normalize_path(path: str) -> str:
    """Collapse every parameter spelling ({id}, :id, ${...}) to "{}"."""
    segments = []
    for segment in path.split("/"):
        segment = re.sub(r"\{[^{}/]*\}", "{}", segment)
        if re.fullmatch(r":[A-Za-z_][\w-]*", segment):
            segment = "{}"
        segments.append(segment)
    return "/".join(segments)


@dataclass(frozen=True, order=True)
class EndpointUse:
    method: str  # GET/POST/... or "*" when the call site's method is unknown
    path: str


def extract_endpoints(source: JsSource) -> set[EndpointUse]:
    """Every (method, path) the module requests.

    A URL literal is attributed to a request-helper call when it is the call's first
    argument, the variable that argument names, or the literal returned by the URL-builder
    function it calls; a template argument starting with such a builder
    (`${flowsBase(id)}/${flowId}`) is composed with the builder's path. A URL literal no call
    can be tied to is still reported, with method "*".
    """
    paths: dict[int, str] = {}
    for literal in source.literals:
        path = endpoint_path(literal, source)
        if path is not None:
            paths[literal.start] = path
    if not paths:
        return set()
    attributed: dict[int, set[str]] = {}
    uses: set[EndpointUse] = set()
    for start, _end, first_argument, arguments in _request_calls(source):
        if source.masked[first_argument[0] : first_argument[1]].strip().startswith("..."):
            continue  # fetch(...args): the URL is not this call's own literal
        method = _call_method(source, arguments)
        for literal_start in _argument_literals(source, start, first_argument):
            if literal_start in paths:
                attributed.setdefault(literal_start, set()).add(method)
        for base_start, path in _composed_paths(source, paths, start, first_argument):
            attributed.setdefault(base_start, set())
            uses.add(EndpointUse(method, path))
    for literal_start, path in paths.items():
        for method in attributed.get(literal_start, set()) or (
            set() if literal_start in attributed else {UNKNOWN_METHOD}
        ):
            uses.add(EndpointUse(method, path))
    return uses


def _composed_paths(
    source: JsSource, paths: dict[int, str], call_start: int, argument: tuple[int, int]
) -> list[tuple[int, str]]:
    offset = len(source.masked[argument[0] : argument[1]]) - len(
        source.masked[argument[0] : argument[1]].lstrip()
    )
    literal = source.literal_at(argument[0] + offset)
    if literal is None or not literal.expressions or not literal.value.startswith("\x000\x00"):
        return []
    if literal.start in paths:
        return []
    expression = literal.expressions[0]
    bases: set[int] = set()
    if re.fullmatch(r"[A-Za-z_$][\w$]*", expression):
        bases = _variable_literals(source, expression, before=call_start)
    else:
        call = re.match(r"(?:[A-Za-z_$][\w$]*\.)*([A-Za-z_$][\w$]*)\s*\(", expression)
        if call:
            bases = _function_literals(source, call.group(1))
    rest = cooked_template(literal)[2:].split("?", 1)[0]
    composed = []
    for base in sorted(bases):
        if base in paths and rest.startswith("/"):
            composed.append((base, normalize_path((paths[base] + rest).rstrip("/"))))
    return composed


def _request_calls(source: JsSource) -> Iterable[tuple[int, int, tuple[int, int], tuple[int, int]]]:
    masked = source.masked
    names = "|".join(sorted(REQUEST_HELPERS))
    pattern = re.compile(r"(?<![\w$.])(" + names + r")(?![\w$])\s*")
    for match in pattern.finditer(masked):
        index = match.end()
        if index < len(masked) and masked[index] == "<":
            depth = 0
            while index < len(masked):
                if masked[index] == "<":
                    depth += 1
                elif masked[index] == ">":
                    depth -= 1
                    if depth == 0:
                        index += 1
                        break
                index += 1
            while index < len(masked) and masked[index].isspace():
                index += 1
        if index >= len(masked) or masked[index] != "(":
            continue
        close = _matching(masked, index)
        if close < 0:
            continue
        arguments = (index + 1, close)
        parts = _top_level_split(masked, index + 1, close)
        yield match.start(), close + 1, parts[0], arguments


_PLAIN_KEY = re.compile(r"[A-Za-z_$][\w$]*|[0-9]+")


def _call_method(source: JsSource, arguments: tuple[int, int]) -> str:
    """The verb from the call's own top-level options object literal, else UNKNOWN.

    `fn(url)` is GET. `fn(url, {...})` is known only when every property of the literal
    is plain -- `key: value` with an unescaped identifier, string or number key, or a
    non-`method` identifier shorthand (`{ body }` is `body: body`) -- and exactly one is
    `method`, whose whole value is one complete string literal, with no spread after it.
    No `method` and no spread is GET. Anything else -- an argument spread, indirect options
    (`fn(url, opts)`), accessors (`get method() {}`), method shorthand, computed (`[k]`) or
    escaped keys, `__proto__`, partial values (`'PO' + x`), a later `...opts` -- is UNKNOWN.
    """
    masked = source.masked
    parts = _top_level_split(masked, *arguments)
    if any(masked[a:b].strip().startswith("...") for a, b in parts):
        return UNKNOWN_METHOD  # fetch(...args): neither the URL nor the options are known
    if len(parts) < 2 or not masked[parts[1][0] : parts[1][1]].strip():
        return "GET"  # fetch() and requestJson() default to GET
    if len(parts) > 2 and any(masked[a:b].strip() for a, b in parts[2:]):
        return UNKNOWN_METHOD
    start, end = parts[1]
    text = masked[start:end]
    open_index = start + len(text) - len(text.lstrip())
    if masked[open_index] != "{":
        return UNKNOWN_METHOD
    close = _matching(masked, open_index)
    if close < 0 or close >= end:
        return UNKNOWN_METHOD
    if not re.fullmatch(r"\s*(?:as\s+const)?\s*", masked[close + 1 : end]):
        return UNKNOWN_METHOD
    method: str | None = None
    methods = 0
    spread_after_method = False
    spread = False
    for prop_start, prop_end in _top_level_split(masked, open_index + 1, close):
        segment = masked[prop_start:prop_end]
        stripped = segment.strip()
        if not stripped:
            continue
        key_start = prop_start + len(segment) - len(segment.lstrip())
        if stripped.startswith("..."):
            spread = True
            spread_after_method = spread_after_method or methods > 0
            continue
        key = source.literal_at(key_start)
        if key is not None:
            raw = source.text[key.start : key.end]
            if key.expressions or "\\" in raw or raw.startswith("`"):
                return UNKNOWN_METHOD  # template or escaped key
            name, key_end = key.value, key.end
        else:
            identifier = _PLAIN_KEY.match(stripped)
            if identifier is None:
                return UNKNOWN_METHOD  # computed key, generator, ...
            name, key_end = identifier.group(0), key_start + identifier.end()
        rest = masked[key_end:prop_end]
        colon = re.match(r"\s*:\s*", rest)
        if colon is None:
            if rest.strip() or key is not None or name in {"method", "__proto__"}:
                return UNKNOWN_METHOD  # accessor, method shorthand, escaped identifier
            continue  # `{ body }`: an identifier shorthand for `body: body`
        if name == "__proto__" or not masked[key_end + colon.end() : prop_end].strip():
            return UNKNOWN_METHOD  # prototype-derived properties
        if name != "method":
            continue
        methods += 1
        value = source.literal_at(key_end + colon.end())
        if value is None or value.expressions or masked[value.end : prop_end].strip():
            return UNKNOWN_METHOD  # not one complete string literal
        method = value.value.upper()
    if methods == 0:
        return UNKNOWN_METHOD if spread else "GET"
    if methods > 1 or spread_after_method or method not in HTTP_METHODS:
        return UNKNOWN_METHOD
    return str(method)


def _argument_literals(source: JsSource, call_start: int, argument: tuple[int, int]) -> set[int]:
    start, end = argument
    found = {literal.start for literal in source.literals_in(start, end)}
    text = source.masked[start:end].strip()
    identifier = re.fullmatch(r"([A-Za-z_$][\w$]*)", text)
    if identifier:
        found |= _variable_literals(source, identifier.group(1), before=call_start)
    call = re.match(r"(?:[A-Za-z_$][\w$]*\.)*([A-Za-z_$][\w$]*)\s*\(", text)
    if call:
        found |= _function_literals(source, call.group(1))
    return found


def _variable_literals(source: JsSource, name: str, *, before: int) -> set[int]:
    pattern = re.compile(r"(?<![\w$.])(?:const|let|var)\s+" + re.escape(name) + r"\s*(?::[^=]+)?=")
    matches = [m for m in pattern.finditer(source.masked, 0, before)]
    if not matches:
        return set()
    start = matches[-1].end()
    end = _statement_end(source.masked, start)
    return {literal.start for literal in source.literals_in(start, end)}


def _function_literals(source: JsSource, name: str) -> set[int]:
    masked = source.masked
    escaped = re.escape(name)
    found: set[int] = set()
    pattern = re.compile(
        r"(?<![\w$.])(?:(function)\s+" + escaped + r"\s*(?:<[^>]*>)?\s*(\()"
        r"|(?:const|let)\s+" + escaped + r"\s*(?::[^=]+)?=)"
    )
    for match in pattern.finditer(masked):
        if match.group(1):
            params_close = _matching(masked, match.start(2))
            if params_close < 0:
                continue
            body = _function_body(masked, params_close + 1)
        else:
            body = _arrow_body(masked, match.end())
        if body is not None:
            found |= {literal.start for literal in source.literals_in(*body)}
    return found


def _function_body(masked: str, position: int) -> tuple[int, int] | None:
    """The `{...}` body after a parameter list, skipping an object-typed return type."""
    while True:
        brace = masked.find("{", position)
        if brace < 0:
            return None
        close = _matching(masked, brace)
        if close < 0:
            return None
        following = masked[close + 1 :].lstrip()
        if following.startswith((">", "|", "&", "[", "{")):
            position = close + 1
            continue
        return brace, close + 1


def _arrow_body(masked: str, position: int) -> tuple[int, int] | None:
    statement_end = _statement_end(masked, position)
    arrow = masked.find("=>", position, statement_end)
    if arrow < 0:
        if re.match(r"\s*(?:async\s+)?function\b", masked[position:]):
            paren = masked.find("(", position, statement_end)
            close = _matching(masked, paren) if paren >= 0 else -1
            return _function_body(masked, close + 1) if close >= 0 else None
        return position, statement_end
    body_start = arrow + 2
    stripped = masked[body_start:statement_end].lstrip()
    if stripped.startswith("{"):
        open_index = masked.index("{", body_start)
        close = _matching(masked, open_index)
        return (open_index, close + 1) if close >= 0 else None
    return body_start, _statement_end(masked, body_start)


def _statement_end(masked: str, start: int) -> int:
    depth = 0
    for index in range(start, len(masked)):
        char = masked[index]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0:
                return index
            depth -= 1
        elif depth == 0 and (char == ";" or char == "\n" and _line_complete(masked, index)):
            return index
    return len(masked)


_CONTINUES_AFTER = ("+", "=", ",", "(", "?", ":", "&", "|", ">")
_CONTINUES_BEFORE = ("+", ".", "?", ":", "&", "|")


def _line_complete(masked: str, index: int) -> bool:
    before = index - 1
    while before >= 0 and masked[before].isspace():
        before -= 1
    after = index + 1
    while after < len(masked) and masked[after].isspace():
        after += 1
    if before >= 0 and masked[before] in _CONTINUES_AFTER:
        return False
    return not (after < len(masked) and masked[after] in _CONTINUES_BEFORE)


# --------------------------------------------------------------------------------------
# Route tables and the cross-check
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RoutePattern:
    methods: frozenset[str] | None  # None = any method
    regex: re.Pattern[str]
    template: str
    upstream: str = ""
    uri_condition: re.Pattern[str] | None = None


def route_regex(template: str) -> re.Pattern[str]:
    path = normalize_path(template.split("?", 1)[0])
    prefix = path.endswith("/*")
    if prefix:
        path = path[:-2]
    expression = re.escape(path).replace(re.escape("{}"), "[^/]+").replace(r"\*", ".*")
    suffix = "(?:/.*)?" if prefix else "/?"
    return re.compile("^" + expression + suffix + "$")


def _pattern(
    methods: Iterable[str] | None,
    template: str,
    upstream: str = "",
    uri_condition: re.Pattern[str] | None = None,
) -> RoutePattern:
    method_set = frozenset(m.upper() for m in methods) if methods is not None else None
    if method_set is not None and not method_set & set(HTTP_METHODS):
        method_set = None
    return RoutePattern(method_set, route_regex(template), template, upstream, uri_condition)


def parse_routes_module(text: str) -> list[RoutePattern]:
    """`{ method: 'GET', path: '/v1/...' }` entries of control-plane/routes.mjs."""
    source = scan_js(text)
    patterns = []
    for match in re.finditer(r"method\s*:\s*", source.masked):
        method = source.literal_at(match.end())
        if method is None:
            continue
        rest = source.masked[method.end : method.end + 40]
        path_match = re.match(r"\s*,\s*path\s*:\s*", rest)
        if path_match is None:
            continue
        path = source.literal_at(method.end + path_match.end())
        if path is None:
            continue
        patterns.append(_pattern([method.value], path.value))
    return patterns


def parse_route_map_json(value: Any) -> list[RoutePattern]:
    rows = value.get("routes", []) if isinstance(value, dict) else value
    patterns = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and isinstance(row.get("path"), str):
            method = row.get("method")
            patterns.append(_pattern([method] if isinstance(method, str) else None, row["path"]))
    return patterns


def parse_openapi(value: Any) -> list[RoutePattern]:
    paths = value.get("paths") if isinstance(value, dict) else None
    patterns = []
    for path, item in (paths or {}).items():
        if not isinstance(item, dict):
            continue
        methods = [key for key in item if key.upper() in HTTP_METHODS]
        patterns.append(_pattern(methods or None, str(path)))
    return patterns


def parse_apisix(value: Any) -> list[RoutePattern]:
    routes = value.get("routes") if isinstance(value, dict) else None
    patterns = []
    for route in routes or []:
        if not isinstance(route, dict):
            continue
        uris = route.get("uris") or ([route["uri"]] if isinstance(route.get("uri"), str) else [])
        upstream = route.get("upstream") or {}
        nodes = upstream.get("nodes") if isinstance(upstream, dict) else None
        if isinstance(nodes, dict):
            hosts = sorted(str(node) for node in nodes)
        elif isinstance(nodes, list):
            hosts = sorted(str(node.get("host", "")) for node in nodes if isinstance(node, dict))
        else:
            hosts = []
        label = ",".join(sorted({host.split(":", 1)[0].split(".", 1)[0] for host in hosts}))
        condition = _uri_condition(route.get("vars"))
        if condition is False:
            continue  # conditioned on request headers/arguments the console never sends
        methods = route.get("methods")
        for uri in uris:
            if isinstance(uri, str) and uri not in {"/*", "/"}:  # console catch-all
                patterns.append(
                    _pattern(
                        methods if isinstance(methods, list) else None,
                        uri,
                        label,
                        condition if isinstance(condition, re.Pattern) else None,
                    )
                )
    return patterns


def _uri_condition(value: Any) -> re.Pattern[str] | bool:
    """APISIX `vars`: a `uri ~~ regex` narrows the route; any other variable (an `apikey`
    header, a query argument) makes it unreachable for the console's bearer calls."""
    if not value:
        return True
    if not isinstance(value, list):
        return False
    pattern: re.Pattern[str] | bool = True
    for item in value:
        if (
            isinstance(item, list)
            and len(item) == 3
            and item[0] == "uri"
            and item[1] == "~~"
            and isinstance(item[2], str)
        ):
            try:
                pattern = re.compile(item[2])
            except re.error:
                return False
        else:
            return False
    return pattern


def _control_plane_upstream(label: str) -> bool:
    return any(re.search(r"(?:^|-)control-plane$", part) for part in label.split(","))


def _console_upstream(label: str) -> bool:
    return any("console" in part for part in label.split(","))


def _matches(patterns: Sequence[RoutePattern], use: EndpointUse) -> list[RoutePattern]:
    probe = use.path.replace("{}", "__param__")
    hits = []
    for pattern in patterns:
        if use.method != UNKNOWN_METHOD and pattern.methods is not None:
            if use.method not in pattern.methods:
                continue
        if pattern.uri_condition is not None and not pattern.uri_condition.search(probe):
            continue
        if pattern.regex.match(probe):
            hits.append(pattern)
    return hits


@dataclass(frozen=True)
class RouteTables:
    control_plane: tuple[RoutePattern, ...]
    catalog: tuple[RoutePattern, ...]
    openapi: tuple[RoutePattern, ...]
    gateway: tuple[RoutePattern, ...]
    digests: dict[str, dict[str, Any]] = field(default_factory=dict)

    def classify(self, use: EndpointUse) -> tuple[EndpointClass, dict[str, Any]]:
        """Method-aware; an UNKNOWN method is never OK unless every verb the control plane
        routes for the path is also published (conservative: no OK on a possible mismatch)."""
        if use.method != UNKNOWN_METHOD:
            klass, evidence = self._classify_exact(use)
            return klass, {**evidence, "method_unknown": False}
        candidates = sorted(
            {
                method
                for pattern in _matches(self.control_plane, use)
                for method in (pattern.methods or HTTP_METHODS)
                if method in HTTP_METHODS
            }
        )
        klass, evidence = self._classify_exact(use)
        if candidates:
            exact = [self._classify_exact(EndpointUse(m, use.path))[0] for m in candidates]
            klass = (
                EndpointClass.OK
                if all(item is EndpointClass.OK for item in exact)
                else EndpointClass.ROUTED_NOT_PUBLISHED
            )
        return klass, {**evidence, "method_unknown": True, "routed_methods": candidates}

    def _classify_exact(self, use: EndpointUse) -> tuple[EndpointClass, dict[str, Any]]:
        control_plane = bool(_matches(self.control_plane, use))
        catalog = bool(_matches(self.catalog, use))
        openapi = bool(_matches(self.openapi, use))
        upstreams = sorted({hit.upstream for hit in _matches(self.gateway, use) if hit.upstream})
        served_elsewhere = any(
            not _control_plane_upstream(label) and not _console_upstream(label)
            for label in upstreams
        )
        published = catalog or openapi
        if control_plane and published:
            klass = EndpointClass.OK
        elif control_plane:
            klass = EndpointClass.ROUTED_NOT_PUBLISHED
        elif published or served_elsewhere:
            klass = EndpointClass.GATEWAY_ONLY
        else:
            klass = EndpointClass.NO_ROUTE
        evidence = {
            "control_plane": control_plane,
            "catalog": catalog,
            "openapi": openapi,
            "gateway_upstreams": upstreams,
        }
        return klass, evidence


def load_route_tables(root: Path, policy: UiMigrationPolicy) -> RouteTables:
    tables = policy.route_tables
    digests: dict[str, dict[str, Any]] = {}

    def load(name: str, relative: str | None) -> list[RoutePattern]:
        if relative is None:
            return []
        path = _inside(root, relative)
        if not path.is_file() or path.is_symlink():
            raise InventoryError(f"route table {name} is missing: {relative}")
        data = path.read_bytes()
        if len(data) > 20 * MAX_SOURCE_BYTES:
            raise InventoryError(f"route table {name} exceeds the size bound")
        text = data.decode("utf-8")
        try:
            if name == "control_plane_routes":
                patterns = parse_routes_module(text)
            elif name == "gateway_config":
                patterns = parse_apisix(yaml.safe_load(text))
            elif name == "executor_openapi":
                patterns = parse_openapi(json.loads(text))
            else:
                patterns = parse_route_map_json(json.loads(text))
        except (ValueError, yaml.YAMLError) as error:
            raise InventoryError(f"route table {name} cannot be parsed") from error
        if not patterns:
            raise InventoryError(f"route table {name} has no routes")
        digests[name] = {
            "path": relative,
            "sha256": hashlib.sha256(data).hexdigest(),
            "entries": len(patterns),
        }
        return patterns

    control_plane = load("control_plane_routes", tables.control_plane_routes) + load(
        "control_plane_runtime_map", tables.control_plane_runtime_map
    )
    return RouteTables(
        tuple(control_plane),
        tuple(load("public_route_catalog", tables.public_route_catalog)),
        tuple(load("executor_openapi", tables.executor_openapi)),
        tuple(load("gateway_config", tables.gateway_config)),
        digests,
    )


# --------------------------------------------------------------------------------------
# Module graph
# --------------------------------------------------------------------------------------


def _inside(root: Path, relative: str) -> Path:
    """`relative` inside the checkout, never reached through a symlink (NEW-136 review).

    A scan root that is or traverses a symlink is refused: the legacy app's Git tree id
    covers only the link, so a symlinked root would let console changes bypass the reuse
    key. Symlinks below a root are skipped and recorded (`_safe_files`)."""
    resolved_root = root.resolve()
    current = resolved_root
    for part in PurePosixPath(relative).parts:
        if part in {"/", ".", ".."}:
            raise InventoryError("inventory path escaped the checkout")
        current = current / part
        if current.is_symlink():
            raise InventoryError(
                f"{SYMLINK_ROOT}: inventory root is or traverses a symlink: {relative}"
            )
    path = current.resolve()
    if not path.is_relative_to(resolved_root):
        raise InventoryError("inventory path escaped the checkout")
    return path


def _safe_files(base: Path, root: Path, skipped: set[str]) -> list[Path]:
    """Regular files under `base` (itself resolved inside `root`), never through a symlink.

    Directory and file symlinks are not followed or read; they are recorded (repository-
    relative) in `skipped`, so a repository cannot point the scan outside its checkout.
    """
    if base.is_symlink() or not base.resolve().is_relative_to(root):
        raise InventoryError("inventory directory escaped the checkout")
    found: list[Path] = []
    for directory, dirnames, filenames in os.walk(base, followlinks=False):
        current = Path(directory)
        kept = []
        for name in sorted(dirnames):
            path = current / name
            if path.is_symlink():
                skipped.add(path.relative_to(root).as_posix())
            elif name != "node_modules":
                kept.append(name)
        dirnames[:] = kept
        for name in sorted(filenames):
            path = current / name
            if path.is_symlink():
                skipped.add(path.relative_to(root).as_posix())
            elif path.is_file() and path.resolve().is_relative_to(root):
                found.append(path)
    return sorted(found)


def _is_test(relative: str) -> bool:
    name = relative.rsplit("/", 1)[-1]
    return ".test." in name or ".spec." in name or "/__tests__/" in f"/{relative}"


@dataclass
class ModuleGraph:
    src: Path  # absolute <legacy_app>/src
    files: dict[str, JsSource]  # src-relative path -> source
    edges: dict[str, set[str]]
    named: dict[str, dict[str, str]]  # file -> local identifier -> imported module

    @classmethod
    def build(cls, src: Path, root: Path, skipped: set[str] | None = None) -> ModuleGraph:
        files: dict[str, JsSource] = {}
        candidates = [
            path
            for path in _safe_files(src, root, skipped if skipped is not None else set())
            if path.suffix in SOURCE_SUFFIXES and not path.name.endswith(".d.ts")
        ]
        if len(candidates) > MAX_SOURCE_FILES:
            raise InventoryError("legacy app has too many source files to inventory")
        for path in candidates:
            if path.stat().st_size > MAX_SOURCE_BYTES:
                raise InventoryError(f"source file exceeds the size bound: {path.name}")
            relative = path.relative_to(src).as_posix()
            if "/node_modules/" in f"/{relative}":
                continue
            files[relative] = scan_js(path.read_text(encoding="utf-8", errors="replace"))
        graph = cls(src, files, {}, {})
        for relative, source in files.items():
            graph.edges[relative] = graph._imports(relative, source)
            graph.named[relative] = graph._named_imports(relative, source)
        return graph

    def resolve(self, specifier: str, importer: str) -> str | None:
        if specifier.startswith("@/"):
            base = specifier[2:]
        elif specifier.startswith("."):
            parent = Path(importer).parent
            parts: list[str] = []
            for part in (parent / specifier).as_posix().split("/"):
                if part == "..":
                    if not parts:
                        return None
                    parts.pop()
                elif part not in {"", "."}:
                    parts.append(part)
            base = "/".join(parts)
        else:
            return None
        candidates = [base]
        stripped = re.sub(r"\.(js|mjs)$", "", base)
        if stripped != base:
            candidates.append(stripped)
        for candidate in candidates:
            for suffix in _RESOLVE_SUFFIXES:
                if candidate + suffix in self.files:
                    return candidate + suffix
        return None

    def _imports(self, relative: str, source: JsSource) -> set[str]:
        resolved: set[str] = set()
        for match in _IMPORT.finditer(source.code):
            target = self.resolve(match.group(1) or match.group(2), relative)
            if target is not None:
                resolved.add(target)
        return resolved

    def _named_imports(self, relative: str, source: JsSource) -> dict[str, str]:
        names: dict[str, str] = {}
        for match in _NAMED_IMPORT.finditer(source.code):
            if match.group(2) is not None:
                target = self.resolve(match.group(2), relative)
                for item in match.group(1).split(","):
                    local = item.strip().split(" as ")[-1].strip()
                    if target and _IDENT.fullmatch(local):
                        names[local] = target
            elif match.group(4) is not None:
                target = self.resolve(match.group(4), relative)
                if target:
                    names[match.group(3)] = target
        for match in _LAZY_IMPORT.finditer(source.code):
            target = self.resolve(match.group(2), relative)
            if target:
                names[match.group(1)] = target
        return names

    def reachable(self, entry: str) -> set[str]:
        seen: set[str] = set()
        stack = [entry]
        while stack:
            current = stack.pop()
            if current in seen or current not in self.files:
                continue
            seen.add(current)
            stack.extend(self.edges.get(current, ()))
        return seen

    def closure(self, start: str) -> set[str]:
        seen = {start}
        stack = [start]
        while stack:
            current = stack.pop()
            for target in self.edges.get(current, ()):
                if target in seen or _is_test(target):
                    continue
                if any(target.startswith(prefix) for prefix in CLOSURE_STOP_PREFIXES):
                    continue
                seen.add(target)
                stack.append(target)
        return seen

    def loc(self, relative: str) -> int:
        text = self.files[relative].text
        return text.count("\n") + (0 if text.endswith("\n") or not text else 1)


# --------------------------------------------------------------------------------------
# Router parsing
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RouteRecord:
    path: str
    page: str | None  # src-relative page file
    component: str | None
    guards: tuple[str, ...]


def parse_router(graph: ModuleGraph, router: str, pages_prefix: str) -> list[RouteRecord]:
    source = graph.files.get(router)
    if source is None:
        raise InventoryError(f"router module is missing: {router}")
    match = _ROUTES_ARRAY.search(source.masked)
    if match is None:
        raise InventoryError("router module has no route table")
    open_index = match.end() - 1
    close = _matching(source.masked, open_index)
    if close < 0:
        raise InventoryError("router table is not balanced")
    records: list[RouteRecord] = []
    _walk_routes(graph, router, source, open_index, close, "", pages_prefix, records)
    return records


def _walk_routes(
    graph: ModuleGraph,
    router: str,
    source: JsSource,
    open_index: int,
    close: int,
    parent: str,
    pages_prefix: str,
    records: list[RouteRecord],
) -> None:
    masked = source.masked
    for start, end in _top_level_split(masked, open_index + 1, close):
        brace = masked.find("{", start, end)
        if brace < 0:
            continue
        object_end = _matching(masked, brace)
        if object_end < 0:
            continue
        properties: dict[str, tuple[int, int]] = {}
        for prop_start, prop_end in _top_level_split(masked, brace + 1, object_end):
            key = re.match(r"\s*([A-Za-z_$][\w$]*)\s*:", masked[prop_start:prop_end])
            if key:
                properties[key.group(1)] = (prop_start + key.end(), prop_end)
        path = parent
        if "path" in properties:
            value = _route_path_value(graph, router, source, *properties["path"])
            path = value if value.startswith("/") else _join(parent, value)
        if "element" in properties:
            element = source.text[slice(*properties["element"])]
            tags = _JSX_TAG.findall(element)
            page: str | None = None
            component: str | None = None
            guards: list[str] = []
            for tag in tags:
                target = graph.named.get(router, {}).get(tag)
                if target is not None and target.startswith(pages_prefix) and page is None:
                    page, component = target, tag
                elif page is None:
                    guards.append(tag)
            if page is not None or "children" not in properties:
                records.append(RouteRecord(path or "/", page, component, tuple(guards)))
        if "children" in properties:
            child_start, child_end = properties["children"]
            bracket = masked.find("[", child_start, child_end)
            if bracket >= 0:
                child_close = _matching(masked, bracket)
                if child_close >= 0:
                    _walk_routes(
                        graph, router, source, bracket, child_close, path, pages_prefix, records
                    )


def _join(parent: str, child: str) -> str:
    if not parent or parent == "/":
        return "/" + child.lstrip("/")
    return parent.rstrip("/") + "/" + child.lstrip("/")


def _route_path_value(
    graph: ModuleGraph, router: str, source: JsSource, start: int, end: int
) -> str:
    literals = source.literals_in(start, end)
    if literals:
        return literals[0].value
    expression = source.masked[start:end].strip()
    member = re.fullmatch(r"([A-Za-z_$][\w$]*)\.([A-Za-z_$][\w$]*)", expression)
    if member:
        module = graph.named.get(router, {}).get(member.group(1))
        target = graph.files.get(module) if module else None
        if target is not None:
            found = re.search(re.escape(member.group(2)) + r"\s*:[^\n]*", target.masked)
            if found:
                candidates = [
                    literal.value
                    for literal in target.literals_in(found.start(), found.end())
                    if literal.value.startswith("/")
                ]
                if candidates:
                    return candidates[-1]
    return "{" + expression + "}"


# --------------------------------------------------------------------------------------
# Inventory
# --------------------------------------------------------------------------------------


def tier_for(loc: int) -> str:
    for limit, name in TIER_THRESHOLDS:
        if loc < limit:
            return name
    return TIER_XL


def _route_matches(route: str, literal: str) -> bool:
    if "{" in route and not route.startswith("/"):
        return False
    pattern = re.escape(route.rstrip("/") or "/")
    pattern = re.sub(r":[A-Za-z_][\w]*", "[^/]+", pattern.replace(r"\:", ":"))
    pattern = pattern.replace(r"\*", ".*")
    return re.fullmatch(pattern + "/?", literal) is not None


def build_inventory(
    checkout: Path,
    policy: UiMigrationPolicy,
    *,
    source_sha: str,
    project: str,
    legacy_app_tree: str,
) -> dict[str, Any]:
    """`legacy_app_tree`: the Git tree id of `policy.legacy_app` at `source_sha` (see
    `legacy_app_tree`); it changes exactly when a file of the legacy app changes."""
    if not re.fullmatch(r"[a-f0-9]{40}", source_sha):
        raise InventoryError("source SHA must be a full 40-character commit")
    if not _GIT_OBJECT.fullmatch(legacy_app_tree):
        raise InventoryError("legacy app tree must be a full Git object id")
    root = checkout.resolve()
    _inside(root, policy.legacy_app)
    src = _inside(root, f"{policy.legacy_app}/src")
    if not src.is_dir():
        raise InventoryError(f"legacy app has no src directory: {policy.legacy_app}")
    tables = load_route_tables(root, policy)
    skipped: set[str] = set()
    graph = ModuleGraph.build(src, root, skipped)
    entry = "main.tsx" if "main.tsx" in graph.files else "main.ts"
    if entry not in graph.files:
        raise InventoryError("legacy app has no src/main.tsx entry point")
    pages_prefix = "pages/"
    routes = parse_router(graph, "router.tsx", pages_prefix)

    endpoint_cache: dict[str, set[EndpointUse]] = {
        name: extract_endpoints(source)
        for name, source in graph.files.items()
        if not _is_test(name)
    }
    classes: dict[EndpointUse, tuple[EndpointClass, dict[str, Any]]] = {}

    def classify(use: EndpointUse) -> EndpointClass:
        if use not in classes:
            classes[use] = tables.classify(use)
        return classes[use][0]

    tests = sorted(name for name in graph.files if _is_test(name))
    playwright = _playwright_specs(_inside(root, f"{policy.legacy_app}/e2e"), root, skipped)
    page_files = sorted(
        name
        for name in graph.files
        if name.startswith(pages_prefix) and name.endswith(".tsx") and not _is_test(name)
    )
    routes_by_page: dict[str, list[RouteRecord]] = {}
    for route in routes:
        if route.page is not None:
            routes_by_page.setdefault(route.page, []).append(route)

    pages: list[dict[str, Any]] = []
    for page in page_files:
        closure = graph.closure(page)
        uses: set[EndpointUse] = set()
        for module in closure:
            uses |= endpoint_cache.get(module, set())
        direct = graph.edges.get(page, set())
        page_routes = sorted({route.path for route in routes_by_page.get(page, [])})
        component = Path(page).stem
        loc = graph.loc(page)
        vitest = sorted(
            f"{policy.legacy_app}/src/{test}"
            for test in tests
            if page in graph.edges.get(test, set())
        )
        specs = sorted(
            spec
            for spec, (literals, text) in playwright.items()
            if any(_route_matches(route, item) for route in page_routes for item in literals)
            or re.search(r"(?<![\w$])" + re.escape(component) + r"(?![\w$])", text)
        )
        pages.append(
            {
                "file": f"{policy.legacy_app}/src/{page}",
                "component": component,
                "routed": bool(page_routes),
                "routes": page_routes,
                "guards": sorted(
                    {guard for route in routes_by_page.get(page, []) for guard in route.guards}
                ),
                "loc": loc,
                "closure_loc": sum(graph.loc(module) for module in closure),
                "tier": tier_for(loc),
                "components": sorted(
                    f"{policy.legacy_app}/src/{d}" for d in direct if d.startswith("components/")
                ),
                "libraries": sorted(
                    f"{policy.legacy_app}/src/{d}"
                    for d in direct
                    if d.startswith(("services/", "lib/", "api/", "actions/", "hooks/"))
                ),
                "endpoints": [
                    {"method": use.method, "path": use.path, "class": classify(use).value}
                    for use in sorted(uses)
                ],
                "tests": {"vitest": vitest, "playwright": specs},
            }
        )

    all_uses: dict[EndpointUse, set[str]] = {}
    for module, uses in endpoint_cache.items():
        for use in uses:
            all_uses.setdefault(use, set()).add(module)
    endpoints = []
    for use in sorted(all_uses):
        klass = classify(use)
        endpoints.append(
            {
                "method": use.method,
                "path": use.path,
                "class": klass.value,
                "sources": classes[use][1],
                "files": sorted(f"{policy.legacy_app}/src/{name}" for name in all_uses[use]),
            }
        )

    reachable = graph.reachable(entry)
    dead = sorted(
        name
        for name in graph.files
        if name not in reachable and not _is_test(name) and "/test/" not in f"/{name}"
    )
    route_records = [
        {
            "path": route.path,
            "page": f"{policy.legacy_app}/src/{route.page}" if route.page else None,
            "component": route.component,
            "guards": list(route.guards),
        }
        for route in sorted(routes, key=lambda r: (r.path, r.page or "", r.component or ""))
    ]
    orphan_pages = [page["file"] for page in pages if not page["routed"]]
    summary = {
        "modules": sum(1 for name in graph.files if not _is_test(name)),
        "test_files": len(tests),
        "playwright_specs": len(playwright),
        "routes": len(route_records),
        "page_files": len(pages),
        "routed_pages": sum(1 for page in pages if page["routed"]),
        "orphan_pages": len(orphan_pages),
        "dead_modules": len(dead),
        "dead_module_loc": sum(graph.loc(name) for name in dead),
        "skipped_symlinks": len(skipped),
        "tiers": dict(sorted(Counter(page["tier"] for page in pages).items())),
        "endpoints": len(endpoints),
        "endpoint_paths": len({item["path"] for item in endpoints}),
        "endpoint_classes": {
            klass.value: sum(1 for item in endpoints if item["class"] == klass.value)
            for klass in EndpointClass
        },
    }
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generator": GENERATOR,
        "project": project,
        "source_sha": source_sha,
        "legacy_app": policy.legacy_app,
        "legacy_app_tree": legacy_app_tree,
        "target_app": policy.target_app,
        "route_tables": dict(sorted(tables.digests.items())),
        "summary": summary,
        "routes": route_records,
        "pages": pages,
        "endpoints": endpoints,
        "dead_modules": [
            {"file": f"{policy.legacy_app}/src/{name}", "loc": graph.loc(name)} for name in dead
        ],
        "orphan_pages": orphan_pages,
        "skipped_symlinks": sorted(skipped),
    }
    # Redact first, so the hash covers exactly what is stored as evidence.
    document = SecretRedactor().redact(document).value
    document["inventory_sha256"] = inventory_hash(document)
    return document


def _playwright_specs(e2e: Path, root: Path, skipped: set[str]) -> dict[str, tuple[set[str], str]]:
    specs: dict[str, tuple[set[str], str]] = {}
    if not e2e.is_dir():
        return specs
    for path in _safe_files(e2e, root, skipped):
        if not re.search(r"\.(e2e|spec)\.ts$", path.name):
            continue
        if path.stat().st_size > MAX_SOURCE_BYTES:
            raise InventoryError(f"spec file exceeds the size bound: {path.name}")
        text = path.read_text(encoding="utf-8", errors="replace")
        source = scan_js(text)
        literals = {
            literal.value.split("?", 1)[0].rstrip("/") or "/"
            for literal in source.literals
            if literal.value.startswith("/") and not literal.expressions
        }
        specs[path.relative_to(root).as_posix()] = (literals, source.code)
    return specs


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode()


def inventory_hash(document: dict[str, Any]) -> str:
    body = {key: value for key, value in document.items() if key != "inventory_sha256"}
    return hashlib.sha256(canonical_json(body)).hexdigest()


_GIT_OBJECT = re.compile(r"[a-f0-9]{40}|[a-f0-9]{64}")
CONTENT_KEY_VERSION = "ui-inventory-content-v1"
# Fields the content key ignores: the evidence hash itself and the legacy repository
# commit, which moves with every unrelated commit (backend fixes, docs, CI).
CONTENT_KEY_EXCLUDED = frozenset({"inventory_sha256", "source_sha"})


def inventory_content_key(document: dict[str, Any]) -> str:
    """NEW-136: the reuse identity of an inventory's content, stable across unrelated commits.

    sha256 over the document without `inventory_sha256` and `source_sha`, which includes
    `legacy_app_tree`: the inventory captures the console's structure and route-table
    digests but not its file contents, so the tree id invalidates the key on any change to
    a legacy console file. Evidence and approvals keep binding the full `inventory_sha256`.
    """
    tree = document.get("legacy_app_tree")
    if not isinstance(tree, str) or not _GIT_OBJECT.fullmatch(tree):
        raise InventoryError("inventory records no legacy app tree (schema before NEW-136)")
    body = {key: value for key, value in document.items() if key not in CONTENT_KEY_EXCLUDED}
    payload = {"version": CONTENT_KEY_VERSION, "inventory": body}
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def console_scan_roots(legacy_app: str) -> tuple[tuple[str, bool], ...]:
    """(path, required) of every console directory the inventory scans from."""
    return ((legacy_app, True), (f"{legacy_app}/src", True), (f"{legacy_app}/e2e", False))


def check_git_scan_roots(object_type: Callable[[str], str | None], legacy_app: str) -> None:
    """Fail closed unless every console scan root is a Git tree at the snapshot commit.

    `object_type(path)` is `git cat-file -t <sha>:<path>`, or None when the path does not
    resolve (absent, or below a symlink: Git never follows one). A symlink is a blob (mode
    120000), so a symlinked root is refused here as well as on the filesystem (`_inside`)."""
    for relative, required in console_scan_roots(legacy_app):
        kind = object_type(relative)
        if kind is None and not required:
            continue
        if kind != "tree":
            raise InventoryError(
                f"{SYMLINK_ROOT}: console scan root is not a directory at the snapshot "
                f"commit (missing, a symlink, or below one): {relative}"
            )


def git_legacy_app_tree(run: Callable[[list[str]], str], source_sha: str, legacy_app: str) -> str:
    """The Git tree id of the legacy app directory at `source_sha`, from the object store
    (`run` executes git with these arguments in the checkout and returns its stdout)."""
    if not re.fullmatch(r"[a-f0-9]{40}", source_sha):
        raise InventoryError("source SHA must be a full 40-character commit")
    if not re.fullmatch(
        r"[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*", legacy_app
    ) or ".." in legacy_app.split("/"):
        raise InventoryError(f"legacy app path is invalid: {legacy_app}")
    tree = run(["rev-parse", "--verify", "--quiet", f"{source_sha}:{legacy_app}"]).strip()
    if not _GIT_OBJECT.fullmatch(tree) or run(["cat-file", "-t", tree]).strip() != "tree":
        raise InventoryError(f"legacy app is not a directory at {source_sha}: {legacy_app}")
    return tree


def render_markdown(document: dict[str, Any]) -> str:
    summary = document["summary"]
    classes = summary["endpoint_classes"]
    lines = [
        f"# UI inventory: {document['project']} `{document['legacy_app']}`",
        "",
        f"- Source: `{document['source_sha']}`",
        f"- Legacy app tree: `{document.get('legacy_app_tree')}`",
        f"- Schema version: {document['schema_version']}",
        f"- Inventory SHA-256: `{document['inventory_sha256']}`",
        f"- Target app: `{document['target_app']}`",
        "",
        "## Summary",
        "",
        "| Measure | Count |",
        "|---|---|",
        f"| Non-test modules | {summary['modules']} |",
        f"| Test files (Vitest) | {summary['test_files']} |",
        f"| Playwright specs | {summary['playwright_specs']} |",
        f"| Routes | {summary['routes']} |",
        f"| Page files | {summary['page_files']} |",
        f"| Routed pages | {summary['routed_pages']} |",
        f"| Orphan pages | {summary['orphan_pages']} |",
        f"| Dead modules (LOC) | {summary['dead_modules']} ({summary['dead_module_loc']}) |",
        f"| Endpoints (method, path) | {summary['endpoints']} |",
        f"| Distinct endpoint paths | {summary['endpoint_paths']} |",
        "",
        "Tiers: " + ", ".join(f"{name} {count}" for name, count in summary["tiers"].items()),
        "",
        "Endpoint classes: " + ", ".join(f"{name} {count}" for name, count in classes.items()),
        "",
        "## Route tables",
        "",
        "| Table | Path | Entries | SHA-256 |",
        "|---|---|---|---|",
    ]
    for name, table in document["route_tables"].items():
        lines.append(
            f"| {name} | `{table['path']}` | {table.get('entries', '')} | "
            f"`{table['sha256'][:16]}` |"
        )
    lines += [
        "",
        "## Pages",
        "",
        "| Page | Routes | LOC / closure | Tier | Endpoints | Not OK | Vitest | Playwright |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for page in document["pages"]:
        not_ok = sum(1 for item in page["endpoints"] if item["class"] != EndpointClass.OK)
        routes = ", ".join(f"`{route}`" for route in page["routes"]) or "orphan"
        lines.append(
            f"| {page['component']} | {routes} | {page['loc']} / {page['closure_loc']} | "
            f"{page['tier']} | {len(page['endpoints'])} | {not_ok} | "
            f"{len(page['tests']['vitest'])} | {len(page['tests']['playwright'])} |"
        )
    for klass in (
        EndpointClass.NO_ROUTE,
        EndpointClass.ROUTED_NOT_PUBLISHED,
        EndpointClass.GATEWAY_ONLY,
    ):
        rows = [item for item in document["endpoints"] if item["class"] == klass]
        lines += ["", f"## {klass.value} ({len(rows)})", ""]
        lines += [f"- `{item['method']} {item['path']}`" for item in rows]
    lines += ["", f"## Dead modules ({len(document['dead_modules'])})", ""]
    lines += [f"- `{item['file']}` ({item['loc']})" for item in document["dead_modules"]]
    lines += ["", f"## Orphan pages ({len(document['orphan_pages'])})", ""]
    lines += [f"- `{item}`" for item in document["orphan_pages"]]
    return "\n".join(lines) + "\n"
