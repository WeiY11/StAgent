"""Inspect proxy env + probe eastmoney connectivity."""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

print("== proxy env ==")
for k, v in sorted(os.environ.items()):
    if "proxy" in k.lower():
        print(f"{k} = {v}")

import requests

hosts = [
    "https://push2his.eastmoney.com",
    "https://82.push2.eastmoney.com",
    "https://www.baidu.com",
]
print("\n== via proxy (env as-is) ==")
for h in hosts:
    try:
        r = requests.get(h, timeout=8)
        print(f"{h} -> {r.status_code}")
    except Exception as e:
        print(f"{h} -> FAIL {type(e).__name__}: {str(e)[:120]}")

print("\n== direct (proxies disabled) ==")
for h in hosts:
    try:
        r = requests.get(h, timeout=8, proxies={"http": None, "https": None})
        print(f"{h} -> {r.status_code}")
    except Exception as e:
        print(f"{h} -> FAIL {type(e).__name__}: {str(e)[:120]}")
