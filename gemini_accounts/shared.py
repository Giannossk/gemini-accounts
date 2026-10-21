"""Share Gemini's persistent files without moving any login's credential home."""

import filecmp
import json
import os
import shutil
import sys
import tomllib
import uuid
from datetime import date, datetime, time
from pathlib import Path

from .state import AccountError, Store, file_lock, private_directory

MARKER = ".gemini-accounts-shared"

# Extensible directories prelinked across all account homes
DIRECTORIES = (
    "skills",
    "plugins",
    "rules",
    "prompts",
    "agents",
    "commands",
    "extensions",
    "tools",
    "memories",
    "automations",
    "hooks",
    "cache",
    "log",
    "history",
    "shell_snapshots",
    "antigravity-cli",
    "sessions",
    "archived_sessions",
    "conversations",
)

# Standard files shared across all account homes
SHARED_FILES = (
    "settings.json",
    "config.toml",
    "mcp_config.json",
    "history.jsonl",
    "session_index.jsonl",
    "conversation_summaries.db",
)

IGNORED_SYSTEM_NAMES = {
    "appdata",
    "application data",
    "local settings",
    "desktop",
    "documents",
    "downloads",
    "music",
    "pictures",
    "videos",
    "saved games",
    "searches",
    "favorites",
    "links",
    "contacts",
    "ntuser.dat",
    "ntuser.ini",
    "desktop.ini",
    "thumbs.db",
    "public",
    "library",
    "applications",
    "node_modules",
    "venv",
    "target",
    "build",
    "dist",
    "__pycache__",
    "cargo",
    "rustup",
}


def is_junction(path: Path) -> bool:
    if os.name != "nt":
        return False
    if hasattr(path, "is_junction"):
        try:
            return path.is_junction()
        except OSError:
            return False
    if hasattr(os.path, "isjunction"):
        try:
            return os.path.isjunction(str(path))
        except OSError:
            return False
    return False


def is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    return is_junction(path)


def create_link(source: Path, target: Path, *, target_is_directory: bool = False) -> None:
    try:
        source.symlink_to(target, target_is_directory=target_is_directory)
        return
    except OSError:
        if os.name != "nt":
            raise
    if target_is_directory or target.is_dir():
        import _winapi

        _winapi.CreateJunction(str(target.resolve()), str(source))
    else:
        try:
            os.link(str(target.resolve()), str(source))
        except OSError:
            shutil.copy2(target, source)


def remove_link(path: Path) -> None:
    if is_junction(path):
        try:
            path.unlink()
        except OSError:
            os.rmdir(path)
    else:
        path.unlink(missing_ok=True)


def _linked(source: Path, target: Path) -> bool:
    if not source.exists() and not is_link(source):
        return False
    if not target.exists():
        return False
    try:
        if is_link(source):
            return source.resolve() == target.resolve()
        if os.name == "nt" and source.is_file() and target.is_file():
            return source.samefile(target)
        return source.resolve() == target.resolve()
    except OSError:
        return False


