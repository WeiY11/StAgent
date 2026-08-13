"""Probe why akshare gets ProxyError while plain requests works."""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

import requests
import urllib.request

url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

print("getproxies():", urllib.request.getproxies())
print("environ proxies:", requests.utils.get_environ_proxies(url))

# plain GET to the exact API path akshare uses
params = {
    "fields1": "f1,f2,f3,f4,f5,f6",
    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
    "ut": "7eea3edcaed734bea9cbfc24409ed989",
    "klt": "101", "fqt": "1", "secid": "1.600519",
    "beg": "20260601", "end": "20260726",
}
try:
    r = requests.get(url, params=params, timeout=10)
    print("plain requests:", r.status_code, r.text[:80])
except Exception as e:
    print("plain requests FAIL:", type(e).__name__, str(e)[:150])

# now via akshare
import akshare as ak
try:
    df = ak.stock_zh_a_hist(symbol="600519", period="daily", start_date="20260601", end_date="20260726", adjust="qfq")
    print("akshare OK rows:", len(df))
except Exception as e:
    print("akshare FAIL:", type(e).__name__, str(e)[:150])

# with NO_PROXY set
os.environ["NO_PROXY"] = "eastmoney.com"
os.environ["no_proxy"] = "eastmoney.com"
try:
    df = ak.stock_zh_a_hist(symbol="600519", period="daily", start_date="20260601", end_date="20260726", adjust="qfq")
    print("akshare NO_PROXY OK rows:", len(df))
except Exception as e:
    print("akshare NO_PROXY FAIL:", type(e).__name__, str(e)[:150])
