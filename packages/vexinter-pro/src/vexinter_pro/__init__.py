"""Vexinter Pro — paid-tier plugin for the Vexinter Community Edition.

Importing this package registers the paid plan tiers (Professional,
Business) into ``vexinter.core.tiers`` and exposes the signed-license
client. The public core looks for it defensively: Community installs
work identically with or without it installed.

Usage::

    pip install vexinter-pro   # from your private package index
    import vexinter_pro         # registers paid tiers
"""

from vexinter_pro import licensing  # noqa: F401  (client module)
from vexinter_pro import tiers as _tiers  # noqa: F401  (import registers paid tiers)

__version__ = "0.1.0"


def client_from_env():
    """LicenseClient built from VEXINTER_LICENSE_* env vars, or None."""
    return licensing.client_from_env()
