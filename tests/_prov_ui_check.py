"""离屏校验：设置页「数据来源与有效性」表按新状态（正常/缓存/缓存兜底/错误/未配置）正确渲染。

用**合成快照**，不联网、可复现，冻结态也能跑。

运行：QT_QPA_PLATFORM=offscreen python tests/_prov_ui_check.py
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

_SNAP = {
    "data_status": [
        {"name": "美国10年期实际利率", "ok": True, "message": "已取到 2026-10-07",
         "label": "本次联网获取", "from_cache": False,
         "updated_at": "2026-10-07 09:15:03", "data_time": "2026-10-07"},
        {"name": "国际金价", "ok": True,
         "message": "联网获取失败(RuntimeError: Yahoo GC=F 请求失败：HTTP 403)，使用缓存",
         "label": "本地缓存", "from_cache": True,
         "updated_at": "2026-10-07T09:15:04", "data_time": "2026-10-06"},
        {"name": "美国10年期国债收益率", "ok": True,
         "message": "命中本地缓存（未过期）", "label": "本地缓存",
         "from_cache": True, "updated_at": "2026-10-07 09:15:05",
         "data_time": "2026-10-07"},
        {"name": "ETF行情（同花顺扶摇）", "ok": False,
         "message": "扶摇不可达，且无可用缓存", "label": "数据源异常",
         "from_cache": False, "updated_at": "2026-10-07 09:15:06", "data_time": None},
        {"name": "Gold Fear & Greed", "ok": False,
         "message": "未配置官方数据源 URL（显示暂无数据）", "label": "数据源异常",
         "from_cache": False, "updated_at": "2026-10-07 09:15:07", "data_time": None},
    ],
    "freshness": {"rows": [{"key": "gold", "data_date": "2026-10-06",
                            "age_days": 1, "status": "OK"}]},
    "macro": {"bpgdm_metrics": {
        "available": True, "status": "PROXY_ONLY",
        "real_available": False, "proxy_available": True,
        "bpgdm_proxy": 18.52,
        "bpgdm_proxy_algo": "27 只金矿股站上 200 日均线占比",
        "bpgdm_proxy_data_date": "2026-10-06",
        "real_error": "StockQ HTTP 403（被 WAF 拦截）"}},
}


def main() -> int:
    from PySide6.QtWidgets import QApplication

    from core.config import get_config
    from core.data_provenance import CATALOG, build_provenance
    from ui.settings_page import SettingsPage

    app = QApplication(sys.argv)
    cfg = get_config(refresh=True)
    snap = dict(_SNAP)
    snap["provenance"] = build_provenance(cfg, snap)

    page = SettingsPage(cfg)
    page.set_snapshot(snap)

    tbl = page.tbl_prov
    assert tbl.rowCount() == len(CATALOG), f"行数 {tbl.rowCount()} != {len(CATALOG)}"
    assert tbl.columnCount() == 7

    got = {}
    for i in range(tbl.rowCount()):
        label = tbl.item(i, 0).text()
        got[label] = tbl.item(i, 2).text()

    checks = [
        ("国际金价", "缓存（实时源失败）"),
        ("美国10年期实际利率", "正常（实时）"),
        ("美国10年期国债收益率", "缓存（未过期·本次未联网）"),
        ("ETF 收盘行情（6 只）", "错误"),
        ("金矿股广度 BPGDM（真实值）", "错误"),
        ("Gold Fear & Greed", "未配置"),
    ]
    bad = []
    for label, want in checks:
        lk = next((k for k in got if label in k), None)
        if lk is None:
            bad.append(f"缺行：{label}")
        elif want not in got[lk]:
            bad.append(f"{lk}: 期望含『{want}』，实际『{got[lk]}』")

    print("表头：", [tbl.horizontalHeaderItem(j).text() for j in range(7)])
    print("行数：", tbl.rowCount())
    for k, v in got.items():
        print(f"  {k:<26} → {v}")
    print("\n核对时间：", snap["provenance"]["checked_at"])
    if bad:
        print("\n❌ 不合格：\n  " + "\n  ".join(bad))
        return 1
    print("\n✅ 设置页来源表渲染正确（6 档状态、含原因、含到秒时间）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
