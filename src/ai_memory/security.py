from __future__ import annotations

import hashlib
import re

# Redaction deliberately happens before import rows are committed or sent to an extractor.
_SECRET_PATTERNS = [
    re.compile(r"(?i)([\"']?(?:password|passwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|token|credential)[\"']?\s*:\s*)([\"'])(?:(?!\2)[\s\S])*\2"),
    re.compile(r"(?i)(--(?:password|passwd|secret|api[_-]?key|token|credential)\s+)(?:\"[^\"]*\"|'[^']*'|[^\s]+)"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{16,}|npm_[A-Za-z0-9]{20,}|pypi-[A-Za-z0-9_-]{20,}|AIzaSy[A-Za-z0-9_-]{30,})\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|token|credential)\s*[:=]\s*(['\"])(?:(?!\2)[\s\S])*\2"),
    re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|token|credential)\s*[:=]\s*[^\s,'\";]+"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)(://[^:/\s]+:)[^@/\s]+(@)"),
]


def redact_secrets(text: str) -> str:
    safe = text
    for pattern in _SECRET_PATTERNS:
        if pattern.pattern.startswith('(?i)(["\']?(?:password'):
            safe = pattern.sub(lambda m: m.group(1) + m.group(2) + "[REDACTED]" + m.group(2), safe)
        elif pattern.pattern.startswith("(?i)(--(?:password"):
            safe = pattern.sub(lambda m: m.group(1) + "[REDACTED]", safe)
        elif pattern.pattern.startswith("(?i)\\b(password"):
            safe = pattern.sub(lambda m: f"{m.group(1)}=[REDACTED]", safe)
        elif "://" in pattern.pattern:
            safe = pattern.sub(r"\1[REDACTED]\2", safe)
        else:
            safe = pattern.sub("[REDACTED]", safe)
    return safe


def redact_value(value):
    """Redact secret-like strings at every depth before JSON persistence or inference."""
    if isinstance(value, str): return redact_secrets(value)
    if isinstance(value, list): return [redact_value(item) for item in value]
    if isinstance(value, tuple): return [redact_value(item) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            name = redact_secrets(str(key))
            if re.search(r"(?i)(password|passwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|token|credential|private[_-]?key)", str(key)):
                result[name] = "[REDACTED]"
            else:
                result[name] = redact_value(item)
        return result
    return value


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
