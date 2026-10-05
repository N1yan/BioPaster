from typing import Any
import yaml
from pathlib import Path
from biopaster.skills.model import Skill



def parse_skill_document(text: str) -> tuple[dict[str, Any], str]:
    """Separate YAML frontmatter from the Markdown body."""
    
    lines = text.lstrip("\ufeff").splitlines(keepends=True)
    
    if not lines or not lines[0].startswith("---"):
        return {}, text
    
    for index in range(1, len(lines)):
        if lines[index].startswith("---"):
            frontmatter = "".join(lines[1:index])
            body = "".join(lines[index + 1:])
            metadata = yaml.safe_load(frontmatter) or {}
            if metadata is None:
                metadata = {}
            
            if not isinstance(metadata, dict):
                raise ValueError("Skill frontmatter must be a mapping.")
    
            return metadata, body
        
    raise ValueError("Skill frontmatter is missing its closing '---'.")

def validate_skill_metadata(metadata: dict[str, Any]) -> None:
    """Validate the skill options supported by BioPaster."""
    description = metadata.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("description must be a non-empty string.")

    disabled = metadata.get("disable-model-invocation", False)
    if not isinstance(disabled, bool):
        raise ValueError(
            "disable-model-invocation must be a boolean."
        )

    unsupported_fields = {
        "allowed-tools",
        "model",
        "context",
        "agent",
        "hooks",
        "arguments",
        "argument-hint",
        "user-invocable",
        "effort",
        "shell",
        "paths",
    }

    unsupported = unsupported_fields.intersection(metadata)
    if unsupported:
        raise ValueError(
            "Unsupported skill options: "
            + ", ".join(sorted(unsupported))
        )


def discover_skills(
    workspace_root: Path,
    *,
    user_skills_dir: Path | None = None,
) -> tuple[dict[str, Skill], list[str]]:
    """Discover skills in the workspace and user skills directory."""
    
    skills: dict[str, Skill] = {}
    diagnostics: list[str] = []
    
    user_dir = (
        Path(user_skills_dir).expanduser()
        if user_skills_dir is not None
        else Path.home() / ".biopaster" / "skills"
    )
    workspace_dir = Path(workspace_root).expanduser() / ".biopaster" / "skills"
    
    for directory in (user_dir, workspace_dir):
        try:
            if not directory.exists():
                continue
            
            entries = sorted(directory.iterdir())
        except Exception as e:
            diagnostics.append(f"Failed to list skills in {directory}: {e}")
            continue
        
        for entry in entries:
            path = entry / "SKILL.md"
            
            try:
                if not entry.is_dir() or not path.is_file():
                    continue
                
                metadata, _  = parse_skill_document(path.read_text(encoding="utf-8"))
                validate_skill_metadata(metadata)
                
                skills[entry.name] = Skill(
                    name=entry.name,
                    description=metadata["description"].strip(),
                    path=path,
                    disable_model_invocation=metadata.get(
                        "disable-model-invocation", False
                    ),
                )
                
            except (OSError, UnicodeError, ValueError, yaml.YAMLError) as e:
                diagnostics.append(f"{path}: {e}")
                        
    return skills, diagnostics


if __name__ == "__main__":
    skill_path = Path("/home/yan/test/BioPaster/.biopaster/skills/pdf/SKILL.md")
    text = skill_path.read_text(encoding="utf-8")
    metadata, body = parse_skill_document(text)

    discovered_skills, diagnostics = discover_skills(
            workspace_root=Path("/home/yan/test/BioPaster")
        )