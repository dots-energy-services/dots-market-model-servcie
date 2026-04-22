from datetime import datetime, timedelta
import helics as h
from dots_infrastructure.DataClasses import TimeStepInformation, EsdlId
from esdl import EnergyMarket, EnergySystem, DateTimeProfile
import pandas as pd

from MarketService.market_service_base import MarketServiceBase
from MarketService.market_service_dataclasses import SendCurrentDayAheadPriceOutput

class MarketService(MarketServiceBase): 

    def parse_profile(self, profile : DateTimeProfile):
        # Parse the profile and return the values
        from_profile = []
        to_profile = []
        value_profile = []
        for el in profile.element:
            value_profile.append(el.value)
            from_profile.append(el.from_)
            to_profile.append(el.to)
        
        ret_val = pd.DataFrame({
            "from": from_profile,
            "to": to_profile,
            "value": value_profile
        })
        ret_val.set_index("from", inplace=True)
        return ret_val

    def init_calculation_service(self, energy_system: EnergySystem):
        super().init_calculation_service(energy_system)
        self.market_prices: dict[esdl_id, pd.DataFrame] = {}
        for esdl_id in self.simulator_configuration.esdl_ids:
            da_market : EnergyMarket = self.esdl_obj_mapping[esdl_id]
            price_profile = da_market.marketPrice
            self.market_prices[esdl_id] = self.parse_profile(price_profile)


    def send_current_day_ahead_price(self, param_dict : dict, simulation_time : datetime, time_step_number : TimeStepInformation, esdl_id : EsdlId, energy_system : EnergySystem):
        to_date_time = simulation_time + timedelta(seconds=self.send_current_day_ahead_price_period_seconds - 1)
        day_ahead_price = self.market_prices[esdl_id][
                                      simulation_time:to_date_time]["value"].tolist()[0]
        self.influx_connector.set_time_step_data_point(esdl_id, "current_pv_power_production", simulation_time, day_ahead_price)
        return SendCurrentDayAheadPriceOutput(day_ahead_price=day_ahead_price)

if __name__ == "__main__":

    helics_simulation_executor = MarketService()
    helics_simulation_executor.start_simulation()
    helics_simulation_executor.stop_simulation()