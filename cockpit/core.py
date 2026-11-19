"""cockpit.core —— 驾驶舱的基础词汇与通用工具（P2 拆分自 server.py）。

只依赖 stdlib 与 ``services/elab``；**不**依赖任何 cockpit 子包，避免环。
"""

from __future__ import annotations

import mimetypes
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "services") not in sys.path:
    sys.path.insert(0, str(_ROOT / "services"))

from elab import adapt as adapt_mod                      # noqa: E402
from elab import project_admin as project_admin_mod      # noqa: E402
from elab import flash as flash_mod                      # noqa: E402
from elab import debugsession as dbgsess_mod           # noqa: E402
from elab.plan import plan_for                          # noqa: E402
from elab.flow import KERNEL_PLANS, STEP_META           # noqa: E402  流程清单（唯一事实源）
from elab import run as run_mod                      # noqa: E402
from elab._winproc import adopt as adopt_proc        # noqa: E402
from elab import provider as provider_mod            # noqa: E402
from elab import zephyr_meta as zephyr_meta_mod      # noqa: E402
from elab import westws as westws_mod                # noqa: E402
from elab import serialcodec as serialcodec_mod      # noqa: E402
from elab import serialconsole as serialconsole_mod  # noqa: E402
from elab import serialcontrol as serialcontrol_mod  # noqa: E402
from elab import logdecode as ld                      # noqa: E402
from elab import serialframes as sf                   # noqa: E402
from elab import framelearn as framelearn_mod         # noqa: E402
from elab import serialmon as serialmon_mod          # noqa: E402
from elab import serialport as serialport_mod        # noqa: E402
from elab import serialtransport as serialtransport_mod  # noqa: E402
from elab import serialterm as serialterm_mod        # noqa: E402
from elab import serialwidgets as serialwidgets_mod  # noqa: E402
from elab import modbus as modbus_mod                # noqa: E402
from elab import modbusmaster as modbusmaster_mod    # noqa: E402
from elab.config import Config, ElabError, Host, to_fwd     # noqa: E402
from elab.kernel import events as ev_mod              # noqa: E402
from elab import pkg as pkg_mod                       # noqa: E402
from elab import setup as setup_mod                   # noqa: E402
from elab import _yaml as yaml_mod                    # noqa: E402



_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT / "services") not in sys.path:
    sys.path.insert(0, str(_ROOT / "services"))

from elab import adapt as adapt_mod                      # noqa: E402
from elab import project_admin as project_admin_mod      # noqa: E402
from elab import flash as flash_mod                      # noqa: E402
from elab import debugsession as dbgsess_mod           # noqa: E402
from elab.plan import plan_for                          # noqa: E402
from elab.flow import KERNEL_PLANS, STEP_META           # noqa: E402  流程清单（唯一事实源）
from elab import run as run_mod                      # noqa: E402
from elab._winproc import adopt as adopt_proc        # noqa: E402
from elab import provider as provider_mod            # noqa: E402
from elab import zephyr_meta as zephyr_meta_mod      # noqa: E402
from elab import westws as westws_mod                # noqa: E402
from elab import serialcodec as serialcodec_mod      # noqa: E402
from elab import serialconsole as serialconsole_mod  # noqa: E402
from elab import serialcontrol as serialcontrol_mod  # noqa: E402
from elab import logdecode as ld                      # noqa: E402
from elab import serialframes as sf                   # noqa: E402
from elab import framelearn as framelearn_mod         # noqa: E402
from elab import serialmon as serialmon_mod          # noqa: E402
from elab import serialport as serialport_mod        # noqa: E402
from elab import serialtransport as serialtransport_mod  # noqa: E402
from elab import serialterm as serialterm_mod        # noqa: E402
from elab import serialwidgets as serialwidgets_mod  # noqa: E402
from elab import modbus as modbus_mod                # noqa: E402
from elab import modbusmaster as modbusmaster_mod    # noqa: E402
from elab.config import Config, ElabError, Host, to_fwd     # noqa: E402
from elab.kernel import events as ev_mod              # noqa: E402
from elab import pkg as pkg_mod                       # noqa: E402
from elab import setup as setup_mod                   # noqa: E402
from elab import _yaml as yaml_mod                    # noqa: E402

