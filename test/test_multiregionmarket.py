"""
Tests for the EUPHEMIA multi-area market-clearing service.

Trimmed to a lean smoke-test suite covering the core contract:
  - TestAreaMapping  : community-name -> bidding-area helper
  - TestBidBuilding  : net-position -> supply/demand bid translation (solver patched)
  - TestClearMarket  : one end-to-end run with the real OPtimiseMultiRegion solver
"""

import sys
import os
import unittest
from datetime import datetime
from unittest.mock import MagicMock

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from EuphemiaMarketClearing.euphemia_market_clearing import EuphemiaMarketClearing as MultiRegionMarket, _area_from_name, RETAIL_PRICE_EUR_MWH
from EuphemiaMarketClearing.euphemia_market_clearing_dataclasses import ClearMarketOutput


# ---------------------------------------------------------------------------
# Shared test data helpers
# ---------------------------------------------------------------------------

def _flat(val: float, n: int = 24) -> list[float]:
    return [val] * n


def _make_supply_df(price: float = 50.0, qty: float = 10.0) -> pd.DataFrame:
    """24-hour GRID supply at constant price and quantity."""
    return pd.DataFrame([
        {"ID": "GRID_import", "Area": "GRID", "Period": t,
         "Step": 1, "Price": price, "Quantity": qty,
         "RampUp": 1e6, "RampDown": 1e6}
        for t in range(1, 25)
    ])


def _make_demand_df() -> pd.DataFrame:
    return pd.DataFrame(columns=["ID", "Area", "Period", "Step", "Price", "Quantity"])


def _make_block_df() -> pd.DataFrame:
    return pd.DataFrame(columns=["ID", "Area", "Period", "Price", "Quantity"])


def _make_lines_df() -> pd.DataFrame:
    """MV ring topology matching scenario_andrei.esdl."""
    return pd.DataFrame([
        {"AreaFrom": "GRID", "AreaTo": "LV1", "FlowMax": 2.0, "FlowMin": -2.0},
        {"AreaFrom": "LV1",  "AreaTo": "LV2", "FlowMax": 2.0, "FlowMin": -2.0},
        {"AreaFrom": "LV2",  "AreaTo": "LV3", "FlowMax": 2.0, "FlowMin": -2.0},
        {"AreaFrom": "GRID", "AreaTo": "LV3", "FlowMax": 2.0, "FlowMin": -2.0},
    ])


# ESDL IDs matching scenario_andrei.esdl
AGG1_ID = "09da0f94-fa4c-456c-afa8-df313e3abcc0"  # agg_consumer_1 -> LV1
AGG2_ID = "512dface-1367-4f4d-9824-deab682e0a88"  # agg_consumer_2 -> LV2
AGG3_ID = "bfe23612-958e-41ad-8970-d0cbbb1814e3"  # agg_consumer_3 -> LV3


def _make_service() -> MultiRegionMarket:
    """
    Build a MultiRegionMarket instance with pre-loaded DataFrames,
    bypassing __init__ (which requires HELICS) and init_calculation_service
    (which requires a real ESDL energy system and the Excel file).
    """
    mock = MagicMock(spec=MultiRegionMarket)
    mock.df_supply_base = _make_supply_df()
    mock.df_demand_base = _make_demand_df()
    mock.df_block_orders = _make_block_df()
    mock.df_lines = _make_lines_df()
    mock.agg_id_to_community = {
        AGG1_ID: "agg_consumer_1",
        AGG2_ID: "agg_consumer_2",
        AGG3_ID: "agg_consumer_3",
    }
    mock.agg_id_to_area = {
        AGG1_ID: "LV1",
        AGG2_ID: "LV2",
        AGG3_ID: "LV3",
    }
    # influx_connector is set by HelicsSimulationExecutor.__init__ at instance
    # level (not as a class attribute), so MagicMock(spec=...) doesn't expose
    # it. Tests bypass __init__, so we fake it explicitly here. The Phase 3
    # flush at end of clear_market also calls write_output() + data_points.clear()
    # on it, so this mock needs both.
    mock.influx_connector = MagicMock()
    mock.influx_connector.data_points = []
    return mock


def _call_clear_market(mock_service, param_dict: dict) -> ClearMarketOutput:
    return MultiRegionMarket.clear_market(
        mock_service,
        param_dict,
        simulation_time=datetime(2020, 8, 11),
        time_step_number=MagicMock(),
        esdl_id=MagicMock(),
        energy_system=MagicMock(),
    )


def _param(agg_id: str, net: list[float], price: list[float]) -> dict:
    return {
        f"AggregatedConsumer/community_net_vector/{agg_id}": net,
        f"AggregatedConsumer/community_price_vector/{agg_id}": price,
    }


# ---------------------------------------------------------------------------
# TestAreaMapping
# ---------------------------------------------------------------------------

class TestAreaMapping(unittest.TestCase):

    def test_standard_naming(self):
        self.assertEqual(_area_from_name("agg_consumer_1"), "LV1")
        self.assertEqual(_area_from_name("agg_consumer_2"), "LV2")
        self.assertEqual(_area_from_name("agg_consumer_3"), "LV3")


