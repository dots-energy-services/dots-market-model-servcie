import logging
import os
from datetime import datetime, timedelta

import pandas as pd
import pyomo.environ as pyo
from esdl import AggregatedConsumer, ElectricityCable, EnergySystem, Import, Joint, Transformer

from dots_infrastructure.DataClasses import EsdlId, TimeStepInformation

from EuphemiaMarketClearing.euphemia_market_clearing_base import EuphemiaMarketClearingBase
from EuphemiaMarketClearing.euphemia_market_clearing_dataclasses import ClearMarketOutput
from EuphemiaMarketClearing.multi_region_optimise import OPtimiseMultiRegion

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
MARKET_DATA_FILE = os.path.join(DATA_DIR, "market_data_multiregion.xlsx")

# Env var override for the retail-fallback price (used when a community has no
# P2P clearing price and we need a buyer price to keep the market feasible).
# Default = baseline band (150 EUR/MWh); GUI passes per-tariff-band values.
RETAIL_PRICE_EUR_MWH = float(os.environ.get("RETAIL_PRICE_EUR_MWH", "150.0"))

# Default symmetric capacity used when (AreaFrom, AreaTo) is not listed in
# CABLE_CAPS. The Interconnections sheet in the xlsx is overridden by the
# CABLE_CAPS dict — this code is the source of truth for cable limits.
MV_CABLE_FLOW_MAX_MWH = 0.0186

# Per-cable flow capacity overrides [MWh/h].
# Keys are (AreaFrom, AreaTo) — match the direction reported in the
# Interconnection Flows sheet. Values are (FlowMax, FlowMin):
#   FlowMax > 0  → max flow AreaFrom → AreaTo
#   FlowMin < 0  → max flow AreaTo → AreaFrom (i.e. reverse direction)
CABLE_CAPS: dict[tuple[str, str], tuple[float, float]] = {
    # LV1 cables — moderately tight (4.5 kWh/h combined) so LV1 congests
    # at evening peak (~9.7 kWh/h mean demand → ~8% curtailment) but
    # clears with the other areas off-peak. See SIMULATION STRUCTURE §6.
    ("GRID", "LV1"): (0.0045, -0.0045),
    ("LV1",  "LV2"): (0.0045, -0.0045),
    # LV2 / LV3 cables — moderate (10 kWh/h), comfortable headroom.
    ("GRID", "LV3"): (0.010, -0.010),
    ("LV2",  "LV3"): (0.010, -0.010),
}


