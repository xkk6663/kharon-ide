"""兼容转发（P3）：``elab.westexec`` 的实现位于 :mod:`elab.platform.westexec`。

本文件用**模块别名**（sys.modules 替换）而不是代理对象：旧路径拿到的
就是新模块本体，``mock.patch("elab.westexec.x")`` 这类测试手段依然有效。
不含第二份实现。
"""

import sys

from elab.platform import westexec as _impl

sys.modules[__name__] = _impl
