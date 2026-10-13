"""配置加载与持久化。

所有可调参数集中在 config/config.json，程序运行期通过 Config 对象访问。
任何修改都写回磁盘，保证"长期可用"。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict


def project_root() -> Path:
    """返回可写项目根目录（兼容 PyInstaller 打包后的 exe）。

    优先级：
      1) 环境变量 GOLD_ETF_HOME（显式指定，最高优先级）
      2) 冻结态（PyInstaller）：exe 所在目录。
         —— 关键：此时 `__file__` 指向 `_internal/core/config.py`，
         `parent.parent` 会错误地算成 `_internal/`，导致报告写进包里。
         必须改用 `sys.executable`。
      3) 源码态：core/ 的上一级 = 项目根。
    """
    env = os.environ.get("GOLD_ETF_HOME")
    if env:
        return Path(env)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # 源码态：core/config.py -> core/ -> 项目根
    return Path(__file__).resolve().parent.parent


ROOT = project_root()
CONFIG_DIR = ROOT / "config"
CONFIG_PATH = CONFIG_DIR / "config.json"
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"

DEFAULT_CONFIG: Dict[str, Any] = {
    "meta": {"version": "1.0.0"},
    "app": {
        "refresh_minutes": 60,
        "timeout_seconds": 10,
        "max_retries": 3,
        "history_years": 5,
        "log_level": "INFO",
        "autostart": False,
        "autostart_times": ["09:00", "18:00"],
        # 主机熔断：同一主机连续连接级失败达到阈值后，在 block 分钟内快速失败，
        # 避免某个不可达的数据源（常见于海外源）把首次加载拖成十几分钟。
        "host_fail_threshold": 2,
        "host_block_minutes": 15,
        # 数据源探测与宏观加载的总时间预算（秒）：超预算就跳过剩余项，
        # 相关指标显示"暂无数据"，绝不阻塞主流程。
        "host_probe_timeout": 6,
        "macro_budget_seconds": 45,
        # 并发预取 ETF 行情的并发数（串行时冷启动要多花几十秒）
        "fetch_workers": 3,
        # 界面默认进入"新手模式"：只显示结论与关键数字，参数收进专业模式
        "simple_mode": True,
    },
    "data_sources": {
        "ths_api_key": "",
        "ths_base": "https://fuyao.aicubes.cn",
        "etf_primary": "ths",
        "etf_fallback": "tencent",
        "yahoo_enabled": True,
        "fred_enabled": True,
        "fear_greed_url": "",
        # 真实 BPGDM 地址（留空则尝试 StockQ 镜像 $BPGDM；仍不可用则退回
        # 【BPGDM代理值】，代理与真实值字段严格分离、界面标注"非真实BPGDM"）
        "bpgdm_url": "",
        "bpgdm_use_stockq_mirror": True,  # 未配置真实源时，尝试 StockQ 镜像 $BPGDM
        # 日线缓存有效期（分钟）。ETF 由 720(12h) 下调为 60(1h)：
        # 日线数据「当天更新一次足够」，但 12 小时会让盘中看到的收盘价过期太久。
        "cache_ttl_minutes": {"etf": 60, "macro": 1440, "sentiment": 1440},
        # 盘中参考报价（腾讯 qt.gtimg.cn）：只用于界面多显示一列「参考现价」，
        # **绝不参与任何判断**（判断一律用日线收盘价）。关掉则完全不联网取报价。
        "realtime_quote": True,
    },
    "portfolio": {
        # ---- 持仓账户隔离 ----
        # 每个组合一个 id；持仓/成本/现金/目标权重/历史数据都按组合隔离，
        # 绝不跨组合读取持仓数量（防止把其他账户的历史持仓混进来）。
        "id": "all_weather_01",
        "name": "当前全天候组合",
        # 总资产（含待投资现金）。填了就自动算现金 = 总资产 - ETF 市值。
        "total_assets": 0.0,
        # 也可以直接指定现金（当 total_assets 为 0 时生效）。
        "cash": None,
        "target_class": {"bond": 0.50, "stock": 0.45, "gold": 0.05},
        "gold_max": 0.05,
        "gold_min": 0.0,
        "stock_max": 0.50,
        "batch_count": 4,
        "rebalance_watch_pct": 3.0,
        "rebalance_priority_pct": 5.0,
    },
    "decision": {
        "note": "组合买入门槛：优先使用【样本外】60日盈利概率；样本外不可用时降级并降低买入信心。",
        "use_oos_first": True,
        "probe_position_hi_pct": 80,     # 1年相对价位高于此值 → 视为高位
        "probe_bias_hi_pct": 4.0,        # BIAS20 高于此值 → 视为不适合追涨
        "entry_score_trigger": 70,       # 单 ETF 触发买入所需的 Entry Score
        "first_batch_factor_probe": 0.5, # B级小仓试探时第一批金额折半
        "consistency_tolerance_pct": 0.5,  # 权重合计允许误差(pp)
    },
    # 【BUY PERMISSION GATE】可信等级决定“买入权限”，不是买入等级：
    # 只有可信等级 ≥ buy_permission_min_level，才允许输出"分批买入/小仓试探"；
    # 研究级/观察级一律不开放实际买入（模拟金额只作研究参考）。
    "trust": {
        "buy_permission_min_level": "辅助交易级",
    },
    "assets": [],
}


class Config:
    """线程安全的配置容器。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else CONFIG_PATH
        self._lock = threading.RLock()
        self.data: Dict[str, Any] = {}
        self.load()

    # ---------- 基础读写 ----------
    def load(self) -> Dict[str, Any]:
        with self._lock:
            if self.path.exists():
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                except Exception:
                    # 配置损坏：备份后回落默认值，绝不静默丢数据
                    bad = self.path.with_suffix(
                        f".bad-{datetime.now():%Y%m%d%H%M%S}.json"
                    )
                    try:
                        shutil.copy2(self.path, bad)
                    except Exception:
                        pass
                    raw = {}
            else:
                raw = {}
            self.data = _deep_merge(DEFAULT_CONFIG, raw)
            return self.data

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.path)

    def backup(self) -> Path:
        """导出配置备份，返回备份文件路径。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        dst = self.path.with_name(
            f"config.backup-{datetime.now():%Y%m%d-%H%M%S}.json"
        )
        shutil.copy2(self.path, dst)
        return dst

    # ---------- 访问器 ----------
    def get(self, dotted: str, default: Any = None) -> Any:
        with self._lock:
            cur: Any = self.data
            for part in dotted.split("."):
                if not isinstance(cur, dict) or part not in cur:
                    return default
                cur = cur[part]
            return cur

    def set(self, dotted: str, value: Any, autosave: bool = True) -> None:
        with self._lock:
            parts = dotted.split(".")
            cur = self.data
            for part in parts[:-1]:
                if not isinstance(cur.get(part), dict):
                    cur[part] = {}
                cur = cur[part]
            cur[parts[-1]] = value
        if autosave:
            self.save()

    # ---------- 业务快捷方式 ----------
    @property
    def portfolios(self) -> dict:
        """多组合容器：{"组合ID": {"name":..., "assets":[...], "portfolio":{...}}}"""
        data = self.get("portfolios", {}) or {}
        return data if isinstance(data, dict) else {}

    def list_portfolios(self) -> list[dict]:
        out = [{"id": self.portfolio_id, "name": self.portfolio_name,
                "assets": len(self.assets), "source": "主配置"}]
        for pid, blk in self.portfolios.items():
            if str(pid) == self.portfolio_id:
                continue
            blk = blk if isinstance(blk, dict) else {}
            out.append({"id": str(pid), "name": blk.get("name") or str(pid),
                        "assets": len(blk.get("assets") or []), "source": "组合列表"})
        return out

    def _asset_scope(self) -> str | None:
        """返回该组合在配置里的存储路径；None 表示用顶层 assets。"""
        pid = self.portfolio_id
        if pid and pid in self.portfolios:
            return f"portfolios.{pid}.assets"
        return None

    @property
    def assets(self) -> list[dict]:
        scope = self._asset_scope()
        if scope is not None:
            return list(self.get(scope, []) or [])
        return list(self.get("assets", []) or [])

    def set_assets(self, assets: list[dict]) -> None:
        """把持仓写回【当前组合】自己的位置，绝不写入其他组合。"""
        scope = self._asset_scope()
        self.set(scope or "assets", assets, autosave=False)

    def _archive_current(self) -> None:
        """在离开当前组合前，把它归档进 portfolios（只写不删）。

        当前组合的持仓存放在顶层 assets / portfolios.<id>.assets 两种位置。
        若直接切换/新建而不归档顶层组合，原组合会从列表里"消失"（数据仍在盘上，
        但 list_portfolios 找不到它）——这里先落一份到 portfolios，确保任何
        组合切换都不会丢数据。
        """
        pid = self.portfolio_id
        if not pid or pid in self.portfolios:
            return
        self.set(f"portfolios.{pid}",
                 {"name": self.portfolio_name,
                  "assets": json.loads(json.dumps(self.assets))},
                 autosave=False)

    def create_portfolio(self, pid: str, name: str = "", copy_from_current: bool = False,
                         make_active: bool = True) -> None:
        """新建一个独立组合（持仓/现金/目标权重彼此隔离）。"""
        pid = str(pid).strip()
        if not pid:
            raise ValueError("组合 ID 不能为空")
        self._archive_current()
        blk: Dict[str, Any] = {"name": name or pid, "assets": []}
        if copy_from_current:
            blk["assets"] = json.loads(json.dumps(self.assets))
            blk["portfolio"] = {
                "target_class": self.get("portfolio.target_class", {}),
                "gold_max": self.get("portfolio.gold_max", 0.05),
                "batch_count": self.get("portfolio.batch_count", 4),
            }
        self.set(f"portfolios.{pid}", blk, autosave=False)
        if make_active:
            self.set("portfolio.id", pid, autosave=False)
            self.set("portfolio.name", name or pid, autosave=False)

    def switch_portfolio(self, pid: str) -> None:
        """切换当前组合；只改活动 ID，不改动任何持仓数据。"""
        pid = str(pid).strip()
        if not pid:
            raise ValueError("组合 ID 不能为空")
        self._archive_current()
        if pid in self.portfolios:
            blk = self.portfolios[pid]
            if isinstance(blk, dict) and isinstance(blk.get("portfolio"), dict):
                for k, v in blk["portfolio"].items():
                    self.set(f"portfolio.{k}", v, autosave=False)
        self.set("portfolio.id", pid, autosave=False)
        name = (self.portfolios.get(pid) or {}).get("name")
        if name:
            self.set("portfolio.name", name, autosave=False)

    def enabled_assets(self) -> list[dict]:
        return [a for a in self.assets if a.get("enabled", True)]

    def asset_by_id(self, aid: str) -> dict | None:
        for a in self.assets:
            if str(a.get("id")) == str(aid):
                return a
        return None

    def ths_api_key(self) -> str:
        """优先环境变量，其次配置文件。"""
        return os.environ.get("THS_API_KEY") or str(
            self.get("data_sources.ths_api_key", "") or ""
        )

    def trade_cost(self) -> float:
        """交易成本，返回小数（0.002 表示 0.20%）。"""
        return float(self.get("cost.trade_cost_pct", 0.20)) / 100.0

    # ---- 账户隔离与资金口径 ----
    @property
    def portfolio_id(self) -> str:
        return str(self.get("portfolio.id", "all_weather_01") or "all_weather_01")

    @property
    def portfolio_name(self) -> str:
        return str(self.get("portfolio.name", "当前全天候组合") or "当前全天候组合")

    @property
    def declared_total_assets(self) -> float:
        """用户填写的账户总资产（含待投资现金）；0 表示未填写。"""
        try:
            return max(0.0, float(self.get("portfolio.total_assets", 0.0) or 0.0))
        except (TypeError, ValueError):
            return 0.0

    @property
    def declared_cash(self) -> float | None:
        """用户直接填写的现金；未填写返回 None。"""
        v = self.get("portfolio.cash", None)
        if v is None or v == "":
            return None
        try:
            return max(0.0, float(v))
        except (TypeError, ValueError):
            return None

    @property
    def gold_cap(self) -> float:
        return float(self.get("portfolio.gold_max", 0.05))


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


_CONFIG: Config | None = None


def get_config(refresh: bool = False) -> Config:
    global _CONFIG
    if _CONFIG is None or refresh:
        _CONFIG = Config()
    return _CONFIG
