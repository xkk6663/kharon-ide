"""兼容转发（P3）：``elab.monitor`` 已迁到 :mod:`elab.workflow.monitor`。

本文件用**模块别名**（sys.modules 替换）而不是代理对象：旧路径拿到的
就是新模块本体，``mock.patch("elab.monitor.x")`` 这类测试手段依然有效。
不含第二份实现；删除决策至少保留一个发布周期（方案 §4-P3-5）。
"""

import sys

from elab.workflow import monitor as _impl

sys.modules[__name__] = _impl
