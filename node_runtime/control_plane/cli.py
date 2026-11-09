"""Public CLI router: client installation stays independent of Host imports.

Existing serve/start/status/stop/teacher commands retain their Host behavior.
The client group delegates to the accepted shared installer; it owns no
configuration mutation logic and must not start a service.
"""
from __future__ import annotations

import sys

CLIENT_GROUP = "client"


def _root_parser():
    from . import client_install, host

    return host._parser(extra=client_install.add_public_group)


def main(argv: list[str] | None = None) -> int:
    """Route client arguments before importing Host; preserve exit codes."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == [CLIENT_GROUP]:
        from . import client_install

        return client_install.main(arguments[1:], prog="agentgenome client")
    if arguments[:1] in (["-h"], ["--help"]):
        _root_parser().print_help()
        return 0
    from . import host

    return host.main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
