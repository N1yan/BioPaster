"""Print readable thinking from the most recently modified BioPaster log.

Usage: python scripts/show_thinking.py
Only completed model responses are read; this is not a live stream viewer.
"""

import argparse
import json
from pathlib import Path
import sys

from inspect_session import display


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path,
        default=Path.home() / ".biopaster" / "sessions",
        help="Session directory (default: ~/.biopaster/sessions)",
    )
    args = parser.parse_args()
    try:
        logs = list(args.root.expanduser().glob("*/events.jsonl"))
        if not logs:
            print("No session logs found. Enable session.log_enabled first.", file=sys.stderr)
            return 1
        path = max(logs, key=lambda p: (p.stat().st_mtime_ns, str(p)))
        print(f"Log: {path}", file=sys.stderr)
        responses = shown = redacted = invalid = 0
        with path.open(encoding="utf-8") as log:
            for line in log:
                try:
                    event = json.loads(line)
                except ValueError:
                    invalid += 1
                    continue
                if not isinstance(event, dict) or event.get("type") != "model_response":
                    continue
                data = event.get("data")
                response = data.get("response") if isinstance(data, dict) else None
                if not isinstance(response, dict):
                    continue
                responses += 1
                parts = []
                reasoning = response.get("reasoning_content")
                if isinstance(reasoning, str) and reasoning.strip():
                    parts.append(reasoning)
                content = response.get("content")
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "redacted_thinking":
                            redacted += 1
                        thinking = block.get("thinking")
                        if block.get("type") == "thinking" and isinstance(thinking, str) and thinking.strip():
                            parts.append(thinking)
                if parts:
                    if shown:
                        print("\n" + "-" * 60 + "\n")
                    display("\n".join(parts), limit=0)
                    shown += 1
        if invalid:
            print(f"Skipped {invalid} invalid or incomplete JSON lines.", file=sys.stderr)
        if redacted:
            print("Redacted thinking is present but cannot be displayed.", file=sys.stderr)
        if not responses:
            print("No completed model_response yet. The request may still be running or may have ended without saving a response.", file=sys.stderr)
        elif not shown:
            print("No readable thinking was saved in the completed responses.", file=sys.stderr)
        return 0
    except (OSError, UnicodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
