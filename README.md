# Gemini Accounts

A complete terminal account selector for the official Google Gemini CLI. Sign into each
account once, list the discovered email addresses, and manually choose which
account Gemini uses. Skills, extensions, settings, commands, tools, agents, and history
are shared across accounts; each login keeps its own credentials.

## Everyday commands

```bash
gemini add account
gemini list accounts
gemini select account
```

Each `gemini add account` starts the official sign-in flow in a new,
private account home. Choose the account to add in the browser. After sign-in,
its email and subscription/account details are discovered automatically. No account names
or labels are requested.

`gemini list accounts` displays every saved email, its plan, and a star beside
the selected account. For example:

```text
Saved accounts

 * 1. alice@example.com  (google)
   2. bob@example.com  (google)

* Selected for new Gemini sessions
```

`gemini select account` opens the account picker. Use **Up/Down** or **j/k**,
then **Enter** to select and start Gemini. **Esc**, **q**, or **Ctrl+C** cancels
without changing the selection or starting Gemini. A numbered-input menu is used
when arrow-key terminal support is unavailable. Long lists scroll in the terminal.

The picker checks each saved account's live quota and shows the percentage left
in its 5-hour and weekly windows, like `/status`:

```text
 > 1. alice@example.com  (google)  5h: 75% left | weekly: 42% left
   2. bob@example.com  (google)  5h: 0% left | weekly: 18% left
```

Quotas are refreshed in parallel whenever the picker opens. These percentages
describe the remaining usage allowance, not a number of hours of model runtime.
Missing windows, signed-out accounts, unsupported account types, and failed
lookups show `unavailable`; the account can still be selected. Each lookup has a
short timeout. Selecting an email or number directly skips quota lookups.

Selection starts Gemini immediately and is saved for future launches. Later,
run `gemini` to reuse the selected account. To select without starting Gemini:

```bash
gemini select account --no-run
```

The existing `--run` flag is still accepted. A running Gemini session keeps its
account. Exit it normally and use `gemini select account` to choose a different
account and start another session; logging out is unnecessary.

## More controls

| Command | Behavior |
| --- | --- |
| `gemini select account alice@example.com` | Select an email directly and start Gemini. |
| `gemini select account 2` | Select a number from the displayed list and start Gemini. |
| `gemini select account --no-run` | Select an account without starting Gemini. |
| `gemini current account` | Show the selected email. |
| `gemini --account alice@example.com [ARGS]` | Use an account for one launch without changing the saved selection. |
| `gemini list accounts --refresh` | Refresh cached email and plan metadata. |
| `gemini list accounts --json` | Read structured account metadata, including IDs and home paths. |
| `gemini remove account` | Pick an account to remove from the list. |
| `gemini remove account alice@example.com` | Remove an email directly. |
| `gemini share data` | Migrate and share existing local data across every saved account now. |

Multiple saved logins can have the same email, including accounts with different
workspace contexts. They remain separate; use the numbered picker when an email
matches more than one row.

Removing an account unregisters it and clears the selection if it was active.
Its home, session history, and credentials are retained. To explicitly sign out,
run `gemini --account EMAIL logout` before removing it. If Gemini requests
reauthentication later, `gemini --account EMAIL login` updates that saved login
and refreshes its displayed email.

To recover an already signed-in home, `gemini add account --home /path/to/home`
discovers its email without copying credentials or starting another sign-in.

## Installation

This is a companion to the official Google Gemini CLI. It does not include the Gemini
runtime and it does not replace the official executable. Install Python 3.11+
and the official Gemini CLI first, then install this checkout.

### 1. Install the official Gemini CLI

Via npm:

```bash
npm install -g @google/gemini-cli
```

Or via your package manager of choice. Confirm that the official command works before installing this package:

```text
gemini --version
```

### 2. Install this checkout

With `pip`:

```powershell
pip install -e .
```

Or with `uv`:

```powershell
uv tool install --editable .
```

Open a new terminal after installation. `gemini` and `gemini-accounts` are placed
directly in your PATH and are immediately available in any directory in both
PowerShell and Command Prompt (CMD), as well as Linux and macOS shells.

### 3. Enable the `gemini` wrapper

On **Windows (Command Prompt & PowerShell)**:
No shell configuration is required. The `gemini` command is globally installed
and automatically detects the official Gemini executable and routes account management commands
seamlessly from any directory.

If you prefer to define an explicit PowerShell function in `$PROFILE`, run:

```powershell
gemini-accounts shell-init powershell | Out-File -Append $PROFILE
```

For Command Prompt (`cmd.exe`), you can generate a doskey macro with:

```cmd
gemini-accounts shell-init cmd
```

On **Bash**, enable it in the current terminal with:

```bash
eval "$(gemini-accounts shell-init bash)"
```

To enable it in future Bash terminals, add that same `eval` line once to
`~/.bashrc`, then run `source ~/.bashrc`. On **Zsh**, use the matching commands:

```zsh
eval "$(gemini-accounts shell-init zsh)"
```

