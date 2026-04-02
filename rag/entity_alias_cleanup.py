from __future__ import annotations

import re


_ALIAS_SEPARATORS = re.compile(r"[、/／,，|｜;；]+")
_QUOTE_WRAP = "\"'`“”‘’"
_EDGE_PUNCTUATION = " \t\r\n()（）[]【】<>《》"


def _split_nested_alias_tokens(text: str) -> list[str]:
    source = str(text or "").strip()
    if not source:
        return []

    queue: list[str] = [source]
    flattened: list[str] = []
    while queue:
        current = str(queue.pop(0) or "").strip()
        if not current:
            continue

        current = current.strip(_QUOTE_WRAP)
        current = current.strip()
        if not current:
            continue

        parts = [item.strip() for item in _ALIAS_SEPARATORS.split(current) if str(item or "").strip()]
        if len(parts) > 1:
            queue.extend(parts)
            continue

        balanced = re.match(r"^(.*?)[（(]([^（）()]*)[）)]$", current)
        if balanced:
            head = str(balanced.group(1) or "").strip()
            inner = str(balanced.group(2) or "").strip()
            if head:
                queue.append(head)
            if inner:
                queue.extend([item.strip() for item in _ALIAS_SEPARATORS.split(inner) if str(item or "").strip()])
            continue

        if "（" in current or "(" in current:
            head, tail = re.split(r"[（(]", current, maxsplit=1)
            if str(head or "").strip():
                queue.append(str(head).strip())
            cleaned_tail = str(tail or "").strip().rstrip("）)")
            if cleaned_tail:
                queue.extend([item.strip() for item in _ALIAS_SEPARATORS.split(cleaned_tail) if str(item or "").strip()])
            continue

        current = current.strip(_EDGE_PUNCTUATION).strip(_QUOTE_WRAP).strip()
        current = current.rstrip("）)").strip()
        if current:
            flattened.append(current)
    return flattened


def finalize_aliases_for_storage(name: str, aliases: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    candidates = [str(name or "").strip(), *[str(alias or "").strip() for alias in aliases]]
    for candidate in candidates:
        for token in _split_nested_alias_tokens(candidate):
            text = str(token or "").strip().strip(_QUOTE_WRAP).strip()
            if not text or text in seen:
                continue
            normalized.append(text)
            seen.add(text)
    return normalized
