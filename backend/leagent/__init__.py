"""LeAgent — Intelligent Office Agent Platform for Enterprise Automation."""

import os

__version__ = "1.2.12"
# Desktop packagers pass their Electron release version at runtime so the
# health endpoint reports the same version as the installer. Keep the literal
# assignment above for the release-version consistency gate.
_desktop_build_version = os.getenv("LEAGENT_BUILD_VERSION", "").strip()
if _desktop_build_version:
    __version__ = _desktop_build_version
__author__ = "NovaMind AI"
__email__ = "engineering@novamind.ai"
