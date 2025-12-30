from __future__ import annotations

import base64
import copy
import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:
    from python_jsonpath import JSONPath
except Exception:  # pragma: no cover - optional dependency
    JSONPath = None

from ...core.types import SpanKind, TransformMetadata
from ...core.types import TransformAction as MetadataAction

ActionFunction = Callable[[str], str]
Direction = Literal["inbound", "outbound"]


@dataclass
class HttpSpanData:
    """Mutable span payload that transformations can operate on."""

    kind: SpanKind
    input_value: dict[str, Any] | None = None
    output_value: dict[str, Any] | None = None
    transform_metadata: TransformMetadata | None = None

    def clone(self) -> HttpSpanData:
        return HttpSpanData(
            kind=self.kind,
            input_value=copy.deepcopy(self.input_value) if self.input_value is not None else None,
            output_value=copy.deepcopy(self.output_value) if self.output_value is not None else None,
        )


@dataclass
class _DropTransform:
    matcher: Callable[[HttpSpanData], bool]
    direction: Direction
    description: str

    def matches(self, span: HttpSpanData) -> bool:
        return self.matcher(span)

    def apply(self, span: HttpSpanData) -> MetadataAction | None:
        if not self.matcher(span):
            return None

        if self.direction == "inbound":
            span.input_value = _create_empty_server_input_value()
            span.output_value = _create_empty_server_output_value()
        else:
            span.input_value = _create_empty_client_input_value()
            span.output_value = _create_empty_client_output_value()

        return MetadataAction(
            type="drop",
            field="entire_span",
            reason="transforms",
            description=self.description,
        )


