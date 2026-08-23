"""Structural interfaces for the memory subsystem.

.. deprecated::
    The canonical protocol home is :mod:`StAgent.sdk.protocols`. This module
    re-exports :class:`~StAgent.sdk.protocols.RecallProvider` for backwards
    compatibility; new code should import it from ``StAgent.sdk.protocols``
    (or the ``StAgent.sdk`` public surface).
"""

from __future__ import annotations

from StAgent.sdk.protocols import RecallProvider

__all__ = ["RecallProvider"]
