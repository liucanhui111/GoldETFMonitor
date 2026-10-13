import os
d = r"F:\etf02\GoldETFMonitor\dist\GoldETFMonitor-cli"
c = open(os.path.join(d, "exe_cli_v2.log"), encoding="gbk", errors="replace").read()
print("== EXE CLI 报告第一屏 ==")
i = c.find("****")
print(c[i - 60:i + 1100] if i > 0 else c[:1200])
print()
print("== 关键内容核对 ==")
for k in ("今天怎么做", "今天投入", "买哪只", "组合60日概率", "为什么（最多 3 条）",
          "下一次", "交易前后组合对比", "再平衡模拟", "（参照）"):
    print(("OK  " if k in c else "MISS"), k)
t = open(os.path.join(d, "exe_self_v2.log"), encoding="gbk", errors="replace").read()
for l in t.splitlines():
    if "自检完成" in l or l.strip().startswith("FAIL") or "第一屏" in l:
        print("SELF |", l)
print("报告文件:", [f for f in os.listdir(d) if f.endswith(".md")])
