from datetime import datetime
import unittest
from MarketModelService.market_service import MarketService
from dots_infrastructure.DataClasses import SimulatorConfiguration, TimeStepInformation
from dots_infrastructure.test_infra.InfluxDBMock import InfluxDBMock
from esdl.esdl_handler import EnergySystemHandler
import helics as h

from dots_infrastructure import CalculationServiceHelperFunctions

BROKER_TEST_PORT = 23404
START_DATE_TIME = datetime(2024, 1, 1, 0, 0, 0)
SIMULATION_DURATION_IN_SECONDS = 960
TEST_ID = "bc0fcabe-fccb-4413-9ed6-188dc4af3c6a"

def simulator_environment_e_connection():
    return SimulatorConfiguration("EConnection", [TEST_ID], "Mock-Econnection", "127.0.0.1", BROKER_TEST_PORT, "test-id", SIMULATION_DURATION_IN_SECONDS, START_DATE_TIME, "test-host", "test-port", "test-username", "test-password", "test-database-name", h.HelicsLogLevel.DEBUG, ["PVInstallation", "EConnection"])

class Test(unittest.TestCase):

    def setUp(self):
        CalculationServiceHelperFunctions.get_simulator_configuration_from_environment = simulator_environment_e_connection
        esh = EnergySystemHandler()
        esh.load_file("test.esdl")
        self.energy_system = esh.get_energy_system()

    def test_send_current_day_ahead_price(self):
        # Arrange
        service = MarketService()
        service.init_calculation_service(self.energy_system)
        service.influx_connector = InfluxDBMock()

        # Execute
        ret_val = service.send_current_day_ahead_price({}, datetime(2020,8,11,0,0), TimeStepInformation(1,2), TEST_ID, self.energy_system)
        
        # Assert
        expected_outcome_price = 0.10354000000000001

        self.assertEqual(ret_val.day_ahead_price, expected_outcome_price)

if __name__ == '__main__':
    unittest.main()