class HttpTransformEngine:
    """Applies redact/mask/replace/drop rules to HTTP spans."""

    def __init__(self, transforms: Sequence[dict[str, Any]] | None = None) -> None:
        self._compiled_transforms: list[Callable[[HttpSpanData], MetadataAction | None]] = []
        self._drop_transforms: list[_DropTransform] = []

        if not transforms:
            return

        for transform in transforms:
            compiled = self._compile_transform(transform)
            if compiled is None:
                continue
            if isinstance(compiled, _DropTransform):
                self._drop_transforms.append(compiled)
            else:
                self._compiled_transforms.append(compiled)

    def should_drop_inbound_request(
        self,
        method: str,
        target: str,
        headers: dict[str, Any] | None = None,
    ) -> bool:
        if not self._drop_transforms:
            return False

        span = HttpSpanData(
            kind=SpanKind.SERVER,
            input_value={
                "method": method,
                "target": target,
                "headers": headers or {},
            },
        )
        return any(drop.matches(span) for drop in self._drop_transforms if drop.direction == "inbound")

    def should_drop_outbound_request(
        self,
        method: str,
        url: str,
        headers: dict[str, Any] | None = None,
    ) -> bool:
        """Check if an outbound CLIENT request should be dropped.

        This should be called BEFORE making the HTTP request to prevent
        network traffic for dropped requests.
        """
        if not self._drop_transforms:
            return False

        span = HttpSpanData(
            kind=SpanKind.CLIENT,
            input_value={
                "method": method,
                "url": url,
                "headers": headers or {},
            },
        )
        return any(drop.matches(span) for drop in self._drop_transforms if drop.direction == "outbound")

    def apply_transforms(self, span: HttpSpanData) -> TransformMetadata | None:
        actions: list[MetadataAction] = []

        for drop in self._drop_transforms:
            action = drop.apply(span)
            if action:
                actions.append(action)

        for transform in self._compiled_transforms:
            action = transform(span)
            if action:
                actions.append(action)

        if not actions:
            return None

        metadata = TransformMetadata(transformed=True, actions=actions)
        span.transform_metadata = metadata
        return metadata

    def _compile_transform(
        self, transform: dict[str, Any]
    ) -> Callable[[HttpSpanData], MetadataAction | None] | _DropTransform | None:
        matcher_config = transform.get("matcher")
        action_config = transform.get("action")
        if not matcher_config or not action_config:
            return None

        matcher = self._compile_matcher(matcher_config)
        target_description = self._describe_target_field(matcher_config)

        if action_config.get("type") == "drop":
            return _DropTransform(
                matcher=matcher,
                direction=_parse_direction(matcher_config.get("direction")),
                description=target_description,
            )

        action_fn = self._compile_action_function(action_config)
        compiled_action = self._compile_target_action(matcher_config, action_fn)
        if compiled_action is None:
            return None

        def _apply(span: HttpSpanData) -> MetadataAction | None:
            if not matcher(span):
                return None
            if not compiled_action(span):
                return None
            return MetadataAction(
                type=action_config["type"],
                field=target_description,
                reason="transforms",
            )

        return _apply

    def _compile_matcher(self, matcher: dict[str, Any]) -> Callable[[HttpSpanData], bool]:
        direction = _parse_direction(matcher.get("direction"))
        methods = [m.upper() for m in matcher.get("method", []) if isinstance(m, str)]
        path_pattern = matcher.get("pathPattern")
        host_pattern = matcher.get("host")

        def _matches(span: HttpSpanData) -> bool:
            if direction == "inbound" and span.kind is not SpanKind.SERVER:
                return False
            if direction == "outbound" and span.kind is not SpanKind.CLIENT:
                return False

            span_method = (span.input_value or {}).get("method")
            if methods and span_method:
                if span_method.upper() not in methods:
                    return False

            if path_pattern:
                path = _extract_path(span)
                if path is None or not fnmatch(path, path_pattern):
                    return False

            if host_pattern:
                host = _extract_host(span)
                if host is None or not fnmatch(host, host_pattern):
                    return False

            return True

        return _matches

    def _compile_target_action(
        self,
        matcher: dict[str, Any],
        action_fn: ActionFunction,
    ) -> Callable[[HttpSpanData], bool] | None:
        direction = _parse_direction(matcher.get("direction"))
        if matcher.get("jsonPath"):
            return self._compile_json_path_action(matcher["jsonPath"], action_fn, direction)
        if matcher.get("queryParam"):
            return self._compile_query_param_action(matcher["queryParam"], action_fn)
        if matcher.get("headerName"):
            return self._compile_header_action(matcher["headerName"], action_fn)
        if matcher.get("urlPath"):
            return self._compile_url_path_action(action_fn)
        if matcher.get("fullBody"):
            return self._compile_full_body_action(action_fn, direction)
        return None

    def _compile_action_function(self, action: dict[str, Any]) -> ActionFunction:
        action_type = action.get("type")
        if action_type == "redact":
            prefix = action.get("hashPrefix", "REDACTED_")

            def _fn(value: str) -> str:
                digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
                return f"{prefix}{digest}"

            return _fn

        if action_type == "mask":
            mask_char = action.get("maskChar", "*")

            def _fn(value: str) -> str:
                return mask_char * len(value)

            return _fn

        if action_type == "replace":
            replace_with = action.get("replaceWith", "")

            def _fn(_: str) -> str:
                return str(replace_with)

            return _fn

        raise ValueError(f"Unsupported transform action: {action_type}")

    def _compile_header_action(self, header_name: str, action_fn: ActionFunction) -> Callable[[HttpSpanData], bool]:
        normalized = header_name.lower()

        def _apply(span: HttpSpanData) -> bool:
            headers = (span.input_value or {}).get("headers") or {}
            applied = False
            for key, value in list(headers.items()):
                if isinstance(key, str) and key.lower() == normalized and isinstance(value, str):
                    headers[key] = action_fn(value)
                    applied = True
            return applied

        return _apply

    def _compile_query_param_action(
        self, query_param: str, action_fn: ActionFunction
    ) -> Callable[[HttpSpanData], bool]:
        def _apply(span: HttpSpanData) -> bool:
            input_value = span.input_value or {}
            updated = False
            for key in ("target", "url"):
                value = input_value.get(key)
                if isinstance(value, str):
                    new_value, changed = _transform_query_string(value, query_param, action_fn)
                    if changed:
                        input_value[key] = new_value
                        updated = True
            return updated

        return _apply

    def _compile_url_path_action(self, action_fn: ActionFunction) -> Callable[[HttpSpanData], bool]:
        def _apply(span: HttpSpanData) -> bool:
            input_value = span.input_value or {}
            updated = False
            for key in ("target", "url"):
                value = input_value.get(key)
                if isinstance(value, str):
                    new_value, changed = _transform_path(value, action_fn)
                    if changed:
                        input_value[key] = new_value
                        updated = True
            return updated

        return _apply

    def _compile_full_body_action(
        self, action_fn: ActionFunction, direction: Direction
    ) -> Callable[[HttpSpanData], bool]:
        def _apply(span: HttpSpanData) -> bool:
            target = span.input_value if direction == "inbound" else span.output_value
            if not isinstance(target, dict):
                return False
            body = target.get("body")
            if not isinstance(body, str):
                return False
            decoded = _decode_base64(body)
            if decoded is None:
                return False
            transformed = action_fn(decoded.decode("utf-8", errors="ignore"))
            encoded = transformed.encode("utf-8")
            target["body"] = base64.b64encode(encoded).decode("ascii")
            target["bodySize"] = len(encoded)
            return True

        return _apply

    def _compile_json_path_action(
        self, json_path: str, action_fn: ActionFunction, direction: Direction
    ) -> Callable[[HttpSpanData], bool]:
        jsonpath_expr = None
        fallback_tokens: list[str | int] | None = None
        if JSONPath is not None:
            try:
                jsonpath_expr = JSONPath(json_path)
            except Exception:
                jsonpath_expr = None
        if jsonpath_expr is None:
            fallback_tokens = _parse_json_path_expression(json_path)

        def _apply(span: HttpSpanData) -> bool:
            target = span.input_value if direction == "inbound" else span.output_value
            if not isinstance(target, dict):
                return False
            body = target.get("body")
            parsed, _ = _load_json_body(body)
            if parsed is None:
                return False

            if jsonpath_expr is not None:
                applied = _apply_with_python_jsonpath(jsonpath_expr, parsed, action_fn)
            else:
                applied = _apply_json_path_tokens(parsed, fallback_tokens or [], action_fn)

            if applied:
                encoded = json.dumps(parsed, separators=(",", ":")).encode("utf-8")
                target["body"] = base64.b64encode(encoded).decode("ascii")
                target["bodySize"] = len(encoded)
            return applied

        return _apply

    def _describe_target_field(self, matcher: dict[str, Any]) -> str:
        if matcher.get("jsonPath"):
            return f"jsonPath:{matcher['jsonPath']}"
        if matcher.get("queryParam"):
            return f"queryParam:{matcher['queryParam']}"
        if matcher.get("headerName"):
            return f"header:{matcher['headerName']}"
        if matcher.get("urlPath"):
            return "urlPath"
        if matcher.get("fullBody"):
            return "body"
        return "unknown"


