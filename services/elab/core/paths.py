"""elab.paths —— 产品定义 / 运行时状态的路径兼容访问。

目标布局把 ``products`` 移到 workspace，把调试/帧/组件状态移到 IDE 的
``.work/projects``；旧布局与测试里的轻量 fake cfg 仍只有 ``root/projects``。
这里统一做一次“有属性就用，没有就回落”，避免每个调用点各自写兼容分支。
"""

from __future__ import annotations

from pathlib import Path


def products_dir(cfg) -> Path:
    """产品定义根：``cfg.products_dir`` > 旧 ``cfg.root/projects``。"""
    value = getattr(cfg, "products_dir", None)
    if value is not None:
        return Path(value)
    return Path(cfg.root) / "projects"


def runtime_dir(cfg) -> Path:
    """运行时状态根：``cfg.runtime_dir`` > 旧 ``cfg.root/projects``。"""
    value = getattr(cfg, "runtime_dir", None)
    if value is not None:
        return Path(value)
    return Path(cfg.root) / "projects"
