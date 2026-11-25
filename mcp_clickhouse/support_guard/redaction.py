"""Redact credentials and shrink query results before they reach the agent's context.

Chatomate services log the full request, headers included, on every request-scoped
line, so authorization headers, cookies, API keys and provider tokens show up in log
bodies and attributes. Redaction runs on every run_query result, whatever the table.
"""

import json
import re
from typing import Any

REDACTED = "[REDACTED]"

_SENSITIVE_KEY_SUFFIXES = (
    "authorization",
    "cookie",
    "apikey",
    "token",
    "secret",
    "password",
    "passwd",
    "signature",
    "privatekey",
    "credentials",
)
_KEY_NORMALIZE_RE = re.compile(r"[^a-z0-9]")

_QUOTED_KEY_RE = re.compile(
    r"""(?P<prefix>(?P<quote>\\?["'])(?P<key>[A-Za-z0-9_.\-]{2,80})(?P=quote)\s*[:=]\s*(?P<vq>\\?["']))"""
    r"""(?P<value>.*?)(?P=vq)""",
    re.DOTALL,
)
_UNQUOTED_KEY_RE = re.compile(
    r"(?P<prefix>(?<![A-Za-z0-9_])(?P<key>[A-Za-z0-9_.\-]{2,80})\s*[=:]\s*)"
    r"(?P<value>[^\s,;&\"'}\]]{4,})"
)
_VALUE_PATTERNS = (
    re.compile(r"(?i)\b(Bearer|Basic|Token)\s+[A-Za-z0-9\-._~+/]{8,}=*"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bEAA[A-Za-z0-9]{30,}"),
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"(?i)\b(sig|signature|x-amz-signature|x-amz-credential|sv)=[^&\s\"']{8,}"),
)


def is_sensitive_key(key: str) -> bool:
    """Return True when a field name looks like it holds a credential.

    @param key - map key, JSON field name, or dotted attribute path
    @returns whether the value under this key should be redacted
    """
    last_segment = key.rsplit(".", 1)[-1]
    normalized = _KEY_NORMALIZE_RE.sub("", last_segment.lower()).rstrip("0123456789")
    return bool(normalized) and normalized.endswith(_SENSITIVE_KEY_SUFFIXES)


def redact_text(text: str) -> str:
    """Redact credentials inside free text, including JSON or query strings embedded in it.

    @param text - a log body or any string cell
    @returns the text with secrets replaced by [REDACTED]
    """
    for pattern in _VALUE_PATTERNS:
        text = pattern.sub(_replace_token, text)
    text = _QUOTED_KEY_RE.sub(_replace_keyed_value, text)
    return _UNQUOTED_KEY_RE.sub(_replace_keyed_value, text)


def _replace_keyed_value(match: re.Match) -> str:
    if not is_sensitive_key(match.group("key")):
        return match.group(0)
    closing = match.groupdict().get("vq") or ""
    return f"{match.group('prefix')}{REDACTED}{closing}"


def _replace_token(match: re.Match) -> str:
    text = match.group(0)
    scheme = re.match(r"(?i)(Bearer|Basic|Token)\s+", text)
    if scheme:
        return f"{scheme.group(0)}{REDACTED}"
    key_value = re.match(r"(?i)([a-z\-]+=)", text)
    if key_value and "=" in text[:20]:
        return f"{key_value.group(1)}{REDACTED}"
    return REDACTED


def redact_value(value: Any, max_cell_chars: int) -> Any:
    """Recursively redact and truncate a decoded JSON value.

    @param value - any decoded JSON value from a query result
    @param max_cell_chars - strings longer than this are cut with a marker
    @returns a sanitized copy of the value
    """
    if isinstance(value, str):
        return _truncate(redact_text(value), max_cell_chars)
    if isinstance(value, list):
        return [redact_value(item, max_cell_chars) for item in value]
    if isinstance(value, dict):
        return {
            key: REDACTED if is_sensitive_key(str(key)) else redact_value(item, max_cell_chars)
            for key, item in value.items()
        }
    return value


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}...[truncated {len(text) - max_chars} chars]"


def sanitize_tool_text(text: str, max_cell_chars: int, max_response_chars: int) -> str:
    """Sanitize a run_query JSON payload and keep it under the response budget.

    @param text - the JSON string returned by run_query ({"columns": [...], "rows": [...]})
    @param max_cell_chars - per-string truncation limit
    @param max_response_chars - total character budget for the returned payload
    @returns a JSON string with secrets redacted, long cells cut, and rows dropped if needed
    """
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return _truncate(redact_text(text), max_response_chars)

    payload = redact_value(payload, max_cell_chars)
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        return _truncate(json.dumps(payload, ensure_ascii=False, default=str), max_response_chars)

    return _fit_rows(payload, max_response_chars)


def _fit_rows(payload: dict, max_response_chars: int) -> str:
    rows = payload["rows"]
    total = len(rows)
    encoded = json.dumps(payload, ensure_ascii=False, default=str)
    if len(encoded) <= max_response_chars:
        return encoded

    kept: list[Any] = []
    budget = max_response_chars - 600
    used = len(json.dumps({**payload, "rows": []}, ensure_ascii=False, default=str))
    for row in rows:
        size = len(json.dumps(row, ensure_ascii=False, default=str)) + 1
        if used + size > budget:
            break
        kept.append(row)
        used += size

    payload = {
        **payload,
        "rows": kept,
        "notice": (
            f"Result cut to {len(kept)} of {total} rows to protect the agent context. "
            "Select fewer columns, filter harder (ServiceName, SeverityText, an id token), "
            "or aggregate with count() GROUP BY before fetching raw rows."
        ),
    }
    return json.dumps(payload, ensure_ascii=False, default=str)
