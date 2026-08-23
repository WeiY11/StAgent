"""Unified runtime event + result types.

**Canonical home:** :mod:`StAgent.sdk.events`.

This module re-exports the SDK event types so existing ``from
StAgent.runtime.events import ...`` statements keep working during the
migration window.  New code should import directly from
:mod:`StAgent.sdk`.
"""

from StAgent.sdk.events import AgentEvent, AgentEventType, AgentResult  # noqa: F401

__all__ = ["AgentEvent", "AgentEventType", "AgentResult"]
