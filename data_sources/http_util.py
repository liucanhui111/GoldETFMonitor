"""HTTP 工具：超时 / 重试 / 缓存回落 / 状态记录。

原则（对应需求第六十三、六十四条）：
* 超时 10 秒，失败最多重试 3 次；
* 全部失败后使用最近一次有效缓存，并明确标记"当前使用缓存数据"；
* 绝不生成任何假数据，取不到就是取不到。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

try:  # 优先 requests
    import requests  # type: ignore
    _HAS_REQUESTS = True
except Exception:  # pragma: no cover
    _HAS_REQUESTS = False

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# --------------------------------------------------------------------------
# 主机熔断（circuit breaker）
# --------------------------------------------------------------------------
# 背景：某些网络环境下海外数据源（如 Yahoo）整体不可达，而 `timeout` 参数并不覆盖
# DNS 解析，实测每次请求要等 16 秒以上。若对同一主机上十几个标的一一"重试 3 次"，
# 首次加载会被拖到十几分钟，界面看起来就像"一直在刷新"。
# 因此：同一主机连续连接级失败达到阈值后，在 block 秒内直接快速失败，
# 由上层把对应数据源标注为"数据源异常 / 暂无数据"——绝不伪造数据。
HOST_FAIL_THRESHOLD = 2
HOST_BLOCK_SECONDS = 900.0

_HOST_LOCK = threading.RLock()
_HOST_FAILS: dict[str, int] = {}
_HOST_BLOCK_UNTIL: dict[str, float] = {}

_ERRNO_NET = {101, 110, 111, 113, 10013, 10050, 10051, 10060, 10061, 10064, 10065}
# 明确的"被拦截"状态码：网络到该主机是通的，但对方拒绝服务
# （典型：WAF 按 IP / UA 与 TLS 指纹一致性拦人）。既用于熔断判断，
# 也用于"换取数通道再试一次"的触发条件。
_BLOCK_CODES = (401, 403, 429, 451)
_CONN_ERROR_NAMES = {
    "ConnectionError", "Timeout", "ConnectTimeout", "ReadTimeout", "ConnectionRefusedError",
    "ConnectionResetError", "RemoteDisconnected", "gaierror", "SSLError", "timeout", "URLError",
}


def configure_breakers(cfg) -> None:
    """从配置读取熔断参数（app.host_fail_threshold / app.host_block_minutes）。

    同时读取 `app.curl_fallback`（默认 true）：默认取数通道被 TLS 指纹/WAF 拦截时，
    是否允许改用系统 curl 兜底。
    """
    global HOST_FAIL_THRESHOLD, HOST_BLOCK_SECONDS, CURL_FALLBACK, UA_FALLBACK
    try:
        HOST_FAIL_THRESHOLD = max(1, int(cfg.get("app.host_fail_threshold", 2)))
        HOST_BLOCK_SECONDS = max(30.0, float(cfg.get("app.host_block_minutes", 15)) * 60.0)
    except Exception:  # noqa: BLE001
        pass
    try:
        CURL_FALLBACK = bool(cfg.get("app.curl_fallback", True))
    except Exception:  # noqa: BLE001
        pass
    try:
        UA_FALLBACK = bool(cfg.get("app.ua_fallback", True))
    except Exception:  # noqa: BLE001
        pass


def _host_of(url: str) -> str:
    try:
        from urllib.parse import urlparse

        return (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def host_block_reason(url: str) -> str | None:
    """若主机处于熔断期，返回说明文字，否则 None。"""
    h = _host_of(url)
    if not h:
        return None
    with _HOST_LOCK:
        until = _HOST_BLOCK_UNTIL.get(h, 0.0)
    left = until - time.time()
    if left > 0:
        return f"主机 {h} 暂不可达（已熔断跳过，约 {int(left)} 秒后恢复尝试）"
    return None


def is_connection_error(exc: BaseException) -> bool:
    """连接级失败（DNS / 超时 / 拒绝 / 不可达）——说明该主机整体打不通。"""
    if isinstance(exc, (socket.timeout, TimeoutError, socket.gaierror,
                        ConnectionError, ssl.SSLError)):
        return True
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        return is_connection_error(reason) if isinstance(reason, BaseException) else True
    if type(exc).__name__ in _CONN_ERROR_NAMES:
        return True
    if isinstance(exc, OSError) and exc.errno in _ERRNO_NET:
        return True
    return False


def note_host_failure(url: str, threshold: int | None = None,
                      block_seconds: float | None = None) -> bool:
    """记录一次连接级失败；返回 True 表示该主机刚刚被熔断。"""
    h = _host_of(url)
    if not h:
        return False
    thr = HOST_FAIL_THRESHOLD if threshold is None else int(threshold)
    secs = HOST_BLOCK_SECONDS if block_seconds is None else float(block_seconds)
    with _HOST_LOCK:
        n = _HOST_FAILS.get(h, 0) + 1
        _HOST_FAILS[h] = n
        if n >= thr:
            _HOST_BLOCK_UNTIL[h] = time.time() + secs
            return True
    return False


def block_host(url: str, seconds: float | None = None) -> None:
    h = _host_of(url)
    if not h:
        return
    secs = HOST_BLOCK_SECONDS if seconds is None else float(seconds)
    with _HOST_LOCK:
        _HOST_FAILS[h] = max(_HOST_FAILS.get(h, 0), HOST_FAIL_THRESHOLD)
        _HOST_BLOCK_UNTIL[h] = time.time() + secs


def reset_host_breakers() -> None:
    with _HOST_LOCK:
        _HOST_FAILS.clear()
        _HOST_BLOCK_UNTIL.clear()


def _call_with_deadline(fn: Callable[[], Any], seconds: float) -> Any:
    """在守护线程里执行 fn，超过 seconds 未返回就放弃本次尝试。

    `socket` 的 timeout 不覆盖 DNS 解析，实测在受限网络下 `timeout=8` 会实际等待 16 秒。
    这里用墙钟硬上限兜底，保证单次尝试不会无限拖延；被放弃的线程是 daemon，
    不会阻止程序退出。"""
    box: dict[str, Any] = {}
    done = threading.Event()

    def _run() -> None:
        try:
            box["v"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["e"] = exc
        finally:
            done.set()

    t = threading.Thread(target=_run, daemon=True, name="http-attempt")
    t.start()
    if not done.wait(max(1.0, float(seconds))):
        raise TimeoutError(f"请求硬性超时（>{seconds:.0f}s，已放弃本次尝试）")
    if "e" in box:
        raise box["e"]
    return box.get("v")


class HttpStatusError(RuntimeError):
    """服务器返回了非 200 状态码（主机是通的，只是这次请求不被接受）。"""

    def __init__(self, code: int) -> None:
        super().__init__(f"HTTP {code}")
        self.code = code


def _do_get_py(url: str, hdrs: dict[str, str], timeout: float) -> tuple[int, bytes, str | None]:
    """执行一次 GET（Python 自带通道），返回 (状态码, 原始字节, 编码)。"""
    if _HAS_REQUESTS:
        r = requests.get(url, headers=hdrs, timeout=timeout)
        return r.status_code, r.content, r.encoding
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            enc = None
            try:
                enc = resp.headers.get_content_charset()
            except Exception:  # noqa: BLE001
                enc = None
            return resp.status, resp.read(), enc
    except urllib.error.HTTPError as e:
        return e.code, b"", None


# --------------------------------------------------------------------------
# 取数通道兜底（UA 与 TLS 指纹一致性 / 系统 curl）
# --------------------------------------------------------------------------
# 背景（实测，2026-10-08）：同一台机器、同一时刻访问 fred.stlouisfed.org：
#   * 带**浏览器 UA**（Chrome/…）+ Python 的 OpenSSL 栈 → 握手被直接 RST
#     （WinError 10054）或读超时；
#   * 带**诚实 UA**（python-requests/x、Python-urllib/x、curl/x）→ HTTP 200 / 2~3s；
#   * 系统 curl（Schannel，UA 与自己的指纹自洽）→ HTTP 200 / 0.6~2s。
# 即：对方 WAF 校验的是「你自称是谁」和「你的握手像谁」是否自洽。浏览器 UA 配
# 非浏览器指纹 = 典型爬虫特征 → 拉黑。这是**通道问题**，不是"源不可用"，更不该
# 悄悄改用缓存冒充实时数据。
# 因此取数按三层尝试，并把**实际生效的通道**记录下来供界面/报告如实标注：
#   ① 浏览器 UA（多数源需要）→ ② 诚实 UA（自洽）→ ③ 系统自带 curl
UA_BROWSER = UA
try:
    if _HAS_REQUESTS:
        UA_HONEST = f"python-requests/{requests.__version__}"
    else:  # pragma: no cover
        import platform
        UA_HONEST = f"Python-urllib/{platform.python_version()}"
except Exception:  # noqa: BLE001
    UA_HONEST = "Python-urllib/3"

CURL_FALLBACK = True            # 由 configure_breakers() 按 app.curl_fallback 覆盖
UA_FALLBACK = True              # 由 configure_breakers() 按 app.ua_fallback 覆盖

_CURL_PATH: str | None = None   # "" 表示探测过且没有
_CURL_ONLY: set[str] = set()    # 确认"必须走 curl"的主机
_HOST_CHANNEL: dict[str, str] = {}   # 主机 → 实际生效的通道（browser/honest/curl）
_CURL_LOCK = threading.RLock()
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_CURL_MARK = "__CURL_HTTP_CODE__:"

CHANNEL_TEXT = {
    "browser": "浏览器 UA（直连）",
    "honest": "诚实 UA（WAF 一致性校验）",
    "curl": "系统 curl 通道",
}


def _curl_exe() -> str | None:
    global _CURL_PATH
    with _CURL_LOCK:
        if _CURL_PATH is None:
            _CURL_PATH = shutil.which("curl") or ""
        return _CURL_PATH or None


def curl_channel_ready() -> bool:
    """curl 备用通道是否可用（未启用/系统无 curl 时返回 False）。"""
    return bool(CURL_FALLBACK and _curl_exe())


def curl_only_hosts() -> set[str]:
    """本进程内已确认"必须走 curl"的主机。"""
    with _CURL_LOCK:
        return set(_CURL_ONLY)


def host_channels() -> dict[str, str]:
    """主机 → 实际生效的取数通道（供界面/报告标注"数据是怎么取到的"）。"""
    with _CURL_LOCK:
        return dict(_HOST_CHANNEL)


def _mark_channel(host: str, channel: str) -> None:
    if not host:
        return
    with _CURL_LOCK:
        _HOST_CHANNEL[host] = channel
        if channel == "curl":
            _CURL_ONLY.add(host)


def reset_curl_channel() -> None:
    global _CURL_PATH
    with _CURL_LOCK:
        _CURL_ONLY.clear()
        _HOST_CHANNEL.clear()
        _CURL_PATH = None


def _with_ua(hdrs: dict[str, str], ua: str) -> dict[str, str]:
    out = dict(hdrs or {})
    out["User-Agent"] = ua
    return out


def _curl_get(url: str, hdrs: dict[str, str], timeout: float) -> tuple[int, bytes, str | None]:
    """用系统 curl 执行一次 GET。仅传输级失败才抛异常；4xx/5xx 如实返回状态码。"""
    exe = _curl_exe()
    if not exe:
        raise RuntimeError("系统未提供 curl（Win10 1803+ 自带），无法使用备用通道")
    lim = int(max(5, min(90, float(timeout) + 5)))
    # Windows 自带 curl 编译时未带 zlib，**不支持 `--compressed`**；显式 identity
    # 既避免压缩响应解不开，又不依赖任何可选特性。
    send = dict(hdrs or {})
    send["Accept-Encoding"] = "identity"
    cmd = [exe, "-s", "-S", "--max-time", str(lim),
           "-w", "\n" + _CURL_MARK + "%{http_code}", url]
    for k, v in send.items():
        cmd[-1:-1] = ["-H", f"{k}: {v}"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=lim + 8,
                           creationflags=_NO_WINDOW)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"curl 备用通道执行失败：{type(exc).__name__}: {exc}") from exc
    raw = p.stdout or b""
    marker = _CURL_MARK.encode()
    idx = raw.rfind(marker)
    if idx < 0:
        err = (p.stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(f"curl 备用通道无响应：{err[:150] or '未知错误'}")
    try:
        code = int(raw[idx + len(marker):].strip()[:3])
    except Exception:  # noqa: BLE001
        code = 0
    if code == 0:
        err = (p.stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(f"curl 备用通道失败：{err[:150] or '无 HTTP 状态'}")
    return code, raw[:idx], None


def _fetch_once(url: str, hdrs: dict[str, str], timeout: float,
                hard: float) -> tuple[int, bytes, str | None]:
    """一次取数，按「浏览器 UA → 诚实 UA → 系统 curl」三层尝试。

    任一层返回了 HTTP 状态码即视为成功（4xx/5xx 由调用方判断）；
    只有**连接级失败**才继续降级，并且把实际生效的通道登记下来。
    """
    host = _host_of(url)
    browser_hdrs = _with_ua(hdrs, UA_BROWSER)
    honest_hdrs = _with_ua(hdrs, UA_HONEST)

    with _CURL_LOCK:
        prefer = _HOST_CHANNEL.get(host or "")

    base = ["browser"]
    if UA_FALLBACK:
        base.append("honest")
    base.append("curl")
    # 该主机上次走通哪个通道，就先试哪个（省掉注定失败的那次握手）；
    # 仍然保留其余通道作为降级，避免偶发失败直接判死。
    if prefer in base:
        order = [prefer] + [c for c in base if c != prefer]
    else:
        order = base

    first_exc: BaseException | None = None
    fallback_hit: tuple[int, bytes, str | None, str] | None = None
    for ch in order:
        try:
            if ch == "curl":
                if not curl_channel_ready():
                    continue
                code, body, enc = _call_with_deadline(
                    lambda: _curl_get(url, honest_hdrs, timeout), hard)
            else:
                use = browser_hdrs if ch == "browser" else honest_hdrs
                code, body, enc = _call_with_deadline(
                    lambda u=use: _do_get_py(url, u, timeout), hard)
        except Exception as exc:  # noqa: BLE001
            if first_exc is None:
                first_exc = exc
            continue
        if 200 <= code < 300:
            _mark_channel(host, ch)
            return code, body, enc
        # 401/403/429/451：很可能是「自称浏览器、握手却不是浏览器」被判为机器人。
        # 换一个与自身指纹自洽的通道再试一次——若换过去给了 200，说明数据本来
        # 就能拿到（这时报 403 才是误报）；若仍被拒，说明是真封禁，如实返回。
        if code in _BLOCK_CODES:
            if fallback_hit is None:
                fallback_hit = (code, body, enc, ch)
            continue
        break   # 404/5xx 等换通道无意义
    if fallback_hit is not None:
        return fallback_hit[0], fallback_hit[1], fallback_hit[2]
    raise first_exc or RuntimeError("所有取数通道均失败")


def _decode(data: bytes, enc: str | None, fallback: str = "utf-8") -> str:
    seen: list[str] = []
    for e in (enc, fallback, "utf-8", "gbk", "latin-1"):
        if not e or e.lower() in seen:
            continue
        seen.append(e.lower())
        try:
            return data.decode(e)
        except Exception:  # noqa: BLE001
            continue
    return data.decode("utf-8", "ignore")


def ensure_host_available(url: str, hard_timeout: float = 6.0) -> tuple[bool, str]:
    """一次性快速探测主机可达性（用于海外源）。

    不可达时立刻熔断整台主机，后续同主机的请求会秒级快速失败，
    避免"每个标的各自超时重试"把首次加载拖成十几分钟。
    """
    h = _host_of(url) or url
    reason = host_block_reason(url)
    if reason:
        return False, reason
    # 明确的"被拦截"状态码：本地网络到该主机是通的，但对方拒绝服务
    # （典型：Yahoo 对某些地区返回 403）。这**不是**可达 —— 必须熔断，
    # 否则后面每个标的都会各自重试，把整个加载时间耗光。
    try:
        code, _, _ = _fetch_once(
            url, {"User-Agent": UA, "Accept": "*/*"}, hard_timeout, hard_timeout)
        if code in _BLOCK_CODES:
            block_host(url)
            return False, (f"{h} 被拦截（HTTP {code}）→ 已熔断，"
                           "本次跳过该主机全部请求（不再逐标的重试）")
        return True, f"{h} 可达（HTTP {code}）"
    except Exception as exc:  # noqa: BLE001
        if is_connection_error(exc):
            block_host(url)
            return False, f"{h} 不可达：{type(exc).__name__}: {str(exc)[:60]}"
        return True, f"{h} 可达（{type(exc).__name__}）"


# --------------------------------------------------------------------------
# 数据源状态
# --------------------------------------------------------------------------
@dataclass
class SourceStatus:
    """单个数据源的运行状态。"""

    name: str
    ok: bool = False
    from_cache: bool = False
    message: str = ""
    updated_at: datetime | None = None
    attempts: int = 0
    data_time: str | None = None  # 数据本身的时间（最新K线日期等）

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ok": self.ok,
            "from_cache": self.from_cache,
            "message": self.message,
            "updated_at": self.updated_at.strftime("%Y-%m-%d %H:%M:%S")
            if self.updated_at
            else None,
            "attempts": self.attempts,
            "data_time": self.data_time,
            "label": self.label(),
        }

    def label(self) -> str:
        if not self.ok:
            return "数据源异常"
        # 注意：这里的口径是"本次数据获取方式"（联网获取/本地缓存），
        # 不是行情价格口径——价格一律以【最新交易日/最新收盘价】表述，
        # 只有真正的实时行情快照才单独标"实时价 + 实时更新时间"。
        return "本地缓存" if self.from_cache else "本次联网获取"


class StatusRegistry:
    """全局数据源状态登记表（线程安全）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._items: dict[str, SourceStatus] = {}

    def report(
        self,
        name: str,
        ok: bool,
        message: str = "",
        from_cache: bool = False,
        attempts: int = 1,
        data_time: str | None = None,
    ) -> SourceStatus:
        with self._lock:
            st = SourceStatus(
                name=name,
                ok=ok,
                from_cache=from_cache,
                message=message,
                updated_at=datetime.now() if ok else None,
                attempts=attempts,
                data_time=data_time,
            )
            self._items[name] = st
            return st

    def get(self, name: str) -> SourceStatus | None:
        return self._items.get(name)

    def all(self) -> list[SourceStatus]:
        with self._lock:
            return list(self._items.values())

    def any_abnormal(self) -> bool:
        return any(not s.ok for s in self.all())

    def summary_text(self) -> str:
        parts = []
        for s in self.all():
            if s.ok:
                parts.append(f"{s.name}:{s.label()}")
            else:
                parts.append(f"{s.name}:异常")
        return " | ".join(parts)


