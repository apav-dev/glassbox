from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="glassbox-mcp-server")
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="Print a Claude Desktop config snippet for this local repo")
    init_parser.add_argument(
        "--project-path",
        default=str(Path.cwd()),
        help="Path to the glassbox repo to use with uvx --from",
    )
    init_parser.add_argument(
        "--env-file",
        default=None,
        help="Path to a .env file for uvx to load before starting the server",
    )

    subparsers.add_parser("serve", help="Run the Glassbox stdio MCP server")

    args = parser.parse_args(argv)

    if args.command == "init":
        _print_init_config(
            project_path=Path(args.project_path).resolve(),
            env_file=Path(args.env_file).resolve() if args.env_file else None,
        )
        return

    from glassbox.api.mcp_stdio import main as serve_main

    serve_main()


def _print_init_config(project_path: Path, env_file: Path | None) -> None:
    resolved_env_file = env_file or (project_path / ".env")
    uvx_command = shutil.which("uvx") or "uvx"
    args = [
        "--directory",
        str(project_path),
        "--env-file",
        str(resolved_env_file),
        "--from",
        str(project_path),
        "glassbox-mcp-server",
        "serve",
    ]

    config = {
        "mcpServers": {
            "glassbox": {
                "type": "stdio",
                "command": uvx_command,
                "args": args,
            }
        }
    }
    print("Add this to Claude Desktop -> Settings -> Developer -> Edit Config:", file=sys.stderr)
    print(json.dumps(config, indent=2))
