"""兼容转发（P3）：``elab.ota_layout`` 已迁到 :mod:`elab.platform.ota_layout`。

本文件用**模块别名**（sys.modules 替换）而不是代理对象：旧路径拿到的
就是新模块本体，``mock.patch("elab.ota_layout.x")`` 这类测试手段依然有效。
不含第二份实现；删除决策至少保留一个发布周期（方案 §4-P3-5）。
"""

import sys

from elab.platform import ota_layout as _impl

sys.modules[__name__] = _impl
