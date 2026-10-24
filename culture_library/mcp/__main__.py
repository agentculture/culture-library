"""``python -m culture_library.mcp --transport stdio|streamable-http``."""

from __future__ import annotations

import argparse
import sys

from culture_library.mcp import server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m culture_library.mcp", description=__doc__)
    p.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    p.add_argument("--bind", default=server.DEFAULT_BIND, help="streamable-http bind address")
    p.add_argument("--port", type=int, default=server.DEFAULT_PORT)
    p.add_argument("--allow-any-interface", action="store_true", help="permit a wildcard bind")
    args = p.parse_args(argv)
    try:
        if args.transport == "stdio":
            server.run_stdio()
        else:
            server.run_http(args.bind, args.port, allow_any_interface=args.allow_any_interface)
    except (server.ServerExtraMissing, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