HOST = "127.0.0.1"            # ★ 只监听本地（§5.6：本地工具，不做远程暴露）
#: ★ C34：默认端口**刻意避开 3333** —— openocd 的 gdb server 固定绑 3333，
#:   驾驶舱若占住它，闭环 debug 步骤的 openocd 会静默退出（绑定失败），
#:   症状是 debug_verify 误导性失败（实测事故 2026-10-06）。换端口用 --port。
DEFAULT_PORT = 8333
SSE_HEARTBEAT_S = 15.0        # §17.3
PROC_BATCH_S = 0.10           # §17.3：proc 行 100ms 合并，事件数降 1~2 个数量级
QUEUE_MAX = 2000              # §17.3：每连接有界队列
#: ★ 16（盘点 #3，原 8）：App 1 条 run 流 + 串口 1 条 + 调试壳层/pane 各 1 条，
#:   两个会话双列时调试就吃 3 条 —— 3 个标签页就能撞满 8。
#:   503 对 EventSource 是"直接关闭"，前端只会看到连接失败，所以满员的代价比
#:   想象的大：静默停更。名额放宽 + 前端按需建流（无会话不建壳层）双管齐下。
MAX_SSE_CONNS = 16
DIST_DIR = _HERE / "web" / "dist"

# ★ 内核自描述（§16 分层解耦）：业务词汇的唯一事实源。
#   工具层（DSH 插件 / CLI 帮助）只渲染这里的定义，不自带业务名词 ——
#   内核改名/加步骤/调档位，工具层无需改代码。
#   ★ 流程清单（步骤标签 / 通道 / 三档）已挪到 `services/elab/flow.py`：
#     驾驶舱只是 HTTP 载体，词汇不该挂在载体上（载体可换，词汇不变）。
#     上面的 import 即本模块的 STEP_META / KERNEL_PLANS —— 保持模块级同名，
#     免得 `server.STEP_META` 这类既有引用点失效。

