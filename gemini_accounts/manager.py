"""Email discovery, account registration, and manual selection."""

import asyncio
import datetime
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from .native import (
    account_environment,
    binary_supports_login,
    delete_keyring_credential,
    gemini_binary,
    read_identity,
    read_keyring_credential,
    read_rate_limits,
    sync_keyring_from_home,
    write_keyring_credential,
    _extract_email_from_jwt,
    _read_identity_from_files,
)
from .picker import pick
from .shared import prepare_home
from .state import AccountError, Store, file_lock, private_directory


def describe(record: dict) -> str:
    email = record.get("email") or "Email unavailable"
    detail = record.get("plan") or record.get("kind") or "metadata pending"
    if record.get("kind") == "signedOut":
        detail = "sign-in required"
    return f"{email}  ({detail})"


def _window_duration(bucket: dict) -> int | None:
    for key in ("window", "bucketId", "displayName", "description", "id", "name"):
        val = str(bucket.get(key) or "").lower()
        if "5h" in val or "5 hour" in val or "5-hour" in val or "five hour" in val:
            return 300
        if "week" in val or "7d" in val:
            return 10080

    reset_time = bucket.get("resetTime") or bucket.get("reset_time")
    if reset_time and isinstance(reset_time, str):
        try:
            clean_time = reset_time.replace("Z", "+00:00")
            dt = datetime.datetime.fromisoformat(clean_time)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            now = datetime.datetime.now(datetime.timezone.utc)
            diff_seconds = (dt - now).total_seconds()
            if diff_seconds > 6 * 3600:
                return 10080
            return 300
        except Exception:
            pass

    return 300


def _calc_pct(frac: float | int) -> int:
    pct = int(round(frac * 100))
    if frac < 1.0 and pct == 100:
        pct = 99
    elif frac > 0.0 and pct == 0:
        pct = 1
    return max(0, min(100, pct))


def describe_quota(result: dict | None) -> str:
    remaining = {300: None, 10080: None}
    has_gemini = False
    if isinstance(result, dict):
        groups = result.get("groups")
        if isinstance(groups, list):
            gemini_group = None
            for g in groups:
                if not isinstance(g, dict):
                    continue
                name = (g.get("displayName") or g.get("name") or "").lower()
                desc = (g.get("description") or "").lower()
                gid = (g.get("groupId") or g.get("id") or "").lower()
                if "gemini" in name or "gemini" in desc or "gemini" in gid:
                    gemini_group = g
                    break
                buckets = g.get("buckets", [])
                if isinstance(buckets, list) and any(
                    "gemini" in str(b.get("bucketId") or b.get("id") or "").lower()
                    for b in buckets
                    if isinstance(b, dict)
                ):
                    gemini_group = g
                    break
            if gemini_group:
                has_gemini = True
                for b in gemini_group.get("buckets", []):
                    if not isinstance(b, dict):
                        continue
                    dur = _window_duration(b)
                    frac = b.get("remainingFraction")
                    if frac is None:
                        frac = b.get("remaining_fraction")
                    if dur in remaining and isinstance(frac, (int, float)):
                        remaining[dur] = _calc_pct(frac)

        model_buckets = result.get("modelBuckets") or result.get("buckets")
        if isinstance(model_buckets, list):
            dur_fracs = {300: [], 10080: []}
            for b in model_buckets:
                if not isinstance(b, dict):
                    continue
                if "gemini" not in str(b.get("modelId") or b.get("id") or "").lower():
                    continue
                frac = b.get("remainingFraction")
                if frac is None:
                    frac = b.get("remaining_fraction")
                if not isinstance(frac, (int, float)):
                    continue
                dur = _window_duration(b)
                if dur in dur_fracs:
                    dur_fracs[dur].append(frac)
            if dur_fracs[300] or dur_fracs[10080]:
                has_gemini = True
                for dur in (300, 10080):
                    if remaining[dur] is None and dur_fracs[dur]:
                        remaining[dur] = _calc_pct(min(dur_fracs[dur]))

        if has_gemini:
            if remaining[300] is None:
                remaining[300] = 100
            if remaining[10080] is None:
                remaining[10080] = 100

        limits = result.get("rateLimits")
        by_id = result.get("rateLimitsByLimitId")
        if isinstance(by_id, dict) and "gemini" in by_id:
            limits = by_id["gemini"]
        if isinstance(limits, dict) and limits.get("limitId") in (None, "gemini"):
            for name in ("primary", "secondary"):
                window = limits.get(name)
                if not isinstance(window, dict):
                    continue
                duration = window.get("windowDurationMins")
                used = window.get("usedPercent")
                if (
                    type(duration) is int
                    and duration in remaining
                    and type(used) is int
                ):
                    remaining[duration] = max(0, min(100, 100 - used))
    return " | ".join(
        f"{label}: {remaining[duration]}% left"
        if remaining[duration] is not None
        else f"{label}: unavailable"
        for label, duration in (("5h", 300), ("weekly", 10080))
    )


