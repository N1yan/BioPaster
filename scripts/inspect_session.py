"""Read BioPaster session logs without importing or running BioPaster.

Examples:
    python scripts/inspect_session.py
    python scripts/inspect_session.py SESSION_ID --thinking
    python scripts/inspect_session.py /path/to/events.jsonl --full --requests
    python scripts/inspect_session.py SESSION_ID --summary
    python scripts/inspect_session.py SESSION_ID --raw
"""

import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import re
import sys


ROOT = Path.home() / ".biopaster" / "sessions"


def load_events(path):
    events, warnings = [], []
    with path.open(encoding="utf-8") as file:
        for number, line in enumerate(file, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                    raise ValueError("expected an event object with a string type")
                events.append(event)
            except ValueError as exc:
                warnings.append(f"Skipped invalid line {number}: {exc}")
    return events, warnings


def data(event):
    value = event.get("data")
    return value if isinstance(value, dict) else {}


def display(value, limit=0):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    # Logs can contain terminal escape sequences from tools. Display as text.
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)
    if limit and len(text) > limit:
        text = text[:limit] + f"\n... ({len(text)} characters total; use --full to view all)"
    print(text)


def duration(start, end):
    try:
        return f"{(datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds():.2f}s"
    except (ValueError, TypeError):
        return "unknown"


def summary(events):
    requests, responses = {}, {}
    calls, finished = {}, set()
    run_ids = {e.get("run_id") for e in events}
    print("\n=== Summary ===")
    print(f"Events: {len(events)}; distinct run IDs: {len(run_ids - {None})}")
    for event in events:
        d = data(event)
        if event["type"] == "model_request":
            requests[d.get("request_id")] = event
        elif event["type"] == "model_response":
            responses[d.get("request_id")] = d.get("response", {})
        elif event["type"] == "agent_event":
            kind = d.get("kind")
            if kind == "tool_use":
                calls[d.get("tool_use_id")] = d.get("tool_name", "?")
            elif kind in {"tool_result", "tool_error"}:
                finished.add(d.get("tool_use_id"))
    print(f"Model requests: {len(requests)}; complete responses: {len(responses)}")
    print(f"Tool calls: {len(calls)}; counts by tool: {dict(Counter(calls.values()))}")
    usage = Counter()
    stops = Counter()
    for response in responses.values():
        stops[str(response.get("stop_reason"))] += 1
        for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            value = (response.get("usage") or {}).get(key)
            if isinstance(value, (int, float)):
                usage[key] += value
    print(f"Model stop reasons: {dict(stops)}")
    print(f"Usage (complete responses only; stream events not counted again): {dict(usage)}")
    failed = [data(e) for e in events if e["type"] == "agent_event"
              and (data(e).get("kind") == "tool_error"
                   or (data(e).get("kind") == "tool_result" and data(e).get("is_error")))]
    print(f"Tool failure events: {len(failed)}")
    for d in failed:
        print(f"  - {d.get('tool_name')} ({d.get('tool_use_id')})")
    print("\n=== Log checks (not an assessment of scientific validity) ===")
    for rid in requests.keys() - responses.keys():
        print(f"- Request {rid} has no complete response: it may have been interrupted, failed, or incompletely logged.")
    for tid in calls.keys() - finished:
        print(f"- Tool {calls[tid]} ({tid}) has no completion event.")
    for rid in sorted(run_ids, key=lambda x: str(x)):
        group = [e for e in events if e.get("run_id") == rid]
        if not any(e["type"] == "model_request" for e in group):
            continue
        types = {e["type"] for e in group}
        label = rid or "Events without a run_id"
        if "run_start" not in types:
            print(f"- {label}: missing run_start; the start of the user task cannot be reliably identified.")
        if "run_end" not in types:
            print(f"- {label}: missing run_end; normal completion cannot be assumed.")
    for response in responses.values():
        if response.get("stop_reason") == "max_tokens":
            print("- A response has stop_reason=max_tokens: generation reached the output limit.")
    print("- run_end=returned or result=success describes program state, not proof of task completion.")


def show_blocks(blocks, args):
    limit = 0 if args.full else args.max_chars
    if isinstance(blocks, str):
        display(blocks, limit)
        return
    for block in blocks or []:
        kind = block.get("type", "unknown")
        if kind == "thinking":
            if args.thinking:
                print("[thinking: content returned by the server]")
                display(block.get("thinking", ""), limit)
            else:
                print("[thinking hidden; use --thinking to view]")
        elif kind == "redacted_thinking":
            print("[redacted_thinking: unreadable; the log cannot recover its text]")
        elif kind == "text":
            display(block.get("text", ""), limit)
        elif kind == "tool_use":
            print(f"[Tool requested: {block.get('name')} / {block.get('id')}]")
            display(block.get("input", {}), limit)
        else:
            print(f"[{kind}]")
            display(block, limit)