#: 串口词汇表（内核自描述，§16 / §4.4）：视图、表示、编码、行尾、解码器。
#: 工具层与前端**只渲染**这里的清单；新增能力只改内核，别在工具层写死业务名词。
SERIAL_META = {
    "views": ["lines", "bytes", "terminal", "wave"],
    # 「自动」不是一个编码，而是「让内核替你选」—— 但它确实能被
    # resolve_payload 接受，所以作为可选项一起自描述，前端不用写死。
    "formats": [*serialterm_mod.SEND_FORMATS, serialterm_mod.AUTO_FORMAT],
    "encodings": ["utf-8", "gbk"],
    "eols": list(serialterm_mod.EOLS.keys()),
    "directions": ["all", "rx", "tx"],
    "decoders": [*serialcodec_mod.DECODERS,
                 {"id": "profile", "label": "项目帧协议",
                  "channels": "fields",
                  "desc": "按 projects/<name>.frames.json 的字段表解码"}],
    # 帧协议配置（M5）：限制与校验目标由内核给出，面板不写死
    "frames": {"max_profiles": sf.MAX_PROFILES, "max_fields": sf.MAX_FIELDS,
               "max_frame_bytes": sf.MAX_FRAME, "max_file_bytes": sf.MAX_FILE,
               "max_variants": sf.MAX_VARIANTS,
               "types": list(sf.TYPES), "endians": list(sf.ENDIANS),
               "checksums": list(sf.CHECKS),
               "crc_presets": list(serialcodec_mod.CRC_PRESETS),
               "cover_anchors": list(serialcodec_mod.COVER_ANCHORS),
               "encodings": list(sf.ENCODINGS),
               "escape_applies": list(sf.ESCAPE_APPLIES),
               "max_array": sf.MAX_ARRAY,
               "bit_orders": list(sf.BIT_ORDERS),
               "length_field_types": list(sf.LENGTH_FIELD_TYPES),
               # 声明式 schema：Agent/前端按此自行构造 profile，不猜字段
               "schema": {
                   "profile": {
                       "id": "[a-z0-9_-]{1,32}",
                       "label": "string",
                       "kind": "binary",
                       "frame": "frame（见下）",
                       "checksum": ("{kind: none|sum8|xor8|crc8|crc16|crc32|"
                                    "crc16modbus, at: tail|head, "
                                    "preset?, poly?, init?, xorout?, refin?, "
                                    "refout?, size?, endian?, offset?, "
                                    "cover?: {from,to}}"),
                       "escape": "{escape_byte, xor, targets[], applies}（可选）",
                       "fields": "[field]（可选，variant 未命中时的兜底）",
                       "variants": "[variant]（可选，最多 max_variants）",
                       "allow_overlap": "bool=false（字段区间重叠必须显式允许）",
                   },
                   "frame": {
                       "head_hex": "hex（可选；与 heads 互斥）",
                       "heads": "[hex]（可选，1..4 个帧头，最长匹配优先）",
                       "tail_hex": "hex（可选）",
                       "length": "int|null（固定帧长，与 length_field 互斥）",
                       "idle_gap_ms": "int（可选，1..5000；按空闲间隔切帧）",
                       "max_bytes": "int（默认 256）",
                       "length_field": {
                           "offset": "int（从帧首字节算）",
                           "type": "u8|u16",
                           "endian": "le|be",
                           "mul": "number=1（总长 = raw * mul + add）",
                           "add": "number=0（可为负）",
                       },
                       "id_field": {
                           "offset": "int",
                           "type": "整数类型",
                           "endian": "le|be",
                           "bit_offset": "0..7（可选）",
                           "bit_length": "1..32（可选）",
                           "bit_order": "lsb0|msb0（可选）",
                       },
                   },
                   "escape": {
                       "escape_byte": "0x 字节",
                       "xor": "0x 字节（转义后与原字节异或）",
                       "targets": "[0x 字节]（需要转义的字节，含 escape_byte 自身）",
                       "applies": "payload（默认，校验字节不转义）|body",
                   },
                   "field": {
                       "name": "string（profile 内唯一）",
                       "type": "u8|u16|u24|u32|i8|i16|i24|i32|f16|f32|f64|ascii|str|bytes",
                       "offset": "int",
                       "endian": "le|be",
                       "length": "ascii/str/bytes 必填",
                       "encoding": "utf-8|gbk|ascii|latin-1（ascii/str）",
                       "scale": "number=1",
                       "bias": "number=0",
                       "unit": "string",
                       "enum": "{raw: label}（可选；有 enum 时不做波形通道）",
                       "bits": "{bit: label}（可选，位标志；原始值仍保留）",
                       "array": "{count: int, stride: int}（重复块，展开为 name[i]）",
                       "plot": "bool",
                       "bit_offset": "0..7（可选，需同时给 bit_length）",
                       "bit_length": "1..32（可选；只对整型）",
                       "bit_order": "lsb0|msb0（默认 lsb0）",
                   },
                   "variant": {
                       "id": "int 或 0x 十六进制字符串",
                       "label": "string",
                       "fields": "[field]",
                       "checksum": "可选；不写则用 profile.checksum",
                   },
               },
               "templates": {"path": str(sf.TEMPLATE_SUBDIR),
                             "list": "/api/serial/frames/templates",
                             "import": "/api/serial/frames/templates/import"},
               "endpoints": {"load": "/api/serial/frames",
                             "save": "/api/serial/frames",
                             "preview": "/api/serial/frames/preview",
                             "encode": "/api/serial/frames/encode",
                             "learn": "/api/serial/frames/learn",
                             "verify": "/api/serial/frames/verify",
                             "auto": "/api/serial/frames/auto",
                             "codegen": "/api/serial/frames/codegen",
                             "templates": "/api/serial/frames/templates",
                             "template_import":
                                 "/api/serial/frames/templates/import"},
               "learn": {"min_head_hits": framelearn_mod.MIN_HEAD_HITS,
                         "auto_min_score": framelearn_mod.AUTO_MIN_SCORE,
                         "auto_margin": framelearn_mod.AUTO_MARGIN,
                         "checksum_probes": list(framelearn_mod.LEARN_CHECKSUMS),
                         "note": ("learn 只产草稿（不落盘）：保存仍走 save 的 "
                                  "rev 乐观锁；草稿可直接喂 preview/verify")}},
    "features": ["hex", "search", "clear", "pause", "epoch", "lease",
                 "capture", "capture_export", "waveform",
                 # 通用发送面板（P1）：自动格式识别 + 服务端字节预览
                 "auto_format", "send_preview",
                 # 会话级共享解码：agent 选的 decoder 与面板同图（P3.5）
                 "decoder_share", "decoded_frames",
                 # 自定义发送组件（M1）：chips / 参数 / 预览 / 单条发送
                 "send_widgets", "widget_dry_run", "widget_jobs",
                 # 序列 / 周期（M2）：send+delay+wait、固定节拍、暂停/取消
                 "widget_sequence", "widget_wait", "widget_repeat", "widget_pause",
                 # 发送框的瞬态周期任务（不落盘，与组件同一引擎）
                 "raw_loop",
                 # 线参数（M1）：7E1/2 停止位/流控 —— 打开时生效
                 "line_config",
                 # DTR/RTS 原始控制 + 包内数据驱动的复位/下载序列（M1）
                 "dtr_rts", "reset_presets",
                 # 会话韧性（M2）：掉线自动重连 + 闭环让位/回归 + 速率统计
                 "reconnect", "conn_state", "run_yield", "rate_stats",
                 # 日志诊断（M6）：结构化 panic + 地址符号化
                 "log_profiles", "panic_issues", "symbolize",
                 # 帧协议（M5）：配置读写 + 无硬件预览 + 实时字段表
                 "frame_profiles", "frame_preview", "frame_fields",
                 # 帧协议增强（P0/P1）：位域 / 变长 / 多 ID / 反向组帧 / 模板
                 "frame_bitfields", "frame_length_field", "frame_variants",
                 "frame_encode", "frame_templates", "agent_frame_edit",
                 # 帧协议补强（P2.2）：校验预设/覆盖范围、转义、多帧头、
                 # 空闲分包、数组/字符串/f16/位标志、重叠校验、错误分类
                 "frame_checksum_presets", "frame_escape", "frame_multi_head",
                 "frame_idle_gap", "frame_arrays", "frame_strings",
                 "frame_half_float", "frame_flag_bits", "frame_overlap_check",
                 "frame_error_reasons",
                 # Agent 生产力（P4）：抓包学习 / round-trip / 自动匹配 / C 头
                 "frame_learn", "frame_verify", "frame_auto", "frame_codegen",
                 # 观测（P3）：帧级健康度 + 触发式录制
                 "frame_stats", "capture_trigger",
                 # 网络传输（M4）：TCP 客户端/服务端 + UDP，默认只绑回环
                 "net_transport", "udp", "allow_remote",
                 # Modbus RTU 主站（协议计划 §5.5）：事务、寄存器映射、轮询
                 "modbus_rtu", "modbus_poll", "modbus_map", "modbus_write"],
    # 传输种类（M4，内核自描述）：面板按此渲染「连接方式」，不写死业务名词
    "transports": {
        "kinds": ["serial", *serialtransport_mod.NET_KINDS],
        "default": "serial",
        "net": {
            "defaults": serialtransport_mod.netio.NET_DEFAULTS,
            "loopback_only": True,
            "note": ("默认只允许 127.0.0.1 / localhost；跨机连接需显式 "
                     "allow_remote（面板首次放开要弹警告）。TCP 服务端一次"
                     "只服务一个客户端，客户端断开视为链路断开并触发重连。"),
        },
    },
    # 组件词汇与限制（内核自描述；工具层/前端只渲染，不写死）
    "widget": serialwidgets_mod.meta(),
    # 线参数（M1）：面板只渲染这些枚举；默认 8N1 = 旧行为
    "line": {
        "data_bits": [5, 6, 7, 8],
        "parity": list(serialport_mod.PARITIES),
        "stop_bits": list(serialport_mod.STOP_BITS),
        "flow": list(serialport_mod.FLOWS),
        "default": serialport_mod.LineConfig().as_dict(),
        "text_forms": ["8N1", "7E1", "8O2", "5N1.5"],
        "note": "线参数是打开时属性 —— 改后需重开监视/发送才生效",
    },
    # 重连（M2）：默认开、200ms→5s 无限退避（附录 B）
    "reconnect": {
        "default": dict(serialmon_mod.RECONNECT_DEFAULT),
        "backoff_ms": [200, 400, 800, 1600, 3200, 5000],
        "states": ["live", "reconnecting", "suspended", "closed"],
    },
    # 缓冲上限（M2 统计）：面板只渲染，不写死数字
    "buffer": serialmon_mod.limits(),
    # 日志诊断（M6）：profile 来自包/工程数据；内核只有通用解释器
    "log_profiles": {"source": "/api/serial/log-profiles",
                     "issues": "/api/serial/monitor/issues",
                     "symbolize": "/api/serial/symbolize",
                     "kinds": ["panic", "assert", "hardfault", "reset"],
                     "session_scoped": True},
    # 闭环前后监视的处理：open（默认）/ restore / off
    "run_yield": {"resume_after_run": ["open", "restore", "off"],
                  "default": "open",
                  "note": "闭环占用串口时监视自动让位；run 结束按此策略回归"},
    # 定时发送的限制（不写死在面板里）：间隔下限、单任务最长时长、count 语义
    "loop": {
        "min_interval_ms": serialwidgets_mod.MIN_REPEAT_MS,
        "max_duration_s": serialwidgets_mod.MAX_REPEAT_S,
        "count_zero_means": "until_stop",
        "interleaving": "exclusive",
    },
}

