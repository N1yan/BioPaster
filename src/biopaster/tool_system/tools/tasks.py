from dataclasses import asdict
from typing import Any

from ..context import ToolContext
from ..errors import ToolExecutionError, ToolInputError
from ..permissions import PermissionRequest, PermissionRule, PermissionTarget
from ..protocol import ToolResult
from ..registry import ToolSpec


class TaskCreateTool:
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="TaskCreate",
            description=(
                "Create a pending task to track work. "
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "subject": {
                        "type": "string",
                        "description": "A short task title.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Detailed requirements for the task.",
                    },
                    "activeForm": {
                        "type": "string",
                        "description": "Text shown while the task is in progress.",
                    },
                },
                "required": ["subject", "description"],
            },
        )

    def prepare_input(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        unknown = set(tool_input) - {
            "subject", "description", "activeForm"
        }
        if unknown:
            raise ToolInputError(
                f"Unknown parameters: {', '.join(sorted(unknown))}"
            )

        for key in ("subject", "description"):
            value = tool_input.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ToolInputError(
                    f"{key} must be a non-empty string."
                )

        active_form = tool_input.get("activeForm", "")
        if not isinstance(active_form, str):
            raise ToolInputError("activeForm must be a string.")

        if context.task_store is None:
            raise ToolExecutionError("Task storage is not configured.")

        return {
            "subject": tool_input["subject"],
            "description": tool_input["description"],
            "activeForm": active_form,
        }

    def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> PermissionRequest:
        name = self.spec().name

        return PermissionRequest(
            tool_name=name,
            tool_use_id=None,
            description=f"Create task: {tool_input['subject']}",
            targets=(PermissionTarget(name, None),),
            suggestions=(PermissionRule(name),),
        )

    def run(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        if context.task_store is None:
            raise ToolExecutionError("Task storage is not configured.")

        task = context.task_store.create_task(**tool_input)

        return ToolResult(
            name=self.spec().name,
            output=[{
                "type": "text",
                "content": {"task": asdict(task)},
                "metadata": {},
            }],
        )
        
        
class TaskGetTool:
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="TaskGet",
            description="Get the full details of a task by its ID.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "taskId": {
                        "type": "string",
                        "description": "The task ID returned by TaskCreate.",
                    },
                },
                "required": ["taskId"],
            },
            is_read_only=True,
        )

    def prepare_input(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        unknown = set(tool_input) - {"taskId"}
        if unknown:
            raise ToolInputError(
                f"Unknown parameters: {', '.join(sorted(unknown))}"
            )

        task_id = tool_input.get("taskId")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ToolInputError("taskId must be a non-empty string.")

        if context.task_store is None:
            raise ToolExecutionError("Task storage is not configured.")

        return {"taskId": task_id.strip()}

    def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> PermissionRequest:
        name = self.spec().name
        return PermissionRequest(
            tool_name=name,
            tool_use_id=None,
            description=f"Get task: {tool_input['taskId']}",
            targets=(PermissionTarget(name, None),),
            suggestions=(PermissionRule(name),),
        )

    def run(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        if context.task_store is None:
            raise ToolExecutionError("Task storage is not configured.")

        task = context.task_store.get_task(tool_input["taskId"])

        return ToolResult(
            name=self.spec().name,
            output=[{
                "type": "text",
                "content": {"task": asdict(task)},
                "metadata": {},
            }],
        )


class TaskListTool:
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="TaskList",
            description="List tasks with their current status and dependencies.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {},
            },
            is_read_only=True,
        )

    def prepare_input(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        if tool_input:
            raise ToolInputError("TaskList does not accept parameters.")

        if context.task_store is None:
            raise ToolExecutionError("Task storage is not configured.")

        return {}

    def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> PermissionRequest:
        name = self.spec().name
        return PermissionRequest(
            tool_name=name,
            tool_use_id=None,
            description="List current tasks.",
            targets=(PermissionTarget(name, None),),
            suggestions=(PermissionRule(name),),
        )

    def run(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        if context.task_store is None:
            raise ToolExecutionError("Task storage is not configured.")

        tasks = context.task_store.list_tasks()

        return ToolResult(
            name=self.spec().name,
            output=[{
                "type": "text",
                "content": {
                    "tasks": [
                        {
                            "id": task.id,
                            "subject": task.subject,
                            "status": task.status,
                            "blockedBy": task.blockedBy,
                        }
                        for task in tasks
                    ],
                },
                "metadata": {},
            }],
        )
        
class TaskUpdateTool:
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="TaskUpdate",
            description=(
                "Update a task's details, status, or dependencies. "
                "Mark completed only when the work is finished. "
                "Use status='deleted' to remove an obsolete task and "
                "its dependency references. Deletion is not completion."
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "taskId": {"type": "string"},
                    "subject": {"type": "string", "minLength": 1},
                    "description": {"type": "string", "minLength": 1},
                    "activeForm": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": [
                            "pending",
                            "in_progress",
                            "completed",
                            "deleted",
                        ],
                    },
                    "addBlockedBy": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "description": "IDs of tasks this task depends on.",
                    },
                    "addBlocks": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "description": "IDs of tasks that depend on this task.",
                    },
                },
                "required": ["taskId"],
            },
        )

    def prepare_input(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        allowed = {
            "taskId",
            "subject",
            "description",
            "activeForm",
            "status",
            "addBlockedBy",
            "addBlocks",
        }
        unknown = set(tool_input) - allowed
        if unknown:
            raise ToolInputError(
                f"Unknown parameters: {', '.join(sorted(unknown))}"
            )

        task_id = tool_input.get("taskId")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ToolInputError("taskId must be a non-empty string.")

        if not (set(tool_input) - {"taskId"}):
            raise ToolInputError("Provide at least one field to update.")

        for key in ("subject", "description", "activeForm"):
            if key not in tool_input:
                continue

            value = tool_input[key]
            if not isinstance(value, str):
                raise ToolInputError(f"{key} must be a string.")

            if key != "activeForm" and not value.strip():
                raise ToolInputError(
                    f"{key} must be a non-empty string."
                )

        if "status" in tool_input:
            if tool_input["status"] not in (
                "pending", "in_progress", "completed", "deleted"
            ):
                raise ToolInputError("Invalid task status.")

        for key in ("addBlockedBy", "addBlocks"):
            if key not in tool_input:
                continue

            value = tool_input[key]
            if not isinstance(value, list) or not all(
                isinstance(item, str) and item.strip()
                for item in value
            ):
                raise ToolInputError(
                    f"{key} must be a list of non-empty task IDs."
                )

        if (
            tool_input.get("status") == "deleted"
            and set(tool_input) != {"taskId", "status"}
        ):
            raise ToolInputError(
                "Deletion cannot be combined with other updates."
            )

        if context.task_store is None:
            raise ToolExecutionError("Task storage is not configured.")

        prepared = dict(tool_input)
        prepared["taskId"] = task_id.strip()

        for key in ("addBlockedBy", "addBlocks"):
            if key in prepared:
                prepared[key] = [
                    item.strip() for item in prepared[key]
                ]

        return prepared

    def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> PermissionRequest:
        name = self.spec().name
        action = (
            "Delete"
            if tool_input.get("status") == "deleted"
            else "Update"
        )

        return PermissionRequest(
            tool_name=name,
            tool_use_id=None,
            description=f"{action} task: {tool_input['taskId']}",
            targets=(PermissionTarget(name, None),),
            suggestions=(PermissionRule(name),),
        )

    def run(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        store = context.task_store
        if store is None:
            raise ToolExecutionError("Task storage is not configured.")

        task_id = tool_input["taskId"]

        if tool_input.get("status") == "deleted":
            deleted = store.delete_task(task_id)
            content = {
                "success": True,
                "taskId": deleted.id,
                "deleted": True,
            }
        else:
            changes = {
                key: value
                for key, value in tool_input.items()
                if key != "taskId"
            }
            task = store.update_task(task_id, **changes)
            content = {
                "success": True,
                "task": asdict(task),
            }

        return ToolResult(
            name=self.spec().name,
            output=[{
                "type": "text",
                "content": content,
                "metadata": {},
            }],
        )