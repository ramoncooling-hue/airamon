"""Small safety checks shared by the local Personal Agent runtime."""

from __future__ import annotations

import re
from typing import Any


SECRET_FIELD_RE = re.compile(
    r"(?i)(api[_-]?key|password|passwd|cookie|authorization|credential|secret|token)"
)


def reject_secrets(value: Any, path: str = 'value') -> None:
    """Reject credential-shaped fields before they reach local runtime data."""
    if isinstance(value, dict):
        for key, child in value.items():
            if SECRET_FIELD_RE.search(str(key)):
                raise ValueError(f'{path}.{key} 不允许保存凭据')
            reject_secrets(child, f'{path}.{key}')
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_secrets(child, f'{path}[{index}]')


__all__ = ['reject_secrets']
