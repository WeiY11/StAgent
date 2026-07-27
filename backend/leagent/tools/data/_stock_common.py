"""Shared helpers for stock tools — akshare access hardening.

Local proxies (Clash / system proxy in the Windows registry / corporate
MITM) intermittently drop connections to the EastMoney quote hosts.
``call_ak`` retries transient connection failures with a short backoff, and
as a last resort retries once with the proxy bypassed for finance data
hosts (proxy env vars stripped + ``NO_PROXY`` extended, both restored
afterwards — hosts that *depend* on the proxy keep working).
"""

from __future__ import annotations

import os
import threading
import time
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from collections.abc import Callable

logger = structlog.get_logger(__name__)

_PROXY_ENV_VARS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
)

# Finance data hosts akshare pulls from. ``requests`` matches NO_PROXY by
# suffix, which also defeats a Windows registry-configured system proxy.
_FINANCE_NO_PROXY = "eastmoney.com,sinajs.cn,sina.com.cn,xueqiu.com,163.com"

_TRANSIENT_ERROR_MARKERS = (
    "ProxyError",
    "Unable to connect to proxy",
    "RemoteDisconnected",
    "Connection aborted",
    "Connection reset",
    "Max retries exceeded",
    "Read timed out",
)

_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF_SEC = 1.5

# Serializes the strip-retry-restore window; other threads may still observe
# the mutated env briefly, which is an accepted tradeoff for simplicity.
_retry_lock = threading.Lock()


def _is_transient(exc: Exception) -> bool:
    text = repr(exc)
    return any(marker in text for marker in _TRANSIENT_ERROR_MARKERS)


def call_ak(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call an akshare function with transient-failure retries.

    Retries up to ``_RETRY_ATTEMPTS`` times with linear backoff on
    connection-style errors, then tries once more with proxy env vars
    stripped. Non-transient errors propagate unchanged; if every attempt
    fails the last error is raised.
    """
    last_exc: Exception | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — retry only transient failures
            if not _is_transient(exc):
                raise
            last_exc = exc
            logger.warning(
                "akshare_transient_error",
                fn=getattr(fn, "__name__", str(fn)),
                attempt=attempt + 1,
                error=str(exc),
            )
            if attempt < _RETRY_ATTEMPTS - 1:
                time.sleep(_RETRY_BACKOFF_SEC * (attempt + 1))

    # Final fallback: bypass any proxy (env or Windows registry) for this call.
    with _retry_lock:
        saved = {var: os.environ.pop(var) for var in _PROXY_ENV_VARS if var in os.environ}
        saved_no_proxy = {var: os.environ.get(var) for var in ("NO_PROXY", "no_proxy")}
        for var in ("NO_PROXY", "no_proxy"):
            existing = saved_no_proxy[var]
            os.environ[var] = f"{existing},{_FINANCE_NO_PROXY}" if existing else _FINANCE_NO_PROXY
        try:
            return fn(*args, **kwargs)
        except Exception as direct_exc:  # noqa: BLE001 — direct also failed; surface last proxy error
            logger.warning(
                "akshare_direct_fallback_failed",
                fn=getattr(fn, "__name__", str(fn)),
                error=str(direct_exc),
            )
            raise last_exc from None  # type: ignore[misc]
        finally:
            os.environ.update(saved)
            for var, value in saved_no_proxy.items():
                if value is None:
                    os.environ.pop(var, None)
                else:
                    os.environ[var] = value
