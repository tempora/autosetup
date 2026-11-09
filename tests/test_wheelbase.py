from autosetup.metrics import signals
from autosetup.metrics.compute import steering_geometry
from autosetup.lmu.setup_space import CarSetup
from tests.conftest import setup_json


def test_wheelbase_by_model_then_class():
    before = signals.livery_models()
    try:
        signals.set_car_models({"TF Sport 2026 #2": "Corvette_Z06_LMGT3R", "Proton Competition 2025 #77": "Ford_Mustang_LMGT3",
                                 "Akkodis ASP 2025 #78": "Lexus_RCF_GT3", "Iron Lynx 2025 #60": "Mercedes_AMG_GT3"})
        assert signals.wheelbase("GT3", "TF Sport 2026 #2") == 2.722
        assert signals.wheelbase("GT3", "Proton Competition 2025 #77") == 2.777
        assert signals.wheelbase("GT3", "Someone 2026 #99") == 2.65  # livery not matched yet
        assert signals.wheelbase("GT3", "Akkodis ASP 2025 #78") == 2.730
        assert signals.wheelbase("GT3", "Iron Lynx 2025 #60") == 2.630
        assert steering_geometry(CarSetup.from_json(setup_json()), "GT3", "TF Sport 2026 #2") == (280.0, 16.5, 2.722)
    finally:
        signals.set_car_models(before)
