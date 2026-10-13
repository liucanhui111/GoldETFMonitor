"""全 GUI 页面渲染测试（离屏）：三页填充 + 新手/专业模式切换 + 所有标签页。

运行：QT_QPA_PLATFORM=offscreen python tests/gui_pages_test.py
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))


def main() -> int:
    from PySide6.QtWidgets import QApplication

    from core.config import get_config
    from core.pipeline import run_full
    from ui.main_window import MainWindow

    app = QApplication(sys.argv)
    cfg = get_config(refresh=True)
    snap = run_full(cfg, progress=None, use_cache_first=True, with_macro=False,
                    save=False)
    win = MainWindow()
    errs: list[str] = []

    # 三页填充
    for name, page in (("portfolio", win.page_portfolio), ("etf", win.page_etf),
                       ("gold", win.page_gold), ("settings", win.page_settings)):
        try:
            page.set_snapshot(snap)
            print(f"[OK] {name} 页面填充")
        except Exception as exc:  # noqa: BLE001
            import traceback
            errs.append(f"{name}: {exc}")
            print(f"[FAIL] {name} 页面填充: {exc}")
            traceback.print_exc()

    # 黄金ETF页必须接入导航且可切换
    try:
        assert win.stack.count() == 5, f"页面数应为 5，实际 {win.stack.count()}"
        assert win.stack.widget(2) is win.page_gold, "第 3 页不是黄金ETF页"
        win.nav_group.button(2).click()
        assert win.stack.currentIndex() == 2, "点击导航后未切到黄金ETF页"
        assert win.page_gold.badge.text() not in ("", "暂无数据"), "黄金状态徽标未渲染"
        print(f"[OK] 黄金ETF页导航接入 · 状态={win.page_gold.badge.text()}")
        win.nav_group.button(0).click()
    except Exception as exc:  # noqa: BLE001
        errs.append(f"gold-nav: {exc}")
        print(f"[FAIL] 黄金ETF页导航: {exc}")

    # 黄金ETF页在『加载宏观/黄金数据』后必须渲染雷达与 8 指标表
    try:
        snap_m = run_full(cfg, progress=None, use_cache_first=True,
                          with_macro=True, save=False)
        win.page_gold.set_snapshot(snap_m)
        radar_rows = win.page_gold.tbl_radar.rowCount()
        ind8_rows = win.page_gold.tbl_ind8.rowCount()
        key_rows = win.page_gold.tbl_key.rowCount()
        assert radar_rows >= 1, "底部雷达表为空"
        assert ind8_rows >= 1, "8 指标表为空"
        assert key_rows >= 1, "关键价位表为空"
        print(f"[OK] 黄金ETF页宏观渲染 · 雷达 {radar_rows} 行 / "
              f"8指标 {ind8_rows} 行 / 关键价位 {key_rows} 行")
    except Exception as exc:  # noqa: BLE001
        errs.append(f"gold-macro: {exc}")
        print(f"[FAIL] 黄金ETF页宏观渲染: {exc}")

    # 新手 / 专业模式切换
    try:
        win.page_portfolio.set_simple(True)
        win.page_portfolio.set_simple(False)
        print("[OK] 新手/专业模式切换")
    except Exception as exc:  # noqa: BLE001
        errs.append(f"simple toggle: {exc}")
        print(f"[FAIL] 模式切换: {exc}")

    # 遍历所有标签页
    for pg_name, pg in (("portfolio", win.page_portfolio), ("etf", win.page_etf)):
        tabs = getattr(pg, "tabs", None)
        if tabs is None:
            continue
        for i in range(tabs.count()):
            tabs.setCurrentIndex(i)
            print(f"[OK] {pg_name} 标签[{i}] {tabs.tabText(i)}")

    # ETF 列表逐行
    for i in range(len(win.page_etf.rows)):
        win.page_etf.list.setCurrentRow(i)

    # ---- 第一屏结论卡：默认（超配）与买入场景都必须渲染出内容 ----
    for label, snapx in (("默认", snap),):
        try:
            hl = ((snapx["portfolio"]["plan"].get("home") or {}).get("headline")) or {}
            big = win.page_portfolio.v_big.text()
            assert big and big != "正在计算…", f"{label} 第一屏大字为空"
            assert hl.get("answer"), f"{label} headline.answer 为空"
            # 非买入态：第一屏必须是 0 元 / 不买
            if hl.get("key") not in ("GREEN_BUY", "GREEN_PROBE"):
                assert (hl.get("amount") or 0) == 0, f"{label} 非买入态却给了金额"
                assert not hl.get("name"), f"{label} 非买入态却给了标的"
            print(f"[OK] 第一屏结论卡（{label}）：{hl.get('answer')} · {big[:40]}")
        except Exception as exc:  # noqa: BLE001
            errs.append(f"verdict-{label}: {exc}")
            print(f"[FAIL] 第一屏结论卡（{label}）: {exc}")

    # ---- 数据陈旧横幅：新鲜→隐藏；陈旧→显示且文案含「非实时」 ----
    try:
        page = win.page_portfolio
        assert hasattr(page, "v_stale"), "首屏缺少数据陈旧横幅控件"
        # snap 内含锁对象，不能 deepcopy；用浅拷贝仅替换 freshness 键
        s_stale = dict(snap)
        s_stale["freshness"] = {
            "ok": False, "action": "DATA_STALE", "quote_basis": "最近交易日收盘",
            "rows": [{"key": "etf_close", "label": "A股ETF收盘",
                      "data_date": "2026-09-30", "age_days": 12,
                      "allowed_days": 7, "status": "STALE"}]}
        page.set_snapshot(s_stale)
        assert not page.v_stale.isHidden(), "陈旧数据时横幅未显示"
        assert "不构成实时" in page.v_stale.text(), "横幅未声明非实时"
        assert "2026-09-30" in page.v_stale.text(), "横幅未给出数据日期"
        s_fresh = dict(snap)
        s_fresh["freshness"] = {
            "ok": True, "action": "PASS", "quote_basis": "最近交易日收盘",
            "rows": [{"key": "etf_close", "label": "A股ETF收盘",
                      "data_date": "2026-10-06", "age_days": 1,
                      "allowed_days": 7, "status": "OK"}]}
        page.set_snapshot(s_fresh)
        assert page.v_stale.isHidden(), "新鲜数据时横幅误显示"
        page.set_snapshot(snap)
        print("[OK] 数据陈旧横幅 · 陈旧显示(含非实时/日期) · 新鲜隐藏")
    except Exception as exc:  # noqa: BLE001
        errs.append(f"stale-banner: {exc}")
        print(f"[FAIL] 数据陈旧横幅: {exc}")

    # ---- 多组合：设置页必须有组合下拉 + 切换/新建，且切换能换活动组合 ----
    try:
        from PySide6.QtWidgets import QMessageBox as _MB
        from core.config import get_config as _gc
        # 打桩模态对话框，否则离屏测试会阻塞
        _mi, _mw, _mq = _MB.information, _MB.warning, _MB.question
        _MB.information = staticmethod(lambda *a, **k: _MB.Ok)
        _MB.warning = staticmethod(lambda *a, **k: _MB.Ok)
        _MB.question = staticmethod(lambda *a, **k: _MB.No)
        sp = win.page_settings
        c = _gc(refresh=True)
        cur = str(c.get("portfolio.id"))
        cur_name = str(c.get("portfolio.name"))
        cur_pfs = dict(c.get("portfolios") or {})
        tmp = "__uitest_tmp__"
        try:
            assert hasattr(sp, "cmb_portfolio"), "设置页缺少组合下拉"
            assert hasattr(sp, "btn_switch") and hasattr(sp, "btn_new"), "缺少切换/新建按钮"
            assert sp.cmb_portfolio.count() >= 1, "组合下拉为空"
            c.create_portfolio(tmp, "UI测试临时组合", copy_from_current=False,
                               make_active=False)
            c.save()
            sp._load()
            ids = [sp.cmb_portfolio.itemData(i) for i in range(sp.cmb_portfolio.count())]
            assert tmp in ids, f"新建组合未出现在下拉：{ids}"
            sp.cmb_portfolio.setCurrentIndex(ids.index(tmp))
            sp._switch_portfolio()
            c2 = _gc(refresh=True)
            assert str(c2.get("portfolio.id")) == tmp, "切换后活动组合未变"
            print(f"[OK] 多组合 · 下拉 {sp.cmb_portfolio.count()} 项 · 切换/新建可用")
        finally:
            # 无论成败都完整还原：id/name/整个 portfolios 快照（避免测试污染真实配置）
            c3 = _gc(refresh=True)
            c3.set("portfolio.id", cur, autosave=False)
            c3.set("portfolio.name", cur_name, autosave=False)
            c3.set("portfolios", cur_pfs, autosave=True)
            sp._load()
    except Exception as exc:  # noqa: BLE001
        import traceback
        errs.append(f"portfolios: {exc}")
        print(f"[FAIL] 多组合: {exc}")
        traceback.print_exc()
    finally:
        try:
            _MB.information, _MB.warning, _MB.question = _mi, _mw, _mq
        except Exception:  # noqa: BLE001
            pass

    # ---- 执行台账：结论卡必须显示台账状态；超配场景「我已按此执行」可用 ----
    try:
        page = win.page_portfolio
        assert page.v_ledger.text(), "台账状态文本为空"
        hlx = ((snap["portfolio"]["plan"].get("home") or {}).get("headline")) or {}
        if hlx.get("key") == "BLUE_REBALANCE":
            pl = page._exec_payload
            assert pl and pl.get("sell_shares"), "再平衡场景未生成可执行动作"
            assert not page.v_exec_row.isHidden(), "再平衡场景按钮未显示"
            assert "卖出" in page.v_exec_btn.text() or "执行" in page.v_exec_btn.text()
            print(f"[OK] 执行台账 · 按钮可用 · {pl['name']} 卖出 {pl['sell_shares']} 份"
                  f" · {page.v_ledger.text()[:34]}")
        else:
            print(f"[OK] 执行台账状态 · {page.v_ledger.text()[:40]}")
    except Exception as exc:  # noqa: BLE001
        errs.append(f"ledger: {exc}")
        print(f"[FAIL] 执行台账: {exc}")

    # ---- 指令栏：控件就绪，且常见写法都能解析（只解析，绝不改配置）----
    try:
        from core import command_bar as cb
        page = win.page_portfolio
        assert hasattr(page, "cmd_edit") and hasattr(page, "cmd_btn"), "指令栏控件缺失"
        probes = {
            "卖黄金800@8.62": "REBALANCE",
            "买纳指500": "BUY",
            "现金20万": "SET_CASH",
            "总资产30万": "SET_TOTAL",
            "上限黄金6%": "SET_GOLD_CAP",
            "忽略": "SKIP",
        }
        bad = [f"{t}→{cb.parse(t, page.cfg).action}(应{want})"
               for t, want in probes.items() if cb.parse(t, page.cfg).action != want]
        assert not bad, "解析不符：" + "；".join(bad)
        c = cb.parse("卖黄金800@8.62", page.cfg)
        assert "卖出" in cb.preview(c, page.cfg), "预览未生成人话"
        assert cb.apply.__doc__, "apply 缺说明"
        print("[OK] 指令栏 · 控件就绪 · 6 种写法解析正确 · 预览正常")
    except Exception as exc:  # noqa: BLE001
        errs.append(f"command-bar: {exc}")
        print(f"[FAIL] 指令栏: {exc}")

    # ---- 实时持仓面板：改「持有份额」→ 立即重算判断（只改内存，不写盘）----
    try:
        from PySide6.QtCore import Qt as _Qt
        from core import live_holdings as _lh
        page = win.page_portfolio
        page.set_snapshot(snap)
        assert hasattr(page, "tbl_hold"), "实时持仓面板缺失"
        n = page.tbl_hold.rowCount()
        assert n > 0, "实时持仓表格为空"
        it_sh = page.tbl_hold.item(0, 2)
        assert it_sh is not None and bool(it_sh.flags() & _Qt.ItemIsEditable), \
            "份额列不可编辑"
        it_w = page.tbl_hold.item(0, 6)
        assert it_w is not None and not (it_w.flags() & _Qt.ItemIsEditable), \
            "权重列不应可编辑"
        # 价格口径：9 列（现价=日线收盘，参考现价=盘中，二者必须分开）
        assert page.tbl_hold.columnCount() == 9, \
            f"持仓面板应为 9 列，实际 {page.tbl_hold.columnCount()}"
        hdr = [page.tbl_hold.horizontalHeaderItem(j).text()
               for j in range(page.tbl_hold.columnCount())]
        assert any("日线" in h for h in hdr), f"缺少「现价(日线收盘)」列：{hdr}"
        assert any("参考" in h for h in hdr), f"缺少「参考现价」列：{hdr}"
        assert page.hold_price.text().strip(), "价格口径说明为空"
        assert "不参与判断" in page.hold_price.text(), \
            f"价格口径未声明参考价不参与判断：{page.hold_price.text()}"
        assert page.tbl_hold.item(0, 4) is not None, "参考现价列未填充"
        rows = _lh.table_rows(snap)
        gi = next((i for i, r in enumerate(rows) if r.get("asset_class") == "gold"), None)
        assert gi is not None, "未找到黄金行"
        before = page.hold_sum.text()
        old_txt = page.tbl_hold.item(gi, 2).text()
        page.tbl_hold.item(gi, 2).setText("0")
        assert page.hold_sum.text() != before, "改份额后摘要未刷新"
        assert page.hold_save.isEnabled(), "改份额后保存按钮未启用"
        page.tbl_hold.item(gi, 2).setText(old_txt)
        page._fill_holdings(snap)
        print(f"[OK] 实时持仓面板 · {n} 行 · 份额可编辑 · 改份额后判断即时刷新")
    except Exception as exc:  # noqa: BLE001
        errs.append(f"holdings: {exc}")
        print(f"[FAIL] 实时持仓面板: {exc}")

    # ---- 买入场景（黄金降到上限以内）也过一遍 UI，确保金额/对比卡片能填充 ----
    import copy
    cfg2 = get_config(refresh=True)
    assets = copy.deepcopy(cfg2.data.get("assets") or [])
    for a in assets:
        if a.get("asset_class") == "gold":
            a["shares"] = 500
    cfg2.data["assets"] = assets
    try:
        snap2 = run_full(cfg2, progress=None, use_cache_first=True,
                         with_macro=False, save=False)
        win.page_portfolio.set_snapshot(snap2)
        win.page_portfolio.set_simple(True)
        win.page_portfolio.set_simple(False)
        win.page_etf.set_snapshot(snap2)
        for i in range(len(win.page_etf.rows)):
            win.page_etf.list.setCurrentRow(i)
        home = (snap2["portfolio"]["plan"].get("home") or {})
        hl2 = home.get("headline") or {}
        if hl2.get("key") in ("GREEN_BUY", "GREEN_PROBE"):
            assert (hl2.get("amount") or 0) > 0 and hl2.get("name"), \
                "买入态第一屏没有金额/标的"
            assert "0 元" not in win.page_portfolio.v_big.text(), "买入态大字仍显示 0 元"
        print("[OK] 买入场景 UI 填充 · 今日动作="
              f"{(home.get('action') or {}).get('text')}"
              f" · 第一屏={hl2.get('big')}"
              f" · 一句话={home.get('one_line')}")
    except Exception as exc:  # noqa: BLE001
        import traceback
        errs.append(f"buy-scenario: {exc}")
        print(f"[FAIL] 买入场景 UI: {exc}")
        traceback.print_exc()

    print("GUI_PAGES_OK" if not errs else f"GUI_PAGES_FAIL: {errs}")
    win.close()
    app.quit()
    return 0 if not errs else 1


if __name__ == "__main__":
    raise SystemExit(main())