Add the Zsh line to `~/.zshrc` for persistence. The generated function keeps
the official Gemini executable as the backend and routes account-management
commands through this package.

### 4. Add and select accounts

After installation, the commands are the same on all platforms and in all shells:

```text
gemini add account
gemini add account
gemini list accounts
gemini select account
```

Each `add account` login is saved in its own Gemini home. `select account`
saves the selection and starts Gemini with that account; it never logs out another
saved account. Use **Up/Down** or **j/k**, then **Enter** to select; numbered
input is also supported as a fallback. On Windows, NTFS junctions and hard links
are used automatically, so Developer Mode is not required.

### Updating or uninstalling

From the checkout, refresh the editable installation with:

```text
pip install -e .
```

Or with `uv`:

```text
uv tool install --editable . --force
```

Remove this package with:

```text
pip uninstall gemini-accounts
```

Or with `uv`:

```text
uv tool uninstall gemini-accounts
```

The official Gemini CLI is a separate installation and must be updated or
removed separately.

## Shared data and separate credentials

Each saved login has a stable, separate home directory for credentials (`GEMINI_CLI_HOME` / `GEMINI_CONFIG_DIR`).
All accounts share the existing data: settings (`settings.json`), MCP configurations (`mcp_config.json`),
skills, extensions, commands, tools, agents, and history. Switching accounts changes the login while keeping your setup.
Selecting an account chooses the login used for both new and resumed sessions.
Use `--no-run` to select before resuming an existing session:

```sh
gemini select account alice@example.com --no-run
gemini resume
```

No manual environment override is required. The wrapper manages account-specific homes
and links each account's session directories, skills, extensions, tools, commands, and
settings to the shared store. The existing default store stays in place. Its
credentials are used only when that login is explicitly selected.

Sharing is prepared when adding a new login, selecting an account, or launching
Gemini. To apply it to all existing accounts immediately, without starting a chat:

```sh
gemini share data
```

Existing custom skills and extensions from all registered homes are combined.
Existing shared files take precedence when the same path has different content.
Missing configuration keys in `settings.json` and `mcp_config.json` are merged
into the shared configuration; its existing values take precedence.

Original account files are retained under
`<account-home>/.shared-backups/<migration-id>/account/`. Shared configuration
files changed by the merge are backed up alongside them under `shared/`.
The migration prints each backup location. No credential files are copied.
Directory links make later skill installations and edits visible to all
accounts immediately.

Credential files (`google_accounts.json`, `oauth_creds.json`, `credentials.json`,
`tokens.json`, `auth.json`), the `secrets` directory, credential backups, and OS
keyring entries remain specific to each login.

Restart existing Gemini sessions to reload the shared skills and configuration.

The shared location is recorded in `history.json` beside the account registry
and is reused for settings and skills as well.
For a custom location, set `GEMINI_ACCOUNTS_HISTORY_HOME` before the first launch;
later launches keep the recorded location.

The manager does not import the original default login automatically. If no
account is selected, an interactive Gemini launch opens the picker; scripts must
select an account with `--no-run` or supply `--account EMAIL`. Empty registries ask
you to add an account. Quota errors never cause automatic switching or retries.

Email discovery reads account information from Gemini CLI's native files
(`google_accounts.json` and decoded ID tokens in `oauth_creds.json`) instantaneously
without running heavy background processes or submitting model requests. It also
supports app-server protocol querying as a fallback.

Only home paths, emails, plan metadata, and the chosen account ID enter the
registry. On Linux it lives in `~/.local/share/gemini-accounts/accounts.json`,
respecting `XDG_DATA_HOME`. New homes are under `homes/<internal-id>/`.
On macOS the default root is `~/Library/Application Support/gemini-accounts`;
on Windows it is `%LOCALAPPDATA%/gemini-accounts`.

Registry changes are locked and written atomically. Created directories use mode
`0700` and registry files `0600` on POSIX. A successful login remains saved
if email lookup temporarily fails; use `list accounts --refresh` to retry.

Named environment credentials (`GEMINI_API_KEY`, `GOOGLE_API_KEY`, and
`GOOGLE_APPLICATION_CREDENTIALS`) are omitted from account launches so they cannot override
the chosen saved login.

| Setting | Purpose |
| --- | --- |
| `GEMINI_ACCOUNTS_HOME` | Override the registry and managed-home root. |
| `GEMINI_ACCOUNTS_GEMINI_BIN` | Set the official Gemini executable; otherwise use `gemini` on `PATH`. |
| `GEMINI_ACCOUNTS_HISTORY_HOME` | Override shared history and settings location. |

## Development

```bash
python -m unittest discover -s tests -v
```

The test suite covers repeated sign-ins, email discovery, cached lists,
credential isolation, registry migration, concurrent selection, failure recovery,
shell forwarding, and manual handling of quota errors. Real PTY tests cover arrow
keys, cancellation, and terminal restoration.

GitHub Actions runs the test, parser, and wheel checks on Ubuntu, macOS, and Windows.
