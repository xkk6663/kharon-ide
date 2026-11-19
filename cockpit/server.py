"""cockpit.server —— L7 驾驶舱后端：零依赖 HTTP + SSE。

设计依据：``docs/技术方案_闭环驾驶舱.md`` §5.5/§5.6/§17、``docs/ICD_cockpit_events.md``。

**为什么必须零第三方依赖**（§5.6 / N3）：
"clone 下来只装 Python 也能跑"是 README 的卖点。驾驶舱不能把它吃掉，
所以 HTTP 用 ``http.server``、推送用 SSE、JSON 用 ``json``。
前端另有一份 ``dist/``（构建产物已入仓），**运行时**同样不需要 Node。

**第一硬约束（§5.1，K1）**：长任务绝不能在本进程里跑。
``builder._run()`` 是 ``subprocess.run(capture_output=True)``，塞进服务端线程会把
HTTP 服务拖死、SSE 推不出去（``flash.debug`` 起 openocd 时还要阻塞 20s 等端口）。
故：**spawn 一个 ``elab run --emit-events`` 子进程，本进程只做"跟读只追加文件 + 转发 SSE + 取消"**。
"""

from __future__ import annotations

import json
import base64
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
for _p in (str(_ROOT), str(_ROOT / "services")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cockpit.core import *  # noqa: F401,F403
from cockpit.run.manager import *  # noqa: F401,F403
from cockpit.platform.summary import *  # noqa: F401,F403
from cockpit.platform.modes import *  # noqa: F401,F403
from cockpit.platform.state import *  # noqa: F401,F403
from cockpit.platform.jobs import *  # noqa: F401,F403
from cockpit.debug.helpers import *  # noqa: F401,F403
from cockpit.debug.manager import *  # noqa: F401,F403
from cockpit.api import router  # noqa: E402

# ══════════════════════════════════════════════════════════════════
#  HTTP
# ══════════════════════════════════════════════════════════════════
class CockpitHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"elab-cockpit/{COCKPIT_VERSION}"

    # 由 make_server 注入
    cfg: Config
    mgr: RunManager
    dbg: DebugManager
    pkg_jobs: PkgJobManager
    dist: Path
    sse_sem: threading.Semaphore

    # ── 小工具 ──────────────────────────────────────────────────
    #: 已经写出过响应头。★ 必须有它：SSE 是"边写边推"的，一旦中途出错
    #: （客户端断开最常见），再去 send_response 会写第二份响应头，
    #: 抛出的次生异常会盖掉真正的根因（实测踩过：WinError 10053 被
    #: 一层 `end_headers()` 的 ConnectionAbortedError 覆盖，日志里看不出真正原因）。
    _sent_headers = False

    def handle_one_request(self):                      # noqa: N802
        """★ 每条请求都必须重置 ``_sent_headers``（keep-alive 下的命门）。

        一个 handler **实例**服务的是一整条**连接**，不是一条请求。HTTP/1.1
        默认 keep-alive，浏览器会在同一条连接上连发
        ``/`` → ``/assets/index.css`` → ``/assets/index.js``。

        ``_sent_headers`` 原来只在 ``_send()`` 里置 True、从不重置，于是**从第二条
        请求起** ``_send()`` 直接 return —— **一个字节都不写**。客户端既拿不到响应，
        也收不到错误，只能挂到超时。实测症状极具迷惑性：

        - `GET /` 200、`index.css` 200，唯独 `index.js` 永远 pending；
        - 页面 ``readyState`` 卡在 ``interactive``，``#root`` 永远 0 个子节点；
        - **控制台一个报错都没有**（因为根本没有 JS 执行过）；
        - 而 `curl`（每次新进程=新连接）与服务端日志**一切正常**。

        为什么测试没抓到：``_Client`` 与 curl 都是"一请求一连接"，从未复用连接。
        回归用例见 ``test_keep_alive_same_connection``。
        """
        self._sent_headers = False
        super().handle_one_request()

    def log_message(self, fmt, *args):                 # 静音默认 access log
        if self.server.verbose:                        # type: ignore[attr-defined]
            sys.stderr.write("[cockpit] " + (fmt % args) + "\n")

    def _send(self, code: int, body: bytes, ctype: str, *, extra: dict | None = None):
        if self._sent_headers:
            return
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self._sent_headers = True
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _err(self, code: int, msg: str):
        self._json({"error": msg}, code)

    _DEBUG_STATUS = {
        "debug-busy": 409, "debug-no-session": 409, "debug-target-running": 409,
        "debug-write-denied": 403, "debug-limit": 400, "debug-bad-request": 400,
        "debug-unavailable": 503, "debug-timeout": 504,
        "debug-rev-conflict": 409, "debug-svd-missing": 404,
        "debug-rtos-unavailable": 503, "debug-elf-missing": 409,
        "debug-gdb-missing": 503, "debug-openocd-dead": 500,
        "debug-port-hijacked": 409,
        # P3 多会话 / 源码页（执行计划 §6.1 / §5.2）
        "debug-target-busy": 409, "debug-session-limit": 409,
        "debug-session-required": 409, "debug-source-unavailable": 404,
    }

    def _debug_err(self, exc: dbgsess_mod.DebugError):
        self._json({"error": exc.message, **exc.as_dict()},
                   self._DEBUG_STATUS.get(exc.code, 500))

    def _fail(self, exc: Exception):
        """统一的失败出口。

        ``ConnectionError`` 家族（``BrokenPipeError`` / ``ConnectionResetError`` /
        ``ConnectionAbortedError``）**全部**是"客户端走了"，属于正常事件，
        不该记成服务端错误 —— 尤其是 ``--noproxy`` 的 curl + ``head`` 这种用法
        会在读够行数后直接关连接。只有真正的异常才回 500，且仅当响应头尚未发出。
        """
        if isinstance(exc, ConnectionError):
            return
        if self._sent_headers:
            sys.stderr.write(f"[cockpit] 响应已开始后出错（无法回错）：{exc!r}\n")
            return
        self._err(500, f"{type(exc).__name__}: {exc}")

    def _drain_body(self, n: int) -> None:
        """读掉 n 字节并丢弃（最多 ``MAX_DRAIN_BYTES``）。

        拒收大请求体时**必须先读掉**：客户端还在上传，服务端直接回错并关连接
        会让对端收到 RST，看到的是 ``ConnectionAbortedError`` 而不是那条
        "请求体太大"的说明 —— 用户永远不知道自己做错了什么。
        超过丢弃上限就不读了（这种量级已经不该出现在客户端代码里），直接关连接。
        """
        if n > MAX_DRAIN_BYTES:
            self.close_connection = True
            return
        left = n
        try:
            while left > 0:
                chunk = self.rfile.read(min(65536, left))
                if not chunk:
                    break
                left -= len(chunk)
        except OSError:
            self.close_connection = True

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        if n > MAX_BODY_BYTES:
            self._drain_body(n)
            self.close_connection = True
            raise ElabError(
                f"请求体 {n / 1048576:.1f} MB 超过上限 "
                f"{MAX_BODY_BYTES // 1048576} MB：大样本请用 capture 指向录制文件，"
                f"别把整段字节塞进请求体")
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            raise ElabError("请求体不是合法 JSON")

    # ── 路由 ────────────────────────────────────────────────────
    def do_GET(self):                                  # noqa: N802
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        try:
            if router.handle_get(self, path, qs):
                return
            if path.startswith("/api/"):
                return self._err(404, f"未知 API：{path}")
            return router.static_fallback(self, path)
        except dbgsess_mod.DebugError as exc:
            return self._debug_err(exc)
        except ElabError as exc:
            return self._err(400, str(exc))
        except Exception as exc:                       # noqa: BLE001
            return self._fail(exc)

    def do_HEAD(self):                                 # noqa: N802
        return self.do_GET()

    def do_POST(self):                                 # noqa: N802
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        try:
            if router.handle_post(self, u, qs):
                return
            return self._err(404, f"未知 API：{u.path}")
        except dbgsess_mod.DebugError as exc:
            return self._debug_err(exc)
        except ElabError as exc:
            return self._err(400, str(exc))
        except Exception as exc:                       # noqa: BLE001
            return self._fail(exc)

    def do_DELETE(self):                               # noqa: N802
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        try:
            if router.handle_delete(self, u, qs):
                return
            return self._err(404, f"未知 API：{u.path}")
        except dbgsess_mod.DebugError as exc:
            return self._debug_err(exc)
        except ElabError as exc:
            return self._err(400, str(exc))
        except Exception as exc:                       # noqa: BLE001
            return self._fail(exc)



class CockpitServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    verbose = False

    def handle_error(self, request, client_address):
        """客户端断开**不是**服务端错误，别打一堆吓人的栈。

        典型的无害场景：``http.client`` 用完后直接关连接，而服务端还在
        keep-alive 地等同一个连接上的下一个请求 → ``ConnectionAbortedError``。
        本地单用户工具里这几乎是常态，掩盖真实错误才是代价。
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, ConnectionError):
            return
        super().handle_error(request, client_address)


def make_server(cfg: Config, *, host: str = HOST, port: int = DEFAULT_PORT,
                dist: Path | None = None, verbose: bool = False,
                log=print) -> CockpitServer:
    # ★ 这里才是"进程启动"的权威时刻（main → make_server）：把当前 ELAB_HOST
    #   记成模式切换的回落基线。放在 import 期会让测试/插件复用同一进程时
    #   拿到别人设的值 —— 那正是"切回去怎么不是我启动时那份"的由来。
    global _LAUNCH_HOST_ENV
    _LAUNCH_HOST_ENV = os.environ.get("ELAB_HOST", "")
    mgr = RunManager(cfg, log=log)
    dbg = DebugManager(cfg, mgr, log=log)
    pkg_jobs = PkgJobManager()
    handler = type("BoundCockpitHandler", (CockpitHandler,), {})
    handler.cfg = cfg
    handler.mgr = mgr
    handler.dbg = dbg
    handler.pkg_jobs = pkg_jobs
    handler.dist = Path(dist) if dist else DIST_DIR
    handler.sse_sem = threading.Semaphore(MAX_SSE_CONNS)
    # 包操作完成后**重建 Config**（芯片/工程对运行中的驾驶舱立即生效）。
    # 与 /api/adapt 写盘后的重载同源：原子替换类属性，在途请求握旧引用跑完。
    def _reload_pack_cfg():
        new_cfg = Config(cfg.root)
        handler.cfg = new_cfg
        mgr.cfg = new_cfg
        dbg.cfg = new_cfg        # ③ 与 /api/mode 同一条纪律：三份引用一起换
    pkg_jobs.bind(_reload_pack_cfg)
    # ★ 开箱即用：全新环境（packs/installed 为空）时自动装内置官方包（st/stm32），
    #   让「已安装」页签首屏就有内容。已有任何已装包时不动（不覆盖用户卸载）。
    try:
        installed = pkg_mod.ensure_bootstrap(cfg, log=log)
        if installed:
            log(f"[cockpit] 已自动安装内置适配包："
                f"{'、'.join(r['name'] for r in installed)}")
            handler.cfg = Config(cfg.root)
            mgr.cfg = handler.cfg
            dbg.cfg = handler.cfg
    except Exception as exc:                       # noqa: BLE001
        log(f"[cockpit] 内置适配包自动安装失败：{exc}")
    srv = CockpitServer((host, port), handler)
    srv.verbose = verbose                                # type: ignore[attr-defined]
    srv.run_manager = mgr                                # type: ignore[attr-defined]
    return srv


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="cockpit.server",
                                 description="elab-Flow 闭环驾驶舱后端（零依赖 HTTP + SSE）")
    ap.add_argument("--root", help="覆盖 ELAB_ROOT")
    ap.add_argument("--host", default=HOST, help=f"监听地址（默认 {HOST}，只监听本地）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"端口（默认 {DEFAULT_PORT}）")
    ap.add_argument("--dist", help="覆盖前端产物目录（默认 cockpit/web/dist）")
    ap.add_argument("--reload", action="store_true", help="不使用 dist/，仅提供 API")
    ap.add_argument("--logfile", help="stdout/stderr 重定向到文件（pythonw 无窗口部署用；"
                                      "行缓冲，替代'最小化控制台'的日志能力）")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印访问日志")
    args = ap.parse_args(argv)

    if args.logfile:
        # C32 教训：重定向 stdout 在 GBK 代码页下会因 ✓ 字符崩服务 —— 显式 utf-8。
        _lf = open(args.logfile, "a", encoding="utf-8")
        sys.stdout = _lf
        sys.stderr = _lf
        try:
            sys.stdout.reconfigure(line_buffering=True)
        except Exception:
            pass

    cfg = Config(args.root)
    dist = None if args.reload else (Path(args.dist) if args.dist else None)
    srv = make_server(cfg, host=args.host, port=args.port, dist=dist,
                      verbose=args.verbose)
    ready = (Path(dist) if dist else DIST_DIR) / "index.html"
    # ★ 打印**实际绑定**的端口，而不是请求值：`--port 0` 时由 OS 分配，
    #   请求值恒为 0（打印 0 会让调用方无法据此连接 —— DSH 插件托管正是这种用法）。
    #   `--port 8333` 等显式端口下两者相同，行为不变。
    bound_port = srv.server_address[1]
    print(f"[cockpit] ELAB_ROOT = {cfg.root}")
    print(f"[cockpit] 监听 http://{args.host}:{bound_port}/")
    print(f"[cockpit] 前端产物 {'✓ ' + to_fwd(ready) if ready.exists() else '✗ 缺失（见页面提示）'}")
    print(f"[cockpit] 运行产物 {to_fwd(srv.run_manager.runs_dir)}")
    print("[cockpit] Ctrl-C 退出")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[cockpit] 退出中…")
    finally:
        srv.shutdown()
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
