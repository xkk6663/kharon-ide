"""cockpit.api.routes.web —— 静态资源与 SPA 回退。"""

from __future__ import annotations

from __future__ import annotations

import json
import base64
import mimetypes
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_ROOT = Path(__file__).resolve()
for _anc in _ROOT.parents:
    if (_anc / "services" / "elab").is_dir():
        _ROOT = _anc
        break
_HERE = _ROOT / "cockpit"
for _p in (str(_ROOT), str(_ROOT / "services")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cockpit.core import *  # noqa: F401,F403
from cockpit.run.manager import *  # noqa: F401,F403
from cockpit.platform.summary import *  # noqa: F401,F403
from cockpit.platform.modes import *  # noqa: F401,F403
from cockpit.platform.state import *  # noqa: F401,F403
from cockpit.platform.jobs import *  # noqa: F401,F403
from cockpit.debug.helpers import *  # noqa: F401,F403
from cockpit.debug.manager import *  # noqa: F401,F403


def static(h, path: str):
    if not (h.dist / "index.html").exists():
        return h._send(200, _NO_DIST_HTML.encode("utf-8"),
                          "text/html; charset=utf-8")
    rel = path.lstrip("/") or "index.html"
    target = (h.dist / rel).resolve()
    # 目录穿越防护：解析后必须仍在 dist 内
    try:
        target.relative_to(h.dist.resolve())
    except ValueError:
        return h._err(403, "路径越界")
    if not target.is_file():
        # SPA 回退：无扩展名 → index.html（前端用 history 路由）
        if "." not in Path(rel).name:
            target = h.dist / "index.html"
        else:
            return h._err(404, f"找不到：{rel}")
    ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype in ("application/javascript",
                                              "application/json"):
        ctype += "; charset=utf-8"
    h._send(200, target.read_bytes(), ctype)
