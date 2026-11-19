"""platform（P3 分包）：宿主适配与生态（pack/provider/market/lock/adapt/west）

模块按需懒加载（``__getattr__``），避免包 ``__init__`` 触发跨 context 的循环导入；
``__all__`` 即本 context 的公开 API 清单（方案 §4-P3 验收）。
"""

__all__ = ['pkg', 'provider', 'market', 'lock', 'adapt', 'skillgen', 'project_admin', 'wszephyr', 'westws', 'zephyr_meta', 'ota_layout', '_winproc']


def __getattr__(name):
    if name in __all__:
        import importlib

        return importlib.import_module(f"{__name__}.{name}")
    raise AttributeError(name)


def __dir__():
    return sorted(set(globals()) | set(__all__))
