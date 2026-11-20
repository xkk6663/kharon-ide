"""cockpit.api.routes.system —— 能力/工程/矩阵（GET）。"""

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
from elab.topology import topology  # noqa: F401


def handle_get(h, path, qs):
    if path == "/api/capabilities":
        return h._json(capabilities(h.cfg, h.mgr))
    if path == "/api/projects":
        return h._json(project_cards(h.cfg))
    if path == "/api/architecture":
        return h._json(topology(h.cfg))
    if path == "/api/matrix":
        # 多板对照：按 projects yaml 的 `matrix:` 分组聚合（只读 run 留档 + 构建产物）
        return h._json(matrix_view(
            h.cfg, group=(qs.get("group") or [""])[0]))
    return False
