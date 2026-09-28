from pathlib import Path
import json
import re
import subprocess
import sys
import warnings
from typing import Any
from jupyter_client.kernelspec import KernelSpecManager

def get_config_path() -> Path:
    config_dir = Path.home() / ".biopaster"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir / "config.json"
    
def save_config(config: dict[str, Any]) -> None:
    config_path = get_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config,indent=2, ensure_ascii=False))


def get_default_config() -> dict[str, Any]:
    return {
        "default_provider": "openrouter",
        "providers": {
            "openrouter": {
                "api_key": "",
                "base_url": "https://openrouter.ai/api",
                "default_model": "nvidia/nemotron-3-nano-30b-a3b:free",
                "context_window": 128000,
                "max_output_tokens": 32000
            }
        },
        "session": {
            "auto_save": True,
        },
        "notebook_kernels": {
            "python3": {
                "environment": Path(sys.prefix).name or "current",
            }
        },
        "NOTEBOOK_ENV": "",
    }


def load_config() -> dict[str, Any]:
    """Load configuration from file.

    Returns:
        Configuration dictionary
    """
    config_path = get_config_path()
    if not config_path.exists():
        config = get_default_config()
        save_config(config)
        return config
    
    try:
        config = json.loads(config_path.read_text())
        return config
    except Exception as e:
        print(f"Error loading config: {e}")
        return get_default_config()
    
def get_provider_config(provider: str) -> dict[str, Any]:
    """Get configuration for a specific provider.

    Args:
        provider: Provider name (anthropic, openai, glm, minimax)

    Returns:
        Provider configuration dictionary
    """
    config = load_config()
    providers = config.get("providers", {})

    if provider not in providers:
        raise ValueError(f"Unknown provider: {provider}")

    return providers[provider]


def get_configured_notebook_kernels() -> dict[str, dict[str, str]]:
    """Return configured Jupyter kernels after confirming they are installed."""
    default = {
        "python3": {
            "environment": Path(sys.prefix).name or "current",
        }
    }
    configured = load_config().get("notebook_kernels", default)
    if isinstance(configured, list):
        if not configured:
            raise ValueError("notebook_kernels must not be empty")
        entries = {name: {} for name in configured}
    elif isinstance(configured, dict):
        if not configured:
            raise ValueError("notebook_kernels must not be empty")
        entries = configured
    else:
        raise ValueError("notebook_kernels must be an object or a list")

    if any(not isinstance(name, str) or not name.strip() for name in entries):
        raise ValueError("notebook kernel names must be non-empty strings")
    if any(not isinstance(details, dict) for details in entries.values()):
        raise ValueError("notebook kernel settings must be objects")

    names = [name.strip() for name in entries]
    if len(set(names)) != len(names):
        raise ValueError("notebook_kernels must not contain duplicates")

    installed = KernelSpecManager().get_all_specs()
    missing = [name for name in names if name not in installed]
    if missing:
        warnings.warn(
            "Configured notebook kernels are not installed and will be ignored: "
            + ", ".join(missing),
            RuntimeWarning,
            stacklevel=2,
        )

    result: dict[str, dict[str, str]] = {}
    for name in names:
        if name not in installed:
            continue
        spec = installed[name].get("spec", {})
        result[name] = {
            "display_name": str(spec.get("display_name") or name),
            "language": str(spec.get("language") or "unknown"),
        }
        environment = entries[name].get("environment")
        if environment is not None:
            if not isinstance(environment, str) or not environment.strip():
                raise ValueError(
                    f"Environment for notebook kernel {name} must be a non-empty string"
                )
            result[name]["environment"] = environment.strip()
    return result

def kernel_register():
    """Register configured Python, R, and Bash kernels with Jupyter."""
    config = load_config()
    environment = str(config.get("NOTEBOOK_ENV") or "").strip()

    environment_label = environment or Path(sys.prefix).name or "current"
    environment_slug = re.sub(r"[^a-z0-9._-]+", "-", environment_label.lower()).strip("-")
    environment_slug = environment_slug or "current"
    environment_prefix = ["conda", "run", "-n", environment] if environment else []

    python = "python" if environment else sys.executable
    python_kernel = f"biopaster-python-{environment_slug}"
    r_kernel = f"biopaster-r-{environment_slug}"
    r_expression = (
        "IRkernel::installspec(user=TRUE, "
        f"name={json.dumps(r_kernel)}, "
        f"displayname={json.dumps(f'R ({environment_label})')})"
    )
    commands = {
        "python": (
            python_kernel,
            environment_prefix + [
                python,
                "-m",
                "ipykernel",
                "install",
                "--user",
                "--name",
                python_kernel,
                "--display-name",
                f"Python ({environment_label})",
            ],
        ),
        "r": (
            r_kernel,
            environment_prefix + ["R", "--vanilla", "--slave", "-e", r_expression],
        ),
        "bash": (
            "bash",
            environment_prefix + [python, "-m", "bash_kernel.install"],
        ),
    }

    results: dict[str, dict[str, str]] = {}
    for language, (kernel_name, command) in commands.items():
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            detail = getattr(exc, "stderr", "") or getattr(exc, "stdout", "") or str(exc)
            results[language] = {
                "status": "skipped",
                "kernel_name": kernel_name,
                "error": detail.strip(),
            }
            continue
        results[language] = {
            "status": "registered",
            "kernel_name": kernel_name,
        }

    return results
