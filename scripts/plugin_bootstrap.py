from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path


PACKAGE_VERSION = "1.1.0"
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


def _source_hash(root: Path) -> str:
    """Fingerprint runtime inputs independently of the plugin cache path."""
    files = {
        root / name
        for name in ("pyproject.toml", "server.py", ".mcp.json", ".codex-plugin/plugin.json")
        if (root / name).is_file()
    }
    source_suffixes = {".py", ".pyi", ".js", ".json", ".pyd", ".dll"}
    for directory, suffixes in (
        (root / "get_wechat_history", source_suffixes),
        (root / "scripts", source_suffixes | {".cmd", ".bat", ".ps1", ".sh"}),
    ):
        if not directory.is_dir():
            continue
        files.update(
            path
            for path in directory.rglob("*")
            if path.is_file()
            and path.suffix.casefold() in suffixes
            and "__pycache__" not in path.relative_to(directory).parts
        )

    digest = hashlib.sha256(b"get-wechat-history-runtime-v1\0")
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        content_hash = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                content_hash.update(chunk)
        digest.update(content_hash.digest())
    return digest.hexdigest()


def _install_state(project_file: Path, package_version: str) -> dict[str, str]:
    return {
        "package_version": package_version,
        "pyproject_sha256": _project_hash(project_file),
        "source_sha256": _source_hash(project_file.parent),
    }


def install_is_current(marker: Path, project_file: Path, package_version: str) -> bool:
    try:
        state = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(state, dict) or not state.get("source_sha256"):
        return False
    expected = _install_state(project_file, package_version)
    return all(state.get(key) == value for key, value in expected.items())


def _write_install_marker(
    marker: Path,
    project_file: Path,
    package_version: str,
    *,
    installed_state: dict[str, str] | None = None,
) -> None:
    marker.parent.mkdir(parents=True, exist_ok=True)
    temporary = marker.with_suffix(marker.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            installed_state if installed_state is not None else _install_state(project_file, package_version),
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
        # Capture inputs before installing so edits during a build invalidate
        # the marker on the next launch instead of being recorded as installed.
        installed_state = _install_state(project_file, PACKAGE_VERSION)
        command = build_install_command(environment, root)
        completed = subprocess.run(
            command,
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"Plugin dependency installation failed with exit code {completed.returncode}.")
        _write_install_marker(marker, project_file, PACKAGE_VERSION, installed_state=installed_state)
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
