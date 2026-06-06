import pyomo.environ as pyo
import pandas as pd
import numpy as np
from pathlib import Path
import highspy
# matplotlib is only used inside .plot() for standalone debugging — imported lazily
# there so the DOTS service container doesn't need it.
class OPtimiseMultiRegion:
    def __init__(self, df_supply, df_demand, df_block_orders, df_lines):
        self.df_supply = df_supply
        self.df_demand = df_demand
        self.df_block_orders = df_block_orders
        self.lines  = df_lines

        #Create a Pyomo model
        self.model = pyo.ConcreteModel()
        self._sets_optimization()
        self._parameters()
        self._build_lookups()
        self._variables()
        self._objective()
        self._constraints()

    def _sets_optimization(self):
        # Sets for the optimization model
        self.model.T = pyo.RangeSet(1, 24)  # Time periods (1 to 24)
        self.model.S = pyo.Set(initialize=self.df_supply['ID'].unique())
        self.model.D = pyo.Set(initialize=self.df_demand['ID'].unique())
        all_steps = set(self.df_supply['Step'].unique()) | set(self.df_demand['Step'].unique())
        self.model.B = pyo.Set(initialize=all_steps)

        
        # get the different ids of the block orders
        bo_ids = [x for x in self.df_block_orders['ID'].unique() if pd.notna(x)]
        self.model.Bo = pyo.Set(initialize=bo_ids)

        # Get the areas where that we have supply and demand
        self.model.A = pyo.Set(initialize=sorted(self.df_supply['Area'].unique()))
        

        ic_pairs = list(zip(self.lines['AreaFrom'], self.lines['AreaTo']))
        self.model.L = pyo.Set(initialize=ic_pairs, dimen=2)

    def _parameters(self):
        # Parameters
        self.model.P_sb = pyo.Param(self.model.S,self.model.T ,self.model.B, 
                                    initialize=self.df_supply.set_index(['ID', 'Period','Step'])['Price'].to_dict(), 
                                    default=0)
        self.model.Q_sb = pyo.Param(self.model.S, self.model.T, self.model.B, initialize=self.df_supply.set_index(['ID', 'Period', 'Step'])['Quantity'].to_dict(), 
                                    default=0)
        self.model.P_db = pyo.Param(self.model.D, self.model.T, self.model.B, initialize=self.df_demand.set_index(['ID', 'Period', 'Step'])['Price'].to_dict(), 
                                    default=0)
        self.model.Q_db = pyo.Param(self.model.D, self.model.T, self.model.B, initialize=self.df_demand.set_index(['ID', 'Period', 'Step'])['Quantity'].to_dict(), 
                                    default=0)
        
        ramp_up_dict = self.df_supply.groupby('ID')['RampUp'].first().to_dict()
        ramp_dn_dict = self.df_supply.groupby('ID')['RampDown'].first().to_dict()

        self.model.RampUp = pyo.Param(self.model.S, initialize=ramp_up_dict, default=1e6)
        self.model.RampDown = pyo.Param(self.model.S, initialize=ramp_dn_dict, default=1e6)

        self.model.P_bo = pyo.Param(self.model.Bo, initialize=self.df_block_orders.groupby('ID')['Price'].first().to_dict()if not self.df_block_orders.empty else {})
        self.model.Q_bo = pyo.Param(self.model.Bo, initialize=self.df_block_orders.groupby('ID')['Quantity'].first().to_dict() if not self.df_block_orders.empty else {})


        #Lines:
        line_max = self.lines.set_index(['AreaFrom', 'AreaTo'])['FlowMax'].to_dict()
        self.model.MaxFlow = pyo.Param(self.model.L, initialize=line_max)
        
        if 'FlowMin' in self.lines.columns:
            line_min = self.lines.set_index(['AreaFrom', 'AreaTo'])['FlowMin'].to_dict()
            self.model.MinFlow = pyo.Param(self.model.L, initialize=line_min)
        else:
            line_max = {pair: -cap for pair, cap in line_max.items()}
            self.model.MinFlow = pyo.Param(self.model.L, initialize=line_max)

        
    def _build_lookups(self):
        
        df_bo = self.df_block_orders

        self._supply_area = self.df_supply.drop_duplicates('ID').set_index('ID')['Area'].to_dict()
        self._demand_area = self.df_demand.drop_duplicates('ID').set_index('ID')['Area'].to_dict()
        self._bo_area = (
            self.df_block_orders.drop_duplicates('ID').set_index('ID')['Area'].to_dict()
            if not self.df_block_orders.empty else {}
        )

        # Supply IDs grouped by area
        self._supply_by_area = {}
        for s, a in self._supply_area.items():
            self._supply_by_area.setdefault(a, []).append(s)

        # Demand IDs grouped by area
        self._demand_by_area = {}
        for d, a in self._demand_area.items():
            self._demand_by_area.setdefault(a, []).append(d)

        # Block order IDs active per (area, period)
        self._bo_by_area_period: dict[tuple, list] = {}
        for bo_id, area in self._bo_area.items():
            for t in df_bo.loc[df_bo['ID'] == bo_id, 'Period'].unique():
                self._bo_by_area_period.setdefault((area, int(t)), []).append(bo_id)


        self._outgoing: dict[str, list] = {}
        self._incoming: dict[str, list] = {}
        for (a_from, a_to) in self.model.L:
            self._outgoing.setdefault(a_from, []).append((a_from, a_to))
            self._incoming.setdefault(a_to,   []).append((a_from, a_to))
        # Get a dict with all active hours for a specific block 
        self._bo_hours = {}
        for bo_id in self._bo_area.keys():
            periods = self.df_block_orders.loc[
                self.df_block_orders['ID'] == bo_id, 'Period'
            ].tolist()
            self._bo_hours[bo_id] = periods

    def _variables(self):
        # Decision variables
        self.model.x_sb = pyo.Var(self.model.S, self.model.T, self.model.B, bounds=(0, 1))
        self.model.x_db = pyo.Var(self.model.D, self.model.T, self.model.B, bounds=(0, 1))
        self.model.u_bo = pyo.Var(self.model.Bo, domain=pyo.Binary)
        # Flow on interconnections
        
        def flow_bounds(m, a_from, a_to, t):
            return (m.MinFlow[a_from, a_to], m.MaxFlow[a_from, a_to])

        self.model.f_lt = pyo.Var(self.model.L, self.model.T, bounds=flow_bounds)

    def _objective(self):
        def objective_function(m):
            return (
                sum(m.P_db[d,t,b] * m.Q_db[d,t,b] * m.x_db[d,t,b] for d in m.D for t in m.T for b in m.B)
                - sum(m.P_sb[s,t,b] * m.Q_sb[s,t,b] * m.x_sb[s,t,b] for s in m.S for t in m.T for b in m.B)
                - sum(m.P_bo[bo] * m.Q_bo[bo] * m.u_bo[bo] * len(self.df_block_orders[self.df_block_orders['ID']==bo]) for bo in m.Bo)
            )
        self.model.obj = pyo.Objective(rule=objective_function, sense=pyo.maximize)

    def _constraints(self):
        # Per-area power balance  (eqs 12 + 13 combined, ATC version)
        # cleared_supply_a_t - cleared_demand_a_t + cleared_bo_a_t
        #   = inflow_a_t - outflow_a_t
        def power_balance_rule(m, a, t):
            supply_cleared = sum(
                m.Q_sb[s, t, b] * m.x_sb[s, t, b]
                for s in self._supply_by_area.get(a, [])
                for b in m.B
            )
            demand_cleared = sum(
                m.Q_db[d, t, b] * m.x_db[d, t, b]
                for d in self._demand_by_area.get(a, [])
                for b in m.B
            )
            bo_cleared = sum(
                m.Q_bo[bo] * m.u_bo[bo]
                for bo in self._bo_by_area_period.get((a, t), [])
            )
            # p_at (net injection) = outflow - inflow  [eq. 13]
            # positive p_at => net exporter => more outflow than inflow
            outflow = sum(m.f_lt[l[0], l[1], t] for l in self._outgoing.get(a, []))
            inflow  = sum(m.f_lt[l[0], l[1], t] for l in self._incoming.get(a, []))
            return supply_cleared - demand_cleared + bo_cleared - outflow + inflow == 0
                
        def ramp_up_rule(m, s, t):
            if t == 1:
                return pyo.Constraint.Skip
            qty_t   = sum(m.Q_sb[s, t,   b] * m.x_sb[s, t,   b] for b in m.B)
            qty_tm1 = sum(m.Q_sb[s, t-1, b] * m.x_sb[s, t-1, b] for b in m.B)
            return qty_t - qty_tm1 <= m.RampUp[s]

        def ramp_down_rule(m, s, t):
            if t == 1:
                return pyo.Constraint.Skip
            qty_t   = sum(m.Q_sb[s, t,   b] * m.x_sb[s, t,   b] for b in m.B)
            qty_tm1 = sum(m.Q_sb[s, t-1, b] * m.x_sb[s, t-1, b] for b in m.B)
            return qty_tm1 - qty_t <= m.RampDown[s]

        self.model.ramp_up   = pyo.Constraint(self.model.S, self.model.T, rule=ramp_up_rule)
        self.model.ramp_down = pyo.Constraint(self.model.S, self.model.T, rule=ramp_down_rule)
        self.model.power_balance = pyo.Constraint(self.model.A, self.model.T, rule=power_balance_rule)

    



    def solve_first_step(self):
        m = self.model
        m.dual = pyo.Suffix(direction=pyo.Suffix.IMPORT)
        filename_path = Path(__file__).parent / "model_multi.mps"
        model_file_name = str(filename_path)
        m.write(model_file_name, io_options={'symbolic_solver_labels': True})

        # Phase 1: solve MILP to determine block order acceptance
        h = highspy.Highs()
        h.setOptionValue("output_flag", False)
        h.setOptionValue("log_to_console", False)
        h.readModel(model_file_name)
        h.run()

        col_names  = [h.getColName(i)[1] for i in range(h.getNumCol())]
        col_values = list(h.getSolution().col_value)
        phase1_values = dict(zip(col_names, col_values))

        # Fix binary block order decisions
        for bo in m.Bo:
            val = round(phase1_values.get(f"u_bo({bo})", 0))
            m.u_bo[bo].fix(val)
            m.u_bo[bo].domain = pyo.NonNegativeReals

        # Phase 2: re-solve as pure LP to get dual variables (MCPs)
        m.write(model_file_name, io_options={'symbolic_solver_labels': True})
        h.clear()
        h.setOptionValue("output_flag", False)
        h.setOptionValue("log_to_console", False)
        h.readModel(model_file_name)
        h.run()

        row_names  = [h.getRowName(i)[1]  for i in range(h.getNumRow())]
        row_duals  = list(h.getSolution().row_dual)
        name_to_dual = dict(zip(row_names, row_duals))

        col_duals = list(h.getSolution().col_dual)
        final_col_names  = [h.getColName(i)[1] for i in range(h.getNumCol())]

        name_to_col_dual = dict(zip(final_col_names, col_duals))
        
        final_col_values = list(h.getSolution().col_value)
        final_values = dict(zip(final_col_names, final_col_values))

        return name_to_dual, final_values, phase1_values,name_to_col_dual
    
    
    def _check_pabs(self, name_to_dual, phase1_values):
        pabs = []
        
        for bo in self.model.Bo:
            # Step 1: was it accepted?
            accepted = round(phase1_values.get(f"u_bo({bo})", 0))
            if not accepted:
                continue
            
            # Step 2: compute WAP
            active_hours = self._bo_hours[bo]
            area = self._bo_area[bo]
            wap = sum(-name_to_dual.get(f"c_e_power_balance({area}_{t})_", 0)
                    for t in active_hours) / len(active_hours)
            
            # Step 3: compare to submitted price
            submitted_price = pyo.value(self.model.P_bo[bo])
            if wap < submitted_price:
                pabs.append(bo)
        
        return pabs


    def solve(self):
        rejected_bos = set()  # permanently rejected blocks
        
        while True:
    # unfix all blocks first
            for bo in self.model.Bo:
                if bo not in rejected_bos:
                    self.model.u_bo[bo].unfix()
                    self.model.u_bo[bo].domain = pyo.Binary

            # fix any previously rejected blocks
            for bo in rejected_bos:
                self.model.u_bo[bo].fix(0)
                self.model.u_bo[bo].domain = pyo.NonNegativeReals
            
            # solve
            name_to_dual, final_values, phase1_values, name_to_col_dual = self.solve_first_step()
            
            # check for PABs
            pabs = self._check_pabs(name_to_dual, phase1_values)
            
            if not pabs:
                break  # no PABs, we are done
            
            # add PABs to permanently rejected set
            print(f"PABs found: {pabs}, removing and re-solving...")
            rejected_bos.update(pabs)
        self.print_results(final_values, name_to_dual, phase1_values, name_to_col_dual)


        self.name_to_dual = name_to_dual
        self.final_values = final_values
        self.phase1_values = phase1_values
        self.name_to_col_dual = name_to_col_dual
        
        return name_to_dual, final_values
    

    def print_results(self, final_values, name_to_dual, phase1_values, name_to_col_dual):
        m = self.model

        print("\n=== CLEARED ENERGY AND MCPs ===")
        for a in m.A:
            print(f"\n  Area {a}:")
            supply_ids = self._supply_by_area.get(a, [])
            demand_ids = self._demand_by_area.get(a, [])
            for t in m.T:
                supply_cl = sum(
                    final_values.get(f"x_sb({s}_{t}_{b})", 0) * pyo.value(m.Q_sb[s, t, b])
                    for s in supply_ids for b in m.B
                )
                demand_cl = sum(
                    final_values.get(f"x_db({d}_{t}_{b})", 0) * pyo.value(m.Q_db[d, t, b])
                    for d in demand_ids for b in m.B
                )
                bo_cl = sum(
                    phase1_values.get(f"u_bo({bo})", 0) * pyo.value(m.Q_bo[bo])
                    for bo in self._bo_by_area_period.get((a, t), [])
                )
                mcp = -name_to_dual.get(f"c_e_power_balance({a}_{t})_", 0)
                print(
                    f"    h{t:02d}: Supply={supply_cl:8.2f} BO={bo_cl:8.2f} "
                    f"Demand={demand_cl:8.2f} MCP={mcp:7.2f} EUR/MWh"
                )

        print("\n=== INTERCONNECTION FLOWS ===")
        for (a_from, a_to) in m.L:
            flows = [
                (t, final_values.get(f"f_lt({a_from}_{a_to}_{t})", 0))
                for t in m.T
            ]
            non_zero = [(t, f) for t, f in flows if abs(f) > 1e-3]
            if non_zero:
                print(f"\n  {a_from} <-> {a_to}:")
                for t, f in non_zero:
                    direction = f"{a_from}->{a_to}" if f > 0 else f"{a_to}->{a_from}"
                    shadow = name_to_col_dual.get(f"f_lt({a_from}_{a_to}_{t})", 0)
                    print(
                        f"    h{t:02d}: {abs(f):8.2f} MW  ({direction})  "
                        f"shadow={shadow:7.2f} €/MWh"
                    )


    def plot(self, what_to_plot, area=None):
        """
        Plot market results for a given area or all areas.

        Parameters
        ----------
        what_to_plot : str
            Type of plot to generate. Options:
                - "price"                : Market Clearing Price per hour (EUR/MWh)
                - "supply_available"     : Total supply quantity offered per hour (MWh)
                - "supply_cleared"       : Accepted hourly supply per hour (MWh)
                - "supply_cleared_with_bo": Accepted hourly supply + block orders per hour (MWh)
                - "demand_available"     : Total demand quantity offered per hour (MWh)
                - "demand_cleared"       : Accepted demand per hour (MWh)

        area : str, optional
            Bidding area to plot (e.g. 'A' or 'B').
            If None, plots all areas in separate figures.
        """
        from matplotlib import pyplot as plt

        name_to_dual, final_values = self.name_to_dual, self.final_values
        areas = [area] if area is not None else list(self.model.A)

    
        if what_to_plot == "price":
            fig, axes = plt.subplots(1, len(areas), figsize=(6 * len(areas), 5), sharey=True)
            if len(areas) == 1:
                axes = [axes]
            for ax, a in zip(axes, areas):
                mcps = [-self.name_to_dual.get(f"c_e_power_balance({a}_{t})_", 0)
                        for t in self.model.T]
                ax.plot(list(self.model.T), mcps, marker='o', label=f'Area {a}')
                ax.set_xlabel('Hour')
                ax.set_ylabel('EUR/MWh')
                ax.set_title(f'Area {a}')
                ax.legend()
                ax.grid(True)
            fig.suptitle('Market Clearing Price per Area', fontsize=14)
            plt.tight_layout()
            plt.show()

        elif what_to_plot == "supply_available":
            fig, axes = plt.subplots(1, len(areas), figsize=(6 * len(areas), 5), sharey=True)
            if len(areas) == 1:
                axes = [axes]
            for ax, a in zip(axes, areas):
                supply = [
                    sum(
                        pyo.value(self.model.Q_sb[s, t, b])
                        for s in self._supply_by_area.get(a, [])
                        for b in self.model.B
                    )
                    for t in self.model.T
                ]
                ax.plot(list(self.model.T), supply, marker='o', label=f'Area {a}')
                ax.set_xlabel('Hour')
                ax.set_ylabel('MWh')
                ax.set_title(f'Area {a}')
                ax.legend()
                ax.grid(True)
            fig.suptitle('Total Supply Available per Area', fontsize=14)
            plt.tight_layout()
            plt.show()

        elif what_to_plot == "supply_cleared":
            fig, axes = plt.subplots(1, len(areas), figsize=(6 * len(areas), 5), sharey=True)
            if len(areas) == 1:
                axes = [axes]
            for ax, a in zip(axes, areas):
                supply_cleared = [
                    sum(
                        final_values.get(f"x_sb({s}_{t}_{b})", 0) * pyo.value(self.model.Q_sb[s, t, b])
                        for s in self._supply_by_area.get(a, [])
                        for b in self.model.B
                    )
                    for t in self.model.T
                ]
                ax.plot(list(self.model.T), supply_cleared, marker='o', label=f'Area {a}')
                ax.set_xlabel('Hour')
                ax.set_ylabel('MWh')
                ax.set_title(f'Area {a}')
                ax.legend()
                ax.grid(True)
            fig.suptitle('Total Supply Cleared per Area', fontsize=14)
            plt.tight_layout()
            plt.show()

        elif what_to_plot == "demand_cleared":
            fig, axes = plt.subplots(1, len(areas), figsize=(6 * len(areas), 5), sharey=True)
            if len(areas) == 1:
                axes = [axes]
            for ax, a in zip(axes, areas):
                demand_cleared = [
                    sum(
                        final_values.get(f"x_db({d}_{t}_{b})", 0) * pyo.value(self.model.Q_db[d, t, b])
                        for d in self._demand_by_area.get(a, [])
                        for b in self.model.B
                    )
                    for t in self.model.T
                ]
                ax.plot(list(self.model.T), demand_cleared, marker='o', label=f'Area {a}')
                ax.set_xlabel('Hour')
                ax.set_ylabel('MWh')
                ax.set_title(f'Area {a}')
                ax.legend()
                ax.grid(True)
            fig.suptitle('Total Demand Cleared per Area', fontsize=14)
            plt.tight_layout()
            plt.show()

        elif what_to_plot == "demand_available":
            fig, axes = plt.subplots(1, len(areas), figsize=(6 * len(areas), 5), sharey=True)
            if len(areas) == 1:
                axes = [axes]
            for ax, a in zip(axes, areas):
                demand = [
                    sum(
                        pyo.value(self.model.Q_db[d, t, b])
                        for d in self._demand_by_area.get(a, [])
                        for b in self.model.B
                    )
                    for t in self.model.T
                ]
                ax.plot(list(self.model.T), demand, marker='o', label=f'Area {a}')
                ax.set_xlabel('Hour')
                ax.set_ylabel('MWh')
                ax.set_title(f'Area {a}')
                ax.legend()
                ax.grid(True)
            fig.suptitle('Total Demand Available per Area', fontsize=14)
            plt.tight_layout()
            plt.show()

        elif what_to_plot == "supply_cleared_with_bo":
            fig, axes = plt.subplots(1, len(areas), figsize=(6 * len(areas), 5), sharey=True)
            if len(areas) == 1:
                axes = [axes]
            for ax, a in zip(axes, areas):
                supply_cleared = [
                    sum(
                        self.final_values.get(f"x_sb({s}_{t}_{b})", 0) * pyo.value(self.model.Q_sb[s, t, b])
                        for s in self._supply_by_area.get(a, [])
                        for b in self.model.B
                    )
                    for t in self.model.T
                ]
                bo_cleared = [
                    sum(
                        self.phase1_values.get(f"u_bo({bo})", 0) * pyo.value(self.model.Q_bo[bo])
                        for bo in self._bo_by_area_period.get((a, t), [])
                    )
                    for t in self.model.T
                ]
                total = [s + b for s, b in zip(supply_cleared, bo_cleared)]
                ax.plot(list(self.model.T), supply_cleared, marker='o', label='Hourly supply')
                ax.plot(list(self.model.T), bo_cleared, marker='s', label='Block orders')
                ax.plot(list(self.model.T), total, marker='^', label='Total')
                ax.set_xlabel('Hour')
                ax.set_ylabel('MWh')
                ax.set_title(f'Area {a}')
                ax.legend()
                ax.grid(True)
            fig.suptitle('Supply Cleared (incl. Block Orders) per Area', fontsize=14)
            plt.tight_layout()
            plt.show()
        else: 
            print(f"Unknown plot type: {what_to_plot}")