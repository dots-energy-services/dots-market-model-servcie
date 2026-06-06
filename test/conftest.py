"""
conftest.py — pytest configuration for multiregion-calculation-service tests.

Stubs out HELICS and dots_infrastructure so tests run without a live broker.
"""

import sys
from types import ModuleType
from unittest.mock import MagicMock


def _make_stub(name: str) -> ModuleType:
    mod = ModuleType(name)
    sys.modules[name] = mod
    return mod


# ---------------------------------------------------------------------------
# helics — stub the entire module so imports don't fail
# ---------------------------------------------------------------------------
if "helics" not in sys.modules:
    helics_mod = _make_stub("helics")
    helics_mod.HelicsDataType = MagicMock()
    helics_mod.HelicsLogLevel = MagicMock()


# ---------------------------------------------------------------------------
# dots_infrastructure stubs
# ---------------------------------------------------------------------------
if "dots_infrastructure" not in sys.modules:
    _make_stub("dots_infrastructure")

if "dots_infrastructure.DataClasses" not in sys.modules:
    dc_mod = _make_stub("dots_infrastructure.DataClasses")
    dc_mod.TimeStepInformation = MagicMock
    dc_mod.EsdlId = str
    dc_mod.HelicsCalculationInformation = MagicMock
    dc_mod.PublicationDescription = MagicMock
    dc_mod.SubscriptionDescription = MagicMock
    dc_mod.SimulatorConfiguration = MagicMock

if "dots_infrastructure.HelicsFederateHelpers" not in sys.modules:
    hfh_mod = _make_stub("dots_infrastructure.HelicsFederateHelpers")
    # HelicsSimulationExecutor is the base class — stub it as a plain object
    class _HelicsSimulationExecutor:
        def __init__(self):
            self._calculations = []
        def add_calculation(self, info):
            self._calculations.append(info)
        def start_simulation(self):
            pass
        def stop_simulation(self):
            pass
    hfh_mod.HelicsSimulationExecutor = _HelicsSimulationExecutor

if "dots_infrastructure.EsdlHelperFunctions" not in sys.modules:
    ehf_mod = _make_stub("dots_infrastructure.EsdlHelperFunctions")
    class _EsdlHelperFunctions:
        @staticmethod
        def get_all_esdl_objects_from_type(contents, esdl_type):
            return [obj for obj in contents if isinstance(obj, esdl_type)]
    ehf_mod.EsdlHelperFunctions = _EsdlHelperFunctions


# ---------------------------------------------------------------------------
# esdl stubs — if pyesdl is not installed, stub the whole module so tests
# that don't exercise ESDL parsing still run.
# ---------------------------------------------------------------------------
try:
    import esdl as _esdl_pkg
    from esdl import (  # noqa: F401
        AggregatedConsumer, ElectricityCable, EnergySystem,
        Import, Joint, Transformer,
    )
except ImportError:
    _esdl_stub = _make_stub("esdl")
    for _cls in [
        "AggregatedConsumer", "ElectricityCable", "EnergySystem",
        "Import", "Joint", "Transformer",
    ]:
        setattr(_esdl_stub, _cls, MagicMock)
