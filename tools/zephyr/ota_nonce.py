#!/usr/bin/env python3
"""nonce 账本：给 tools/ 下所有 OTA 脚本共用（ota_push / ota_drop / ota_resume_probe）。

为什么需要它
------------
设备端把"上一次接受过的 nonce"持久化在 NVS（applications/.../src/l0/ota.c 的 g_nonce_last），
规则是**只增不减**：`nonce <= last_used` 直接判重放并拒收，客户端只会看到发出
2048 B 后连接被 RST。

原来每个脚本各自维护 tools/.ota_nonce：只有 ota_push 成功后会回写，而 ota_drop 和
ota_resume_probe 只读 +1、不回写。于是最常见的用法（drop 制造断点 → probe 探记录 →
push --resume 续传）里，drop 和 probe 会发出**同一个** nonce，probe 必被拒；
再加上 .ota_nonce 一度被 git 跟踪，换机器 / 重新 clone 就回到旧值，和设备端的
last_used 彻底脱钩。

现在的做法
----------
    next_nonce() = max(账本值 + 1, 当前 UTC 秒)

- 取 max 而不是"恒取 UTC 秒"：本机时钟回拨或没对时的时候，账本那条单调线兜底；
- 每个请求发出后（设备已经接受并抬高了 last_used）调用 save_nonce() 回写；
- .ota_nonce 进 .gitignore，它是**本机运行状态**，不是源码。
"""

import json
import os
import time

NONCE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".ota_nonce")


def load_nonce():
    """读账本里记下的"设备端已接受的最大 nonce"；没有就返回 0。

    踩过的坑：这里曾经是 `except Exception: return 0`，而 PowerShell 的
    `Out-File -Encoding utf8` 会带 BOM，json.load 抛 "Unexpected UTF-8 BOM"，
    于是静默退回 0 —— 下一次发出 1，被设备端判 403 replay。
    静默兜底等于把"状态读坏了"伪装成"第一次运行"，所以这里用 utf-8-sig 并让读取
    失败直接吵出来：读坏了要人来确认，别拿 0 去撞 replay。
    """
    if not os.path.exists(NONCE_FILE):
        return 0
    try:
        with open(NONCE_FILE, "r", encoding="utf-8-sig") as f:
            return int(json.load(f)["nonce"])
    except Exception as e:
        raise SystemExit(
            f"{NONCE_FILE} 读不出来（{e}）；先修好它，或用 --nonce 显式指定一个大于设备端上次的值"
        )


def next_nonce():
    """下一个可用的 nonce：账本 +1 与当前 UTC 秒取大。"""
    return max(load_nonce() + 1, int(time.time()))


def save_nonce(v):
    """把设备已接受的 nonce 写回账本。"""
    with open(NONCE_FILE, "w", encoding="utf-8") as f:
        json.dump({"nonce": int(v)}, f)
