"""The gemini add/list/select account command interface."""

import argparse
import os
import shlex
import subprocess
import sys

from .manager import add_account, list_accounts, refresh, remove_account, select_account
from .native import account_environment, gemini_binary, launch
from .shared import prepare_home
from .state import AccountError, Store, default_root

ACCOUNT_HELP = """
Account commands:
  gemini add account                Sign in and discover the account's email
  gemini list accounts              List saved email accounts
  gemini select account             Choose an account and start Gemini
  gemini select account EMAIL       Select by email and start Gemini
  gemini select account --no-run    Choose an account without starting Gemini
  gemini current account            Show the selected email
  gemini remove account             Remove an account from the list
  gemini share data                 Share settings, skills, extensions, and history now
  gemini --account EMAIL [ARGS]     Use an email for this launch only
"""


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="gemini",
        description="Manage saved Gemini accounts by email.",
        epilog=ACCOUNT_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = root.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="Sign into another account")
    add.add_argument("subject", choices=["account"])
    add.add_argument("--device-auth", action="store_true")
    add.add_argument(
        "--home", metavar="PATH", help="Discover an existing login from its Gemini home"
    )
    listing = commands.add_parser("list", help="Show saved email accounts")
    listing.add_argument("subject", choices=["accounts"])
    listing.add_argument("--json", action="store_true")
    listing.add_argument(
        "--refresh",
        action="store_true",
        help="Refresh cached email and plan information",
    )
    listing.add_argument(
        "-q",
        "--quota",
        action="store_true",
        help="Check and display live account quotas",
    )
    selecting = commands.add_parser("select", help="Select an account and start Gemini")
    selecting.add_argument("subject", choices=["account"])
    selecting.add_argument(
        "email", nargs="?", help="An email or a number from the account list"
    )
    launching = selecting.add_mutually_exclusive_group()
    launching.add_argument(
        "--run",
        action="store_true",
        default=True,
        help="Start Gemini after selecting (default)",
    )
    launching.add_argument(
        "--no-run",
        dest="run",
        action="store_false",
        help="Select an account without starting Gemini",
    )
    current = commands.add_parser("current", help="Show the selected email")
    current.add_argument("subject", choices=["account"])
    remove = commands.add_parser("remove", help="Remove an account from the saved list")
    remove.add_argument("subject", choices=["account"])
    remove.add_argument("email", nargs="?")
    sharing = commands.add_parser(
        "share", help="Share local Gemini data across all accounts"
    )
    sharing.add_argument("subject", choices=["data"])
    shell = commands.add_parser("shell-init", help="Print shell integration")
    shell.add_argument("shell", choices=["bash", "zsh", "powershell", "pwsh", "cmd"])
    return root


def manage(store: Store, arguments: list[str]) -> int:
    args = parser().parse_args(arguments)
    if args.command == "add":
        return add_account(store, device_auth=args.device_auth, existing_home=args.home)
    if args.command == "list":
        list_accounts(
            store,
            as_json=args.json,
            force=args.refresh,
            show_quota=args.quota,
        )
        return 0
    if args.command == "select":
        key = select_account(store, args.email)
        if key is None:
            return 130
        return launch(store.home(key), []) if args.run else 0
    if args.command == "remove":
        return remove_account(store, args.email)
    if args.command == "share":
        print(
            f"All accounts share Gemini data at {prepare_home(store)}. Credentials remain separate."
        )
        return 0
    if args.command == "current":
        state = refresh(store)
        record = state["accounts"].get(state["selected"])
        print(
            record.get("email") or "Email unavailable"
            if record
            else "No account selected. Run gemini select account."
        )
        return 0
    if args.command == "shell-init":
        bin_path = gemini_binary()
        py_exe = sys.executable
        if args.shell in ("bash", "zsh"):
            print(
                "gemini() {\n"
                f"    GEMINI_ACCOUNTS_GEMINI_BIN={shlex.quote(bin_path)} "
                f'{shlex.quote(py_exe)} -m gemini_accounts "$@"\n'
                "}"
            )
        elif args.shell in ("powershell", "pwsh"):
            print(
                f'$script:GeminiOfficial = "{bin_path}"\n'
                "function gemini {\n"
                "    $previous = $env:GEMINI_ACCOUNTS_GEMINI_BIN\n"
                "    $env:GEMINI_ACCOUNTS_GEMINI_BIN = $script:GeminiOfficial\n"
                "    try {\n"
                f'        & "{py_exe}" -m gemini_accounts @args\n'
                "        $exitCode = $LASTEXITCODE\n"
                "    }\n"
                "    finally {\n"
                "        if ($null -eq $previous) {\n"
                "            Remove-Item Env:GEMINI_ACCOUNTS_GEMINI_BIN -ErrorAction SilentlyContinue\n"
                "        } else {\n"
                "            $env:GEMINI_ACCOUNTS_GEMINI_BIN = $previous\n"
                "        }\n"
                "    }\n"
                "    if ($null -ne $exitCode) {\n"
                "        $global:LASTEXITCODE = $exitCode\n"
                "    }\n"
                "}"
            )
        elif args.shell == "cmd":
            print(
                "@echo off\n"
                "REM Gemini Accounts CMD integration\n"
                f'doskey gemini="{py_exe}" -m gemini_accounts $*'
            )
        return 0
    raise AccountError("Unknown account command.")