#: Modbus 自描述（协议计划 §5.5）：功能码 / 类型 / 字序 / 限制 / 异常码 / 错误分类
#: 全部来自 ``elab.modbus``，时间参数来自 ``elab.modbusmaster`` —— 面板与 Agent
#: 只渲染，不各自记数字（记的数字迟早会和内核不同步）。
MODBUS_META = {
    "mode": "rtu-master",
    "transport": "serial",
    "unit": {"min": modbus_mod.UNIT_MIN, "max": modbus_mod.UNIT_MAX,
             "broadcast": modbus_mod.BROADCAST_UNIT,
             "note": "0 只用于广播写，不应答；从站地址 1..247"},
    "limits": modbus_mod.limits(),
    "timing": {"default_timeout_ms": modbusmaster_mod.DEFAULT_TIMEOUT_MS,
               "min_timeout_ms": modbusmaster_mod.MIN_TIMEOUT_MS,
               "max_timeout_ms": modbusmaster_mod.MAX_TIMEOUT_MS,
               "max_retries": modbusmaster_mod.MAX_RETRIES,
               "min_interval_ms": modbusmaster_mod.MIN_INTERVAL_MS,
               "merge_gap": modbusmaster_mod.MERGE_GAP,
               "max_backoff": modbusmaster_mod.MAX_BACKOFF},
    "poll": {"max_points": modbusmaster_mod.MAX_POINTS,
             "max_groups": modbusmaster_mod.MAX_GROUPS,
             "max_pending": modbusmaster_mod.MAX_PENDING,
             "single_flight": True,
             "merge": "同功能码、地址相邻（允许跨 merge_gap 个空洞）合并为一条读"},
    "map": {
        "path": "projects/<name>.modbus.json",
        "rev": "乐观锁：保存必须带 rev，冲突返回 409",
        "point": {
            "name": "string（唯一）",
            "fc": "1|2|3|4（读功能码）",
            "address": "int（协议偏移，0 基；40001 的换算在文档里）",
            "type": "bit|u16|s16|u32|s32|f32",
            "word_order": "big|little（u32/s32/f32 必填）",
            "scale": "number=1（只影响展示与波形）",
            "bias": "number=0",
            "unit": "string",
            "writable": "bool=false（写点必须显式 true）",
            "plot": "bool=true（bit 点默认不画）",
            "enum": "{raw: label}（可选）",
            "note": "string",
        },
        "group": {"name": "string", "interval_ms": "int（≥ min_interval_ms）",
                  "points": "[name]（省略=全部点）", "enabled": "bool=true"},
    },
    "endpoints": {
        "meta": "/api/modbus/meta",
        "state": "/api/modbus/state",
        "open": "/api/modbus/open",
        "close": "/api/modbus/close",
        "start": "/api/modbus/start",
        "stop": "/api/modbus/stop",
        "read": "/api/modbus/read",
        "write": "/api/modbus/write",
        "map": "/api/modbus/map",
        "transactions": "/api/modbus/transactions",
    },
    "note": ("主站挂在**已打开的串口会话**上，不新开口、不复制读线程；"
             "写操作要求 confirm=true（面板弹确认），广播写不等应答。"),
    "errors": {"kinds": list(modbus_mod.ERROR_KINDS),
               "note": "timeout/port 由事务层产生，crc/exception/malformed 由协议层判定"},
}


