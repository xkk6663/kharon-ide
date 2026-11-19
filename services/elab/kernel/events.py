"""兼容转发（P3）：``elab.kernel.events`` 已迁到 :mod:`elab.core.kernel.events`。"""

import sys

from elab.core.kernel import events as _impl

sys.modules[__name__] = _impl
