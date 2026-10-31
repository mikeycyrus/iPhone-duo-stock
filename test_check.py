#!/usr/bin/env python3
"""Offline tests for the stock checker. No network."""

import io
import json
import unittest
import urllib.error
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import check

NOW = datetime(2026, 10, 29, 19, 52, tzinfo=ZoneInfo("Australia/Melbourne"))


def _store(status="unavailable", quote="Currently unavailable"):
    return {"status": status, "text": quote}


def _all(status="unavailable"):
    return {store_id: _store(status) for store_id, _, _ in check.STORES}


def _observations(status="unavailable", parts=None):
    chosen = parts or [part for part, _, _, _ in check.PARTS]
    return {part: _all(status) for part in chosen}


def _baseline(status="unavailable"):
    return {
        "status_updated": "2026-10-29T19:00:00+11:00",
        "last_success": "2026-10-29T19:40:00+11:00",
        "statuses": {part: {store_id: status for store_id, _, _ in check.STORES} for part, _, _, _ in check.PARTS},
    }


class LogicTests(unittest.TestCase):
    def test_available_prefix(self):
        self.assertTrue(check.is_available("available"))
        self.assertTrue(check.is_available("Available today"))
        self.assertFalse(check.is_available("unavailable"))
        self.assertFalse(check.is_available("ineligible"))
        self.assertFalse(check.is_available(None))

    def test_topic_missing_invalid_and_url(self):
        self.assertEqual(check.normalize_topic(None), (None, "missing"))
        self.assertEqual(check.normalize_topic("  "), (None, "missing"))
        self.assertEqual(check.normalize_topic("https://ntfy.sh/my-phone_1"), ("my-phone_1", None))
        self.assertEqual(check.normalize_topic("https://example.com/secret"), (None, "invalid"))
        self.assertEqual(check.normalize_topic("has space"), (None, "invalid"))
        self.assertEqual(check.normalize_topic("a" * 65), (None, "invalid"))

    def test_no_alert_on_first_baseline_even_if_in_stock(self):
        observations = _observations("unavailable")
        observations["MK2D4X/A"]["R504"] = _store("available", "Available today")
        built = check.build_result(None, observations, [], NOW)
        self.assertEqual(built["arrivals"], [])
        self.assertTrue(built["data"]["any_in_stock"])

    def test_alert_only_on_transition_to_available(self):
        previous = _baseline("unavailable")
        observations = _observations("unavailable")
        observations["MK2F4X/A"]["R180"] = _store("available", "Available today at Apple Chadstone")
        observations["MK2D4X/A"]["R504"] = _store("available", "Available today")
        # Ongoing stock must not alert again.
        previous["statuses"]["MK2D4X/A"]["R504"] = "available"
        previous["alerted"] = {
            part: dict(stores) for part, stores in previous["statuses"].items()
        }
        built = check.build_result(previous, observations, [], NOW)
        self.assertEqual(
            [(item["part"], item["store_id"]) for item in built["arrivals"]],
            [("MK2F4X/A", "R180")],
        )

    def test_missing_previous_cell_can_alert_after_baseline_exists(self):
        previous = _baseline("unavailable")
        del previous["statuses"]["MK2F4X/A"]
        observations = {"MK2F4X/A": _all("available")}
        built = check.build_result(previous, observations, [], NOW)
        self.assertEqual(len(built["arrivals"]), 5)
        self.assertEqual(len(built["transitions"]), 5)
        self.assertTrue(all(event["change"] == "in_stock" for event in built["transitions"]))

    def test_unsent_arrival_stays_pending_without_rewriting_history(self):
        previous = _baseline("unavailable")
        observations = _observations("unavailable")
        observations["MK2D4X/A"]["R342"] = _store("available", "Available today at Apple Doncaster")
        built = check.build_result(previous, observations, [], NOW)
        self.assertEqual([(item["store"]) for item in built["arrivals"]], ["Doncaster"])
        self.assertEqual(built["transitions"][0]["change"], "in_stock")
        self.assertEqual(built["transitions"][0]["store_id"], "R342")
        self.assertEqual(built["data"]["models"][0]["stores"]["R342"]["last_seen_display"], "7:52pm")
        held = check.hold_alerted(previous, built["baseline"], built["arrivals"])
        self.assertEqual(held["statuses"]["MK2D4X/A"]["R342"], "available")
        self.assertEqual(held["alerted"]["MK2D4X/A"]["R342"], "unavailable")
        again = check.build_result(held, observations, [], NOW)
        self.assertEqual(again["transitions"], [])
        self.assertEqual([(item["store"]) for item in again["arrivals"]], ["Doncaster"])

    def test_out_of_stock_is_history_not_an_alert(self):
        previous = _baseline("available")
        previous["last_seen"] = {
            part: {store_id: "2026-10-29T19:40:00+11:00" for store_id, _, _ in check.STORES}
            for part, _, _, _ in check.PARTS
        }
        built = check.build_result(previous, _observations("unavailable"), [], NOW)
        self.assertEqual(built["arrivals"], [])
        self.assertEqual(len(built["transitions"]), len(check.PARTS) * len(check.STORES))
        self.assertTrue(all(event["change"] == "out_of_stock" for event in built["transitions"]))
        self.assertEqual(built["data"]["models"][0]["stores"]["R504"]["last_seen_display"], "7:40pm")

    def test_history_merges_and_scheduler_waits(self):
        first = {"at": "2026-10-29T19:00:00+11:00", "part": "MK2D4X/A", "store_id": "R504", "change": "in_stock"}
        second = {"at": "2026-10-29T19:05:00+11:00", "part": "MK2D4X/A", "store_id": "R504", "change": "out_of_stock"}
        merged = check.merge_events([second], [first, second])
        self.assertEqual([event["change"] for event in merged], ["in_stock", "out_of_stock"])
        self.assertEqual(check.seconds_until_next(1000, 1020, 60), 40)
        self.assertEqual(check.seconds_until_next(1000, 1075, 60), 0)

    def test_sold_out_does_not_alert(self):
        previous = _baseline("available")
        built = check.build_result(previous, _observations("unavailable"), [], NOW)
        self.assertEqual(built["arrivals"], [])
        self.assertFalse(built["data"]["any_in_stock"])

    def test_failed_part_keeps_old_status_and_does_not_alert(self):
        previous = _baseline("unavailable")
        previous["statuses"]["MK2F4X/A"]["R530"] = "available"
        observations = _observations("available")
        del observations["MK2F4X/A"]
        errors = [
            {
                "part": "MK2F4X/A",
                "blocked": True,
                "status": 403,
                "message": check.plain_http_message(403, True),
            }
        ]
        built = check.build_result(previous, observations, errors, NOW)
        self.assertNotIn("MK2F4X/A", {item["part"] for item in built["arrivals"]})
        self.assertEqual(built["baseline"]["statuses"]["MK2F4X/A"]["R530"], "available")
        cell = next(model for model in built["data"]["models"] if model["part"] == "MK2F4X/A")["stores"]["R530"]
        self.assertTrue(cell["stale"])
        self.assertEqual(cell["status"], "available")
        self.assertEqual(built["data"]["problem"]["kind"], "blocked")
        self.assertFalse(built["data"]["problem"]["stale"])

    def test_total_block_keeps_grid_and_says_so(self):
        previous = _baseline("unavailable")
        errors = [
            {"part": part, "blocked": True, "status": 403, "message": check.plain_http_message(403, True)}
            for part, _, _, _ in check.PARTS
        ]
        built = check.build_result(previous, {}, errors, NOW)
        self.assertTrue(built["total_failure"])
        self.assertTrue(built["data"]["problem"]["stale"])
        self.assertIn("HTTP 403", built["data"]["problem"]["text"])
        self.assertIn("not that the phones are sold out", built["data"]["problem"]["text"])
        self.assertIn("last saved reading", built["data"]["problem"]["text"])
        self.assertEqual(built["data"]["models"][0]["stores"]["R504"]["status"], "unavailable")

    def test_total_block_without_history(self):
        errors = [
            {"part": part, "blocked": True, "status": 541, "message": check.plain_http_message(541, True)}
            for part, _, _, _ in check.PARTS
        ]
        built = check.build_result(None, {}, errors, NOW)
        self.assertIn("no earlier reading", built["data"]["problem"]["text"])
        self.assertEqual(built["data"]["models"][0]["stores"]["R504"]["status"], "unknown")

    def test_choose_newer_baseline(self):
        older = {"status_updated": "2026-10-29T10:00:00+11:00", "last_success": "2026-10-29T18:00:00+11:00", "statuses": {"MK2D4X/A": {"R504": "unavailable"}}}
        newer = {"status_updated": "2026-10-29T19:00:00+11:00", "last_success": "2026-10-29T19:00:00+11:00", "statuses": {"MK2D4X/A": {"R504": "available"}}}
        same_status_newer_check = {"status_updated": "2026-10-29T19:00:00+11:00", "last_success": "2026-10-29T19:30:00+11:00", "statuses": {"MK2D4X/A": {"R504": "available"}}}
        self.assertEqual(check.choose_baseline(older, newer)["statuses"]["MK2D4X/A"]["R504"], "available")
        self.assertEqual(check.choose_baseline(newer, older)["statuses"]["MK2D4X/A"]["R504"], "available")
        self.assertEqual(check.choose_baseline(newer, same_status_newer_check)["last_success"], same_status_newer_check["last_success"])
        self.assertFalse(check.statuses_differ(newer, same_status_newer_check))
        self.assertTrue(check.statuses_differ(older, newer))

    def test_notification_copy(self):
        one = [
            {
                "name": "iPhone Duo 256GB Star White",
                "store": "Highpoint",
                "buy_url": check.PARTS[0][3],
                "text": "Available today at Apple Highpoint",
            }
        ]
        title, body, click, actions = check.format_notification(one, "https://mikeycyrus.github.io/iphone-duo-stock/")
        self.assertIn("Highpoint", title)
        self.assertIn("Star White", body)
        self.assertIn("Available today", body)
        self.assertEqual(click, check.PARTS[0][3])
        self.assertIn("https://mikeycyrus.github.io/iphone-duo-stock/", actions)
        many = one + [
            {
                "name": "iPhone Duo 512GB Star White",
                "store": "Doncaster",
                "buy_url": check.PARTS[1][3],
                "text": "",
            }
        ]
        title, body, click, _actions = check.format_notification(many, "https://mikeycyrus.github.io/iphone-duo-stock/")
        self.assertIn("2 stores", title)
        self.assertEqual(click, "https://mikeycyrus.github.io/iphone-duo-stock/")
        self.assertIn("Doncaster", body)

    def test_alert_fields_do_not_echo_topic(self):
        fields = check.alert_fields("https://ntfy.sh/super-secret-topic", "none")
        self.assertEqual(fields["alerts"], "on")
        self.assertNotIn("super-secret-topic", json.dumps(fields))
        self.assertEqual(check.alert_fields("", "none")["alerts"], "off")
        self.assertEqual(check.alert_fields("not a topic", "error")["alerts"], "invalid")

    def test_send_ntfy_posts_to_topic_host_only(self):
        class Fake:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"ok"

        with patch("urllib.request.urlopen", return_value=Fake()) as opened:
            check.send_ntfy(
                "unit-test-topic",
                "iPhone Duo 256GB Star White in stock at Highpoint",
                "**iPhone Duo 256GB Star White** is available for pickup at **Highpoint**.",
                check.PARTS[0][3],
                "view, Stock page, https://mikeycyrus.github.io/iphone-duo-stock/",
            )
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "https://ntfy.sh/unit-test-topic")
        self.assertIn("Highpoint", request.data.decode())
        self.assertEqual(request.get_header("Click"), check.PARTS[0][3])

    def test_interpret_block_and_html(self):
        blocked = check.interpret_response(403, b"nope", "MK2D4X/A")
        self.assertTrue(blocked["blocked"])
        self.assertIn("HTTP 403", blocked["message"])
        html = check.interpret_response(200, b"<html><title>Access Denied</title></html>", "MK2D4X/A")
        self.assertTrue(html["blocked"])
        payload = {
            "head": {"status": "200"},
            "body": {
                "stores": [
                    {
                        "storeNumber": "R504",
                        "storeName": "Highpoint",
                        "partsAvailability": {
                            "MK2D4X/A": {"pickupDisplay": "unavailable", "pickupSearchQuote": "Currently unavailable"}
                        },
                    },
                    {
                        "storeNumber": "R999",
                        "storeName": "Ignored",
                        "partsAvailability": {"MK2D4X/A": {"pickupDisplay": "available"}},
                    },
                ]
            },
        }
        only_other = {
            "head": {"status": "200"},
            "body": {"stores": payload["body"]["stores"][1:]},
        }
        parsed = check.interpret_response(200, json.dumps(only_other).encode(), "MK2D4X/A")
        self.assertFalse(parsed["ok"])
        self.assertIn("did not include the Melbourne stores", parsed["message"])
        for store_id, _, _ in check.STORES:
            payload["body"]["stores"].append(
                {
                    "storeNumber": store_id,
                    "partsAvailability": {
                        "MK2D4X/A": {"pickupDisplay": "unavailable", "pickupSearchQuote": "Currently unavailable"}
                    },
                }
            )
        parsed = check.interpret_response(200, json.dumps(payload).encode(), "MK2D4X/A")
        self.assertTrue(parsed["ok"])
        self.assertNotIn("R999", parsed["stores"])

    def test_last_seen_on_another_day_stays_short(self):
        previous = _baseline("unavailable")
        previous["last_seen"] = {"MK2D4X/A": {"R504": "2026-10-28T09:05:00+11:00"}}
        built = check.build_result(previous, _observations("unavailable"), [], NOW)
        self.assertEqual(built["data"]["models"][0]["stores"]["R504"]["last_seen_display"], "28 Oct 9:05am")

    def test_last_success_display_uses_melbourne_zone_name(self):
        self.assertEqual(check._display_time(datetime.fromisoformat("2026-10-29T19:40:00+11:00")), "Thu 29 Oct 2026, 7:40 PM AEDT")
        self.assertEqual(check._display_time(datetime.fromisoformat("2026-06-15T12:00:00+10:00")), "Mon 15 Jun 2026, 12:00 PM AEST")
        previous = _baseline("unavailable")
        errors = [
            {"part": part, "blocked": True, "status": 541, "message": check.plain_http_message(541, True)}
            for part, _, _, _ in check.PARTS
        ]
        built = check.build_result(previous, {}, errors, NOW)
        self.assertEqual(built["data"]["last_success_display"], "Thu 29 Oct 2026, 7:40 PM AEDT")
        self.assertNotIn("UTC+", built["data"]["last_success_display"])
        self.assertIn("AEDT", built["data"]["problem"]["text"])
        published = {
            "last_success": "2026-10-29T18:00:00+11:00",
            "last_success_display": "Thu 29 Oct 2026, 6:00 PM UTC+11:00",
            "problem": {
                "stale": True,
                "text": "Apple rejected this check (HTTP 541). The grid below is the last saved reading from Thu 29 Oct 2026, 6:00 PM UTC+11:00.",
            },
        }
        check.apply_published_time(published, datetime.fromisoformat("2026-10-29T19:40:00+11:00"))
        self.assertEqual(published["last_success_display"], "Thu 29 Oct 2026, 7:40 PM AEDT")
        self.assertNotIn("UTC+", published["problem"]["text"])

    def _http_error(self, status, body=b"nope"):
        return urllib.error.HTTPError(
            "https://www.apple.com/au/shop/retail/pickup-message",
            status,
            "error",
            hdrs=None,
            fp=io.BytesIO(body),
        )

    def _pickup_body(self, part="MK2D4X/A"):
        stores = []
        for store_id, _, _ in check.STORES:
            stores.append(
                {
                    "storeNumber": store_id,
                    "partsAvailability": {
                        part: {"pickupDisplay": "unavailable", "pickupSearchQuote": "Currently unavailable"}
                    },
                }
            )
        return json.dumps({"head": {"status": "200"}, "body": {"stores": stores}}).encode()

    def test_http_541_retries_once_before_success(self):
        class FakeResponse:
            status = 200

            def __init__(self, body):
                self._body = body

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with patch("urllib.request.urlopen", side_effect=[self._http_error(541), FakeResponse(self._pickup_body())]) as opened, patch(
            "time.sleep"
        ) as slept, patch("random.randint", return_value=7) as wait:
            result = check.fetch_part("MK2D4X/A")
        self.assertTrue(result["ok"])
        self.assertEqual(opened.call_count, 2)
        wait.assert_called_once_with(5, 10)
        slept.assert_called_once_with(7)

    def test_http_541_is_recorded_when_the_retry_fails(self):
        with patch("urllib.request.urlopen", side_effect=[self._http_error(541), self._http_error(541)]) as opened, patch(
            "time.sleep"
        ) as slept, patch("random.randint", return_value=9):
            result = check.fetch_part("MK2D4X/A")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 541)
        self.assertIn("HTTP 541", result["message"])
        self.assertEqual(opened.call_count, 2)
        slept.assert_called_once_with(9)

    def test_other_failures_keep_their_existing_retry(self):
        waits = [check._retry_wait(541) for _ in range(30)]
        self.assertTrue(all(5 <= wait <= 10 for wait in waits))
        self.assertIsNone(check._retry_wait(403))
        self.assertEqual(check._retry_wait(429), 1.5)
        with patch("urllib.request.urlopen", side_effect=[self._http_error(403)]) as opened, patch("time.sleep") as slept:
            result = check.fetch_part("MK2D4X/A")
        self.assertEqual(result["status"], 403)
        self.assertEqual(opened.call_count, 1)
        slept.assert_not_called()

    def test_fulfillment_endpoint_is_not_used(self):
        source = (check.ROOT / "check.py").read_text(encoding="utf-8")
        self.assertNotRegex(source, r"https://\S*fulfillment-messages")
        self.assertIn("/shop/retail/pickup-message", source)

    def test_part_and_store_order(self):
        catalog = [part for part, _, _, _ in check.CATALOG]
        self.assertEqual(
            catalog,
            [
                "MK2D4X/A",
                "MK2E4X/A",
                "MK2F4X/A",
                "MK2G4X/A",
                "MK2H4X/A",
                "MK2J4X/A",
                "MK2K4X/A",
                "MK2L4X/A",
            ],
        )
        self.assertEqual([part for part, _, _, _ in check.PARTS], ["MK2D4X/A", "MK2F4X/A"])
        self.assertEqual(check.WATCHED_PARTS, ("MK2D4X/A", "MK2F4X/A"))
        self.assertEqual([store_id for store_id, _, _ in check.STORES], ["R504", "R180", "R342", "R343", "R530"])
        colors = [color for _, _, color, _ in check.CATALOG]
        self.assertEqual(colors[0::2], ["Star White", "Star White", "Star White", "Star White"])
        self.assertEqual(colors[1::2], ["Night Sky", "Night Sky", "Night Sky", "Night Sky"])


if __name__ == "__main__":
    unittest.main()
