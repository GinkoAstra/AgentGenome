"""Unified control plane: Graph /2, pinned task/Agent definitions and execution bindings.

This isolated implementation is promoted only after independent host review.
No retired runtime or Graph /1 execution fallback is imported here.

Api and Controller are resolved lazily so client-only modules (discovery,
client_access, client_mcp) do not import the state owner merely by importing
the package. Existing `from node_runtime.control_plane import Api` keeps working.
"""
from .resources import Actor, ControlPlaneError

__all__ = ["Api", "Actor", "ControlPlaneError", "Controller"]


def __getattr__(name: str):
    if name == "Api":
        from .api import Api
        return Api
    if name == "Controller":
        from .controller import Controller
        return Controller
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