def _parse_direction(value: Any) -> Direction:
    return "inbound" if value != "outbound" else "outbound"


def _extract_headers(span: HttpSpanData) -> dict[str, Any]:
    headers = (span.input_value or {}).get("headers")
    if isinstance(headers, dict):
        return headers
    return {}


def _extract_path(span: HttpSpanData) -> str | None:
    target = (span.input_value or {}).get("target")
    candidate = target if isinstance(target, str) else (span.input_value or {}).get("url")
    if not isinstance(candidate, str):
        return None
    parts = urlsplit(candidate)
    if parts.path:
        return parts.path
    return candidate.split("?")[0] if "?" in candidate else candidate


def _extract_host(span: HttpSpanData) -> str | None:
    headers = _extract_headers(span)
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == "host" and isinstance(value, str):
            return value
    return None


def _transform_query_string(value: str, query_param: str, action_fn: ActionFunction) -> tuple[str, bool]:
    parts = urlsplit(value)
    if not parts.query:
        return value, False
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    updated = False
    new_pairs: list[tuple[str, str]] = []
    for key, val in pairs:
        if key == query_param:
            new_pairs.append((key, action_fn(val)))
            updated = True
        else:
            new_pairs.append((key, val))
    if not updated:
        return value, False
    new_query = urlencode(new_pairs, doseq=True)
    rebuilt = urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))
    if parts.scheme or parts.netloc or rebuilt.startswith("/"):
        return rebuilt, True
    # Relative paths without scheme/netloc
    prefix = parts.path or ""
    query_section = f"?{new_query}" if new_query else ""
    suffix = f"#{parts.fragment}" if parts.fragment else ""
    return f"{prefix}{query_section}{suffix}", True


