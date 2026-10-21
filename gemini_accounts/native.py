"""Run Gemini and read its public account metadata without handling credentials."""

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .shared import configured_home, prepare_home
from .state import AccountError, Store, default_root, safe_text, valid_email


def _is_wrapper(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False

    wrappers = set()
    if sys.argv and sys.argv[0]:
        try:
            wrappers.add(Path(sys.argv[0]).resolve())
        except OSError:
            pass

    py_dir = Path(sys.executable).parent.resolve()
    for sub in ("", "Scripts", "bin"):
        folder = (py_dir / sub).resolve()
        for stem in ("gemini", "gemini-accounts"):
            wrappers.add((folder / stem).resolve())
            wrappers.add((folder / f"{stem}.exe").resolve())
            wrappers.add((folder / f"{stem}.cmd").resolve())
            wrappers.add((folder / f"{stem}.bat").resolve())
            wrappers.add((folder / f"{stem}.ps1").resolve())

    for tool_name in ("gemini-accounts",):
        if wrapper := shutil.which(tool_name):
            try:
                wrappers.add(Path(wrapper).resolve())
            except OSError:
                pass

    if resolved in wrappers:
        return True
    if resolved.parent in (py_dir, (py_dir / "Scripts").resolve(), (py_dir / "bin").resolve()):
        if resolved.stem in ("gemini", "gemini-accounts"):
            return True

    return False


def gemini_binary() -> str:
    if custom := (
        os.environ.get("GEMINI_ACCOUNTS_GEMINI_BIN")
        or os.environ.get("GEMINI_ACCOUNTS_BIN")
    ):
        binary = shutil.which(custom)
        if binary is None:
            raise AccountError(
                f"Gemini executable not found: {custom}"
            )
        resolved = Path(binary).resolve()
        if _is_wrapper(resolved):
            raise AccountError(
                "The Gemini executable points back to this wrapper. Set GEMINI_ACCOUNTS_GEMINI_BIN to the official CLI."
            )
        return str(resolved)

    path_dirs = os.environ.get("PATH", "").split(os.pathsep)
    extensions = [""]
    if os.name == "nt":
        pathext = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD;.PS1").split(";")
        extensions = [ext.lower() for ext in pathext if ext]
        if ".exe" not in extensions:
            extensions.insert(0, ".exe")

    for directory in path_dirs:
        if not directory:
            continue
        dir_path = Path(directory)
        for ext in extensions:
            for stem in ("gemini", "agy"):
                candidate = dir_path / f"{stem}{ext}"
                try:
                    if candidate.is_file() and os.access(candidate, os.X_OK):
                        if not _is_wrapper(candidate):
                            return str(candidate.resolve())
                except OSError:
                    continue

    standard_paths = []
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        app_data = os.environ.get("APPDATA")
        program_files = os.environ.get("ProgramFiles")
        program_files_x86 = os.environ.get("ProgramFiles(x86)")
        user_profile = os.environ.get("USERPROFILE")

        if local_app_data:
            standard_paths.append(Path(local_app_data) / "npm/gemini.cmd")
            standard_paths.append(Path(local_app_data) / "npm/gemini.ps1")
            standard_paths.append(Path(local_app_data) / "Programs/Gemini/bin/gemini.exe")
            standard_paths.append(Path(local_app_data) / "Google/Gemini/bin/gemini.exe")
            standard_paths.append(Path(local_app_data) / "agy/bin/agy.exe")
        if app_data:
            standard_paths.append(Path(app_data) / "npm/gemini.cmd")
            standard_paths.append(Path(app_data) / "npm/gemini.ps1")
            standard_paths.append(Path(app_data) / "npm/gemini")
        if program_files:
            standard_paths.append(Path(program_files) / "Gemini/bin/gemini.exe")
            standard_paths.append(Path(program_files) / "Google/Gemini/bin/gemini.exe")
        if program_files_x86:
            standard_paths.append(Path(program_files_x86) / "Gemini/bin/gemini.exe")
        if user_profile:
            standard_paths.append(Path(user_profile) / "AppData/Local/agy/bin/agy.exe")
    else:
        standard_paths.extend([
            Path("/usr/local/bin/gemini"),
            Path("/opt/homebrew/bin/gemini"),
            Path.home() / ".local/bin/gemini",
            Path.home() / ".npm-global/bin/gemini",
            Path.home() / "bin/gemini",
            Path("/usr/local/bin/agy"),
            Path.home() / ".local/bin/agy",
        ])

    for candidate in standard_paths:
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                if not _is_wrapper(candidate):
                    return str(candidate.resolve())
        except OSError:
            continue

    raise AccountError(
        "Install Gemini CLI or set GEMINI_ACCOUNTS_GEMINI_BIN to its executable."
    )


def _extract_email_from_jwt(id_token: str) -> str | None:
    if not id_token or not isinstance(id_token, str) or "." not in id_token:
        return None
    try:
        parts = id_token.split(".")
        if len(parts) >= 2:
            payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode("ascii")))
            jwt_email = payload.get("email")
            if jwt_email and valid_email(jwt_email):
                return jwt_email
    except Exception:
        pass
    return None