def _require_modbus():
    """取当前 Modbus 主站；没有就给出**能照做**的下一步，而不是 500。"""
    eng = modbusmaster_mod.current()
    if eng is None:
        raise ElabError("Modbus 主站未打开：先 POST /api/modbus/open"
                        "（它会复用/打开串口会话，不新开口）")
    return eng


def _as_int(body: dict, key: str, default: int) -> int:
    raw = body.get(key, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ElabError(f"{key} 必须是整数：{raw!r}") from None


def _mb_call(fn, *args, **kw):
    """把协议层的边界错误翻成 400（而不是让它变成 500 让用户猜）。"""
    try:
        return fn(*args, **kw)
    except modbus_mod.ModbusError as exc:
        raise ElabError(exc.detail) from exc


def _frame_profile_of(cfg, body: dict) -> dict:
    """从请求体取 profile：优先**未保存草稿**，否则 project+id 装载。

    Agent 的用法是"先学一个草稿 → preview/verify 验 → 再 save"，所以每个
    帧协议端点都必须接受草稿，不然学习结果没法在落盘前验证。
    """
    draft = body.get("profile")
    if isinstance(draft, dict):
        try:
            return sf.normalize_profile(draft)
        except ValueError as exc:
            raise ElabError(str(exc))
    project = str(body.get("project") or "")
    pid = str(body.get("id") or "")
    if not project or not pid:
        raise ElabError("需要 project+id，或 profile 草稿")
    prof = sf.get(cfg, project, pid)
    if prof is None:
        raise ElabError(f"未找到帧协议 profile：{pid!r}")
    return prof


#: 录制文件读入上限：学/校验只用开头几万字节，几 MB 的文件纯属浪费内存。
MAX_CAPTURE_FILE_BYTES = 16 * 1024 * 1024

#: 请求体上限：正常配置/样本都远小于它；这条只挡"把几十 MB 字节塞进 body"的误用。
MAX_BODY_BYTES = 8 * 1024 * 1024

#: 拒收时最多读掉多少字节（读了才能把错误回给客户端，见 ``_drain_body``）。
MAX_DRAIN_BYTES = 64 * 1024 * 1024


def _capture_text(cfg: Config, raw) -> str:
    """``capture`` 参数：既接受 JSONL **文本**，也接受录制文件**路径**。

    agent 手里通常只有文件名（录制是它刚用 capture 端点起的），让它把几 KB
    JSONL 塞进请求体不现实；而"按路径读文件"是个危险入口 —— 所以路径必须
    落在本工程的 ``.work/.cockpit`` 里且是 ``.jsonl``，别处一律拒绝（源码、
    配置、密钥都在别处）。
    """
    s = str(raw or "")
    if not s.strip() or s.lstrip().startswith("{"):
        return s                              # 已经是内容（或空）：原样交下去
    p = Path(s)
    if not p.is_absolute():
        p = Path(cfg.root) / p
    try:
        p = p.resolve()
    except OSError as exc:
        raise ElabError(f"capture 路径无法解析：{s}（{exc}）")
    cap_dir = (Path(cfg.root) / ".work" / ".cockpit").resolve()
    if p.suffix.lower() != ".jsonl" or cap_dir not in p.parents:
        raise ElabError(f"capture 只接受 {cap_dir} 下的 .jsonl 录制文件：{s}")
    if not p.exists():
        raise ElabError(f"录制文件不存在：{s}")
    try:
        size = p.stat().st_size
    except OSError as exc:
        raise ElabError(f"读不到录制文件大小：{exc}")
    if size > MAX_CAPTURE_FILE_BYTES:
        raise ElabError(
            f"录制文件 {size / 1048576:.1f} MB 太大（上限 "
            f"{MAX_CAPTURE_FILE_BYTES // 1048576} MB）："
            f"学习只需开头几万字节，请截一段再学，或重录一段更短的")
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ElabError(f"读取录制文件失败：{exc}")


def _frame_bytes_of(body: dict, cfg: Config) -> bytes:
    """请求体里的样本字节：b64 / hex / capture(JSONL 文本或录制路径) 三选一。"""
    if body.get("capture"):
        chunks, _ = framelearn_mod.parse_capture(_capture_text(cfg, body.get("capture")))
        return b"".join(chunks)
    if body.get("b64"):
        try:
            return base64.b64decode(str(body["b64"]))
        except Exception as exc:                       # noqa: BLE001
            raise ElabError(f"b64 不是合法 base64：{exc}")
    if body.get("hex"):
        try:
            return bytes.fromhex("".join(str(body["hex"]).split()))
        except ValueError as exc:
            raise ElabError(f"hex 不是合法十六进制：{exc}")
    return b""


COCKPIT_VERSION = "4.0.0"


def _sync_serial_decoder(cfg, project: str, pid: str, deleted: bool) -> None:
    """profile 保存/删除后热更新会话解码器（只动正在用该 profile 的会话）。"""
    snap = serialmon_mod.status()
    if not snap.get("active"):
        return
    cur = str(snap.get("decoder") or "")
    if not (cur == "profile" or cur.startswith("profile:")):
        return
    cur_id = cur.split(":", 1)[1].strip() if ":" in cur else ""
    if cur_id != pid:
        return
    if deleted:
        serialmon_mod.set_decoder("", actor="profile-sync", cfg=cfg,
                                  project=project)
    else:
        serialmon_mod.set_decoder(cur, actor="profile-sync", cfg=cfg,
                                  project=project)

DEBUG_LEASE_DEFAULT_S = 600
DEBUG_LEASE_MIN_S = 60
DEBUG_LEASE_MAX_S = 1800
DEBUG_AUDIT_MAX = 500

#: 调试能力自描述（§16 内核自描述）：工具层按此渲染，不写死业务名词。
#: ★ 内核过旧（缺 ``meta.debug``）时插件隐藏调试入口 —— 不在工具层写兼容分支。
DEBUG_META = {
    "available": True,
    "max_sessions": 2,
    "features": ["session", "control", "stack", "variables", "registers",
                 "breakpoints", "memory", "disasm", "watch", "source",
                 "svd", "rtos"],
    "actions": ["continue", "pause", "step_over", "step_in", "step_out",
                "stepi", "reset", "restart", "run_to", "run_to_line"],
    "stop_at": ["main", "reset", "none"],
    "breakpoint_kinds": ["func", "addr", "source"],
    "registers_group": ["core", "fpu", "all"],
    "formats": ["natural", "hex"],
    "watch_formats": ["natural", "hex", "binary"],
    "svd": {"tree_path": "PERIPH 或 PERIPH.REG", "read": True},
    "rtos": {"available_via": "elf symbols (FreeRTOS pxCurrentTCB)", "granularity": "task"},
    "lease": {"default_s": DEBUG_LEASE_DEFAULT_S, "min_s": DEBUG_LEASE_MIN_S,
              "max_s": DEBUG_LEASE_MAX_S},
    "limits": {"memory_read_max": 4096, "disasm_max": 128,
               "variables_depth": 8, "audit_records": DEBUG_AUDIT_MAX,
               "watch_max": 32, "source_lines_max": 400,
               "source_bytes_max": 512 * 1024, "register_batch_max": 16},
    "error_codes": ["debug-unavailable", "debug-busy", "debug-no-session",
                    "debug-elf-missing", "debug-gdb-missing", "debug-openocd-dead",
                    "debug-port-hijacked", "debug-timeout", "debug-target-running",
                    "debug-bad-request", "debug-write-denied", "debug-limit",
                    "debug-rev-conflict", "debug-svd-missing",
                    "debug-rtos-unavailable", "debug-error",
                    "debug-target-busy", "debug-session-limit",
                    "debug-session-required", "debug-source-unavailable"],
    # 互斥语义（面板据此显示人话）
    "exclusive_with": ["run(flash/debug_verify/monitor)", "serial-monitor"],
    "on_close": {"resume": True, "method": "monitor reset run"},
}

#: 调试页签的**项目级**收敛：全局 ``DEBUG_META.features`` 是内核能力上限，
#: 实际显示按「项目（芯片）→ 会话（snapshot.features）」逐级收敛。
_DEBUG_BASE_FEATURES = ["session", "control", "stack", "variables", "registers",
                        "breakpoints", "memory", "disasm", "watch", "source"]


def _debug_meta(cfg: Config) -> dict:
    """``capabilities.meta.debug`` —— 会话上限取 L0（``debug.max_sessions``）。"""
    meta = dict(DEBUG_META)
    try:
        raw = int(((cfg.host or {}).get("debug") or {}).get("max_sessions", 2))
    except (TypeError, ValueError):
        raw = 2
    meta["max_sessions"] = max(1, min(raw, 4))
    return meta


#: 构建产物内存读数的进程内缓存：key=(elf 路径, mtime) → memory dict。
#: 只读产物、按 mtime 失效 —— 不打数据库、不依赖 run 历史（重启后仍能显示）。
_MEASURED_CACHE: dict[tuple[str, float], dict] = {}


def _zephyr_work_dir(cfg: Config, project: str) -> str:
    """项目名 → 展开后的构建目录（项目不存在或未声明时返回空串）。

    Zephyr 的构建元数据（runners / .config / zephyr.dts / footprint）全在
    work_dir 里，所以这一条是四个只读端点的共同入口。
    """
    proj = cfg.projects.get(project)
    if not proj:
        return ""
    try:
        return to_fwd(cfg.project_ctx(proj).get("work_dir") or "")
    except (KeyError, ElabError):
        return ""


def _measured_memory(work_dir: str) -> dict:
    """已构建工程的**实测**内存（来自构建产物，不是芯片 yaml 的声明）。

    ★ 红线（方案 §3.3/§4.3）：Zephyr 的芯片包刻意不写 memory —— 那在 dts/Kconfig 里。
      面板要显示数字，只能读构建产物，并明确标注「实测」。数字与声明一旦混在一处，
      体检结论就会和构建结果打架。
    Zephyr 走 ELF 读数（FLASH+RAM 与链接器自报逐字节一致）；其他工程返回空。
    """
    if not work_dir:
        return {}
    try:
        elf = Path(work_dir) / "zephyr" / "zephyr.elf"
        if not elf.is_file():
            return {}
        key = (str(elf), elf.stat().st_mtime)
        hit = _MEASURED_CACHE.get(key)
        if hit is not None:
            return hit
        mem = zephyr_meta_mod.memory(work_dir)
        if len(_MEASURED_CACHE) > 64:                 # 有界，别当缓存服务用
            _MEASURED_CACHE.clear()
        _MEASURED_CACHE[key] = mem
        return mem
    except OSError:
        return {}


def _debug_features(chip: dict) -> list[str]:
    """芯片声明的调试能力（如 ESP32 无 SVD → 不出现「外设」页签）。"""
    dbg = chip.get("debug") or {}
    feats = list(_DEBUG_BASE_FEATURES)
    if dbg.get("svd"):
        feats.append("svd")
    # RTOS 是固件能力：会话打开后才探测符号；项目级沿用乐观声明（与 debugsession 一致）
    if dbg.get("rtos") is not False:
        feats.append("rtos")
    return feats


#: 服务端可能通过 SSE 发出的**命名事件**全集（跨端契约的镜像）。
#:
#: ★ 为什么必须在这里再列一遍（而不是"从代码里自然得到"）：
#:   SSE 用的是**命名事件**，`EventSource.onmessage` 只接收**没有** `event:`
#:   字段的帧，所以前端必须为每个 topic 显式 `addEventListener`。漏一个，
#:   该 topic 的全部事件在浏览器里**静默消失** —— 不报错、`onerror` 不触发、
#:   服务端日志也一切正常（ICD §6.1）。
#:   实测事故：前端白名单漏了 `proc/stdout-batch`，于是 **run 跑起来之后所有
#:   实时编译日志都不可见**；而同一个 run 结束后再看（走按 seq 重放分支、
#:   逐条 `proc/stdout`）却是全的 —— 这个"历史有、实时没有"的不对称极难归因。
#:
#: 谁在守它：
#:   ① `_sse_warn_unknown()` —— 真发出未登记 topic 时打一条 WARN（把静默变有声）；
#:   ② `tests/it_cockpit_server.py::test_known_topics_covers_server_emitted`
#:      —— 与前端 `KNOWN_TOPICS` 做集合比对，CI 层拦截。
SSE_TOPICS = frozenset({
    "run/start", "run/step-enter", "run/step-exit", "run/end", "run/cancel",
    "proc/stdout", "proc/stderr", "proc/stdout-batch", "proc/exit",
    "serial/open", "serial/line", "serial/close", "serial/error",
    "serial/closed-loop",
    # 调试会话（独立信封，不复用 run 信封）
    "debug/opened", "debug/state", "debug/stop", "debug/breakpoint",
    "debug/output", "debug/error", "debug/closed",
    "stream/overrun", "stream/closed",
})


#: 服务端**合成**（不写事件日志）的 topic。它们不在 ``kernel.events`` 里，
#: 因为它们是"父进程对这条流的观察"，不是 run 自己的事实。
SYNTHETIC_TOPICS = frozenset({"stream/closed", "stream/overrun"})

#: 其中**绝不可丢**的那一个。
#:
#: ★ ``stream/closed`` 的 ``is_activity()`` 是 **True**（它前缀是 ``stream/``，
#:   非 ``run/``），于是队列一满它就会被当成"可丢的活动事件"丢掉 ——
#:   而它恰恰是浏览器**唯一**的"run 结束了、可以收尾了"信号
#:   （``sse.ts`` 收到它才 ``es.close()`` + 回调 ``onClosed``）。
#:   丢掉它的后果：``markStreamClosed`` 永不触发，界面**永远显示"运行中"**、
#:   取消按钮永远亮着，而进程早就没了。
#:   唯一的兜底是心跳分支（``heartbeats > 8`` ≈ 2 分钟）后断开连接，
#:   让浏览器重连走"已结束 run 的重放分支" —— 也就是说：
#:   **恢复要两分钟，且表现为"卡住"而不是"报错"**。
#:   故把它明确划进"不可丢"（与 ``run/*`` 同级）。
CONTROL_TOPICS = frozenset({"stream/closed"})


__all__ = [n for n in list(globals()) if not n.startswith("__")]
