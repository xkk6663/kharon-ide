"""debug（P3 分包）：调试会话、GDB/MI、SVD、RTOS 视图

模块按需懒加载（``__getattr__``），避免包 ``__init__`` 触发跨 context 的循环导入；
``__all__`` 即本 context 的公开 API 清单（方案 §4-P3 验收）。
"""

__all__ = ['debugsession', 'debugstore', 'debugdata', 'debugsource', 'gdbmi', 'svd', 'rtos', 'zephyr']


def __getattr__(name):
    if name in __all__:
        import importlib

        return importlib.import_module(f"{__name__}.{name}")
    raise AttributeError(name)


def __dir__():
    return sorted(set(globals()) | set(__all__))
