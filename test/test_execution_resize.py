"""Exercise real terminal reflow, including rows pushed into scrollback."""

import shlex
import shutil
import subprocess
import sys
import textwrap
import time

import pytest


@pytest.mark.skipif(shutil.which("tmux") is None, reason="requires tmux")
def test_resize_repaints_one_execution_area_and_preserves_draft(tmp_path):
    socket = str(tmp_path / "terminal.sock")

    def tmux(*args):
        return subprocess.run(
            ["tmux", "-S", socket, *args], capture_output=True, text=True, timeout=10
        )

    code = textwrap.dedent("""
        import subprocess, sys
        from biopaster.repl.core import build_prompt_session
        from biopaster.repl.execution_view import ExecutionView
        from biopaster.tool_system.permissions import PermissionRequest

        socket = sys.argv[1]
        history = "EARLIER_USER\\nEARLIER_ANSWER\\nCURRENT_USER\\n"
        print("BEFORE_APP", flush=True)
        print(history, end="", flush=True)
        session = build_prompt_session([])
        original_handlers = list(session.app.before_render._handlers)
        view = ExecutionView(session)
        view.render_history = lambda width: history
        view.start()
        view.append_text("```json\\n" + "\\n".join(
            '  "field_%s": "%s",' % (i, "中文内容" * 18)
            for i in range(70)
        ) + "\\n```")
        subprocess.run(["tmux", "-S", socket, "set-option", "-g", "@stage", "running"])
        subprocess.run(["tmux", "-S", socket, "wait-for", "permission"])
        answer = view.ask_permission(PermissionRequest(
            tool_name="Read", tool_use_id="test",
            description="PERMISSION_REQUEST", targets=(), suggestions=()
        ))
        assert answer.allowed
        subprocess.run(["tmux", "-S", socket, "set-option", "-g", "@stage", "approved"])
        subprocess.run(["tmux", "-S", socket, "wait-for", "finish"])
        view.finish("FINAL_ANSWER")
        subprocess.run(["tmux", "-S", socket, "set-option", "-g", "@stage", "finished"])
        draft = view.read_next()
        view.close()
        assert session.app.before_render._handlers == original_handlers
        print("FINAL_ANSWER", flush=True)
        print("SUBMITTED:" + draft, flush=True)
        session.prompt("NEXT_PROMPT> ", handle_sigint=False)
    """)
    command = shlex.join([sys.executable, "-c", code, socket])
    started = tmux("-f", "/dev/null", "new-session", "-d", "-x", "160", "-y", "45", command)
    if started.returncode:
        pytest.skip(f"isolated tmux unavailable: {started.stderr.strip()}")

    def wait_for(check):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if check():
                return
            time.sleep(0.05)
        pytest.fail("terminal did not reach the expected state")

    def capture():
        result = tmux("capture-pane", "-p", "-S", "-10000")
        assert result.returncode == 0, result.stderr
        return result.stdout

    try:
        wait_for(lambda: tmux("show-option", "-gv", "@stage").stdout.strip() == "running")
        tmux("send-keys", "-l", "my draft")
        for columns, rows in ((100, 26), (180, 45), (80, 20), (160, 45)):
            tmux("resize-window", "-x", str(columns), "-y", str(rows))
            time.sleep(0.65)

        screen = capture()
        assert screen.count("Current execution") == 1
        assert "BEFORE_APP" not in screen
        for text in ("EARLIER_USER", "EARLIER_ANSWER", "CURRENT_USER", "my draft"):
            assert screen.count(text) == 1

        tmux("wait-for", "-S", "permission")
        wait_for(lambda: "PERMISSION_REQUEST" in capture())
        for columns, rows in ((85, 24), (160, 45)):
            tmux("resize-window", "-x", str(columns), "-y", str(rows))
            time.sleep(0.65)
        assert capture().count("Current execution") == 1
        tmux("send-keys", "1", "Enter")
        wait_for(lambda: tmux("show-option", "-gv", "@stage").stdout.strip() == "approved")
        wait_for(lambda: "my draft" in capture())

        tmux("wait-for", "-S", "finish")
        wait_for(lambda: tmux("show-option", "-gv", "@stage").stdout.strip() == "finished")
        for columns, rows in ((90, 22), (150, 40)):
            tmux("resize-window", "-x", str(columns), "-y", str(rows))
            time.sleep(0.65)
        screen = capture()
        assert screen.count("Current execution") == 1
        assert screen.count("FINAL_ANSWER") == 1
        assert "my draft" in screen

        tmux("send-keys", "Enter")
        wait_for(lambda: "NEXT_PROMPT>" in capture())
        screen = capture()
        assert "Current execution" not in screen
        assert screen.count("FINAL_ANSWER") == 1
        assert "SUBMITTED:my draft" in screen
    finally:
        tmux("kill-server")
