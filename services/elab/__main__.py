"""elab CLI 入口（P3：实现迁到 :mod:`elab.api.cli`）。

这里保留 `elab.__main__` 的**完整名字面**：旧测试/脚本会
``import elab.__main__`` 并 patch 其中的 ``run_mod`` / ``_provider_build``，
因此除转发生效外还要暴露实现模块的属性（模块级 ``__getattr__``）。
"""

from elab.api import cli as _cli
from elab.api.cli import *  # noqa: F401,F403
from elab.api.cli import main  # noqa: F401


def __getattr__(name):
    return getattr(_cli, name)


def __dir__():
    return sorted(set(globals()) | set(dir(_cli)))


if __name__ == "__main__":
    raise SystemExit(main())