def quota_labels(accounts: dict[str, dict]) -> dict[str, str]:
    """Refresh picker quotas in parallel; unavailable accounts stay selectable."""
    try:
        binary = gemini_binary()
    except AccountError:
        return {key: describe_quota(None) for key in accounts}

    saved_keyring = read_keyring_credential("gemini", "antigravity")

    async def collect():
        semaphore = asyncio.Semaphore(4)

        async def read(key, record):
            result = None
            home = Path(record["home"])
            if record.get("kind") in (None, "google") and home.is_dir():
                async with semaphore:
                    try:
                        result = await read_rate_limits(binary, home)
                    except (AccountError, OSError):
                        pass
            return key, describe_quota(result)

        try:
            return dict(await asyncio.gather(*(read(k, r) for k, r in accounts.items())))
        finally:
            if saved_keyring:
                try:
                    write_keyring_credential("gemini", "antigravity", saved_keyring)
                except Exception:
                    pass

    return asyncio.run(collect())

    return asyncio.run(collect())


def refresh(store: Store, *, force: bool = False, only: str | None = None) -> dict:
    state = store.read()
    pending = {
        key: record
        for key, record in state["accounts"].items()
        if (only is None or key == only) and (force or record.get("kind") is None)
    }
    if not pending:
        return state
    binary = gemini_binary()

    async def collect():
        semaphore = asyncio.Semaphore(4)

        async def read(key, record):
            async with semaphore:
                try:
                    home = Path(record["home"])
                    if not home.is_dir():
                        raise AccountError("Saved home is missing.")
                    return key, await read_identity(binary, home)
                except (AccountError, OSError):
                    return key, None

        return await asyncio.gather(
            *(read(key, record) for key, record in pending.items())
        )

    results = asyncio.run(collect())
    failed = 0
    with store.edit() as current:
        for key, identity in results:
            if (
                key not in current["accounts"]
                or current["accounts"][key]["home"] != pending[key]["home"]
            ):
                continue
            if identity is None:
                failed += 1
                continue
            if identity["kind"] == "signedOut":
                identity["email"] = current["accounts"][key].get("email")
            current["accounts"][key].update(identity)
    if failed:
        print(
            f"Could not refresh {failed} saved account(s). Try gemini list accounts --refresh.",
            file=sys.stderr,
        )
    return store.read()


def add_account(
    store: Store, *, device_auth: bool = False, existing_home: str | None = None
) -> int:
    if device_auth and existing_home:
        raise AccountError("--home uses an existing login; omit --device-auth.")
    binary = gemini_binary()
    private_directory(store.root)
    with file_lock(store.root / "signin.lock", blocking=False):
        state = store.read()
        account_id = uuid.uuid4().hex
        if existing_home:
            home = Path(existing_home).expanduser().resolve()
            if not home.is_dir():
                raise AccountError("The specified Gemini home does not exist.")
            if any(
                Path(record["home"]).resolve() == home
                for record in state["accounts"].values()
            ):
                raise AccountError("That saved login is already in the account list.")
        else:
            homes = store.root / "homes"
            if homes.is_symlink():
                raise AccountError(
                    "The managed homes directory cannot be a symbolic link."
                )
            private_directory(homes)
            home = homes / account_id
            private_directory(home)
            private_directory(home / ".gemini")
            prepare_home(store, home)

            supports_login = binary_supports_login(binary)

            active_cred = read_keyring_credential("gemini", "antigravity")
            active_email = None
            if active_cred:
                try:
                    data = json.loads(active_cred)
                    id_token = data.get("id_token") or (data.get("token") or {}).get("id_token")
                    if id_token:
                        active_email = _extract_email_from_jwt(id_token)
                except Exception:
                    pass
            if not active_email:
                default_gemini = Path.home() / ".gemini"
                if default_gemini.is_dir():
                    id_info = _read_identity_from_files(default_gemini)
                    if id_info and id_info.get("email"):
                        active_email = id_info["email"]

            already_registered = (
                any(record.get("email") == active_email for record in state["accounts"].values())
                if active_email
                else True
            )

            if not supports_login and active_email and not already_registered:
                print(f"Discovered active login for {active_email}.", flush=True)
                if active_cred:
                    (home / "keyring_token.json").write_text(active_cred, encoding="utf-8")
                    (home / ".gemini" / "keyring_token.json").write_text(active_cred, encoding="utf-8")
                default_gemini = Path.home() / ".gemini"
                if default_gemini.is_dir():
                    for fname in ("google_accounts.json", "oauth_creds.json", "auth.json", "credentials.json"):
                        src = default_gemini / fname
                        if src.is_file():
                            shutil.copy2(src, home / ".gemini" / fname)
            else:
                print("Sign into the account you want to add.", flush=True)
                if supports_login:
                    arguments = [binary, "login"]
                    if device_auth:
                        arguments.append("--device-auth")
                else:
                    arguments = [binary]

                cmd = arguments
                if os.name == "nt":
                    lower = cmd[0].lower()
                    if lower.endswith((".cmd", ".bat")):
                        cmd = [os.environ.get("COMSPEC", "cmd.exe"), "/c", *cmd]
                    elif lower.endswith(".ps1"):
                        cmd = ["powershell.exe", "-ExecutionPolicy", "Bypass", "-File", *cmd]

                backup_cred = None
                if not supports_login:
                    backup_cred = read_keyring_credential("gemini", "antigravity")
                    if backup_cred:
                        delete_keyring_credential("gemini", "antigravity")

                try:
                    result = subprocess.run(
                        cmd, env=account_environment(home), check=False
                    )
                    if not supports_login:
                        new_cred = read_keyring_credential("gemini", "antigravity")
                        if new_cred:
                            (home / "keyring_token.json").write_text(new_cred, encoding="utf-8")
                            (home / ".gemini" / "keyring_token.json").write_text(new_cred, encoding="utf-8")
                finally:
                    if not supports_login and backup_cred and not read_keyring_credential("gemini", "antigravity"):
                        write_keyring_credential("gemini", "antigravity", backup_cred)

                if result.returncode:
                    print(
                        "Sign-in did not complete. Your account selection is unchanged.",
                        file=sys.stderr,
                    )
                    return (
                        result.returncode
                        if result.returncode > 0
                        else 128 - result.returncode
                    )

        metadata_failed = False
        try:
            identity = asyncio.run(read_identity(binary, home))
        except (AccountError, OSError):
            if existing_home:
                raise AccountError(
                    "Could not verify the email for that saved login. Try again."
                ) from None
            identity = {"email": None, "plan": None, "kind": None}
            metadata_failed = True
        if existing_home and identity["kind"] == "signedOut":
            raise AccountError("There is no saved login in that Gemini home.")

        with store.edit() as current:
            if any(
                Path(record["home"]).resolve() == home
                for record in current["accounts"].values()
            ):
                raise AccountError(
                    "That saved login was already added by another terminal."
                )
            current["accounts"][account_id] = {"home": str(home), **identity}
            if current["selected"] is None:
                current["selected"] = account_id
        if existing_home:
            prepare_home(store, home)
        if identity["email"]:
            print(f"Added {identity['email']}.")
        else:
            print("Login saved. Its email is not available yet.")
        if metadata_failed:
            print(
                "Run gemini list accounts --refresh to retry email discovery.",
                file=sys.stderr,
            )
        print(
            "Run gemini add account to add another, or gemini select account to choose one."
        )
        return 0


