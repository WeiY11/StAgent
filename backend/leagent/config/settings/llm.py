"""LLM settings sub-module."""

from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LLM_")

    embedding_endpoint: str = ""
    embedding_model: str = "bge-large-zh-v1.5"
    embedding_dim: int = 1024

    openai_api_key: str = ""
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-20250514"

    dashscope_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("LLM_DASHSCOPE_API_KEY", "DASHSCOPE_API_KEY"),
    )
    dashscope_base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        validation_alias=AliasChoices("LLM_DASHSCOPE_BASE_URL", "DASHSCOPE_BASE_URL"),
    )
    dashscope_enable_thinking: bool | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "LLM_DASHSCOPE_ENABLE_THINKING",
            "DASHSCOPE_ENABLE_THINKING",
        ),
    )
    dashscope_enable_search: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "LLM_DASHSCOPE_ENABLE_SEARCH",
            "DASHSCOPE_ENABLE_SEARCH",
        ),
    )

    deepseek_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("LLM_DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY"),
    )
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com",
        validation_alias=AliasChoices("LLM_DEEPSEEK_BASE_URL", "DEEPSEEK_BASE_URL"),
    )
    deepseek_thinking_type: Literal["enabled", "disabled"] | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "LLM_DEEPSEEK_THINKING_TYPE",
            "DEEPSEEK_THINKING_TYPE",
        ),
    )
    deepseek_reasoning_effort: Literal["high", "max"] | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "LLM_DEEPSEEK_REASONING_EFFORT",
            "DEEPSEEK_REASONING_EFFORT",
        ),
    )

    ollama_endpoint: str = ""
    ollama_model: str = "llama3.2"

    vllm_endpoint: str = Field(
        default="",
        validation_alias=AliasChoices("LLM_VLLM_ENDPOINT", "VLLM_ENDPOINT"),
    )
    vllm_model: str = Field(
        default="",
        validation_alias=AliasChoices("LLM_VLLM_MODEL", "VLLM_MODEL"),
    )
    vllm_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("LLM_VLLM_API_KEY", "VLLM_API_KEY"),
    )
    vllm_timeout: int = Field(
        default=120,
        validation_alias=AliasChoices("LLM_VLLM_TIMEOUT", "VLLM_TIMEOUT"),
    )
    vllm_enable_auto_tool_choice: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "LLM_VLLM_ENABLE_AUTO_TOOL_CHOICE",
            "VLLM_ENABLE_AUTO_TOOL_CHOICE",
        ),
    )

    local_only: bool = False
