"""常量模块"""

from __future__ import annotations

DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_NEGATIVE_FILTER_TAG = "{{{{{nsfw}}}}}"

SIZE_ALIASES = {
    "竖": "832x1216",
    "竖图": "832x1216",
    "v": "832x1216",
    "portrait": "832x1216",
    "横": "1216x832",
    "横图": "1216x832",
    "h": "1216x832",
    "landscape": "1216x832",
    "方": "1024x1024",
    "方图": "1024x1024",
    "s": "1024x1024",
    "square": "1024x1024",
}

SIZE_DISPLAY_NAMES = {
    "832x1216": "竖图 832x1216",
    "1216x832": "横图 1216x832",
    "1024x1024": "方图 1024x1024",
}
