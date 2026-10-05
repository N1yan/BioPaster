from pathlib import Path
from typing import Any

from ...skills.loader import (
    parse_skill_document,
    validate_skill_metadata,
)
from ..context import ToolContext
from ..errors import ToolInputError
from ..permissions import PermissionRequest, PermissionRule, PermissionTarget
from ..protocol import ToolResult
from ..registry import ToolSpec

class SkillTool:
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="skill",
            description=(
                "Load the full instructions of a skill listed in the skills "
                "catalog. Use it when a skill matches the user's task. "
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "skill": {
                        "type": "string",
                         "description": "The skill name shown in the catalog.",
                    },
                    "args": {
                        "type": "string",
                         "description": "Optional arguments for the skill.",
                    }
                },
                "required": ["skill"],
            },
            is_read_only=True,
        )
        
    def prepare_input(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        unknown = set(tool_input) - {"skill", "args"}
        if unknown:
            raise ToolInputError(
                f"Unknown parameters: {', '.join(sorted(unknown))}"
            )

        name = tool_input.get("skill")
        if not isinstance(name, str) or not name.strip():
            raise ToolInputError("skill must be a non-empty string.")

        name = name.strip()
        skill = context.skills.get(name)
        if skill is None:
            raise ToolInputError(f"Unknown skill: {name}")

        if skill.disable_model_invocation:
            raise ToolInputError(
                f"Model invocation is disabled for skill: {name}"
            )

        args = tool_input.get("args", "")
        if not isinstance(args, str):
            raise ToolInputError("args must be a string.")

        paths = context.resolve_permission_paths(skill.path)

        return {
            "skill": name,
            "args": args,
            "path": paths[-1],
            "_paths": paths,
        }

    def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> PermissionRequest:
        name = tool_input["skill"]
        tool_name = self.spec().name

        return PermissionRequest(
                tool_name=tool_name,
                tool_use_id=None,
                description=f"Load skill: {name}\nFile: {tool_input['path']}",
                targets=(
                    PermissionTarget(tool_name, name),
                ),
                suggestions=(
                    PermissionRule(tool_name, name),
                ),
            )
                
    def run(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        path: Path = tool_input["path"]
        metadata, body = parse_skill_document(
            path.read_text(encoding="utf-8")
        )
        try:
            validate_skill_metadata(metadata)
        except ValueError as e:
            raise ToolInputError(str(e)) from e
        
        if metadata.get("disable-model-invocation", False):
            raise ToolInputError(
                f"Model invocation is disabled for skill: "
                f"{tool_input['skill']}"
            )
        
        if not body.strip():
            raise ToolInputError("Skill instructions cannot be empty.")
        
        args = tool_input["args"]
        if "$ARGUMENTS" in body:
            body = body.replace("$ARGUMENTS", args)
        elif args:
            body += f"\n\nArguments:\n{args}"

        content = (
            f"Skill: {tool_input['skill']}\n"
            f"Base directory for this skill: {path.parent}\n"
            f"{body}"
        )
        
        return ToolResult(
                    name="Skill",
                    output=[{
                        "type": "text",
                        "content": content,
                        "metadata": {
                            "skill_name": tool_input["skill"],
                            "skill_path": str(path.absolute()),
                            "skill_directory": str(path.absolute().parent),
                            },
                    }],
                )