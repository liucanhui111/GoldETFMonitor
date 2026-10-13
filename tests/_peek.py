import os
root = r"F:\etf02\GoldETFMonitor"
for f, lab in [("gui_v2.log", "GUI"), ("self_v2.log", "SELF")]:
    p = os.path.join(root, f)
    if not os.path.exists(p):
        print(lab, "MISSING")
        continue
    t = open(p, encoding="utf-8", errors="replace").read()
    for l in t.splitlines():
        if ("GUI_PAGES" in l or "自检完成" in l or l.strip().startswith("FAIL")
                or "第一屏" in l or "报告最顶部" in l):
            print(lab, "|", l)
