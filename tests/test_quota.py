import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from gemini_accounts.manager import describe_quota, quota_labels
from gemini_accounts.state import AccountError


class QuotaTests(unittest.TestCase):
    def test_window_duration_identifies_weekly_only_and_reordered_limits(self):
        weekly = {"usedPercent": 60, "windowDurationMins": 10080}
        self.assertEqual(
            describe_quota({"rateLimits": {"primary": weekly, "secondary": None}}),
            "5h: unavailable | weekly: 40% left",
        )
        self.assertEqual(
            describe_quota(
                {
                    "rateLimits": {
                        "primary": weekly,
                        "secondary": {"usedPercent": 5, "windowDurationMins": 300},
                    }
                }
            ),
            "5h: 95% left | weekly: 40% left",
        )

    def test_gemini_bucket_takes_precedence_over_other_model_limits(self):
        other = {
            "limitId": "gemini_other",
            "primary": {"usedPercent": 10, "windowDurationMins": 300},
        }
        self.assertEqual(
            describe_quota(
                {
                    "rateLimits": other,
                    "rateLimitsByLimitId": {
                        "gemini_other": other,
                        "gemini": {
                            "primary": {"usedPercent": 80, "windowDurationMins": 300}
                        },
                    },
                }
            ),
            "5h: 20% left | weekly: unavailable",
        )
        self.assertEqual(
            describe_quota({"rateLimits": other}),
            "5h: unavailable | weekly: unavailable",
        )

    def test_missing_and_malformed_usage_never_claims_available_quota(self):
        unavailable = "5h: unavailable | weekly: unavailable"
        for result in (None, {}, {"rateLimits": None}, {"rateLimits": []}):
            with self.subTest(result=result):
                self.assertEqual(describe_quota(result), unavailable)
        for used in (None, True, "50", "\x1b[2J", [], {}, float("nan")):
            with self.subTest(used=used):
                self.assertEqual(
                    describe_quota(
                        {
                            "rateLimits": {
                                "primary": {
                                    "usedPercent": used,
                                    "windowDurationMins": 300,
                                }
                            }
                        }
                    ),
                    unavailable,
                )
        for duration in (None, True, "300", 15, [], {}):
            with self.subTest(duration=duration):
                self.assertEqual(
                    describe_quota(
                        {
                            "rateLimits": {
                                "primary": {
                                    "usedPercent": 0,
                                    "windowDurationMins": duration,
                                }
                            }
                        }
                    ),
                    unavailable,
                )

    def test_remaining_percent_is_clamped_at_empty_and_full(self):
        self.assertEqual(
            describe_quota(
                {
                    "rateLimits": {
                        "primary": {"usedPercent": 120, "windowDurationMins": 300},
                        "secondary": {"usedPercent": -5, "windowDurationMins": 10080},
                    }
                }
            ),
            "5h: 0% left | weekly: 100% left",
        )

    def test_parallel_lookups_are_bounded_and_failures_are_isolated(self):
        with tempfile.TemporaryDirectory() as temporary:
            accounts = {}
            for number in range(9):
                home = Path(temporary) / str(number)
                home.mkdir()
                accounts[str(number)] = {"home": str(home), "kind": "google"}
            active = peak = 0

            async def lookup(binary, home):
                nonlocal active, peak
                self.assertEqual(binary, "gemini-fixture")
                active += 1
                peak = max(peak, active)
                try:
                    await asyncio.sleep(0.01)
                    if home.name == "0":
                        raise AccountError("Quota unavailable")
                    return {
                        "rateLimits": {
                            "primary": {
                                "usedPercent": int(home.name),
                                "windowDurationMins": 300,
                            }
                        }
                    }
                finally:
                    active -= 1

            with (
                patch(
                    "gemini_accounts.manager.gemini_binary", return_value="gemini-fixture"
                ),
                patch("gemini_accounts.manager.read_rate_limits", side_effect=lookup),
            ):
                labels = quota_labels(accounts)

            self.assertGreater(peak, 1)
            self.assertLessEqual(peak, 4)
            self.assertEqual(labels["0"], "5h: unavailable | weekly: unavailable")
            for number in range(1, 9):
                self.assertEqual(
                    labels[str(number)],
                    f"5h: {100 - number}% left | weekly: unavailable",
                )

    def test_unsupported_accounts_and_missing_homes_skip_lookup(self):
        with tempfile.TemporaryDirectory() as temporary:
            accounts = {
                "signed-out": {"home": temporary, "kind": "signedOut"},
                "api-key": {"home": temporary, "kind": "apiKey"},
                "missing": {
                    "home": str(Path(temporary) / "missing"),
                    "kind": "google",
                },
            }
            with (
                patch(
                    "gemini_accounts.manager.gemini_binary", return_value="gemini-fixture"
                ),
                patch(
                    "gemini_accounts.manager.read_rate_limits", new_callable=AsyncMock
                ) as lookup,
            ):
                labels = quota_labels(accounts)
            lookup.assert_not_awaited()
            self.assertTrue(
                all(
                    label == "5h: unavailable | weekly: unavailable"
                    for label in labels.values()
                )
            )


    def test_gemini_groups_and_buckets_describe_quota(self):
        gemini_summary = {
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "buckets": [
                        {"window": "5h", "remainingFraction": 0.65},
                        {"window": "weekly", "remainingFraction": 0.94},
                    ],
                }
            ]
        }
        self.assertEqual(
            describe_quota(gemini_summary),
            "5h: 65% left | weekly: 94% left",
        )

    def test_gemini_models_group_takes_priority_over_other_groups(self):
        summary = {
            "groups": [
                {
                    "displayName": "Claude and GPT models",
                    "buckets": [
                        {"bucketId": "3p-5h", "window": "5h", "remainingFraction": 1.0},
                        {"bucketId": "3p-weekly", "window": "weekly", "remainingFraction": 1.0},
                    ],
                },
                {
                    "displayName": "Gemini Models",
                    "buckets": [
                        {"bucketId": "gemini-5h", "window": "5h", "remainingFraction": 1.0},
                        {"bucketId": "gemini-weekly", "window": "weekly", "remainingFraction": 0.8058},
                    ],
                },
            ]
        }
        self.assertEqual(
            describe_quota(summary),
            "5h: 100% left | weekly: 81% left",
        )

    def test_quota_fraction_edge_clamping(self):
        summary_almost_full = {
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "buckets": [
                        {"window": "5h", "remainingFraction": 0.999},
                        {"window": "weekly", "remainingFraction": 0.001},
                    ],
                }
            ]
        }
        self.assertEqual(
            describe_quota(summary_almost_full),
            "5h: 99% left | weekly: 1% left",
        )

        summary_exact = {
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "buckets": [
                        {"window": "5h", "remainingFraction": 1.0},
                        {"window": "weekly", "remainingFraction": 0.0},
                    ],
                }
            ]
        }
        self.assertEqual(
            describe_quota(summary_exact),
            "5h: 100% left | weekly: 0% left",
        )

    def test_bucket_id_fallback_when_window_missing(self):
        summary = {
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "buckets": [
                        {"bucketId": "gemini-5h", "remainingFraction": 0.5},
                        {"bucketId": "gemini-weekly", "remainingFraction": 0.75},
                    ],
                }
            ]
        }
        self.assertEqual(
            describe_quota(summary),
            "5h: 50% left | weekly: 75% left",
        )

    def test_model_buckets_fallback_when_no_gemini_group(self):
        summary_3p_only = {
            "groups": [
                {
                    "displayName": "Claude and GPT models",
                    "buckets": [
                        {"bucketId": "3p-5h", "window": "5h", "remainingFraction": 1.0},
                        {"bucketId": "3p-weekly", "window": "weekly", "remainingFraction": 1.0},
                    ],
                }
            ],
            "modelBuckets": [
                {"modelId": "gemini-2.5-flash", "remainingFraction": 0.84},
                {"modelId": "gemini-3-flash", "remainingFraction": 0.84},
                {"modelId": "claude-3-5-sonnet", "remainingFraction": 1.0},
            ],
        }
        self.assertEqual(
            describe_quota(summary_3p_only),
            "5h: 84% left | weekly: 100% left",
        )

    def test_model_buckets_takes_lowest_gemini_fraction(self):
        summary = {
            "modelBuckets": [
                {"modelId": "gemini-2.5-flash", "remainingFraction": 0.85},
                {"modelId": "gemini-2.5-pro", "remainingFraction": 0.60},
            ]
        }
        self.assertEqual(
            describe_quota(summary),
            "5h: 60% left | weekly: 100% left",
        )

    def test_model_buckets_identifies_weekly_by_reset_time(self):
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        future_weekly = (now + datetime.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        summary = {
            "modelBuckets": [
                {
                    "modelId": "gemini-2.5-flash",
                    "remainingFraction": 0.27,
                    "resetTime": future_weekly,
                }
            ]
        }
        self.assertEqual(
            describe_quota(summary),
            "5h: 100% left | weekly: 27% left",
        )

    def test_model_buckets_identifies_5h_by_reset_time(self):
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        future_5h = (now + datetime.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        summary = {
            "modelBuckets": [
                {
                    "modelId": "gemini-2.5-flash",
                    "remainingFraction": 0.82,
                    "resetTime": future_5h,
                }
            ]
        }
        self.assertEqual(
            describe_quota(summary),
            "5h: 82% left | weekly: 100% left",
        )

    def test_model_buckets_both_5h_and_weekly(self):
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        future_5h = (now + datetime.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        future_weekly = (now + datetime.timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        summary = {
            "modelBuckets": [
                {
                    "modelId": "gemini-2.5-flash",
                    "remainingFraction": 0.80,
                    "resetTime": future_5h,
                },
                {
                    "modelId": "gemini-2.5-flash-lite",
                    "remainingFraction": 0.35,
                    "resetTime": future_weekly,
                },
            ]
        }
        self.assertEqual(
            describe_quota(summary),
            "5h: 80% left | weekly: 35% left",
        )

    def test_fetch_live_quota_queries_production_first(self):
        import io
        from gemini_accounts.native import _fetch_live_quota

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            tok_file = home / "keyring_token.json"
            tok_file.write_text(
                '{"token": {"access_token": "mock_token"}}', encoding="utf-8"
            )

            requested_urls = []

            def fake_urlopen(req, timeout=5):
                requested_urls.append(req.full_url)
                if "retrieveUserQuotaSummary" in req.full_url:
                    return io.BytesIO(b'{"groups": [{"displayName": "Claude and GPT models", "buckets": []}]}')
                return io.BytesIO(b'{"buckets": [{"modelId": "gemini-2.5-flash", "remainingFraction": 0.84}]}')

            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                result = _fetch_live_quota(home)
                self.assertIsNotNone(result)
                self.assertEqual(
                    requested_urls[0],
                    "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
                )
                self.assertEqual(
                    requested_urls[1],
                    "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota",
                )
                self.assertEqual(len(result["modelBuckets"]), 1)
                self.assertEqual(result["modelBuckets"][0]["modelId"], "gemini-2.5-flash")


    def test_list_accounts_with_show_quota(self):
        import io
        from unittest.mock import MagicMock
        from gemini_accounts.manager import list_accounts

        store = MagicMock()
        state = {
            "selected": "acc1",
            "accounts": {
                "acc1": {"email": "user1@example.com", "home": "/tmp/1", "kind": "google"},
            },
        }
        store.read.return_value = state
        store.rows.return_value = [("acc1", state["accounts"]["acc1"])]

        with (
            patch("gemini_accounts.manager.refresh", return_value=state),
            patch("gemini_accounts.manager.quota_labels", return_value={"acc1": "5h: 80% left | weekly: 90% left"}),
            patch("sys.stdout", new_callable=io.StringIO) as out,
        ):
            list_accounts(store, show_quota=True)
            output = out.getvalue()
            self.assertIn("5h: 80% left | weekly: 90% left", output)
            self.assertIn("user1@example.com", output)

    def test_describe_quota_antigravity_cli_snake_case_format(self):
        agy_usage_output = {
            "groups": [
                {
                    "name": "Gemini Models",
                    "description": "Models within this group: Gemini Flash, Gemini Pro",
                    "buckets": [
                        {
                            "id": "gemini-weekly",
                            "name": "Weekly Limit Remaining",
                            "window": "weekly",
                            "remaining_fraction": 0.3845,
                            "reset_time": "2026-10-25T11:36:18Z",
                        },
                        {
                            "id": "gemini-5h",
                            "name": "Five Hour Limit Remaining",
                            "window": "5h",
                            "remaining_fraction": 0.6193,
                            "reset_time": "2026-10-22T02:11:43Z",
                        },
                    ],
                },
                {
                    "name": "Claude and GPT models",
                    "buckets": [
                        {
                            "id": "3p-weekly",
                            "window": "weekly",
                            "remaining_fraction": 1.0,
                        }
                    ],
                },
            ]
        }
        self.assertEqual(
            describe_quota(agy_usage_output),
            "5h: 62% left | weekly: 38% left",
        )

    def test_fetch_cli_quota_success_and_failure(self):
        import subprocess
        from gemini_accounts.native import _fetch_cli_quota

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            # 1. Success case
            fake_stdout = (
                '{"conversation_id":"","status":"SUCCESS","command":{"name":"usage","data":{"groups":[{"name":"Gemini Models"}]}}}'
            )
            mock_proc = subprocess.CompletedProcess(
                args=["agy", "--output-format", "json", "--print", "/usage"],
                returncode=0,
                stdout=fake_stdout,
                stderr="",
            )
            with (
                patch("gemini_accounts.native.sync_keyring_from_home"),
                patch("subprocess.run", return_value=mock_proc),
            ):
                res = _fetch_cli_quota("agy", home)
                self.assertIsNotNone(res)
                self.assertIn("groups", res)
                self.assertEqual(res["groups"][0]["name"], "Gemini Models")

            # 2. Failure case (returncode != 0)
            mock_fail = subprocess.CompletedProcess(
                args=["agy"],
                returncode=1,
                stdout="",
                stderr="error",
            )
            with (
                patch("gemini_accounts.native.sync_keyring_from_home"),
                patch("subprocess.run", return_value=mock_fail),
            ):
                res = _fetch_cli_quota("agy", home)
                self.assertIsNone(res)

    def test_read_rate_limits_prefers_cli_quota(self):
        from gemini_accounts.native import read_rate_limits

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            cli_data = {"groups": [{"name": "Gemini Models"}]}
            with (
                patch("gemini_accounts.native._fetch_cli_quota", return_value=cli_data),
                patch("gemini_accounts.native._fetch_live_quota") as mock_live,
            ):
                loop = asyncio.new_event_loop()
                try:
                    res = loop.run_until_complete(read_rate_limits("agy", home))
                finally:
                    loop.close()
                self.assertEqual(res, cli_data)
                mock_live.assert_not_called()


if __name__ == "__main__":
    unittest.main()


