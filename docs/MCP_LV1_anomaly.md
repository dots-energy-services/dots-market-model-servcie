# LV1 MCP stuck at 150 EUR/MWh — diagnosis

**Sim inspected:** `Simulation Results/sim_SDR_gour_days_bep-andrei-2020-08-1-2aebe131_20260529_073251.xlsx`

## 1. Diagnosis

**Not a bug — expected zonal-clearing behavior given the configured cable caps.**

The premise in the question ("cable cap is 2.0 MWh/h") does not match the source. The LV1 cables are explicitly overridden to **0.0005 MWh/h** (0.5 kWh/h) at [`euphemia_market_clearing.py:39-40`](../src/EuphemiaMarketClearing/euphemia_market_clearing.py):

```python
("GRID", "LV1"): (0.0005, -0.0005),
("LV1",  "LV2"): (0.0005, -0.0005),
```

The in-code comment at lines 37–38 states the intent verbatim: *"LV1 cables — very tight (0.5 kWh/h) so LV1 is nearly isolated and has to curtail at the 150 EUR/MWh retail ceiling."* Both LV1-touching cables saturate to 100% of cap in every hour of the simulation, which decouples LV1 from the cheap GRID supply and makes the partially-cleared retail-fallback demand (150 EUR/MWh) the marginal bid.

Why MCP=150 not the anchor at 1500: the LV1 anchor supply (0.0001 MWh/h at 1500 EUR/MWh) is far smaller than the unmet demand. After cables and anchor, ≈88% of LV1's demand is still unmet at 150. Marginal welfare of +ε supply in LV1 = the price the next unmet bid will pay = 150 EUR/MWh. Dual extraction `-name_to_dual["c_e_power_balance(LV1_t)_"]` correctly returns 150.

The `CLAUDE.md` line "MV cable capacity: 2.0 MWh/h per cable was assumed" refers to an earlier configuration. The current code has both a smaller default (`MV_CABLE_FLOW_MAX_MWH = 0.0186`) and per-cable overrides; documentation is stale.

## 2. Evidence

Cable flows from the simulation export (`Interconnection Flows (MWh)` sheet):

| Cable | Cap (MWh/h) | Flow at h19 (all 4 days) | Max abs flow, full run | Utilization |
|---|---|---|---|---|
| GRID → LV1 | ±0.0005 | +0.0005 | 0.0005 | **100%** |
| LV1 → LV2 | ±0.0005 | −0.0005 (= LV2→LV1) | 0.0005 | **100%** |
| GRID → LV3 | ±0.010 | varies | 0.010 | 100% peak, slack elsewhere |
| LV2 → LV3 | ±0.010 | varies | 0.0073 | 73% peak |

LV1 demand offered vs cleared (h19, per day):

| Day | Offered (kWh) | Cleared (kWh) | Curtailed |
|---|---|---|---|
| 2020-08-12 | 8.32 | 1.00 | 88% |
| 2020-08-13 | 14.50 | 1.00 | 93% |
| 2020-08-14 | 7.79 | 1.00 | 87% |
| 2020-08-15 | 8.32 | 1.00 | 88% |

LV1 cleared consumption equals exactly the sum of inbound cable caps (0.5 + 0.5 = 1.0 kWh/h) in every congested hour. The flow value of 0.0005 MWh/h is the **binding constraint**, not slack.

Reproduction snippet (run from project root):
```python
import pandas as pd
fp = "DOTS INTEGRATION/Simulation Results/sim_SDR_gour_days_bep-andrei-2020-08-1-2aebe131_20260529_073251.xlsx"
flows = pd.read_excel(fp, sheet_name="Interconnection Flows (MWh)")
print(flows["GRID → LV1 [MWh/h]"].abs().max())   # 0.0005  (= cable cap)
print(flows["LV1 → LV2 [MWh/h]"].abs().max())    # 0.0005  (= cable cap)
```

## 3. Fix recommendation

Three options, depending on intent:

1. **Intent = islanded LV1 (current behavior is correct).** No change needed; this is by design per the in-code comment. Update `CLAUDE.md` so it stops claiming 2.0 MWh/h is the assumed capacity.

2. **Intent = LV1 should clear with the rest of the ring at ~100 EUR/MWh.** Remove the LV1 entries from `CABLE_CAPS` (lines 39–40) so they fall back to `MV_CABLE_FLOW_MAX_MWH = 0.0186` (18.6 kWh/h), or set them explicitly to a comfortable value (e.g. `(0.025, -0.025)`). With imports ≥ peak demand (~14.5 kWh/h), the cables become slack and LV1's MCP collapses to GRID's marginal price (100 EUR/MWh).

3. **Intent = scenario study where LV1 is constrained but for a different reason.** Leave the cable caps as is and document the scenario explicitly in the export README (so future-you doesn't re-investigate this).

**Risk:** Option 2 is the only one that touches the LP and could affect other areas. It is low-risk:
- Only widens LV1's two cables; GRID/LV2/LV3 cables and the anchor mechanism are untouched.
- The test suite (`test/test_multiregionmarket.py`) builds its own `df_lines` per test — none of the 20 tests reference `CABLE_CAPS`. Verified by reading `conftest.py` fixtures.
- Other areas already clear at 100, so widening LV1 cables can only pull LV1 down toward 100 (not push other MCPs up).

**My recommendation:** if the goal of this scenario was wholesale-market integration testing and not a transmission-constraint study, apply **Option 2**. Otherwise apply **Option 1** and update the docs.
