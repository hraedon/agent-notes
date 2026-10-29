"""Secret patterns for read-only scans (Plan 027 WI-1.5, §6).

Shared by ``memory hygiene`` and, later, harvest redaction (WI-4.3). A scan
reports *which pattern labels* matched, never the matched text: callers print
a row ID and a label, so a report can be pasted into a notice or a log without
re-leaking the secret. :func:`redact_if_secret` lets them show a name or other
metadata only when it carries no secret itself.

Two kinds of pattern:

- *shape* patterns recognise a token by its own prefix and length
  (``ghp_…``, ``AKIA…``, a private-key header);
- *context* patterns recognise a value by what it is assigned to
  (``NETBOX_TOKEN=…``, ``"password": "…"``, ``Bearer …``, a 40-hex token next
  to the word NetBox). The key must be a whole credential word (``token``,
  not ``tokenizer`` or ``max_tokens``), and the value must look like a
  credential: no spaces or brackets, at least 12 characters, letters *and*
  digits, and not a placeholder (``<token>``, ``changeme``, ``${VAR}``).

Every pattern is linear in the input: quantifiers are bounded, no quantified
group overlaps its neighbour, and the context patterns may only start at a
token boundary (``tests/test_memory_hygiene.py`` times them on 200 KB
adversarial inputs). They are deterministic and explainable, not
entropy-based, and will miss secrets with no recognisable shape or context: a
tripwire, not a guarantee.
"""

from __future__ import annotations

import re

# label -> pattern. Labels are part of the JSON contract of `memory hygiene`.
# Named groups: `key` is checked by _is_credential_key, `value` by
# _looks_like_credential.
SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "bearer_token": re.compile(
        r"(?i)(?<![a-z])bearer[ \t]{1,4}(?P<value>[A-Za-z0-9\-._~+/=]{20,512})"
    ),
    "netbox_token": re.compile(
        # v2 tokens by prefix; legacy 40-hex tokens only near the word NetBox.
        r"(?<![A-Za-z0-9_])nbt_[A-Za-z0-9]{8,200}"
        r"|(?i:(?<![a-z])netbox(?![a-z])[^\n]{0,60}?(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f]))"
    ),
    "github_token": re.compile(
        r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{30,255}|github_pat_[A-Za-z0-9_]{30,255})"
    ),
    "sk_api_key": re.compile(r"(?<![A-Za-z0-9_\-])sk-(?:[a-z]{1,10}-)?[A-Za-z0-9_\-]{20,255}"),
    "slack_token": re.compile(r"(?<![A-Za-z0-9_])xox[abposr]-[A-Za-z0-9\-]{10,255}"),
    "aws_access_key": re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Za-z0-9])"),
    "private_key": re.compile(r"-----BEGIN (?:[A-Z0-9]{1,20} ){0,4}PRIVATE KEY-----"),
    "url_credentials": re.compile(
        r"(?<![a-z0-9+.\-])[a-z][a-z0-9+.\-]{0,20}://[^\s:/@'\"`]{1,128}:"
        r"(?P<value>[^\s@/'\"`]{3,200})@"
    ),
    # KEY=value / KEY: value, e.g. NETBOX_TOKEN=..., DB_PASSWORD: ...
    "secret_assignment": re.compile(
        r"(?<![\w.\-])(?P<key>[A-Za-z][\w.\-]{0,63})[ \t]{0,3}[:=][ \t]{0,3}[\"']?"
        r"(?P<value>[^\s\"'`<>,;()\[\]{}]{1,200})"
    ),
    # "key": "value" in JSON (attributes, pasted configs).
    "secret_json": re.compile(
        r"\"(?P<key>[A-Za-z][\w.\-]{0,63})\"[ \t]{0,3}:[ \t]{0,3}\"(?P<value>[^\"\n]{1,200})\""
    ),
}

# A credential key ends in one of these words (split on `_`, `-` or `.`).
_KEY_WORDS = frozenset({"token", "secret", "password", "passwd", "pwd", "passphrase", "apikey"})
_KEY_PAIR_PREFIXES = frozenset({"api", "access", "secret", "private", "auth", "client"})
_KEY_SPLIT_RE = re.compile(r"[_.\-]")

# Values that are documentation placeholders, not secrets.
_PLACEHOLDER_RE = re.compile(
    r"^(?:\$.*|%\w+%|<.*|\{.*|\*+|x+|\.+|.*changeme.*|.*change[_\-]me.*|.*example.*|"
    r".*redacted.*|.*placeholder.*|.*dummy.*|your[\w\-]*|none|null|true|false|"
    r"password|passwd|pass|pw|pwd|secret|token)$",
    re.IGNORECASE,
)


def _is_credential_key(key: str) -> bool:
    parts = [p for p in _KEY_SPLIT_RE.split(key.lower()) if p]
    if not parts:
        return False
    if parts[-1] in _KEY_WORDS:
        return True
    return parts[-1] == "key" and len(parts) > 1 and parts[-2] in _KEY_PAIR_PREFIXES


def _looks_like_credential(value: str, *, min_len: int = 12) -> bool:
    """A value shaped like a credential rather than a word, call or reference."""
    if len(value) < min_len or _PLACEHOLDER_RE.match(value):
        return False
    if any(c.isspace() or c in "()[]{}" for c in value):
        return False
    return any(c.isdigit() for c in value) and any(c.isalpha() for c in value)


def scan_secrets(text: str) -> dict[str, int]:
    """Count secret-shaped matches in ``text`` by pattern label (no match text)."""
    found: dict[str, int] = {}
    if not text:
        return found
    for label, pattern in SECRET_PATTERNS.items():
        count = 0
        for m in pattern.finditer(text):
            groups = m.groupdict()
            key, value = groups.get("key"), groups.get("value")
            if key is not None and not _is_credential_key(key):
                continue
            if label == "url_credentials":
                if value is not None and _PLACEHOLDER_RE.match(value):
                    continue
            elif value is not None and not _looks_like_credential(value):
                continue
            count += 1
        if count:
            found[label] = count
    return found


def redact_if_secret(value: str | None, *, row_id: int | None = None) -> str | None:
    """``value`` unchanged, or a redaction marker when it carries a secret."""
    if not value or not scan_secrets(value):
        return value
    return "[redacted]" if row_id is None else f"[redacted: memory id {row_id}]"