def list_accounts(
    store: Store,
    *,
    as_json: bool = False,
    force: bool = False,
    show_quota: bool = False,
) -> None:
    state = refresh(store, force=force)
    rows = store.rows(state)
    quotas = {}
    if show_quota and rows:
        quotas = quota_labels(state["accounts"])
    if as_json:
        print(
            json.dumps(
                {
                    "selected": state["selected"],
                    "accounts": [
                        {
                            "id": key,
                            "selected": key == state["selected"],
                            **({"quota": quotas[key]} if show_quota and key in quotas else {}),
                            **record,
                        }
                        for key, record in rows
                    ],
                },
                indent=2,
            )
        )
        return
    if not rows:
        print("No saved accounts. Run gemini add account.")
        return
    print("Saved accounts\n")
    for number, (key, record) in enumerate(rows, 1):
        marker = "*" if key == state["selected"] else " "
        extra = f"  {quotas[key]}" if show_quota and key in quotas else ""
        print(f" {marker} {number}. {describe(record)}{extra}")
    print(
        "\n* Selected for new Gemini sessions"
        if state["selected"]
        else "\nChoose one with gemini select account."
    )


def choose(
    store: Store, selector: str | None = None, *, remove: bool = False
) -> str | None:
    state = refresh(store)
    if not state["accounts"]:
        raise AccountError("No saved accounts. Run gemini add account.")
    if selector is not None:
        return store.resolve(selector, state)
    quotas = {}
    if not remove:
        print("Checking account quotas...", flush=True)
        quotas = quota_labels(state["accounts"])
    title = "Choose an account to remove:" if remove else "Choose the account to use:"
    print(title)
    return pick(
        [
            (key, describe(record) + (f"  {quotas[key]}" if key in quotas else ""))
            for key, record in store.rows(state)
        ],
        selected=state["selected"],
    )


def select_account(store: Store, selector: str | None = None) -> str | None:
    key = choose(store, selector)
    if key is None:
        return None
    home = store.home(key)
    prepare_home(store, home)
    sync_keyring_from_home(home)
    with store.edit() as state:
        store.home(key, state)
        state["selected"] = key
        record = state["accounts"][key]
    print(f"Selected {record.get('email') or 'the saved login'}.", flush=True)
    return key


def remove_account(store: Store, selector: str | None = None) -> int:
    key = choose(store, selector, remove=True)
    if key is None:
        return 130
    with store.edit() as state:
        record = state["accounts"].pop(key, None)
        if record is None:
            raise AccountError("That account was already removed.")
        if state["selected"] == key:
            state["selected"] = None
    print(
        f"Removed {record.get('email') or 'the account'} from the list. Its saved home is retained."
    )
    return 0
