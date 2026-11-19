"""兼容转发：``elab.envgen`` → :mod:`elab.platform.envgen`。

新代码请直接 ``from elab.platform import envgen``。
"""

from elab.platform.envgen import *              # noqa: F401,F403
from elab.platform.envgen import (              # noqa: F401
    PROVIDER_REF,
    cmd_env,
    collect,
    render_pwsh,
)
