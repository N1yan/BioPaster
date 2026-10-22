"""Third-party skill metadata must not bypass host permissions."""
import pytest

from biopaster.skills.loader import discover_skills, validate_skill_metadata
from biopaster.tool_system.context import ToolContext
from biopaster.tool_system.permissions import PermissionAnswer, PermissionRule, PermissionTarget
from biopaster.tool_system.protocol import ToolCall
from biopaster.tool_system.registry import ToolRegistry
from biopaster.tool_system.tools.skill import SkillTool


@pytest.mark.parametrize("allowed", ["Read Write Edit Bash", "[Read, Bash]"])
def test_allowed_tools_is_preserved_without_granting_permissions(tmp_path, allowed):
    directory = tmp_path / "skills" / "example"
    directory.mkdir(parents=True)
    original = (
        "---\nname: example\ndescription: Example workflow.\n"
        f"allowed-tools: {allowed}\n---\nInspect the input before analysis.\n"
    )
    path = directory / "SKILL.md"
    path.write_text(original)
    skills, diagnostics = discover_skills(tmp_path, user_skills_dir=directory.parent)
    assert not diagnostics
    assert "example" in skills
    context = ToolContext(workspace_root=tmp_path, skills=skills)
    context.permission_context.permission_handler = lambda request: PermissionAnswer(allowed=True)
    context.permission_context.add_rule(PermissionRule("Bash", behavior="deny"))
    target = PermissionTarget("Bash", "echo test")
    before = context.permission_context.evaluate(target)
    result = ToolRegistry([SkillTool()]).dispatch(
        ToolCall(name="skill", input={"skill": "example"}, tool_use_id="test-skill"), context
    )
    assert not result.is_error
    assert "Inspect the input before analysis." in result.output[0]["content"]
    assert context.permission_context.evaluate(target) == before
    assert before.behavior == "deny"
    assert path.read_text() == original


@pytest.mark.parametrize("field", ["hooks", "model"])
def test_other_unsupported_fields_remain_rejected(field):
    with pytest.raises(ValueError, match="Unsupported skill options"):
        validate_skill_metadata({"description": "Example", field: "unsupported"})
