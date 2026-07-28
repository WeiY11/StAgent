"""Web-related settings sub-modules (search, fetch, browser, image search)."""

from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class ImageSearchSettings(BaseSettings):
    """Web image search (e.g. Google Custom Search API image mode)."""

    model_config = SettingsConfigDict(env_prefix="IMAGE_SEARCH_")

    provider: Literal["google_cse"] = "google_cse"
    api_key: str = ""
    cx: str = ""
    endpoint: str = "https://www.googleapis.com/customsearch/v1"
    max_results_default: int = 8


class WebBrowserSettings(BaseSettings):
    """Playwright defaults for web automation tools (locale / timezone / UA)."""

    model_config = SettingsConfigDict(env_prefix="WEB_BROWSER_")

    headless: bool = True
    locale: str = "en-US"
    timezone: str = "America/New_York"
    user_agent: str = ""
    ignore_https_errors: bool = False


class WebSearchSettings(BaseSettings):
    """General web search (HTTP APIs + lite HTML; respects HTTP(S)_PROXY via trust_env)."""

    model_config = SettingsConfigDict(env_prefix="WEB_SEARCH_")

    provider: Literal[
        "auto",
        "bing_playwright",
        "duckduckgo_lite",
        "searxng",
        "bing",
        "brave",
        "tavily",
        "exa",
        "firecrawl",
        "serper",
    ] = "tavily"
    searxng_base_url: str = ""
    bing_api_key: str = ""
    bing_endpoint: str = "https://api.bing.microsoft.com/v7.0/search"
    brave_api_key: str = ""
    tavily_api_key: str = ""
    exa_api_key: str = ""
    firecrawl_api_key: str = ""
    firecrawl_api_url: str = ""
    serper_api_key: str = ""
    user_agent: str = "LeAgent/1.0 web_search (+https://github.com)"
    timeout_sec: float = 25.0
    max_results_default: int = 8
    cache_ttl_minutes: float = 15.0
    allowlist_config_path: str = ""


class WebFetchSettings(BaseSettings):
    """Polite outbound HTTP defaults for single-machine installs (spacing, retries, robots).

    Designed to reduce accidental rate-limit / soft-block triggers—not to defeat protections.
    Also drives the lightweight ``web_fetch`` tool (cache + size-gated summarization).
    """

    model_config = SettingsConfigDict(env_prefix="WEB_FETCH_")

    enabled: bool = True
    min_interval_ms: float = 750.0
    jitter_ms_max: float = 400.0
    max_retries: int = 2
    retry_backoff_base_ms: float = 650.0
    check_robots_txt: bool = True
    pre_navigation_delay_ms: float = 350.0
    robots_cache_ttl_sec: float = 3600.0
    user_agent: str = ""
    cache_ttl_minutes: float = 15.0
    max_content_chars: int = 200_000
    summarize_threshold_chars: int = 5_000
    summarize_output_chars: int = 5_000
    refuse_over_chars: int = 2_000_000
    timeout_sec: float = 30.0
