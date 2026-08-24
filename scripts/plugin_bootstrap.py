from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path


PACKAGE_VERSION = "0.2.0"
MISSING_PYTHON_MESSAGE = (
    "Get Wechat History requires Python 3.10 or newer. "
    "Install Python 3.10+ and make sure the Windows Python launcher or python command is available."
)


def plugin_root() -> Path:
    return Path(__file__).resolve().parents[1]


def runtime_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return Path(local_app_data) / "GetWechatHistory" / "runtime"


def venv_root() -> Path:
    return runtime_root() / "venv"


def install_marker_path() -> Path:
    return runtime_root() / "install-state.json"


def build_server_command(venv_root_path: Path) -> list[str]:
    scripts_dir = "Scripts" if os.name == "nt" else "bin"
    python_name = "python.exe" if os.name == "nt" else "python"
    python_exe = venv_root_path / scripts_dir / python_name
    return [str(python_exe), "-m", "get_wechat_history.mcp_server"]


def build_install_command(venv_root_path: Path, root: Path) -> list[str]:
    return [
        str(_venv_python(venv_root_path)),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--quiet",
        "--timeout",
        "20",
        "--retries",
        "1",
        str(root),
    ]


def _project_hash(project_file: Path) -> str:
    return hashlib.sha256(project_file.read_bytes()).hexdigest()


def install_is_current(marker: Path, project_file: Path, package_version: str) -> bool:
    try:
        state = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        state.get("package_version") == package_version
        and state.get("pyproject_sha256") == _project_hash(project_file)
    )


def _write_install_marker(marker: Path, project_file: Path, package_version: str) -> None:
    marker.parent.mkdir(parents=True, exist_ok=True)
    temporary = marker.with_suffix(marker.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            {
                "package_version": package_version,
                "pyproject_sha256": _project_hash(project_file),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(marker)


def _venv_python(venv_root_path: Path) -> Path:
    return Path(build_server_command(venv_root_path)[0])


def ensure_environment(root: Path | None = None) -> Path:
    root = (root or plugin_root()).resolve()
    project_file = root / "pyproject.toml"
    runtime = runtime_root()
    environment = runtime / "venv"
    python_exe = _venv_python(environment)
    runtime.mkdir(parents=True, exist_ok=True)

    if sys.version_info < (3, 10):
        raise RuntimeError(MISSING_PYTHON_MESSAGE)
    if not python_exe.is_file():
        venv.EnvBuilder(with_pip=True, clear=False, symlinks=False).create(environment)
    if not python_exe.is_file():
        raise RuntimeError(f"The plugin virtual environment could not be created at {environment}.")

    marker = runtime / "install-state.json"
    if not install_is_current(marker, project_file, PACKAGE_VERSION):
        command = build_install_command(environment, root)
        completed = subprocess.run(
            command,
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        if completed.stdout:
            print(completed.stdout, file=sys.stderr, end="")
        if completed.returncode != 0:
            raise RuntimeError(f"Plugin dependency installation failed with exit code {completed.returncode}.")
        _write_install_marker(marker, project_file, PACKAGE_VERSION)
    return environment


def run_server(root: Path | None = None) -> int:
    environment = ensure_environment(root)
    command = build_server_command(environment)
    completed = subprocess.run(command, cwd=str((root or plugin_root()).resolve()), check=False)
    return completed.returncode


def main() -> int:
    try:
        return run_server()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
