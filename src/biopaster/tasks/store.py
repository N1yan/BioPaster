import json
import os
import tempfile
from pathlib import Path
from .model import Task
from dataclasses import asdict


class TaskStore:
    def __init__(self, store_path: Path):
        self.path = Path(store_path).expanduser().resolve()
        
    def _read(self) -> dict:
        """Read and validate the task-list snapshot."""
        with self.path.open("r", encoding="utf-8") as file:
            data = json.load(file)
            
        self._validate_snapshot(data)
        self._validate_dependencies(data["tasks"])
        return data
    
    def _validate_snapshot(self, data: dict) -> None:
        """Validate the complete task-list snapshot."""
        if not isinstance(data, dict):
            raise ValueError("Task storage must be a JSON object.")
        
        if type(data.get("version")) is not int or data["version"] != 1:
            raise ValueError("Unsupported task storage version.")
        
        next_id = data.get("next_id")
        if type(next_id) is not int or next_id < 1:
            raise ValueError("next_id must be a positive integer.")
        
        tasks = data.get("tasks")
        if not isinstance(tasks, dict):
            raise ValueError("tasks must be an object.")
        
        highest_id = 0
        
        for task_id, task in tasks.items():
            if (
                not isinstance(task_id, str)
                or not task_id.isascii()
                or not task_id.isdecimal()
                or int(task_id) < 1
                or str(int(task_id)) != task_id
            ):
                raise ValueError(f"Invalid task ID: {task_id!r}")
            
            if not isinstance(task, dict):
                raise ValueError(f"Task {task_id} must be an object.")

            if task.get("id") != task_id:
                raise ValueError(f"Task ID does not match its key: {task_id}")

            self._validate_task(task)
            highest_id = max(highest_id, int(task_id))
                    
        if next_id <= highest_id:
                    raise ValueError("next_id must be greater than existing task IDs.")
    
    @staticmethod
    def _validate_task(task: dict) -> None:
        expected_fields = {
            "id",
            "subject",
            "description",
            "activeForm",
            "status",
            "blockedBy"
        }
        if set(task) != expected_fields:
            raise ValueError("Task fields do not match the storage format.")
        
        for key in ("subject", "description"):
            value = task[key]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{key} must be a non-empty string.")
            
        if not isinstance(task["activeForm"], str):
            raise ValueError("activeForm must be a string.")
        
        if task["status"] not in (
            "pending",
            "in_progress",
            "completed",
        ):
            raise ValueError("Invalid task status.")
        
        dependencies = task["blockedBy"]
        if not isinstance(dependencies, list) or not all(
            isinstance(item, str) for item in dependencies
        ):
            raise ValueError("blockedBy must be a list of task IDs.")
        
        if len(dependencies) != len(set(dependencies)):
            raise ValueError("blockedBy must not contain duplicate IDs.")
        
    @staticmethod
    def _validate_dependencies(tasks: dict[str, dict]) -> None:
        """Reject missing dependencies, self-dependencies, and cycles."""
        remaining = {}
        dependents = {task_id: [] for task_id in tasks}

        for task_id, task in tasks.items():
            dependencies = task["blockedBy"]
            remaining[task_id] = len(dependencies)

            for dependency_id in dependencies:
                if dependency_id == task_id:
                    raise ValueError(
                        f"Task {task_id} cannot depend on itself."
                    )

                if dependency_id not in tasks:
                    raise ValueError(
                        f"Task {task_id} depends on missing task: "
                        f"{dependency_id}"
                    )

                dependents[dependency_id].append(task_id)

        ready = [
            task_id
            for task_id, count in remaining.items()
            if count == 0
        ]

        visited = 0

        while ready:
            task_id = ready.pop()
            visited += 1

            for dependent_id in dependents[task_id]:
                remaining[dependent_id] -= 1
                if remaining[dependent_id] == 0:
                    ready.append(dependent_id)

        if visited != len(tasks):
            raise ValueError("Task dependencies contain a cycle.")
    
        
    def _save(self, data: dict) -> None:
        """Validate and atomically replace the stored snapshot."""
        self._validate_snapshot(data)
        self._validate_dependencies(data["tasks"])
        # Serialize before touching the destination file.
        content = json.dumps(data, ensure_ascii=False, indent=2)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=".tasks-",
                suffix=".tmp",
                delete=False,
            ) as file:
                temporary_path = Path(file.name)
                file.write(content)
            
            os.replace(temporary_path, self.path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
                
    def get_blocks(self, task_id: str) -> list[str]:
        """Return IDs of tasks that depend on this task."""
        data = self._read()

        if task_id not in data["tasks"]:
            raise ValueError(f"Task not found: {task_id}")

        return sorted(
            (
                dependent_id
                for dependent_id, task in data["tasks"].items()
                if task_id in task["blockedBy"]
            ),
            key=int,
        )
            
    def initialize(self) -> None:
        """Initialize the task store if it does not exist."""
        if self.path.exists() or self.path.is_symlink():
            self._read()
            return

        self._save({
            "version": 1,
            "next_id": 1,
            "tasks": {},
        })
                
    def get_task(self, task_id: str) -> Task | None:
        """Retrieve a task by its ID."""
        data = self._read()
        if task_id not in data["tasks"]:
            raise ValueError(f"Task not found: {task_id}")
        return Task(**data["tasks"][task_id])
    
    def list_tasks(self) -> list[Task]:
        """List all tasks in the store."""
        data = self._read()
        return [
            Task(**task)
            for _, task in sorted(
                data["tasks"].items(),
                key=lambda item: int(item[0]),
            )
        ]
        
    def create_task(
        self,
        subject: str,
        description: str,
        activeForm: str = ""
    ) -> Task:
        """Create and persist a pending task."""
        data = self._read()
        
        task_id = str(data["next_id"])
        task = Task(
            id=task_id,
            subject=subject,
            description=description,
            activeForm=activeForm,
            status="pending",
        )
        
        data["tasks"][task_id] = asdict(task)
        data["next_id"] += 1
        self._save(data)
        return task
    
    def update_task(
        self,
        task_id: str,
        *,
        subject: str | None = None,
        description: str | None = None,
        activeForm: str | None = None,
        status: str | None = None,
        addBlockedBy: list[str] | None = None,
        addBlocks: list[str] | None = None,
    ) -> Task:
        """Update task details and persist the validated snapshot."""
        data = self._read()
        
        if task_id not in data["tasks"]:
            raise ValueError(f"Task not found: {task_id}")
        
        task_data = data["tasks"][task_id]
        changes = {
            "subject": subject,
            "description": description,
            "activeForm": activeForm,
            "status": status,
        }

        for key, value in changes.items():
            if value is not None:
                task_data[key] = value
                
        if addBlockedBy is not None:
            if not isinstance(addBlockedBy, list) or not all(
                isinstance(item, str) for item in addBlockedBy
            ):
                raise ValueError("addBlockedBy must be a list of task IDs.")

            task_data["blockedBy"] = list(dict.fromkeys(
                [*task_data["blockedBy"], *addBlockedBy]
            ))
        
        if addBlocks is not None:
            if not isinstance(addBlocks, list) or not all(
                isinstance(item, str) for item in addBlocks
            ):
                raise ValueError("addBlocks must be a list of task IDs.")

            for dependent_id in addBlocks:
                if dependent_id not in data["tasks"]:
                    raise ValueError(f"Task not found: {dependent_id}")

                dependent = data["tasks"][dependent_id]
                dependent["blockedBy"] = list(dict.fromkeys(
                    [*dependent["blockedBy"], task_id]
                ))
                
        self._save(data)
        return Task(**task_data)
    
    def delete_task(self, task_id: str) -> Task:
        """Delete a task and remove references to it in one save."""
        data = self._read()

        if task_id not in data["tasks"]:
            raise ValueError(f"Task not found: {task_id}")

        deleted = data["tasks"].pop(task_id)

        for task in data["tasks"].values():
            task["blockedBy"] = [
                dependency_id
                for dependency_id in task["blockedBy"]
                if dependency_id != task_id
            ]

        self._save(data)
        return Task(**deleted)