STATUS = StatusRegistry()


# --------------------------------------------------------------------------
# 缓存
# --------------------------------------------------------------------------
def _default_cache_dir() -> Path:
    # 统一走 core.config.project_root()：它已处理 PyInstaller 冻结态
    # （__file__ 位于 _internal/，不能再用 parent.parent），避免缓存写进包内。
    try:
        from core.config import project_root

        root = project_root()
    except Exception:  # noqa: BLE001
        root = Path(os.environ.get("GOLD_ETF_HOME")
                    or Path(__file__).resolve().parent.parent)
    return root / "data" / "cache"

class DiskCache:
    """按 key 缓存 JSON，用于网络失败时回落。"""

    def __init__(self, cache_dir: Path) -> None:
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, key: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)
        return self.dir / f"{safe}.json"

    def put(self, key: str, payload: Any, data_time: str | None = None) -> None:
        with self._lock:
            body = {
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
                "data_time": data_time,
                "payload": payload,
            }
            tmp = self._path(key).with_suffix(".tmp")
            tmp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._path(key))

    def get(self, key: str) -> tuple[Any, datetime | None, str | None] | None:
        p = self._path(key)
        if not p.exists():
            return None
        try:
            body = json.loads(p.read_text(encoding="utf-8"))
            ts = body.get("fetched_at")
            dt = datetime.fromisoformat(ts) if ts else None
            return body.get("payload"), dt, body.get("data_time")
        except Exception:
            return None

    def age_minutes(self, key: str) -> float | None:
        got = self.get(key)
        if not got or not got[1]:
            return None
        return (datetime.now() - got[1]).total_seconds() / 60.0

    def list_items(self) -> list[dict]:
        out = []
        for p in sorted(self.dir.glob("*.json")):
            try:
                body = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                body = {}
            out.append(
                {
                    "key": p.stem,
                    "file": p.name,
                    "size": p.stat().st_size,
                    "fetched_at": body.get("fetched_at"),
                    "data_time": body.get("data_time"),
                }
            )
        return out

    def clear(self) -> int:
        n = 0
        for p in self.dir.glob("*.json"):
            try:
                p.unlink()
                n += 1
            except Exception:
                pass
        return n


