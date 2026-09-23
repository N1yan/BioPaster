import json
from dataclasses import asdict

from biopaster.agent.conversation import (
    Message,
    TextContentBlock,
    ToolUseContentBlock,
)
from biopaster.tool_system.permissions import (
    PermissionDecision,
    PermissionRequest,
)
from biopaster.tool_system.permissions import PermissionReviewResult


def build_review_input(
    messages: list[Message],
    request: PermissionRequest,
    decisions: tuple[PermissionDecision, ...],
) -> str:
    if not request.tool_use_id:
        raise ValueError("Permission review requires a tool_use_id.")

    history = []
    found_current = False

    for message in messages:
        if message._is_internal:
            continue

        if message.role == "user":
            if isinstance(message.content, str):
                history.append({"user": message.content})
            else:
                for block in message.content:
                    if isinstance(block, TextContentBlock):
                        history.append({"user": block.text})

        elif message.role == "assistant":
            if isinstance(message.content, str):
                continue

            for block in message.content:
                if not isinstance(block, ToolUseContentBlock):
                    continue

                if block.id == request.tool_use_id:
                    found_current = True
                    break

                history.append({
                    "tool": block.name,
                    "input": block.input,
                })

        if found_current:
            break

    if not found_current:
        raise ValueError("Current tool call was not found in conversation.")

    return json.dumps(
        {
            "history": history,
            "proposed_operation": {
                "tool": request.tool_name,
                "tool_use_id": request.tool_use_id,
                "description": request.description,
                "targets": [
                    asdict(target) for target in request.targets
                ],
            },
            "permission_checks": [
                asdict(decision) for decision in decisions
            ],
        },
        ensure_ascii=False,
        indent=2,
    )
    
def review_permission(
    provider,
    messages: list[Message],
    request: PermissionRequest,
    decisions: tuple[PermissionDecision, ...],
    system_prompt: str,
) -> PermissionReviewResult:
    review_input = build_review_input(messages, request, decisions)

    response = provider.chat_stream_response(
        messages=[
            {"role": "user", "content": review_input},
        ],
        system=system_prompt,
        tools=None,
        on_text_chunk=None,
        max_retries=0,
        max_tokens=4096,
        timeout=30.0,
        thinking={"type": "disabled"},
    )

    if response.tool_uses:
        raise ValueError("Permission reviewer must not call tools.")

    if response.finish_reason != "end_turn":
        raise ValueError(
            f"Permission review did not finish normally: "
            f"{response.finish_reason}"
        )

    text = response.content.strip()
    lines = text.splitlines()

    if (
        len(lines) >= 3
        and lines[0].strip().lower() in {"```json", "```"}
        and lines[-1].strip() == "```"
    ):
        text = "\n".join(lines[1:-1])

    result = json.loads(text)

    if not isinstance(result, dict):
        raise ValueError("Permission review must return a JSON object.")

    if set(result) != {"shouldBlock", "reason"}:
        raise ValueError(
            "Permission review must contain only shouldBlock and reason."
        )

    if type(result["shouldBlock"]) is not bool:
        raise ValueError("shouldBlock must be a boolean.")

    return PermissionReviewResult(
        behavior="deny" if result["shouldBlock"] else "allow",
        reason=result["reason"],
    )