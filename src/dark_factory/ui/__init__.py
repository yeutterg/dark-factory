"""Terminal operator interface.

The UI is an adapter: projections are read-only and all state changes travel
through the same operator command boundary as the CLI and API.
"""

from dark_factory.ui.projection import controller_projection

__all__ = ["controller_projection"]