# --------------------------------------------------------------------------
# 带重试的请求
# --------------------------------------------------------------------------
@dataclass
class FetchResult:
    ok: bool
    value: Any = None
    error: str = ""
    from_cache: bool = False
    attempts: int = 0
    data_time: str | None = None
    fetched_at: datetime | None = None
    cache_age_minutes: float | None = None


def http_get_json(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: int = 10,
    retries: int = 3,
    backoff: float = 1.2,
) -> tuple[Any, str]:
    """GET 并解析 JSON。返回 (payload, error)。

    行为要点：
    * 每次尝试都有**墙钟硬上限**（timeout + 1.5s），防止 DNS 卡死把超时拖长；
    * 连接级失败累计到阈值后熔断该主机，后续请求秒级快速失败；
    * 4xx 属于"请求本身不被接受"，直接返回不重试；5xx 才重试。
    """
    hdrs = {"User-Agent": UA, "Accept": "application/json,text/plain,*/*"}
    if headers:
        hdrs.update(headers)
    reason = host_block_reason(url)
    if reason:
        return None, reason
    hard = max(3.0, float(timeout)) + 1.5
    last_err = ""
    for attempt in range(1, retries + 1):
        try:
            code, body, enc = _fetch_once(url, hdrs, timeout, hard)
            if code != 200:
                raise HttpStatusError(code)
            return json.loads(_decode(body, enc)), ""
        except HttpStatusError as exc:
            if 400 <= exc.code < 500:
                return None, str(exc)
            last_err = str(exc)
        except Exception as exc:  # noqa: BLE001
            last_err = f"{type(exc).__name__}: {exc}"
            if is_connection_error(exc) and note_host_failure(url):
                last_err += "（该主机已熔断，其余请求将快速跳过）"
                break
        if attempt < retries:
            time.sleep(backoff * attempt)
    return None, last_err or "unknown error"


