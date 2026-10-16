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
                resp = io.BytesIO(b'{"groups": [{"displayName": "Gemini Models", "buckets": []}]}')
                resp.read = resp.getvalue
                return io.BytesIO(b'{"groups": [{"displayName": "Gemini Models", "buckets": []}]}')

            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                result = _fetch_live_quota(home)
                self.assertIsNotNone(result)
                self.assertEqual(
                    requested_urls[0],
                    "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
                )


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


if __name__ == "__main__":
    unittest.main()