def _transform_path(value: str, action_fn: ActionFunction) -> tuple[str, bool]:
    parts = urlsplit(value)
    if not parts.path:
        return value, False
    transformed = action_fn(parts.path)
    if transformed == parts.path:
        return value, False
    rebuilt = urlunsplit((parts.scheme, parts.netloc, transformed, parts.query, parts.fragment))
    if parts.scheme or parts.netloc:
        return rebuilt, True
    path = transformed
    if parts.query:
        path += f"?{parts.query}"
    if parts.fragment:
        path += f"#{parts.fragment}"
    return path, True


def _decode_base64(value: str) -> bytes | None:
    try:
        return base64.b64decode(value.encode("ascii"))
    except Exception:
        return None


def _load_json_body(value: Any) -> tuple[Any | None, str]:
    if isinstance(value, str):
        decoded = _decode_base64(value)
        if decoded is None:
            return None, ""
        try:
            return json.loads(decoded.decode("utf-8")), "base64"
        except json.JSONDecodeError:
            return None, ""
    if isinstance(value, (dict, list)):
        return copy.deepcopy(value), "object"
    return None, ""


def _create_empty_server_input_value() -> dict[str, Any]:
    return {
        "method": "",
        "url": "",
        "target": "",
        "headers": {},
        "httpVersion": "",
        "body": base64.b64encode(b"").decode("ascii"),
        "bodySize": 0,
    }


def _create_empty_server_output_value() -> dict[str, Any]:
    return {
        "statusCode": 0,
        "statusMessage": "",
        "headers": {},
        "body": base64.b64encode(b"").decode("ascii"),
        "bodySize": 0,
    }


def _create_empty_client_input_value() -> dict[str, Any]:
    return {
        "method": "",
        "url": "",
        "target": "",
        "headers": {},
    }


def _create_empty_client_output_value() -> dict[str, Any]:
    return {
        "statusCode": 0,
        "headers": {},
    }


def _parse_json_path_expression(expression: str) -> list[str | int]:
    if not expression.startswith("$"):
        raise ValueError(f"Unsupported JSONPath expression: {expression}")

    tokens: list[str | int] = []
    current = ""
    i = 1
    length = len(expression)

    while i < length:
        char = expression[i]
        if char == ".":
            if current:
                tokens.append(current)
                current = ""
            i += 1
            continue
        if char == "[":
            if current:
                tokens.append(current)
                current = ""
            end = expression.find("]", i)
            if end == -1:
                raise ValueError(f"Invalid JSONPath expression: {expression}")
            content = expression[i + 1 : end].strip().strip("'\"")
            if content == "*":
                tokens.append("*")
            elif content.isdigit():
                tokens.append(int(content))
            else:
                tokens.append(content)
            i = end + 1
            if i < length and expression[i] == ".":
                i += 1
            continue
        current += char
        i += 1

    if current:
        tokens.append(current)

    # Remove leading empty token from expressions like "$..foo"
    if tokens and tokens[0] == "":
        tokens = tokens[1:]
    return tokens