def _area_from_name(name: str) -> str:
    """Convert ESDL AggregatedConsumer name to a short market area label.
    e.g. 'agg_consumer_1' → 'LV1'
    """
    parts = name.rsplit("_", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return f"LV{parts[1]}"
    return name


def _build_lines_from_esdl(energy_system: EnergySystem) -> pd.DataFrame:
    """
    Derive the market interconnection table from the ESDL physical topology.

    Strategy:
      1. Build a map from every port ID to the asset that owns it.
      2. Identify the GRID joint — the secondary-side joint of the HV/MV
         transformer (name='jointhighvoltagetrafo'). Every MV cable that
         connects back to this joint belongs to the GRID area.
      3. For every 10/0.4 kV transformer, follow its secondary OutPort to
         find the AggregatedConsumer downstream, then follow its primary
         InPort to find the MV-side Joint. That joint → LV area mapping is
         stored.
      4. Walk each ElectricityCable with assetType='mv_line'. Trace its
         InPort back to a joint and its OutPort forward to a joint. Look
         both joints up in the area map to get AreaFrom / AreaTo.

    Cable flow capacity is NOT in the ESDL (no FlowMax attribute is set on
    the cables), so MV_CABLE_FLOW_MAX_MWH is used for all MV lines.
    """
    all_objects = list(energy_system.eAllContents())

    # Step 1 — port_id → containing asset
    port_to_asset: dict[str, object] = {}
    for obj in all_objects:
        if hasattr(obj, "port"):
            for port in obj.port:
                port_to_asset[port.id] = obj

    # Step 2 — identify the GRID joint (HV/MV transformer secondary side)
    joint_to_area: dict[str, str] = {}
    for obj in all_objects:
        if isinstance(obj, Joint) and obj.name == "jointhighvoltagetrafo":
            joint_to_area[obj.id] = "GRID"
            break

    # Step 3 — for each MV/LV transformer (10→0.4 kV), find:
    #   • which AggregatedConsumer is on the LV (secondary) side  → the LV area
    #   • which Joint is on the MV (primary) side                 → map it to that area
    for obj in all_objects:
        if isinstance(obj, Transformer) and getattr(obj, "voltagePrimary", None) == 10.0:
            lv_area = None
            mv_joint = None

            for port in obj.port:
                if port.__class__.__name__ == "OutPort":
                    for connected_port in port.connectedTo:
                        asset = port_to_asset.get(connected_port.id)
                        if isinstance(asset, AggregatedConsumer):
                            lv_area = _area_from_name(asset.name)

                elif port.__class__.__name__ == "InPort":
                    for connected_port in port.connectedTo:
                        asset = port_to_asset.get(connected_port.id)
                        if isinstance(asset, Joint):
                            mv_joint = asset

            if lv_area and mv_joint:
                joint_to_area[mv_joint.id] = lv_area

    # Step 4 — trace each MV cable to its two endpoint joints → market areas
    rows = []
    for obj in all_objects:
        if isinstance(obj, ElectricityCable) and getattr(obj, "assetType", "") == "mv_line":
            in_joint = None
            out_joint = None

            for port in obj.port:
                if port.__class__.__name__ == "InPort":
                    for connected_port in port.connectedTo:
                        asset = port_to_asset.get(connected_port.id)
                        if isinstance(asset, Joint):
                            in_joint = asset

                elif port.__class__.__name__ == "OutPort":
                    for connected_port in port.connectedTo:
                        asset = port_to_asset.get(connected_port.id)
                        if isinstance(asset, Joint):
                            out_joint = asset

            if in_joint and out_joint:
                area_from = joint_to_area.get(in_joint.id)
                area_to = joint_to_area.get(out_joint.id)

                if area_from and area_to:
                    # Per-cable override if specified, otherwise symmetric default.
                    flow_max, flow_min = CABLE_CAPS.get(
                        (area_from, area_to),
                        (MV_CABLE_FLOW_MAX_MWH, -MV_CABLE_FLOW_MAX_MWH),
                    )
                    rows.append({
                        "AreaFrom": area_from,
                        "AreaTo": area_to,
                        "FlowMax": flow_max,
                        "FlowMin": flow_min,
                    })
                else:
                    logging.warning(
                        "[EuphemiaMarketClearing] MV cable '%s': could not resolve areas "
                        "(in_joint=%s→%s, out_joint=%s→%s) — skipping.",
                        obj.name,
                        in_joint.name, area_from,
                        out_joint.name, area_to,
                    )

    if not rows:
        logging.warning(
            "[EuphemiaMarketClearing] _build_lines_from_esdl: no MV cable interconnections "
            "found — market will have no transmission constraints."
        )

    return pd.DataFrame(rows, columns=["AreaFrom", "AreaTo", "FlowMax", "FlowMin"])


def _build_grid_supply_from_esdl(energy_system: EnergySystem) -> pd.DataFrame:
    """
    Build the GRID area supply bid table.

    The ESDL Import asset confirms that an HV grid connection exists, but
    carries no bid price or market capacity. The full supply curve (price
    and quantity per period) comes from the Supply sheet of
    market_data_multiregion.xlsx — that file is the single source of truth
    for the wholesale HV import schedule (TOU price curve, capacity).
    """
    has_import = any(isinstance(obj, Import) for obj in energy_system.eAllContents())
    if not has_import:
        logging.warning(
            "[EuphemiaMarketClearing] No Import asset found in ESDL — "
            "the GRID supply bid table from the xlsx will still be used."
        )

    df = pd.read_excel(MARKET_DATA_FILE, sheet_name="Supply")
    required = {"ID", "Area", "Period", "Step", "Price", "Quantity"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"market_data_multiregion.xlsx Supply sheet missing columns: {missing}"
        )
    if "RampUp" not in df.columns:
        df["RampUp"] = 1e6
    if "RampDown" not in df.columns:
        df["RampDown"] = 1e6

    logging.info(
        "[EuphemiaMarketClearing] Loaded GRID supply schedule from xlsx (%d rows). "
        "Price range: %.1f–%.1f EUR/MWh.",
        len(df), float(df["Price"].min()), float(df["Price"].max()),
    )
    return df


class EuphemiaMarketClearing(EuphemiaMarketClearingBase):

    def init_calculation_service(self, energy_system: EnergySystem):
        super().init_calculation_service(energy_system)

        # Supply and interconnections derived from ESDL topology.
        # Block orders have no ESDL equivalent — read from Excel (empty by default).
        # Base demand is empty — all demand comes from P2P community bids.
        self.df_supply_base = _build_grid_supply_from_esdl(energy_system)
        self.df_demand_base = pd.DataFrame(columns=["ID", "Area", "Period", "Step", "Price", "Quantity"])
        self.df_block_orders = pd.read_excel(MARKET_DATA_FILE, sheet_name="BlockOrders")
        self.df_lines = _build_lines_from_esdl(energy_system)

        logging.info(
            "[EuphemiaMarketClearing] Interconnections derived from ESDL:\n%s",
            self.df_lines.to_string(index=False),
        )

        # Build UUID → community name and UUID → market area lookups.
        # param_dict keys contain UUIDs, not human-readable names.
        self.agg_id_to_community: dict[str, str] = {}
        self.agg_id_to_area: dict[str, str] = {}
        for obj in energy_system.eAllContents():
            if isinstance(obj, AggregatedConsumer) and hasattr(obj, "id"):
                area = _area_from_name(obj.name)
                self.agg_id_to_community[obj.id] = obj.name
                self.agg_id_to_area[obj.id] = area

        logging.info(
            "[EuphemiaMarketClearing] Registered %d communities: %s",
            len(self.agg_id_to_area),
            ", ".join(
                f"{name}->{area}"
                for name, area in zip(
                    self.agg_id_to_community.values(),
                    self.agg_id_to_area.values(),
                )
            ),
        )

    def clear_market(
        self,
        param_dict: dict,
        simulation_time: datetime,
        time_step_number: TimeStepInformation,
        esdl_id: EsdlId,
        energy_system: EnergySystem,
    ) -> ClearMarketOutput:

        # 1. Parse param_dict
        # Keys look like "AggregatedConsumer/community_net_vector/<uuid>"
        community_data: dict[str, dict] = {}
        for key, vector in param_dict.items():
            if vector is None:
                continue
            parts = key.split("/")
            if len(parts) < 3:
                continue
            input_name = parts[1]
            agg_id = parts[2]
            community_data.setdefault(agg_id, {})
            if input_name == "community_net_vector":
                community_data[agg_id]["net"] = [float(v) for v in vector]
            elif input_name == "community_price_vector":
                community_data[agg_id]["price"] = [float(v) for v in vector]

        # 2. Build supply and demand bids from community net positions
        supply_rows: list[dict] = []
        demand_rows: list[dict] = []

        for agg_id, data in community_data.items():
            np_vector = data.get("net", [0.0] * 24)
            px_vector = data.get("price", [0.0] * 24)

            area = self.agg_id_to_area.get(agg_id, "LV?")
            community = self.agg_id_to_community.get(agg_id, agg_id)

            # Safety pad to exactly 24 values
            np_vector = (np_vector + [0.0] * 24)[:24]
            px_vector = (px_vector + [0.0] * 24)[:24]

            for t0, (np_kwh, px_kwh) in enumerate(zip(np_vector, px_vector)):
                period = t0 + 1                     # EUPHEMIA uses 1-indexed periods
                qty_mwh = abs(np_kwh) / 1000.0      # kWh → MWh
                bid_eur_mwh = px_kwh * 1000.0       # EUR/kWh → EUR/MWh

                if qty_mwh < 1e-6:
                    continue

                if np_kwh > 0:
                    # Surplus → community is a seller
                    supply_rows.append({
                        "ID": f"COM_{community}_supply",
                        "Area": area,
                        "Period": period,
                        "Step": 1,
                        "Price": bid_eur_mwh,
                        "Quantity": qty_mwh,
                        "RampUp": 1e6,
                        "RampDown": 1e6,
                    })
                else:
                    # Deficit → community is a buyer
                    # Fall back to RETAIL_PRICE if P2P returned zero (e.g. single-house community)
                    effective_price = bid_eur_mwh if bid_eur_mwh > 0.0 else RETAIL_PRICE_EUR_MWH
                    demand_rows.append({
                        "ID": f"COM_{community}_demand",
                        "Area": area,
                        "Period": period,
                        "Step": 1,
                        "Price": effective_price,
                        "Quantity": qty_mwh,
                    })

        # 3. Merge community bids with GRID base supply
        df_supply = pd.concat(
            [self.df_supply_base, pd.DataFrame(supply_rows)], ignore_index=True
        ) if supply_rows else self.df_supply_base.copy()

        df_demand = pd.concat(
            [self.df_demand_base, pd.DataFrame(demand_rows)], ignore_index=True
        ) if demand_rows else self.df_demand_base.copy()

        # 3b. Anchor supply — ensure every area in df_lines appears in model.A
        # so HiGHS creates a power balance constraint for it. Without this,
        # demand-only areas are missing from model.A and the solver exploits
        # the unconstrained interconnection flow as a free energy source,
        # collapsing all MCPs to zero.
        all_line_areas = set(
            self.df_lines["AreaFrom"].tolist() + self.df_lines["AreaTo"].tolist()
        )
        missing_areas = all_line_areas - set(df_supply["Area"].tolist())
        if missing_areas:
            anchor_rows = []
            for area in sorted(missing_areas):
                for t in range(1, 25):
                    anchor_rows.append({
                        "ID": f"ANCHOR_{area}",
                        "Area": area,
                        "Period": t,
                        "Step": 1,
                        "Price": RETAIL_PRICE_EUR_MWH * 10.0,
                        "Quantity": 1e-4,
                        "RampUp": 1e6,
                        "RampDown": 1e6,
                    })
            df_supply = pd.concat(
                [df_supply, pd.DataFrame(anchor_rows)], ignore_index=True
            )

        # Guard: if still no demand at all, add negligible reference demand
        if df_demand.empty:
            logging.warning(
                "[EuphemiaMarketClearing] No demand bids received — inserting "
                "reference demand per area to maintain feasibility."
            )
            ref_rows = []
            for area in sorted(df_supply["Area"].unique()):
                for period in range(1, 25):
                    ref_rows.append({
                        "ID": f"REF_demand_{area}",
                        "Area": area,
                        "Period": period,
                        "Step": 1,
                        "Price": RETAIL_PRICE_EUR_MWH,
                        "Quantity": 0.001,
                    })
            df_demand = pd.DataFrame(ref_rows)

        # 4. Solve EUPHEMIA
        euphemia = OPtimiseMultiRegion(
            df_supply, df_demand, self.df_block_orders, self.df_lines
        )
        name_to_dual, final_values = euphemia.solve()

        # 5. Extract MCPs from dual variables of power balance constraints
        # MCP = -dual because the solver maximises welfare (demand - supply cost),
        # so the dual of the equality constraint has the opposite sign to the price.
        areas = sorted(euphemia.model.A)
        mcp_by_area: dict[str, list[float]] = {}
        for area in areas:
            mcp_by_area[area] = [
                -name_to_dual.get(f"c_e_power_balance({area}_{t})_", 0.0)
                for t in range(1, 25)
            ]

        # Pack into a flat vector: [GRID_h1..h24, LV1_h1..h24, LV2_h1..h24, LV3_h1..h24]
        mcp_vector: list[float] = []
        for area in areas:
            mcp_vector.extend(mcp_by_area[area])

        logging.info(
            "[EuphemiaMarketClearing] Cleared at %s | areas=%s | noon MCPs: %s",
            simulation_time,
            areas,
            ", ".join(f"{a}={mcp_by_area[a][11]:.2f} EUR/MWh" for a in areas),
        )

        # ── Per-area, per-hour wholesale market metrics ──────────────────────
        # total_demand_*_mwh        : total demand bid quantity OFFERED  (pre-clearing)
        # total_consumption_*_mwh   : total demand quantity CLEARED      (post-clearing)
        # total_supply_cleared_*_mwh: total supply quantity CLEARED      (post-clearing)
        total_demand_offered: dict[str, list[float]] = {a: [0.0] * 24 for a in areas}
        total_demand_cleared: dict[str, list[float]] = {a: [0.0] * 24 for a in areas}
        total_supply_cleared: dict[str, list[float]] = {a: [0.0] * 24 for a in areas}

        if not df_demand.empty:
            offered = df_demand.groupby(["Area", "Period"])["Quantity"].sum()
            for (area, period), qty in offered.items():
                if area in total_demand_offered and 1 <= period <= 24:
                    total_demand_offered[area][period - 1] += float(qty)

        m = euphemia.model
        for area in areas:
            for d in euphemia._demand_by_area.get(area, []):
                for t in range(1, 25):
                    for b in m.B:
                        x = float(final_values.get(f"x_db({d}_{t}_{b})", 0.0))
                        q = float(pyo.value(m.Q_db[d, t, b]))
                        total_demand_cleared[area][t - 1] += x * q
            for s in euphemia._supply_by_area.get(area, []):
                for t in range(1, 25):
                    for b in m.B:
                        x = float(final_values.get(f"x_sb({s}_{t}_{b})", 0.0))
                        q = float(pyo.value(m.Q_sb[s, t, b]))
                        total_supply_cleared[area][t - 1] += x * q

        # Interconnection flows: f_lt(a_from_a_to_t)
        flow_by_line: dict[tuple[str, str], list[float]] = {}
        for (a_from, a_to) in self.df_lines[["AreaFrom", "AreaTo"]].itertuples(index=False):
            flow_by_line[(a_from, a_to)] = [
                float(final_values.get(f"f_lt({a_from}_{a_to}_{t})", 0.0))
                for t in range(1, 25)
            ]

        # Write all fields to InfluxDB (single EnergySystem measurement)
        for t0 in range(24):
            ts = simulation_time + timedelta(hours=t0)
            for area in areas:
                self.influx_connector.set_time_step_data_point(
                    esdl_id, f"mcp_{area}_eur_mwh", ts, mcp_by_area[area][t0]
                )
                self.influx_connector.set_time_step_data_point(
                    esdl_id, f"total_demand_{area}_mwh", ts, total_demand_offered[area][t0]
                )
                self.influx_connector.set_time_step_data_point(
                    esdl_id, f"total_consumption_{area}_mwh", ts, total_demand_cleared[area][t0]
                )
                self.influx_connector.set_time_step_data_point(
                    esdl_id, f"total_supply_cleared_{area}_mwh", ts, total_supply_cleared[area][t0]
                )
            for (a_from, a_to), flows in flow_by_line.items():
                self.influx_connector.set_time_step_data_point(
                    esdl_id, f"flow_{a_from}_to_{a_to}_mwh", ts, flows[t0]
                )

        # Phase 3 requires day-D MCPs to be durable in InfluxDB before day-(D+1)
        # starts. The connector buffers writes by default and only flushes at
        # end-of-simulation (or after 100k points). Force a synchronous flush
        # so the Communitymanager's day-(D+1) read can find yesterday's row.
        self.influx_connector.write_output()
        self.influx_connector.data_points.clear()

        return ClearMarketOutput(mcp_vector=mcp_vector)


if __name__ == "__main__":
    helics_simulation_executor = EuphemiaMarketClearing()
    helics_simulation_executor.start_simulation()
    helics_simulation_executor.stop_simulation()