def _write_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}-{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            if os.name == "posix":
                temporary.chmod(0o600)
            json.dump(value, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _merge_lines(source: Path, target: Path) -> None:
    lock_path = target if os.name != "nt" else target.with_name(f".{target.name}.lock")
    if os.name != "nt" and target.exists() and target.stat().st_size == 0:
        with target.open("ab") as handle:
            handle.write(b"\n")
    with file_lock(lock_path):
        existing = target.read_bytes() if target.exists() else b""
        seen = set(existing.splitlines())
        with target.open("ab") as handle:
            if existing and not existing.endswith(b"\n"):
                handle.write(b"\n")
            for line in source.read_bytes().splitlines():
                if line and line not in seen:
                    handle.write(line + b"\n")
                    seen.add(line)
            handle.flush()
            os.fsync(handle.fileno())


def shareable(name: str) -> bool:
    """Credentials, recovery copies, process-local files, and OS directories stay in place."""
    if name.startswith("."):
        return False
    plain = name.casefold()
    if plain in IGNORED_SYSTEM_NAMES:
        return False
    if any(
        plain == word or plain.startswith((word + ".", word + "-", word + "_"))
        for word in (
            "auth",
            "credentials",
            "secrets",
            "tokens",
            "keyring",
            "google_accounts",
            "oauth_creds",
            "oauth",
            "accounts",
        )
    ):
        return False
    return not (
        plain in ("tmp", "sandbox", "sandbox-secrets", "env", "gemini")
        or plain.startswith(("gemini-accounts-", "history-", "shared-", "backup"))
        or ".backup" in plain
        or ".sqlite" in plain
        or plain.endswith((".lock", ".sock", ".pid"))
    )


def shared_home(store: Store) -> Path:
    """Pin the existing default store; never derive history from the selected login."""
    path = store.root / "history.json"
    requested = os.environ.get("GEMINI_ACCOUNTS_HISTORY_HOME")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        target = (
            Path(requested).expanduser() if requested else Path.home() / ".gemini"
        ).resolve()
        private_directory(target)
        _write_json(path, {"version": 1, "home": str(target)})
        return target
    except (ValueError, UnicodeError) as error:
        raise AccountError(f"Invalid shared history settings: {path}") from error
    if (
        not isinstance(value, dict)
        or value.get("version") != 1
        or not isinstance(value.get("home"), str)
        or not Path(value["home"]).is_absolute()
    ):
        raise AccountError(f"Invalid shared history settings: {path}")
    target = Path(value["home"])
    if requested and Path(requested).expanduser().resolve() != target:
        raise AccountError(f"Shared history is already configured at {target}.")
    if not target.is_dir():
        raise AccountError(f"The shared history directory is missing: {target}")
    return target


def configured_home(account_home: Path) -> Path | None:
    marker = account_home / MARKER
    if marker.is_file():
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            if isinstance(data, str) and Path(data).is_absolute():
                return Path(data)
            if isinstance(data, dict) and isinstance(data.get("home"), str):
                return Path(data["home"])
        except (ValueError, UnicodeError):
            pass
    return None


def _manifest(home: Path, target: Path) -> set[str]:
    try:
        value = json.loads((home / MARKER).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()
    except (ValueError, UnicodeError) as error:
        raise AccountError(f"Invalid shared home marker: {home / MARKER}") from error
    if (
        not isinstance(value, dict)
        or value.get("version") != 1
        or value.get("home") != str(target)
        or not isinstance(value.get("names"), list)
        or not all(isinstance(name, str) for name in value["names"])
    ):
        raise AccountError(f"Invalid shared home marker: {home / MARKER}")
    return set(value["names"])


def _merge_missing(target: dict, source: dict) -> bool:
    changed = False
    for key, value in source.items():
        if key not in target:
            target[key] = value
            changed = True
        elif isinstance(target[key], dict) and isinstance(value, dict):
            changed = _merge_missing(target[key], value) or changed
        elif isinstance(target[key], list) and isinstance(value, list):
            for item in value:
                if item in target[key]:
                    continue
                match = None
                if isinstance(item, dict):
                    identity = next(
                        (name for name in ("path", "name", "id") if name in item), None
                    )
                    if identity:
                        match = next(
                            (
                                entry
                                for entry in target[key]
                                if isinstance(entry, dict)
                                and entry.get(identity) == item[identity]
                            ),
                            None,
                        )
                if match is not None:
                    changed = _merge_missing(match, item) or changed
                else:
                    target[key].append(item)
                    changed = True
    return changed


def _toml_value(value: object) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return (
            "{ "
            + ", ".join(
                f"{_toml_value(key)} = {_toml_value(item)}"
                for key, item in value.items()
            )
            + " }"
        )
    raise AccountError("Unsupported value in Gemini configuration; originals retained.")


def _toml_document(value: dict, prefix: tuple[str, ...] = ()) -> str:
    lines = []
    if prefix:
        lines.append("[" + ".".join(_toml_value(key) for key in prefix) + "]")
    for key, item in value.items():
        if not isinstance(item, dict):
            lines.append(f"{_toml_value(key)} = {_toml_value(item)}")
    for key, item in value.items():
        if isinstance(item, dict):
            lines.append("\n" + _toml_document(item, (*prefix, key)))
    return "\n".join(lines) + "\n"


def _save_original(source: Path, backup: Path) -> None:
    if backup.exists() or backup.is_symlink():
        return
    private_directory(backup.parent)
    shutil.copy2(source, backup)


def _replace_file(source: Path, target: Path, backup: Path) -> None:
    destination = target.resolve()
    _save_original(destination, backup)
    temporary = destination.with_name(f".shared-{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(source, temporary)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _merge_config(source: Path, target: Path, backup: Path) -> None:
    try:
        source_value = tomllib.loads(source.read_text(encoding="utf-8"))
        target_value = tomllib.loads(target.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as error:
        raise AccountError(
            f"Invalid configuration at {source} or {target}; originals retained."
        ) from error
    if not _merge_missing(target_value, source_value):
        return
    contents = _toml_document(target_value)
    tomllib.loads(contents)
    destination = target.resolve()
    _save_original(destination, backup)
    temporary = destination.with_name(f".shared-{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            if os.name == "posix":
                temporary.chmod(0o600)
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _merge_json_config(source: Path, target: Path, backup: Path) -> None:
    try:
        source_value = json.loads(source.read_text(encoding="utf-8"))
        target_value = json.loads(target.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as error:
        raise AccountError(
            f"Invalid configuration at {source} or {target}; originals retained."
        ) from error
    if not isinstance(source_value, dict) or not isinstance(target_value, dict):
        return
    if not _merge_missing(target_value, source_value):
        return
    contents = json.dumps(target_value, indent=2) + "\n"
    destination = target.resolve()
    _save_original(destination, backup)
    temporary = destination.with_name(f".shared-{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            if os.name == "posix":
                temporary.chmod(0o600)
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _merge(
    source: Path,
    target: Path,
    backup: Path,
    *,
    repair: bool = False,
    ancestors: frozenset[Path] = frozenset(),
) -> None:
    if not source.exists() and not is_link(source):
        return
    if is_link(target) and target.resolve().is_relative_to(source.absolute()):
        raise AccountError(
            f"Shared data links back into an account home: {target}; originals retained."
        )
    if source.resolve() == target.resolve():
        return
    if source.is_dir():
        resolved = source.resolve()
        if resolved in ancestors or target.resolve().is_relative_to(resolved):
            raise AccountError(
                f"Recursive directory link in {source}; originals retained."
            )
        ancestors = ancestors | {resolved}
    if not target.exists() and not is_link(target):
        if is_link(source):
            create_link(target, source.resolve(), target_is_directory=source.is_dir())
        elif source.is_dir():
            private_directory(target)
            for child in sorted(source.iterdir()):
                _merge(
                    child, target / child.name, backup / child.name, ancestors=ancestors
                )
        elif source.is_file():
            shutil.copy2(source, target)
        else:
            raise AccountError(
                f"Cannot share special file: {source}; originals retained."
            )
        return
    if source.is_dir() and target.is_dir():
        for child in sorted(source.iterdir()):
            _merge(
                child,
                target / child.name,
                backup / child.name,
                repair=repair,
                ancestors=ancestors,
            )
    elif source.is_file() and target.is_file():
        if filecmp.cmp(source, target, shallow=True) or filecmp.cmp(source, target, shallow=False):
            return
        if repair and source.stat().st_mtime_ns > target.stat().st_mtime_ns:
            _replace_file(source, target, backup)
        elif source.name in ("settings.json", "mcp_config.json") or source.name.endswith(".settings.json"):
            _merge_json_config(source, target, backup)
        elif source.name == "config.toml" or source.name.endswith(".config.toml"):
            _merge_config(source, target, backup)
        elif source.name in ("history.jsonl", "session_index.jsonl"):
            _merge_lines(source, target)


def prepare_home(store: Store, account_home: Path | None = None) -> Path:
    """Migrate all registered homes, then link all noncredential persistent files."""
    private_directory(store.root)
    target = shared_home(store)

    with file_lock(store.root / "shared.lock"):
        candidates = {
            Path(row["home"]).resolve() for row in store.read()["accounts"].values()
        }
        if account_home is not None:
            candidates.add(account_home.resolve())
        candidates = {home for home in candidates if home.is_dir() and home != target}
        for home in candidates:
            if home.is_relative_to(target) or target.is_relative_to(home):
                raise AccountError(
                    "Shared and account homes cannot contain one another."
                )
        names = set(DIRECTORIES) | set(SHARED_FILES)
        for home in [target, *sorted(candidates)]:
            names.update(path.name for path in home.iterdir() if shareable(path.name))

        manifests = {}
        for home in sorted(candidates):
            manifests[home] = _manifest(home, target)
            probe = home / f".shared-link-test-{uuid.uuid4().hex}"
            try:
                create_link(probe, target, target_is_directory=True)
            except OSError as error:
                raise AccountError(
                    "Shared Gemini data requires symbolic links or NTFS junctions."
                ) from error
            finally:
                remove_link(probe)

        for name in DIRECTORIES:
            private_directory(target / name)
        for fname in ("settings.json", "config.toml"):
            if not (target / fname).exists():
                if os.name == "posix":
                    (target / fname).touch(mode=0o600)
                else:
                    (target / fname).touch()

        # Clean up any previously linked items that are no longer shareable.
        for home in sorted(candidates):
            stale = manifests[home] - names
            for stale_name in stale:
                stale_path = home / stale_name
                if is_link(stale_path) and _linked(stale_path, target / stale_name):
                    try:
                        remove_link(stale_path)
                    except OSError:
                        pass

        # Import all homes before linking, so new files reach every account at once.
        migrations = []
        for home in sorted(candidates):
            pending = [
                name
                for name in sorted(names)
                if not _linked(home / name, target / name)
            ]
            if not pending and manifests[home] == names:
                continue
            backup = home / ".shared-backups" / uuid.uuid4().hex
            for name in pending:
                _merge(
                    home / name,
                    target / name,
                    backup / "shared" / name,
                    repair=name in manifests[home],
                )
            migrations.append((home, pending, backup))

        for home, pending, backup in migrations:
            for name in pending:
                source, destination = home / name, target / name
                if not destination.exists() and not is_link(destination):
                    continue
                if source.exists() or is_link(source):
                    private_directory(backup / "account")
                    source.rename(backup / "account" / name)
                try:
                    create_link(
                        source, destination, target_is_directory=destination.is_dir()
                    )
                except OSError:
                    original = backup / "account" / name
                    if original.exists() or is_link(original):
                        original.rename(source)
                    raise
            _write_json(
                home / MARKER,
                {"version": 1, "home": str(target), "names": sorted(names)},
            )
            if backup.exists():
                print(
                    f"Shared Gemini data in {target}. Originals retained at {backup}.",
                    file=sys.stderr,
                )
        return target