def http_get_text(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: int = 10,
    retries: int = 3,
    encoding: str = "utf-8",
    backoff: float = 1.2,
) -> tuple[str | None, str]:
    hdrs = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        hdrs.update(headers)
    reason = host_block_reason(url)
    if reason:
        return None, reason
    hard = max(3.0, float(timeout)) + 1.5
    last_err = ""
    for attempt in range(1, retries + 1):
        try:
            code, body, enc = _fetch_once(url, hdrs, timeout, hard)
            if code != 200:
                raise HttpStatusError(code)
            return _decode(body, enc, encoding), ""
        except HttpStatusError as exc:
            if 400 <= exc.code < 500:
                return None, str(exc)
            last_err = str(exc)
        except Exception as exc:  # noqa: BLE001
            last_err = f"{type(exc).__name__}: {exc}"
            if is_connection_error(exc) and note_host_failure(url):
                last_err += "（该主机已熔断，其余请求将快速跳过）"
                break
        if attempt < retries:
            time.sleep(backoff * attempt)
    return None, last_err or "unknown error"


def fetch_with_cache(
    name: str,
    key: str,
    loader: Callable[[], Any],
    cache: DiskCache,
    ttl_minutes: float | None = None,
    use_cache_first: bool = False,
    status_registry: StatusRegistry | None = None,
) -> FetchResult:
    """先查缓存（可选）→ 调 loader → 失败回落缓存。

    loader 抛异常或返回 None 都视为失败。绝不返回伪造数据。
    """
    reg = status_registry or STATUS
    cached = cache.get(key)

    if use_cache_first and cached and ttl_minutes is not None:
        age = cache.age_minutes(key)
        if age is not None and age <= ttl_minutes:
            reg.report(name, True, "命中本地缓存（未过期）", True, 0, cached[2])
            return FetchResult(True, cached[0], "", True, 0, cached[2], cached[1], age)

    attempts = 0
    last_error = ""
    for _ in range(1):
        attempts += 1
        try:
            value = loader()
        except Exception as exc:  # noqa: BLE001
            value = None
            last_error = f"{type(exc).__name__}: {exc}"
        if value:
            data_time = _extract_data_time(value)
            cache.put(key, value, data_time)
            reg.report(name, True, "获取成功", False, attempts, data_time)
            return FetchResult(True, value, "", False, attempts, data_time, datetime.now(), 0.0)

    if cached:
        age = cache.age_minutes(key)
        reg.report(
            name,
            True,
            f"联网获取失败({last_error})，使用缓存",
            True,
            attempts,
            cached[2],
        )
        return FetchResult(True, cached[0], last_error, True, attempts, cached[2], cached[1], age)

    reg.report(name, False, f"获取失败：{last_error}", False, attempts, None)
    return FetchResult(False, None, last_error, False, attempts, None, None, None)


def _extract_data_time(value: Any) -> str | None:
    """尽力从返回结构中提取数据本身的时间，用于展示"数据时间"。"""
    try:
        if isinstance(value, dict):
            for k in ("data_time", "date", "last_date", "asof"):
                if isinstance(value.get(k), str):
                    return value[k]
            if "dates" in value and value["dates"]:
                return str(value["dates"][-1])
    except Exception:
        pass
    return None


# 全局磁盘缓存（data/cache/）
DATA_CACHE = DiskCache(_default_cache_dir())

