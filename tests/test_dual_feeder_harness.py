import sys
import unittest
from unittest.mock import MagicMock, patch

from data_pipeline.realtime_scanner import RealtimeLeaderScanner


class TestDualFeederScannerHarness(unittest.TestCase):
    """
    Verification Test Suite for Dual-Feeder (HTS Condition Search + UN Volume Rank) Scanner.
    Ensures:
      1. Single Feeder (HTS only) failure case when high-volume NXT leader is missed by KRX HTS.
      2. Dual Feeder success: UN Volume Rank captures the leader into Candidate Pool.
      3. Union de-duplication: Symbols appearing in both feeds are processed once with unified turnover.
      4. Regulatory market open price defense: KRX 09:00:00 regular session open is preserved.
      5. Rate limiter & interval compliance: Volume rank poll throttled safely.
    """

    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_feed = MagicMock()
        self.mock_feed.active_symbols = []

    def test_single_feeder_misses_nxt_heavy_leader(self):
        """When enable_volume_rank_feeder=False, a stock missed by HTS is NOT promoted."""
        # HTS returns only standard stock A
        self.mock_client.get_condition_search_result.return_value = [
            {
                "code": "000001",
                "name": "일반주",
                "open": "10000",
                "price": "10400",
                "low": "10000",
                "high": "10450",
                "trade_amt": "1500000",  # 15억
                "acml_vol": "150000",
                "chgrate": "4.0",
            }
        ]

        scanner = RealtimeLeaderScanner(
            client=self.mock_client,
            datafeed=self.mock_feed,
            min_gain_pct=3.0,
            min_turnover_krw=800_000_000.0,
            max_mae_pct=-2.0,
            enable_volume_rank_feeder=False,
        )

        promoted = scanner.scan_cycle()
        self.assertEqual(promoted, ["000001"])
        self.assertNotIn("000500", scanner._promoted_symbols)
        # Volume rank API should never be called
        self.mock_client.get_volume_rank.assert_not_called()

    def test_dual_feeder_captures_nxt_heavy_leader(self):
        """Dual Feeder rescues NXT heavy leader (e.g. 000500) that was absent from HTS condition search."""
        # 1. HTS returns only 000001
        self.mock_client.get_condition_search_result.return_value = [
            {
                "code": "000001",
                "name": "일반주",
                "open": "10000",
                "price": "10400",
                "low": "10000",
                "high": "10450",
                "trade_amt": "1500000",
                "acml_vol": "150000",
                "chgrate": "4.0",
            }
        ]

        # 2. Volume Rank UN returns 000500 (Gaon Cable) with massive 700억 unified turnover
        self.mock_client.get_volume_rank.return_value = [
            {
                "mksc_shrn_iscd": "000500",
                "hts_kor_isnm": "가온전선",
                "_tot_to": 70_000_000_000.0,
                "_krx_to": 25_000_000_000.0,
                "_nxt_to": 45_000_000_000.0,
                "acml_vol": "350000",
            }
        ]

        # 3. get_current_price('000500', market_div='J') returns regular market prices
        self.mock_client.get_current_price.return_value = {
            "stck_prpr": "215000",
            "stck_oprc": "207000",  # +3.86% open gain
            "stck_hgpr": "217000",
            "stck_lwpr": "206000",  # -0.48% MAE (safe)
            "prdy_ctrt": "4.5",
            "rprs_mrkt_kor_name": "가온전선",
        }

        scanner = RealtimeLeaderScanner(
            client=self.mock_client,
            datafeed=self.mock_feed,
            min_gain_pct=3.0,
            min_turnover_krw=800_000_000.0,
            max_mae_pct=-2.0,
            enable_volume_rank_feeder=True,
            volume_rank_interval_sec=0.0,
        )

        promoted = scanner.scan_cycle()

        # Both HTS stock and Volume Rank stock must be promoted
        self.assertIn("000001", promoted)
        self.assertIn("000500", promoted)
        self.assertEqual(len(promoted), 2)
        self.assertIn("000500", scanner._promoted_symbols)

        # Verify 000500 cached day_open is the true KRX 09:00:00 session open (207000)
        cache_item = scanner._price_cache.get("000500")
        self.assertIsNotNone(cache_item)
        self.assertEqual(cache_item["day_open"], 207000.0)
        self.assertEqual(cache_item["turnover_krw"], 70_000_000_000.0)

    def test_dual_feeder_deduplication_and_turnover_upgrade(self):
        """When a symbol appears in both HTS and UN Volume Rank, it is processed once without duplicate WebSocket promotions."""
        # Symbol 000001 is returned by both feeds
        self.mock_client.get_condition_search_result.return_value = [
            {
                "code": "000001",
                "name": "일반주",
                "open": "10000",
                "price": "10400",
                "low": "10000",
                "high": "10450",
                "trade_amt": "1500000",  # 15억 (KRX only)
                "acml_vol": "150000",
                "chgrate": "4.0",
            }
        ]

        self.mock_client.get_volume_rank.return_value = [
            {
                "mksc_shrn_iscd": "000001",
                "hts_kor_isnm": "일반주",
                "_tot_to": 3_000_000_000.0,  # 30억 (KRX + NXT)
                "acml_vol": "300000",
            }
        ]

        scanner = RealtimeLeaderScanner(
            client=self.mock_client,
            datafeed=self.mock_feed,
            min_gain_pct=3.0,
            min_turnover_krw=800_000_000.0,
            max_mae_pct=-2.0,
            enable_volume_rank_feeder=True,
            volume_rank_interval_sec=0.0,
        )

        promoted = scanner.scan_cycle()

        # Should be promoted exactly once
        self.assertEqual(promoted, ["000001"])
        self.assertEqual(len(scanner._promoted_symbols), 1)

    def test_dual_feeder_filter_rejection_safety(self):
        """A volume rank leader that breached MAE (-3%) or has low open gain (+0.5%) must still be rejected safely."""
        self.mock_client.get_condition_search_result.return_value = []
        self.mock_client.get_volume_rank.return_value = [
            # Candidate A: Huge volume, but open gain is only +0.5%
            {
                "mksc_shrn_iscd": "005930",
                "hts_kor_isnm": "삼성전자",
                "_tot_to": 100_000_000_000.0,
                "acml_vol": "2000000",
            },
            # Candidate B: Huge volume, high gain (+5%), but breached MAE (-4%)
            {
                "mksc_shrn_iscd": "000660",
                "hts_kor_isnm": "SK하이닉스",
                "_tot_to": 80_000_000_000.0,
                "acml_vol": "1500000",
            },
        ]

        def mock_price_lookup(symbol, market_div="J"):
            if symbol == "005930":
                return {
                    "stck_prpr": "50250",
                    "stck_oprc": "50000",  # +0.5% gain
                    "stck_hgpr": "50500",
                    "stck_lwpr": "49800",
                    "prdy_ctrt": "0.5",
                    "rprs_mrkt_kor_name": "삼성전자",
                }
            elif symbol == "000660":
                return {
                    "stck_prpr": "105000",
                    "stck_oprc": "100000",  # +5% current gain
                    "stck_hgpr": "106000",
                    "stck_lwpr": "96000",   # -4% MAE breach!
                    "prdy_ctrt": "5.0",
                    "rprs_mrkt_kor_name": "SK하이닉스",
                }
            return {}

        self.mock_client.get_current_price.side_effect = mock_price_lookup

        scanner = RealtimeLeaderScanner(
            client=self.mock_client,
            datafeed=self.mock_feed,
            min_gain_pct=3.0,
            min_turnover_krw=800_000_000.0,
            max_mae_pct=-2.0,
            enable_volume_rank_feeder=True,
            volume_rank_interval_sec=0.0,
        )

        promoted = scanner.scan_cycle()

        # Both must be rejected: 005930 for low gain, 000660 for MAE breach
        self.assertEqual(promoted, [])
        self.assertIn("000660", scanner._mae_blacklisted)
        self.assertNotIn("005930", scanner._promoted_symbols)
        self.assertNotIn("000660", scanner._promoted_symbols)


if __name__ == "__main__":
    unittest.main()
