import asyncio
import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gemini_accounts.cli import run
from gemini_accounts.manager import add_account, list_accounts, select_account, remove_account
from gemini_accounts.native import (
    account_environment,
    gemini_binary,
    read_identity,
    sync_keyring_from_home,
    _fetch_live_quota,
    _read_identity_from_files,
    _read_rate_limits_from_files,
)
from gemini_accounts.shared import _merge_json_config, prepare_home
from gemini_accounts.state import AccountError, Store, default_root


class GeminiAccountsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.shared = self.root / "shared"
        self.shared.mkdir()
        self.store = Store(self.root / "state")
        env_patch = patch.dict(os.environ, {
            "GEMINI_ACCOUNTS_HISTORY_HOME": str(self.shared),
        })
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def test_read_identity_from_google_accounts_json(self):
        home = self.root / "acc1"
        gemini_dir = home / ".gemini"
        gemini_dir.mkdir(parents=True)
        (gemini_dir / "google_accounts.json").write_text(
            json.dumps({"active": "developer@google.com", "old": []}),
            encoding="utf-8",
        )

        identity = _read_identity_from_files(home)
        self.assertIsNotNone(identity)
        self.assertEqual(identity["email"], "developer@google.com")
        self.assertEqual(identity["kind"], "google")

    def test_read_identity_from_oauth_creds_jwt(self):
        home = self.root / "acc2"
        gemini_dir = home / ".gemini"
        gemini_dir.mkdir(parents=True)

        header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').decode("ascii").rstrip("=")
        payload = base64.urlsafe_b64encode(b'{"email":"jwtuser@example.com","sub":"123"}').decode("ascii").rstrip("=")
        jwt = f"{header}.{payload}.signature"

        (gemini_dir / "oauth_creds.json").write_text(
            json.dumps({"id_token": jwt, "access_token": "token123"}),
            encoding="utf-8",
        )

        identity = _read_identity_from_files(home)
        self.assertIsNotNone(identity)
        self.assertEqual(identity["email"], "jwtuser@example.com")

    def test_read_identity_signed_out_when_active_null(self):
        home = self.root / "acc_out"
        gemini_dir = home / ".gemini"
        gemini_dir.mkdir(parents=True)
        (gemini_dir / "google_accounts.json").write_text(
            json.dumps({"active": None, "old": []}),
            encoding="utf-8",
        )

        identity = _read_identity_from_files(home)
        self.assertIsNotNone(identity)
        self.assertIsNone(identity["email"])
        self.assertEqual(identity["kind"], "signedOut")

    def test_read_rate_limits_from_quota_json(self):
        home = self.root / "acc_quota"
        home.mkdir(parents=True)
        (home / "quota.json").write_text(
            json.dumps({"rateLimits": {"primary": {"usedPercent": 20, "windowDurationMins": 300}}}),
            encoding="utf-8",
        )

        limits = _read_rate_limits_from_files(home)
        self.assertIsNotNone(limits)
        self.assertIn("rateLimits", limits)

    def test_account_environment_sets_gemini_homes_and_cleans_keys(self):
        home = self.root / "acc_env"
        gemini_dir = home / ".gemini"
        gemini_dir.mkdir(parents=True)

        with patch.dict(os.environ, {
            "GEMINI_API_KEY": "secret-key",
            "GOOGLE_API_KEY": "google-key",
        }):
            env = account_environment(home)
            self.assertEqual(env["GEMINI_CLI_HOME"], str(home))
            self.assertEqual(env["GEMINI_CONFIG_DIR"], str(gemini_dir))
            self.assertEqual(env["GEMINI_HOME"], str(gemini_dir))
            self.assertNotIn("GEMINI_API_KEY", env)
            self.assertNotIn("GOOGLE_API_KEY", env)

    def test_account_environment_resolves_antigravity_app_data_dir(self):
        home = self.root / "acc_agy"
        home.mkdir(parents=True)
        agy_dir = home / "antigravity-cli"
        agy_dir.mkdir()
        env = account_environment(home)
        self.assertEqual(env["ANTIGRAVITY_APP_DATA_DIR"], str(agy_dir.resolve()))

    def test_add_account_from_existing_home(self):
        existing = self.root / "existing_gemini"
        existing.mkdir(parents=True)
        (existing / "google_accounts.json").write_text(
            json.dumps({"active": "existing@example.com"}),
            encoding="utf-8",
        )

        with patch("gemini_accounts.manager.gemini_binary", return_value="dummy"):
            code = add_account(self.store, existing_home=str(existing))
            self.assertEqual(code, 0)

        state = self.store.read()
        self.assertEqual(len(state["accounts"]), 1)
        account_id = list(state["accounts"].keys())[0]
        self.assertEqual(state["accounts"][account_id]["email"], "existing@example.com")

    def test_select_and_remove_account(self):
        home1 = self.root / "h1"
        home1.mkdir()
        home2 = self.root / "h2"
        home2.mkdir()
        for home, email in ((home1, "user1@example.com"), (home2, "user2@example.com")):
            (home / "google_accounts.json").write_text(
                json.dumps({"active": email}), encoding="utf-8"
            )

        with self.store.edit() as state:
            state["accounts"]["id1"] = {"home": str(home1), "email": "user1@example.com", "kind": "google"}
            state["accounts"]["id2"] = {"home": str(home2), "email": "user2@example.com", "kind": "google"}

        selected = select_account(self.store, "user2@example.com")
        self.assertEqual(selected, "id2")
        self.assertEqual(self.store.read()["selected"], "id2")

        code = remove_account(self.store, "user2@example.com")
        self.assertEqual(code, 0)
        state_after = self.store.read()
        self.assertNotIn("id2", state_after["accounts"])
        self.assertIsNone(state_after["selected"])

    def test_select_account_without_saved_login_is_refused(self):
        # Regression: an account whose home has no token used to be "selected"
        # while Gemini kept running with whichever login was left in the keyring.
        good = self.root / "good"
        good.mkdir()
        (good / "google_accounts.json").write_text(
            json.dumps({"active": "user1@example.com"}), encoding="utf-8"
        )
        empty = self.root / "empty"
        (empty / ".gemini").mkdir(parents=True)

        with self.store.edit() as state:
            state["accounts"]["id1"] = {"home": str(good), "email": "user1@example.com", "kind": "google"}
            state["accounts"]["id2"] = {"home": str(empty), "email": "user2@example.com", "kind": "google"}
            state["selected"] = "id1"

        with patch("gemini_accounts.manager.sync_keyring_from_home") as sync:
            with self.assertRaisesRegex(AccountError, "user2@example.com has no saved login"):
                select_account(self.store, "user2@example.com")
            sync.assert_not_called()
        self.assertEqual(self.store.read()["selected"], "id1")

    def test_select_account_with_other_users_login_is_refused(self):
        home = self.root / "mismatch"
        home.mkdir()
        (home / "google_accounts.json").write_text(
            json.dumps({"active": "someone-else@example.com"}), encoding="utf-8"
        )
        with self.store.edit() as state:
            state["accounts"]["id1"] = {"home": str(home), "email": "user1@example.com", "kind": "google"}

        with self.assertRaisesRegex(AccountError, "belongs to someone-else@example.com"):
            select_account(self.store, "user1@example.com")
        self.assertIsNone(self.store.read()["selected"])

    def test_sync_keyring_skips_tokens_for_other_emails(self):
        home = self.root / "sync"
        (home / ".gemini").mkdir(parents=True)
        (home / "keyring_token.json").write_text(
            json.dumps({"id_token_email": "other@example.com", "token": {}}), encoding="utf-8"
        )
        (home / ".gemini" / "keyring_token.json").write_text(
            json.dumps({"id_token_email": "me@example.com", "token": {}}), encoding="utf-8"
        )
        with patch("gemini_accounts.native.write_keyring_credential") as write:
            self.assertTrue(sync_keyring_from_home(home, "me@example.com"))
            self.assertIn("me@example.com", write.call_args[0][2])

        with patch("gemini_accounts.native.write_keyring_credential") as write:
            self.assertFalse(sync_keyring_from_home(self.root / "nothing-here"))
            write.assert_not_called()

    def test_live_quota_does_not_use_global_keyring_for_empty_home(self):
        home = self.root / "quota_empty"
        home.mkdir()
        cred = json.dumps({"token": {"access_token": "someone-elses-token"}})
        with patch("gemini_accounts.native.read_keyring_credential", return_value=cred), \
             patch("urllib.request.urlopen") as urlopen:
            self.assertIsNone(_fetch_live_quota(home))
            urlopen.assert_not_called()

    def test_merge_json_config(self):
        source = self.root / "source.json"
        target = self.root / "target.json"
        backup = self.root / "backup.json"

        source.write_text(json.dumps({"theme": "dark", "tools": {"shell": True}}), encoding="utf-8")
        target.write_text(json.dumps({"theme": "light", "model": "gemini-2.0-flash"}), encoding="utf-8")

        _merge_json_config(source, target, backup)

        merged = json.loads(target.read_text(encoding="utf-8"))
        # theme should keep target's existing setting, but new keys should be merged in
        self.assertEqual(merged["theme"], "light")
        self.assertEqual(merged["model"], "gemini-2.0-flash")
        self.assertEqual(merged["tools"], {"shell": True})
        self.assertTrue(backup.exists())


    def test_read_identity_from_keyring_token_json(self):
        home = self.root / "acc_keyring"
        home.mkdir(parents=True)
        header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').decode("ascii").rstrip("=")
        payload = base64.urlsafe_b64encode(b'{"email":"keyringuser@gmail.com"}').decode("ascii").rstrip("=")
        jwt = f"{header}.{payload}.sig"
        (home / "keyring_token.json").write_text(
            json.dumps({"id_token": jwt, "token": {"access_token": "ya29.xyz"}}),
            encoding="utf-8",
        )
        identity = _read_identity_from_files(home)
        self.assertIsNotNone(identity)
        self.assertEqual(identity["email"], "keyringuser@gmail.com")

    def test_read_identity_from_keyring_token_json_id_token_email(self):
        home = self.root / "acc_keyring_id_token_email"
        home.mkdir(parents=True)
        (home / "keyring_token.json").write_text(
            json.dumps({
                "token": {"access_token": "ya29.xyz"},
                "auth_method": "consumer",
                "id_token_email": "directemail@gmail.com",
            }),
            encoding="utf-8",
        )
        identity = _read_identity_from_files(home)
        self.assertIsNotNone(identity)
        self.assertEqual(identity["email"], "directemail@gmail.com")

    def test_binary_supports_login_agy_returns_false(self):
        from gemini_accounts.native import binary_supports_login
        self.assertFalse(binary_supports_login("agy"))
        self.assertFalse(binary_supports_login("agy.exe"))
        self.assertFalse(binary_supports_login(r"C:\some\path\to\agy.exe"))

    def test_add_account_adopts_active_keyring_login_when_no_login_support(self):
        header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').decode("ascii").rstrip("=")
        payload = base64.urlsafe_b64encode(b'{"email":"activekeyring@gmail.com"}').decode("ascii").rstrip("=")
        jwt = f"{header}.{payload}.sig"
        mock_cred = json.dumps({"id_token": jwt, "token": {"access_token": "ya29.123"}})

        with patch("gemini_accounts.manager.gemini_binary", return_value="agy"), \
             patch("gemini_accounts.manager.binary_supports_login", return_value=False), \
             patch("gemini_accounts.manager.read_keyring_credential", return_value=mock_cred):
            code = add_account(self.store)
            self.assertEqual(code, 0)

        state = self.store.read()
        self.assertEqual(len(state["accounts"]), 1)
        acc = list(state["accounts"].values())[0]
        self.assertEqual(acc["email"], "activekeyring@gmail.com")
        self.assertEqual(state["selected"], list(state["accounts"].keys())[0])


    def test_read_identity_does_not_fallback_to_global_keyring_for_empty_home(self):
        home = self.root / "empty_home"
        home.mkdir()
        header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').decode("ascii").rstrip("=")
        payload = base64.urlsafe_b64encode(b'{"email":"global@example.com"}').decode("ascii").rstrip("=")
        jwt = f"{header}.{payload}.sig"
        mock_cred = json.dumps({"id_token": jwt, "token": {"access_token": "tok"}})

        with patch("gemini_accounts.native.read_keyring_credential", return_value=mock_cred):
            identity = asyncio.run(read_identity("dummy", home))
            self.assertEqual(identity["kind"], "signedOut")
            self.assertIsNone(identity["email"])

    def test_add_account_cancelled_does_not_readd_previous_account(self):
        home1 = self.root / "acc1"
        home1.mkdir(parents=True)
        header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').decode("ascii").rstrip("=")
        payload = base64.urlsafe_b64encode(b'{"email":"first@example.com"}').decode("ascii").rstrip("=")
        jwt = f"{header}.{payload}.sig"
        cred1 = json.dumps({"id_token": jwt, "token": {"access_token": "tok1"}})
        (home1 / "keyring_token.json").write_text(cred1, encoding="utf-8")

        with self.store.edit() as state:
            state["accounts"]["id1"] = {"home": str(home1), "email": "first@example.com", "kind": "google"}
            state["selected"] = "id1"

        keyring_val = cred1

        def mock_read(s="gemini", u="antigravity"):
            return keyring_val

        def mock_write(s="gemini", u="antigravity", secret=""):
            nonlocal keyring_val
            keyring_val = secret
            return True

        def mock_delete(s="gemini", u="antigravity"):
            nonlocal keyring_val
            keyring_val = None
            return True

        class FakeProc:
            returncode = 0

        with patch("gemini_accounts.manager.gemini_binary", return_value="agy"), \
             patch("gemini_accounts.manager.binary_supports_login", return_value=False), \
             patch("gemini_accounts.manager.read_keyring_credential", side_effect=mock_read), \
             patch("gemini_accounts.manager.write_keyring_credential", side_effect=mock_write), \
             patch("gemini_accounts.manager.delete_keyring_credential", side_effect=mock_delete), \
             patch("gemini_accounts.native.read_keyring_credential", side_effect=mock_read), \
             patch("gemini_accounts.native.write_keyring_credential", side_effect=mock_write), \
             patch("gemini_accounts.native.delete_keyring_credential", side_effect=mock_delete), \
             patch("subprocess.run", return_value=FakeProc()):
            code = add_account(self.store)
            self.assertNotEqual(code, 0)

        state = self.store.read()
        self.assertEqual(len(state["accounts"]), 1)
        self.assertIn("id1", state["accounts"])
        self.assertEqual(state["accounts"]["id1"]["email"], "first@example.com")
        self.assertEqual(keyring_val, cred1)

    def test_add_account_duplicate_email_rejected(self):
        from gemini_accounts.state import AccountError

        home1 = self.root / "acc_dup1"
        home1.mkdir(parents=True)
        header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').decode("ascii").rstrip("=")
        payload = base64.urlsafe_b64encode(b'{"email":"dup@example.com"}').decode("ascii").rstrip("=")
        jwt = f"{header}.{payload}.sig"
        cred = json.dumps({"id_token": jwt, "token": {"access_token": "tok"}})
        (home1 / "keyring_token.json").write_text(cred, encoding="utf-8")

        with self.store.edit() as state:
            state["accounts"]["id_dup1"] = {"home": str(home1), "email": "dup@example.com", "kind": "google"}
            state["selected"] = "id_dup1"

        keyring_val = cred

        def mock_read(s="gemini", u="antigravity"):
            return keyring_val

        def mock_write(s="gemini", u="antigravity", secret=""):
            nonlocal keyring_val
            keyring_val = secret
            return True

        def mock_delete(s="gemini", u="antigravity"):
            nonlocal keyring_val
            keyring_val = None
            return True

        class FakeProc:
            returncode = 0
            def __init__(self):
                nonlocal keyring_val
                keyring_val = cred

        with patch("gemini_accounts.manager.gemini_binary", return_value="agy"), \
             patch("gemini_accounts.manager.binary_supports_login", return_value=False), \
             patch("gemini_accounts.manager.read_keyring_credential", side_effect=mock_read), \
             patch("gemini_accounts.manager.write_keyring_credential", side_effect=mock_write), \
             patch("gemini_accounts.manager.delete_keyring_credential", side_effect=mock_delete), \
             patch("gemini_accounts.native.read_keyring_credential", side_effect=mock_read), \
             patch("gemini_accounts.native.write_keyring_credential", side_effect=mock_write), \
             patch("gemini_accounts.native.delete_keyring_credential", side_effect=mock_delete), \
             patch("subprocess.run", side_effect=lambda *a, **k: FakeProc()):
            with self.assertRaises(AccountError) as ctx:
                add_account(self.store)
            self.assertIn("already in the account list", str(ctx.exception))

        state = self.store.read()
        self.assertEqual(len(state["accounts"]), 1)

    def test_consolidated_prepare_home_links_all_directories_and_files(self):
        from gemini_accounts.shared import DIRECTORIES, SHARED_FILES, configured_home, prepare_home

        home = self.root / "acc_consolidated"
        home.mkdir()
        with self.store.edit() as state:
            state["accounts"]["acc_cons"] = {"home": str(home), "email": "test@example.com"}

        target = prepare_home(self.store, home)
        self.assertTrue(target.is_dir())

        # Verify configured_home finds the shared target
        self.assertEqual(configured_home(home), target)

        # Verify all directories exist in target
        for d in DIRECTORIES:
            self.assertTrue((target / d).is_dir(), f"Missing directory {d}")
            self.assertTrue((home / d).exists(), f"Account home missing link {d}")

        # Verify config files exist in target and home
        for f in ("settings.json", "config.toml"):
            self.assertTrue((target / f).is_file())
            self.assertTrue((home / f).exists())

        # Repeated calls should be idempotent and fast
        target2 = prepare_home(self.store, home)
        self.assertEqual(target, target2)



if __name__ == "__main__":
    unittest.main()