def run(arguments: list[str]) -> int:
    store = Store(default_root())
    if arguments and arguments[0] in (
        "add",
        "list",
        "select",
        "current",
        "remove",
        "share",
        "shell-init",
    ):
        return manage(store, arguments)
    if arguments and arguments[0] == "account":
        raise AccountError(
            "Use gemini add account, gemini list accounts, or gemini select account. Accounts are identified by email."
        )
    if arguments in (["--help"], ["-h"], ["help"], ["--version"], ["-V"], ["-v"]):
        binary = gemini_binary()
        cmd = [binary, *arguments]
        if os.name == "nt":
            lower = binary.lower()
            if lower.endswith((".cmd", ".bat")):
                cmd = [os.environ.get("COMSPEC", "cmd.exe"), "/c", *cmd]
            elif lower.endswith(".ps1"):
                cmd = ["powershell.exe", "-ExecutionPolicy", "Bypass", "-File", *cmd]
        code = subprocess.call(cmd)
        if arguments[0] in ("--help", "-h", "help") and code == 0:
            print(ACCOUNT_HELP)
        return code

    override = None
    if arguments and (
        arguments[0] == "--account" or arguments[0].startswith("--account=")
    ):
        flag = arguments.pop(0)
        if flag == "--account":
            if not arguments:
                raise AccountError("--account requires an email address.")
            override = arguments.pop(0)
        else:
            override = flag.partition("=")[2]
        if not override:
            raise AccountError("--account requires an email address.")

    state = store.read()
    key = store.resolve(override, state) if override is not None else state["selected"]
    if key is None:
        if not state["accounts"]:
            raise AccountError("No saved accounts. Run gemini add account.")
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise AccountError("No account selected. Run gemini select account.")
        key = select_account(store)
        if key is None:
            return 130
    home = store.home(key)
    if arguments and arguments[0] in ("login", "logout"):
        prepare_home(store, home)
        binary = gemini_binary()
        cmd = [binary, *arguments]
        if os.name == "nt":
            lower = binary.lower()
            if lower.endswith((".cmd", ".bat")):
                cmd = [os.environ.get("COMSPEC", "cmd.exe"), "/c", *cmd]
            elif lower.endswith(".ps1"):
                cmd = ["powershell.exe", "-ExecutionPolicy", "Bypass", "-File", *cmd]
        result = subprocess.run(
            cmd, env=account_environment(home), check=False
        )
        if result.returncode == 0 and not any(
            arg in ("--help", "-h") for arg in arguments
        ):
            refresh(store, force=True, only=key)
        return result.returncode if result.returncode >= 0 else 128 - result.returncode
    return launch(home, arguments)


def main() -> int:
    try:
        return run(sys.argv[1:])
    except AccountError as error:
        print(f"gemini: {error}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"gemini: {error.strerror or 'Operation failed'}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
