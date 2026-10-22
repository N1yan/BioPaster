"""Render persisted task state for periodic agent reminders."""

from ..tasks.store import TaskStore


def format_task_context(store: TaskStore | None) -> str:
    """Read one fresh snapshot; propagate read errors to the caller."""
    if store is None:
        return ""
    tasks = store.list_tasks()
    if not tasks:
        return ""

    by_id = {task.id: task for task in tasks}
    lines = [
        "\n\nTask state at the time of this reminder:",
        "Later Task tool updates take precedence over this snapshot. "
        "Use TaskGet for full task requirements.",
    ]
    for status in ("in_progress", "pending", "completed"):
        for task in tasks:
            if task.status != status:
                continue
            subject = " ".join(task.subject.split())
            line = f"- [{status}] {task.id}: {subject}"
            blockers = [
                task_id for task_id in task.blockedBy
                if by_id[task_id].status != "completed"
            ]
            if blockers and status != "completed":
                line += f" (blocked by unfinished tasks: {', '.join(blockers)})"
            lines.append(line)

    if all(task.status == "completed" for task in tasks):
        lines.append(
            "All recorded tasks are marked completed. Check whether the actual "
            "deliverables satisfy the user's request. If so, deliver the result; "
            "otherwise update or add tasks for the remaining work."
        )
    else:
        lines.append(
            "Advance the current work using this task state. If no task is in "
            "progress, choose an appropriate pending task whose dependencies "
            "are complete. Keep task records aligned with actual progress."
        )
    return "\n".join(lines)