def _apply_json_path_tokens(body: Any, tokens: list[str | int], action_fn: ActionFunction) -> bool:
    matches: list[tuple[Any, str | int]] = []

    def _collect(current: Any, index: int, parent: Any, key: str | int | None) -> None:
        if index == len(tokens):
            if parent is not None and key is not None:
                matches.append((parent, key))
            return

        token = tokens[index]
        if token == "*":
            if isinstance(current, dict):
                for child_key, child_value in current.items():
                    _collect(child_value, index + 1, current, child_key)
            elif isinstance(current, list):
                for idx, child_value in enumerate(current):
                    _collect(child_value, index + 1, current, idx)
            return

        if isinstance(token, int):
            if isinstance(current, list) and 0 <= token < len(current):
                _collect(current[token], index + 1, current, token)
            return

        if isinstance(current, dict) and token in current:
            _collect(current[token], index + 1, current, token)

    _collect(body, 0, None, None)

    applied = False
    for parent, key in matches:
        try:
            original = parent[key]
        except (KeyError, IndexError, TypeError):
            continue
        parent[key] = action_fn(str(original))
        applied = True
    return applied


def _apply_with_python_jsonpath(expr: Any, body: Any, action_fn: ActionFunction) -> bool:
    applied = False
    for match in _execute_jsonpath_expression(expr, body):
        container, key, current_value = _locate_container_from_match(match, body)
        if container is None or key is None:
            continue
        existing_value = current_value
        if existing_value is None:
            try:
                existing_value = container[key]
            except Exception:
                continue
        container[key] = action_fn(str(existing_value))
        applied = True
    return applied


def _execute_jsonpath_expression(expr: Any, body: Any) -> list[Any]:
    for method_name in ("find", "parse", "match"):
        method = getattr(expr, method_name, None)
        if callable(method):
            try:
                result = method(body)
            except Exception:
                continue
            if result is None:
                continue
            if isinstance(result, list):
                return result
            try:
                return list(result)
            except TypeError:
                return [result]
    return []


def _locate_container_from_match(match: Any, root: Any) -> tuple[Any | None, str | int | None, Any | None]:
    path = _extract_path_from_match(match)
    if not path:
        return None, None, None
    tokens = _jsonpath_path_to_tokens(path)
    if not tokens:
        return None, None, None
    container, key = _navigate_to_parent(root, tokens)
    if container is None:
        return None, None, None
    value = _extract_value_from_match(match)
    return container, key, value


def _extract_path_from_match(match: Any) -> str | None:
    if isinstance(match, dict):
        for key in ("path", "jsonpath", "full_path"):
            value = match.get(key)
            if isinstance(value, str):
                return value
    if isinstance(match, tuple):
        for item in match:
            if isinstance(item, str):
                return item
    for attr in ("path", "jsonpath", "full_path"):
        value = getattr(match, attr, None)
        if isinstance(value, str):
            return value
    return None


def _extract_value_from_match(match: Any) -> Any:
    if isinstance(match, dict):
        return match.get("value")
    if isinstance(match, tuple):
        for item in match:
            if not isinstance(item, str):
                return item
        return None
    return getattr(match, "value", None)


def _jsonpath_path_to_tokens(path: str) -> list[str | int]:
    if not path:
        return []
    tokens: list[str | int] = []
    i = 0
    length = len(path)
    # Skip leading root symbol
    if path.startswith("$"):
        i = 1

    while i < length:
        char = path[i]
        if char == ".":
            i += 1
            start = i
            while i < length and path[i] not in ".[":
                i += 1
            if start < i:
                tokens.append(path[start:i])
            continue
        if char == "[":
            end = path.find("]", i)
            if end == -1:
                break
            content = path[i + 1 : end]
            if content and content[0] in "'\"" and content[-1] in "'\"":
                tokens.append(content[1:-1])
            elif content == "*":
                return []
            else:
                try:
                    tokens.append(int(content))
                except ValueError:
                    tokens.append(content)
            i = end + 1
            continue
        i += 1

    return tokens


def _navigate_to_parent(data: Any, tokens: list[str | int]) -> tuple[Any | None, str | int | None]:
    if not tokens:
        return None, None
    current = data
    for token in tokens[:-1]:
        if isinstance(token, int):
            if not isinstance(current, list) or token >= len(current):
                return None, None
            current = current[token]
        else:
            if not isinstance(current, dict) or token not in current:
                return None, None
            current = current[token]
    return current, tokens[-1]