def timeline(events, args):
    complete = {data(e).get("request_id") for e in events if e["type"] == "model_response"}
    pending, starts = {}, {}
    limit = 0 if args.full else args.max_chars
    print("\n=== Execution timeline ===")
    for event in events:
        kind, d = event["type"], data(event)
        rid = d.get("request_id")
        if kind == "model_stream":
            if rid not in complete:
                raw = d.get("event", {})
                blocks = pending.setdefault(rid, {})
                index = raw.get("index", 0)
                if raw.get("type") == "content_block_start":
                    blocks[index] = dict(raw.get("content_block", {}))
                elif raw.get("type") == "content_block_delta":
                    delta = raw.get("delta", {})
                    block = blocks.setdefault(index, {})
                    for field, block_type in (("text", "text"), ("thinking", "thinking"), ("partial_json", "tool_use")):
                        if field in delta:
                            block.setdefault("type", block_type)
                            block[field] = block.get(field, "") + delta[field]
            continue
        # agent_return repeats the final result already displayed by ResultEvent.
        if kind == "agent_return":
            continue
        print(f"\n[{event.get('time', '?')}] {kind}  run={event.get('run_id')}")
        if kind == "model_request":
            starts[rid] = event.get("time")
            request = d.get("request", {})
            print(f"request={rid} model={request.get('model')} max_tokens={request.get('max_tokens')}")
            if args.requests:
                display(request, limit)
        elif kind == "model_response":
            response = d.get("response", {})
            print(f"request={rid} stop_reason={response.get('stop_reason')} elapsed={duration(starts.get(rid), event.get('time'))}")
            show_blocks(response.get("content"), args)
        elif kind == "agent_event":
            if d.get("type") == "result":
                print(f"Result subtype: {d.get('subtype')}; turns: {d.get('num_turns')}")
                if d.get("is_error"):
                    display(d.get("errors", []), limit)
            else:
                print(f"{d.get('kind')} {d.get('tool_name')} id={d.get('tool_use_id')} is_error={d.get('is_error')}")
                for key in ("tool_input", "tool_output", "error"):
                    if d.get(key) is not None:
                        display(d[key], limit)
        else:
            display(event.get("data"), limit)
    for rid, blocks in pending.items():
        print(f"\n[Incomplete model response {rid}: showing received fragments only]")
        for index in sorted(blocks):
            block = blocks[index]
            show_blocks([block], args)
            if "partial_json" in block:
                print("[Unconfirmed partial tool arguments; this does not mean the tool executed]")
                display(block["partial_json"], limit)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("session", nargs="?", help="Session ID, directory, or JSONL file; defaults to the most recently modified log")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--run", help="Show only the specified run_id")
    parser.add_argument("--summary", action="store_true", help="Show only statistics and log checks")
    parser.add_argument("--thinking", action="store_true", help="Show readable thinking returned by the server")
    parser.add_argument("--requests", action="store_true", help="Show actual model requests, including system and message history")
    parser.add_argument("--full", action="store_true", help="Display content without truncation")
    parser.add_argument("--raw", action="store_true", help="Show all raw events, including stream events; overrides other display options")
    parser.add_argument("--max-chars", type=int, default=2000, help="Maximum characters per preview; default: 2000")
    args = parser.parse_args()
    if args.max_chars < 1:
        parser.error("--max-chars must be greater than 0")
    try:
        root = args.root.expanduser()
        if args.session:
            path = Path(args.session).expanduser()
            if not path.exists():
                path = root / args.session
            if path.is_dir():
                path = path / "events.jsonl"
        else:
            candidates = list(root.glob("*/events.jsonl"))
            if not candidates:
                raise FileNotFoundError(f"No logs found: {root}")
            path = max(candidates, key=lambda p: p.stat().st_mtime_ns)
        events, warnings = load_events(path)
        if args.run:
            events = [e for e in events if e.get("run_id") == args.run]
            if not events:
                raise ValueError(f"No matching run_id: {args.run}")
        display(f"Log: {path.resolve()}")
        for warning in warnings:
            display(f"Warning: {warning}")
        if args.raw:
            for event in events:
                display(event)
        else:
            summary(events)
            if not args.summary:
                timeline(events, args)
        return 0
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
