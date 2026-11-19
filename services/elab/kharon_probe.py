"""兼容转发：``elab.kharon_probe`` → :mod:`elab.platform.kharon_probe`。

新代码请直接 ``from elab.platform import kharon_probe``。
"""

from elab.platform.kharon_probe import *          # noqa: F401,F403
from elab.platform.kharon_probe import (          # noqa: F401
    CMAKE_INCLUDE,
    cmd_adapt,
    probe,
    render_yaml,
)
