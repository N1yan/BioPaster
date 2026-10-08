from pathlib import Path
import json
import re
import subprocess
import sys
import warnings
from typing import Any
from jupyter_client.kernelspec import KernelSpecManager


class ConfigError(ValueError):
    """An actionable configuration error, safe to display without a traceback."""


def get_config_path() -> Path:
    return Path.home() / ".biopaster" / "config.json"
    
def save_config(config: dict[str, Any]) -> None:
    config_path = get_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config,indent=2, ensure_ascii=False))


def get_default_config() -> dict[str, Any]:
    return {
        "default_model": "nvidia/nemotron-3-nano-30b-a3b:free",
        "providers": {
            "openrouter": {
                "api_key": "",
                "base_url": "https://openrouter.ai/api",
                "models": [{
                    "id": "nvidia/nemotron-3-nano-30b-a3b:free",
                    "context_window": 128000,
                    "max_output_tokens": 32000,
                }],
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
    """Read configuration without replacing invalid user settings."""
    config_path = get_config_path()
    try:
        if not config_path.exists():
            config_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with config_path.open("x", encoding="utf-8") as file:
                    json.dump(get_default_config(), file, indent=2, ensure_ascii=False)
            except FileExistsError:
                pass  # Another process created it; read that file below.
            else:
                raise ConfigError(
                    "Created configuration file. Fill in providers.<name>.api_key, "
                    "check base_url and default_model, then run biopaster again."
                )
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    except UnicodeError as exc:
        raise ConfigError("Configuration must be UTF-8 encoded.") from exc
    except OSError as exc:
        raise ConfigError(f"Cannot read or create configuration: {exc.strerror}") from exc
    if not isinstance(config, dict):
        raise ConfigError("Configuration must be a JSON object.")
    return config
    
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
        raise ConfigError(f"Unknown provider: {provider}")

    return providers[provider]


def get_model_options(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Read configured models in provider and model declaration order."""
    providers = config.get("providers", {})
    if not isinstance(providers, dict) or not providers:
        raise ConfigError("providers must be a non-empty object")
    options = []
    for name, connection in providers.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(connection, dict):
            raise ConfigError("Provider names and settings are invalid")
        for field in ("api_key", "base_url"):
            if not isinstance(connection.get(field), str) or not connection[field].strip():
                raise ConfigError(f"{name}.{field} must be a non-empty string")
        models = connection.get("models")
        if not isinstance(models, list) or not models:
            raise ConfigError(f"{name}.models must be a non-empty list")
        ids = set()
        for model in models:
            if not isinstance(model, dict):
                raise ConfigError(f"{name}.models entries must be objects")
            model_id = model.get("id")
            if not isinstance(model_id, str) or not model_id.strip() or model_id in ids:
                raise ConfigError(f"Invalid or duplicate model ID in {name}")
            ids.add(model_id)
            window = model.get("context_window", connection.get("context_window", 128000))
            output = model.get("max_output_tokens", connection.get("max_output_tokens", 32000))
            if type(window) is not int or type(output) is not int or output < 1 or window <= output:
                raise ConfigError(f"Invalid token budgets for {name}/{model_id}")
            options.append({
                "provider": name,
                "api_key": connection["api_key"],
                "base_url": connection["base_url"],
                "model": model_id,
                "context_window": window,
                "max_output_tokens": output,
            })
    return options


def resolve_model_config(config: dict[str, Any]) -> dict[str, Any]:
    """Select the first connection exposing the global default model."""
    options = get_model_options(config)
    default_model = config.get("default_model")
    if not isinstance(default_model, str) or not default_model.strip():
        raise ConfigError("default_model must be a non-empty string")
    for option in options:
        if option["model"] == default_model:
            return option
    raise ConfigError(f"default_model is not present in providers.*.models: {default_model}")


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
            raise ConfigError("notebook_kernels must not be empty")
        entries = {name: {} for name in configured}
    elif isinstance(configured, dict):
        if not configured:
            raise ConfigError("notebook_kernels must not be empty")
        entries = configured
    else:
        raise ConfigError("notebook_kernels must be an object or a list")

    if any(not isinstance(name, str) or not name.strip() for name in entries):
        raise ConfigError("notebook kernel names must be non-empty strings")
    if any(not isinstance(details, dict) for details in entries.values()):
        raise ConfigError("notebook kernel settings must be objects")

    names = [name.strip() for name in entries]
    if len(set(names)) != len(names):
        raise ConfigError("notebook_kernels must not contain duplicates")

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
                raise ConfigError(
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