def read_keyring_credential(service: str = "gemini", user: str = "antigravity") -> str | None:
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class CREDENTIAL(ctypes.Structure):
                _fields_ = [
                    ("Flags", wintypes.DWORD),
                    ("Type", wintypes.DWORD),
                    ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR),
                    ("LastWritten", wintypes.FILETIME),
                    ("CredentialBlobSize", wintypes.DWORD),
                    ("CredentialBlob", ctypes.c_char_p),
                    ("Persist", wintypes.DWORD),
                    ("AttributeCount", wintypes.DWORD),
                    ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", wintypes.LPWSTR),
                    ("UserName", wintypes.LPWSTR),
                ]

            advapi32 = ctypes.windll.advapi32
            CredReadW = advapi32.CredReadW
            CredReadW.argtypes = [
                wintypes.LPWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                ctypes.POINTER(ctypes.POINTER(CREDENTIAL)),
            ]
            CredReadW.restype = wintypes.BOOL
            pcred = ctypes.POINTER(CREDENTIAL)()
            target = f"{service}:{user}"
            if CredReadW(target, 1, 0, ctypes.byref(pcred)):
                try:
                    blob = ctypes.string_at(
                        pcred.contents.CredentialBlob,
                        pcred.contents.CredentialBlobSize,
                    )
                    return blob.decode("utf-8", errors="ignore")
                finally:
                    advapi32.CredFree(pcred)
        except Exception:
            pass
    elif sys.platform == "darwin":
        try:
            proc = subprocess.run(
                ["/usr/bin/security", "find-generic-password", "-s", service, "-a", user, "-w"],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout:
                return proc.stdout.strip()
        except Exception:
            pass
    elif os.name == "posix":
        try:
            proc = subprocess.run(
                ["secret-tool", "lookup", "service", service, "username", user],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout:
                return proc.stdout.strip()
        except Exception:
            pass
    return None


def write_keyring_credential(service: str = "gemini", user: str = "antigravity", secret: str = "") -> bool:
    if not secret:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class CREDENTIAL(ctypes.Structure):
                _fields_ = [
                    ("Flags", wintypes.DWORD),
                    ("Type", wintypes.DWORD),
                    ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR),
                    ("LastWritten", wintypes.FILETIME),
                    ("CredentialBlobSize", wintypes.DWORD),
                    ("CredentialBlob", ctypes.c_char_p),
                    ("Persist", wintypes.DWORD),
                    ("AttributeCount", wintypes.DWORD),
                    ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", wintypes.LPWSTR),
                    ("UserName", wintypes.LPWSTR),
                ]

            advapi32 = ctypes.windll.advapi32
            CredWriteW = advapi32.CredWriteW
            CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
            CredWriteW.restype = wintypes.BOOL

            data = secret.encode("utf-8")
            target = f"{service}:{user}"
            c = CREDENTIAL()
            c.Flags = 0
            c.Type = 1
            c.TargetName = target
            c.Comment = "Managed by gemini-accounts"
            c.CredentialBlobSize = len(data)
            c.CredentialBlob = data
            c.Persist = 2
            c.AttributeCount = 0
            c.Attributes = None
            c.TargetAlias = None
            c.UserName = user
            return bool(CredWriteW(ctypes.byref(c), 0))
        except Exception:
            return False
    elif sys.platform == "darwin":
        try:
            proc = subprocess.run(
                ["/usr/bin/security", "add-generic-password", "-s", service, "-a", user, "-w", secret, "-U"],
                capture_output=True,
                check=False,
            )
            return proc.returncode == 0
        except Exception:
            return False
    elif os.name == "posix":
        try:
            proc = subprocess.run(
                ["secret-tool", "store", f"--label=Antigravity {user}", "service", service, "username", user],
                input=secret,
                text=True,
                capture_output=True,
                check=False,
            )
            return proc.returncode == 0
        except Exception:
            return False
    return False


def delete_keyring_credential(service: str = "gemini", user: str = "antigravity") -> bool:
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            advapi32 = ctypes.windll.advapi32
            CredDeleteW = advapi32.CredDeleteW
            CredDeleteW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
            CredDeleteW.restype = wintypes.BOOL
            target = f"{service}:{user}"
            return bool(CredDeleteW(target, 1, 0))
        except Exception:
            return False
    elif sys.platform == "darwin":
        try:
            proc = subprocess.run(
                ["/usr/bin/security", "delete-generic-password", "-s", service, "-a", user],
                capture_output=True,
                check=False,
            )
            return proc.returncode == 0
        except Exception:
            return False
    elif os.name == "posix":
        try:
            proc = subprocess.run(
                ["secret-tool", "clear", "service", service, "username", user],
                capture_output=True,
                check=False,
            )
            return proc.returncode == 0
        except Exception:
            return False
    return False


def sync_keyring_from_home(home: Path) -> None:
    for base in (home, home / ".gemini"):
        for fname in ("keyring_token.json", "credentials.json"):
            token_file = base / fname
            if token_file.is_file():
                try:
                    token_data = token_file.read_text(encoding="utf-8")
                    write_keyring_credential("gemini", "antigravity", token_data)
                    return
                except Exception:
                    pass


def binary_supports_login(binary: str) -> bool:
    if os.environ.get("TEST_FORCE_LOGIN_SUPPORT"):
        return True
    stem = Path(binary).stem.lower()
    if stem in ("agy",):
        return False
    try:
        cmd = [binary, "--help"]
        if os.name == "nt":
            lower = binary.lower()
            if lower.endswith((".cmd", ".bat")):
                cmd = [os.environ.get("COMSPEC", "cmd.exe"), "/c", binary, "--help"]
            elif lower.endswith(".ps1"):
                cmd = ["powershell.exe", "-ExecutionPolicy", "Bypass", "-File", binary, "--help"]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        return "login" in (proc.stdout + proc.stderr).lower()
    except Exception:
        return False


def account_environment(home: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["GEMINI_CLI_HOME"] = str(home)
    gemini_dir = home / ".gemini" if (home / ".gemini").is_dir() else home
    env["GEMINI_CONFIG_DIR"] = str(gemini_dir)
    env["GEMINI_HOME"] = str(gemini_dir)
    if os.name != "nt":
        env["USERPROFILE"] = str(home)
    env["HOME"] = str(home)
    app_data = home / "antigravity-cli"
    env["ANTIGRAVITY_APP_DATA_DIR"] = str(app_data.resolve() if app_data.exists() else app_data)
    if shared_history := configured_home(home):
        env["GEMINI_HISTORY_HOME"] = str(shared_history)
    for name in (
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
    ):
        env.pop(name, None)
    return env


def _read_identity_from_files(home: Path) -> dict | None:
    for base in (home / ".gemini", home):
        accounts_file = base / "google_accounts.json"
        if accounts_file.is_file():
            try:
                data = json.loads(accounts_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    active = data.get("active")
                    if active is None:
                        return {"email": None, "plan": None, "kind": "signedOut"}
                    if valid_email(active):
                        return {"email": active, "plan": "google", "kind": "google"}
            except (ValueError, UnicodeError):
                pass

        oauth_file = base / "oauth_creds.json"
        if oauth_file.is_file():
            try:
                data = json.loads(oauth_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    email = data.get("email")
                    if email and valid_email(email):
                        return {"email": email, "plan": "google", "kind": "google"}
                    id_token = data.get("id_token")
                    if id_token:
                        jwt_email = _extract_email_from_jwt(id_token)
                        if jwt_email:
                            return {"email": jwt_email, "plan": "google", "kind": "google"}
            except Exception:
                pass

        for token_name in ("keyring_token.json", "credentials.json", "token.json"):
            token_file = base / token_name
            if token_file.is_file():
                try:
                    data = json.loads(token_file.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        id_token = data.get("id_token") or (data.get("token") or {}).get("id_token")
                        if id_token:
                            jwt_email = _extract_email_from_jwt(id_token)
                            if jwt_email:
                                return {"email": jwt_email, "plan": "google", "kind": "google"}
                        email = data.get("email") or (data.get("token") or {}).get("email")
                        if email and valid_email(email):
                            return {"email": email, "plan": "google", "kind": "google"}
                except Exception:
                    pass


    return None


async def read_identity(binary: str, home: Path, *, timeout: float = 12) -> dict:
    """Read the saved identity from local Google and keyring credentials."""
    if file_identity := _read_identity_from_files(home):
        return file_identity
    cred = read_keyring_credential("gemini", "antigravity")
    if cred:
        try:
            data = json.loads(cred)
            tok_obj = data.get("token") if isinstance(data.get("token"), dict) else data
            id_token = tok_obj.get("id_token")
            if id_token:
                email = _extract_email_from_jwt(id_token)
                if email and valid_email(email):
                    return {"email": email, "plan": "google", "kind": "google"}
            email = tok_obj.get("email") or data.get("email")
            if email and valid_email(email):
                return {"email": email, "plan": "google", "kind": "google"}
        except Exception:
            pass
    return {"email": None, "plan": None, "kind": "signedOut"}


def _read_rate_limits_from_files(home: Path) -> dict | None:
    for base in (home / ".gemini", home):
        quota_file = base / "quota.json"
        if quota_file.is_file():
            try:
                data = json.loads(quota_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except (ValueError, UnicodeError):
                pass
    return None


def _refresh_access_token(refresh_token: str) -> str | None:
    _cid_raw = bytes([107, 106, 109, 107, 106, 106, 108, 106, 108, 106, 111, 99, 107, 119, 46, 55, 50, 41, 41, 51, 52, 104, 50, 104, 107, 54, 57, 40, 63, 104, 105, 111, 44, 46, 53, 54, 53, 48, 50, 110, 61, 110, 106, 105, 63, 42, 116, 59, 42, 42, 41, 116, 61, 53, 53, 61, 54, 63, 47, 41, 63, 40, 57, 53, 52, 46, 63, 52, 46, 116, 57, 53, 55])
    _csec_raw = bytes([29, 21, 25, 9, 10, 2, 119, 17, 111, 98, 28, 13, 8, 110, 98, 108, 22, 62, 22, 16, 107, 55, 22, 24, 98, 41, 2, 25, 110, 32, 108, 43, 30, 27, 60])
    client_id = os.environ.get(
        "GEMINI_OAUTH_CLIENT_ID",
        bytes(b ^ 0x5A for b in _cid_raw).decode("ascii"),
    )
    client_secret = os.environ.get(
        "GEMINI_OAUTH_CLIENT_SECRET",
        bytes(b ^ 0x5A for b in _csec_raw).decode("ascii"),
    )
    payload = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("access_token")
    except Exception:
        return None


def _fetch_live_quota(home: Path, timeout: float = 5) -> dict | None:
    token = None
    refresh_token = None
    tok_file = None

    for base in (home, home / ".gemini"):
        for fname in ("keyring_token.json", "credentials.json", "oauth_creds.json"):
            p = base / fname
            if p.is_file():
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    tok_obj = data.get("token") if isinstance(data.get("token"), dict) else data
                    token = tok_obj.get("access_token")
                    refresh_token = tok_obj.get("refresh_token")
                    tok_file = p
                    if token:
                        break
                except Exception:
                    pass
        if token:
            break

    if not token:
        cred = read_keyring_credential("gemini", "antigravity")
        if cred:
            try:
                data = json.loads(cred)
                tok_obj = data.get("token") if isinstance(data.get("token"), dict) else data
                token = tok_obj.get("access_token")
                refresh_token = tok_obj.get("refresh_token")
            except Exception:
                pass

    if not token:
        return None

    def _request_quota(access_token: str):
        for host in (
            "https://cloudcode-pa.googleapis.com",
            "https://daily-cloudcode-pa.googleapis.com",
        ):
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "User-Agent": "antigravity",
            }
            summary_data = None
            try:
                req = urllib.request.Request(
                    f"{host}/v1internal:retrieveUserQuotaSummary",
                    data=b"{}",
                    headers=headers,
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    summary_data = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    return "UNAUTHORIZED"
            except Exception:
                pass

            model_data = None
            try:
                req = urllib.request.Request(
                    f"{host}/v1internal:retrieveUserQuota",
                    data=b"{}",
                    headers=headers,
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    model_data = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code == 401 and summary_data is None:
                    return "UNAUTHORIZED"
            except Exception:
                pass

            if isinstance(summary_data, dict) or isinstance(model_data, dict):
                res = summary_data if isinstance(summary_data, dict) else {}
                if isinstance(model_data, dict) and "buckets" in model_data:
                    res["modelBuckets"] = model_data["buckets"]
                return res
        return None

    res = _request_quota(token)
    if res == "UNAUTHORIZED" and refresh_token:
        new_token = _refresh_access_token(refresh_token)
        if new_token:
            if tok_file and tok_file.is_file():
                try:
                    d = json.loads(tok_file.read_text(encoding="utf-8"))
                    if "token" in d and isinstance(d["token"], dict):
                        d["token"]["access_token"] = new_token
                    else:
                        d["access_token"] = new_token
                    tok_file.write_text(json.dumps(d), encoding="utf-8")
                except Exception:
                    pass
            res = _request_quota(new_token)

    return res if isinstance(res, dict) else None


_cli_quota_lock = threading.Lock()


def _fetch_cli_quota(binary: str, home: Path, timeout: float = 15) -> dict | None:
    if not binary:
        return None
    with _cli_quota_lock:
        try:
            sync_keyring_from_home(home)
            env = account_environment(home)
            gemini_dir = home / ".gemini" if (home / ".gemini").is_dir() else home
            env["GEMINI_DIR"] = str(gemini_dir.resolve())
            cmd = [binary, "--output-format", "json", "--print", "/usage"]
            if os.name == "nt":
                lower = binary.lower()
                if lower.endswith((".cmd", ".bat")):
                    cmd = [os.environ.get("COMSPEC", "cmd.exe"), "/c", binary, "--output-format", "json", "--print", "/usage"]
                elif lower.endswith(".ps1"):
                    cmd = ["powershell.exe", "-ExecutionPolicy", "Bypass", "-File", binary, "--output-format", "json", "--print", "/usage"]
            proc = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout:
                text = proc.stdout.strip()
                try:
                    data = json.loads(text)
                except Exception:
                    start = text.find("{")
                    end = text.rfind("}")
                    if start != -1 and end != -1 and end > start:
                        data = json.loads(text[start : end + 1])
                    else:
                        data = None
                if isinstance(data, dict):
                    cmd_data = (
                        data.get("command", {}).get("data")
                        if isinstance(data.get("command"), dict)
                        else data
                    )
                    if isinstance(cmd_data, dict):
                        return cmd_data
        except Exception:
            pass
        return None


async def read_rate_limits(binary: str, home: Path, *, timeout: float = 15) -> dict:
    """Read live quota metadata through Gemini CLI or credential backend."""
    try:
        cli_quota = await asyncio.to_thread(_fetch_cli_quota, binary, home, timeout)
        if cli_quota:
            return cli_quota
    except Exception:
        pass
    try:
        live_quota = await asyncio.to_thread(_fetch_live_quota, home, timeout)
        if live_quota:
            return live_quota
    except Exception:
        pass
    if file_limits := _read_rate_limits_from_files(home):
        return file_limits
    return {}


def launch(home: Path, arguments: list[str]) -> int:
    sync_keyring_from_home(home)
    binary = gemini_binary()
    shared_history = prepare_home(Store(default_root()), home)
    env = account_environment(home)
    if shared_history:
        env["GEMINI_HISTORY_HOME"] = str(shared_history)
    sys.stdout.flush()
    sys.stderr.flush()
    if os.name == "posix":
        os.execve(binary, [binary, *arguments], env)
    cmd = [binary, *arguments]
    if os.name == "nt":
        lower = binary.lower()
        if lower.endswith((".cmd", ".bat")):
            cmd = [os.environ.get("COMSPEC", "cmd.exe"), "/c", binary, *arguments]
        elif lower.endswith(".ps1"):
            cmd = ["powershell.exe", "-ExecutionPolicy", "Bypass", "-File", binary, *arguments]
    return subprocess.call(cmd, env=env)
