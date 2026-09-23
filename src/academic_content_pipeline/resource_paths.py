"""Resolve immutable resources bundled with the package."""

from pathlib import Path, PurePosixPath
from typing import Union


PACKAGE_ROOT = Path(__file__).resolve().parent
RESOURCE_ROOT = PACKAGE_ROOT / "resources"


def resource_path(relative_path: Union[Path, str]) -> Path:
    """Return an absolute path below the package's immutable resource root."""
    relative = PurePosixPath(str(relative_path).replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"Resource path must be package-relative: {relative_path}")
    return RESOURCE_ROOT.joinpath(*relative.parts)