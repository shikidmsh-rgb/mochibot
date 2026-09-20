"""Import-light entry point for trusted draft scripts; not a security sandbox."""

from pathlib import Path


def run_candidate(
    extension_id: str, package_dir: Path, data_dir: Path, *, config: dict | None = None,
):
    """Load with schema defaults plus explicit fixtures, never live config/DB/env."""
    from mochi.extensions.loader import load_from_path, validate_package
    from mochi.extensions.store import ExtensionError
    from mochi.skill_config_resolver import _cast

    if config is not None and not isinstance(config, dict):
        raise ExtensionError("invalid_config", "Fixture config must be a dictionary")
    fixture = {}
    for field in validate_package(extension_id, package_dir)["config_schema"]:
        if isinstance(field, dict):
            key, kind, default = field["key"], field.get("type", "str"), field.get("default", "")
        else:
            key, kind, default = field.key, field.type, field.default
        try:
            fixture[key] = _cast(default, kind)
        except (ValueError, TypeError) as exc:
            raise ExtensionError("invalid_config", f"Invalid default for {key}") from exc
    fixture.update(config or {})
    return load_from_path(extension_id, package_dir, data_dir=data_dir, config=fixture)


def main() -> None:
    import runpy
    import sys

    # On Windows the parent assigns our process to its cleanup job before releasing
    # this gate. No user script may spawn children before that assignment.
    if sys.stdin.buffer.read(1) != b"1":
        raise SystemExit("Runner did not authorize script startup")
    script, *arguments = sys.argv[1:]
    sys.argv = [script, *arguments]
    sys.path.insert(0, str(Path(script).parent))
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()