# ---------------------------------------------------------------------------
# TestBidBuilding — verify param_dict parsing without running the solver
# ---------------------------------------------------------------------------

class TestBidBuilding(unittest.TestCase):
    """
    Call clear_market with controlled inputs and verify that the supply /
    demand DataFrames passed to OPtimiseMultiRegion contain the right rows.

    We patch OPtimiseMultiRegion so the solver never runs.
    """

    def _run_with_capture(self, param_dict: dict):
        """Run clear_market and capture the DataFrames handed to the solver."""
        captured = {}

        class _FakeOpt:
            def __init__(self, df_s, df_d, df_bo, df_l):
                captured["supply"] = df_s
                captured["demand"] = df_d
                # Production code reads these to compute cleared volumes per area
                # (euphemia_market_clearing.py around line 392). Empty dicts make
                # the per-area loop iterate over nothing — the cleared-volume
                # metrics stay at 0, which is fine for bid-building tests.
                self._demand_by_area = {}
                self._supply_by_area = {}
            def solve(self):
                # Return dummy duals: MCP = 50 EUR/MWh for every area/hour
                duals = {}
                for area in ["GRID", "LV1", "LV2", "LV3"]:
                    for t in range(1, 25):
                        duals[f"c_e_power_balance({area}_{t})_"] = -50.0
                return duals, {}
            @property
            def model(self):
                m = MagicMock()
                m.A = ["GRID", "LV1", "LV2", "LV3"]
                m.B = []
                return m

        import EuphemiaMarketClearing.euphemia_market_clearing as mrm
        original = mrm.OPtimiseMultiRegion
        mrm.OPtimiseMultiRegion = _FakeOpt
        try:
            result = _call_clear_market(_make_service(), param_dict)
        finally:
            mrm.OPtimiseMultiRegion = original

        return result, captured

    def test_surplus_creates_supply_bid(self):
        """Positive net position -> supply row in df_supply (kWh->MWh, EUR/kWh->EUR/MWh)."""
        params = _param(AGG1_ID, _flat(1.0), _flat(0.05))  # 1 kWh surplus at 0.05 EUR/kWh
        _, cap = self._run_with_capture(params)
        community_supply = cap["supply"][cap["supply"]["ID"].str.startswith("COM_")]
        self.assertFalse(community_supply.empty)
        row = community_supply.iloc[0]
        self.assertEqual(row["Area"], "LV1")
        self.assertAlmostEqual(row["Quantity"], 1.0 / 1000.0, places=6)   # kWh -> MWh
        self.assertAlmostEqual(row["Price"], 0.05 * 1000.0, places=4)     # EUR/kWh -> EUR/MWh

    def test_deficit_creates_demand_bid(self):
        """Negative net position -> demand row in df_demand."""
        params = _param(AGG2_ID, _flat(-2.0), _flat(0.10))
        _, cap = self._run_with_capture(params)
        community_demand = cap["demand"][cap["demand"]["ID"].str.startswith("COM_")]
        self.assertFalse(community_demand.empty)
        row = community_demand.iloc[0]
        self.assertEqual(row["Area"], "LV2")
        self.assertAlmostEqual(row["Quantity"], 2.0 / 1000.0, places=6)
        self.assertAlmostEqual(row["Price"], 0.10 * 1000.0, places=4)

    def test_zero_price_demand_falls_back_to_retail(self):
        """If P2P price is 0, demand bid price falls back to RETAIL_PRICE_EUR_MWH."""
        params = _param(AGG3_ID, _flat(-1.0), _flat(0.0))
        _, cap = self._run_with_capture(params)
        community_demand = cap["demand"][cap["demand"]["ID"].str.startswith("COM_")]
        for _, row in community_demand.iterrows():
            self.assertAlmostEqual(row["Price"], RETAIL_PRICE_EUR_MWH, places=4)


# ---------------------------------------------------------------------------
# TestClearMarket — one integration test with the real Pyomo/HiGHS solver
# ---------------------------------------------------------------------------

class TestClearMarket(unittest.TestCase):
    """
    Run clear_market end-to-end using the real OPtimiseMultiRegion solver.
    Uses in-memory DataFrames so no Excel file is required.
    """

    def _run(self, param_dict: dict) -> ClearMarketOutput:
        return _call_clear_market(_make_service(), param_dict)

    def test_clears_to_grid_price_with_expected_vector_length(self):
        """
        With no community bids the only active players are GRID supply and the
        reference demand, so every MCP clears at the GRID price (50 EUR/MWh).
        Also checks the published vector shape: (GRID + LV1 + LV2 + LV3) * 24.
        """
        params = _param(AGG1_ID, _flat(0.0), _flat(0.0))
        result = self._run(params)
        self.assertIsInstance(result, ClearMarketOutput)
        self.assertEqual(len(result.mcp_vector), 4 * 24)
        for mcp in result.mcp_vector:
            self.assertAlmostEqual(mcp, 50.0, delta=1.0)


if __name__ == "__main__":
    unittest.main()
