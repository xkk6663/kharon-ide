"""elab.envgen —— 从主机事实源生成等效 shell 环境脚本（T9）。

单一事实源：``config/host.<profile>.yaml`` + ``host.local.yaml``（经 ``Host``
解析/环境变量覆盖） + ``providers/official/zephyr/west/provider.yaml`` 的
``defaults.env`` / ``defaults.env_prepend_path``。

**不复制**一份变量清单：provider 里写什么，脚本就导什么。所以：

- provider 增删一个 env 键 → 重新 ``kharon env`` 即同步；
- host 里为 ``null`` 的键（如未装 SDK）→ 脚本里跳过并注明，不写 ``None``；
- ``${host:...}`` / ``${elab_root}`` 用与 run 时**同一个** ``_host_value``
  解析（含 ``ELAB_*`` 环境变量覆盖），避免"脚本里的路径"与"子进程里的路径"
  是两套。

用法::

    kharon env --shell pwsh                 # 打印（可 Invoke-Expression）
    kharon env --shell pwsh --out scripts/kharon-env.ps1
"""

from __future__ import annotations

import re
from pathlib import Path

from elab.core.config import Config, ElabError, to_fwd

PROVIDER_REF = "zephyr/west"
_HOST_REF = re.compile(r"\$\{host:([^}]+)\}")
_ELAB_ROOT_REF = "${elab_root}"
_IDE_ROOT_REF = "${ide_root}"
_ZEPHYR_ROOT_REF = "${zephyr_root}"


def _host_value(cfg: Config, key: str) -> str:
    """与 run 时同源的 ``${host:...}`` 解析；未配置（null）返回空串。"""
    from elab.workflow.run import RunError, _host_value as _hv

    try:
        return _hv(cfg, key)
    except RunError:
        return ""


def _expand(cfg: Config, text) -> str:
    """展开 ``${elab_root}`` 与 ``${host:...}``；仍未解析的模板 → 报错。"""
    s = str(text)
    for _ in range(10):
        new = s.replace(_ELAB_ROOT_REF, to_fwd(Path(cfg.ide_root)))
        new = new.replace(_IDE_ROOT_REF, to_fwd(Path(cfg.ide_root)))
        new = new.replace(_ZEPHYR_ROOT_REF, to_fwd(Path(cfg.zephyr_root)))
        new = _HOST_REF.sub(lambda m: _host_value(cfg, m.group(1)), new)
        if new == s:
            break
        s = new
    if "${" in s:
        raise ElabError(f"envgen: 无法解析模板 {s!r}")
    return s.strip()


def collect(cfg: Config) -> dict:
    """读 provider defaults，产出 ``{env, prepend, skipped, ...}``（不写盘）。"""
    from elab.platform import provider as provider_mod

    reg = provider_mod.ProviderRegistry(cfg)
    prov = reg.get(PROVIDER_REF)
    if prov is None:
        raise ElabError(
            f"未安装 provider {PROVIDER_REF}（查看 `elab provider list`；"
            f"Kharon profile 的 roots 应包含 providers/official/zephyr）")
    defaults = prov.man.get("defaults") or {}

    env: dict[str, str] = {}
    skipped: list[str] = []
    for k, v in (defaults.get("env") or {}).items():
        val = _expand(cfg, v)
        if not val:
            skipped.append(f"env.{k}")
            continue
        env[str(k)] = val

    prepend: list[str] = []
    for i, v in enumerate(defaults.get("env_prepend_path") or []):
        val = _expand(cfg, v)
        if not val:
            skipped.append(f"env_prepend_path[{i}]")
            continue
        prepend.append(val)

    sel = cfg.host_selection
    return {
        "env": env,
        "prepend": prepend,
        "skipped": skipped,
        "provider": prov.ref.name,
        "host_file": to_fwd(sel["path"]),
        "host_profile": sel["profile"] or str(sel["origin"]),
    }


def _ps(value: str) -> str:
    """PowerShell 单引号字符串（内部 ``'`` 双写）。"""
    return "'" + str(value).replace("'", "''") + "'"


def render_pwsh(info: dict) -> str:
    """把 :func:`collect` 的结果渲染成可 ``. script.ps1`` 的脚本。"""
    lines = [
        "# 由 `kharon env --shell pwsh` 生成 —— 勿手改；改 host yaml / provider 后重新生成。",
        f"# 主机事实源：{info['host_file']}（profile={info['host_profile']}）",
        f"# provider：{info['provider']} 的 defaults.env / defaults.env_prepend_path",
        "# 用法：. .\\scripts\\kharon-env.ps1",
        "",
    ]
    for k, v in info["env"].items():
        lines.append(f"$env:{k} = {_ps(v)}")
    if info["skipped"]:
        lines.append("")
        for k in info["skipped"]:
            lines.append(f"# ⚠ 未配置，已跳过：{k}（在 host.local.yaml 补上后重新生成）")
    if info["prepend"]:
        lines += [
            "",
            "# PATH 前置：声明顺序即优先级（列表首项在最前）",
            "$__kharon_prepend = @(",
        ]
        lines += [f"  {_ps(p)}" for p in info["prepend"]]
        lines += [
            ")",
            "$env:PATH = (($__kharon_prepend -join ';') + ';' + $env:PATH)",
            "Remove-Variable __kharon_prepend",
        ]
    lines += [
        "",
        "# 自检（T9 验收）：west 必须解析到 .deps/venv/Scripts 里的那个",
        "$__west = Get-Command west -ErrorAction SilentlyContinue",
        "if (-not $__west) { Write-Warning 'PATH 里找不到 west（检查 .deps/venv/Scripts 是否已前置）' }",
        r"elseif ($__west.Source -notmatch '[\\/]\.deps[\\/]venv[\\/]') { Write-Warning \"west → $($__west.Source)（不是 .deps/venv）\" }",
        "else { Write-Host \"west → $($__west.Source)\" }",
        "Remove-Variable __west",
        "",
    ]
    return "\n".join(lines)


def cmd_env(argv: list[str]) -> int:
    """``kharon env`` 子命令。"""
    import argparse
    import os

    ap = argparse.ArgumentParser(
        prog="kharon env",
        description="从 host yaml + provider defaults 生成等效环境脚本（T9）")
    ap.add_argument("--shell", default="pwsh", choices=("pwsh",),
                    help="目标 shell（当前只支持 pwsh）")
    ap.add_argument("--out", default="", help="写到文件（缺省打印到 stdout）")
    ap.add_argument("--check", action="store_true",
                    help="只校验 collect 能否成功（不输出脚本）")
    args = ap.parse_args(argv)

    root = Path(os.environ.get("ELAB_ROOT") or Path(__file__).resolve().parents[3])
    cfg = Config(root)
    info = collect(cfg)
    if args.check:
        print(f"[kharon env] ✓ {len(info['env'])} 个变量 / "
              f"{len(info['prepend'])} 条 PATH 前置"
              + (f"；跳过 {len(info['skipped'])} 项未配置" if info["skipped"] else ""))
        return 0

    text = render_pwsh(info)
    if not args.out:
        print(text, end="")
        return 0
    out = Path(args.out)
    out = out if out.is_absolute() else Path(cfg.root) / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8", newline="\n")
    print(f"[kharon env] ✓ 已写入 {to_fwd(out)}"
          + (f"（跳过 {len(info['skipped'])} 项未配置）" if info["skipped"] else ""))
    return 0
