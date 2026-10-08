import argparse
import os
import sys
from pathlib import Path

def start_repl(workspace_root: Path) -> int:
    from .config import ConfigError, get_config_path
    from .repl.core import BioPasterStreamingREPL
    try:
        repl = BioPasterStreamingREPL(workspace_root=workspace_root)
    except ConfigError as exc:
        print(f"Configuration: {get_config_path()}\n{exc}", file=sys.stderr)
        return 1
    repl.run()
    return 0

def main():
    """CLI main entry point."""
    from . import __version__

    parser = argparse.ArgumentParser(description="Start BioPaster in a local workspace.")
    parser.add_argument(
        "--version", "-v", "-V", action="version",
        version=f"BioPaster version {__version__}",
    )
    parser.add_argument(
        "--workspace", type=Path, default=None, metavar="PATH",
        help="Workspace directory (default: current directory).",
    )
    args = parser.parse_args()
    try:
        requested = args.workspace if args.workspace is not None else Path.cwd()
        workspace_root = requested.expanduser().resolve(strict=True)
        if not workspace_root.is_dir():
            raise NotADirectoryError(f"Not a directory: {workspace_root}")
        if not os.access(workspace_root, os.R_OK | os.X_OK):
            raise PermissionError(f"Directory is not accessible: {workspace_root}")
    except (OSError, RuntimeError, ValueError) as exc:
        parser.error(f"Invalid workspace: {exc}")
    return start_repl(workspace_root)

if __name__ == '__main__':
    sys.exit(main())
