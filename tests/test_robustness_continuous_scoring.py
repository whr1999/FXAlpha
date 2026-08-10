from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
QUANTGPT_ROOT = PROJECT_ROOT / "third_party" / "quantgpt"
if str(QUANTGPT_ROOT) not in sys.path:
    sys.path.insert(0, str(QUANTGPT_ROOT))

from quantgpt.anti_overfit import TestResult as AntiTestResult, score_anti_overfit_tests
from quantgpt.adversarial_validator import AdvTestResult as AdversarialTestResult, score_adversarial_tests


def test_anti_overfit_score_is_continuous_and_margin_sensitive():
    weak_tests = [
        AntiTestResult("IC Stability", True, {"ic_mean": 0.021, "positive_rate": 0.56, "yearly_ic": {"2022": 0.018, "2023": 0.024}, "has_reversal": False}),
        AntiTestResult("Sub-sample Stress", True, {"consistency": 0.62, "sub_sample_ics": {"bull": 0.028, "bear": 0.020, "sideways": 0.018}}),
        AntiTestResult("Placebo", True, {"real_ic": 0.030, "perm_95th": 0.026, "shift_ics": {"5": 0.027, "10": 0.024, "20": 0.021}}),
        AntiTestResult("Half-life", True, {"half_life_days": 5.5, "period_ics": {"1": 0.060, "2": 0.052, "5": 0.041, "10": 0.039}}),
    ]
    strong_tests = [
        AntiTestResult("IC Stability", True, {"ic_mean": 0.052, "positive_rate": 0.72, "yearly_ic": {"2022": 0.044, "2023": 0.051, "2024": 0.048}, "has_reversal": False}),
        AntiTestResult("Sub-sample Stress", True, {"consistency": 0.92, "sub_sample_ics": {"bull": 0.051, "bear": 0.047, "sideways": 0.043, "high_vol": 0.050}}),
        AntiTestResult("Placebo", True, {"real_ic": 0.058, "perm_95th": 0.012, "shift_ics": {"5": 0.024, "10": 0.014, "20": 0.006}}),
        AntiTestResult("Half-life", True, {"half_life_days": 12.0, "period_ics": {"1": 0.091, "2": 0.081, "5": 0.061, "10": 0.038, "20": 0.020}}),
    ]

    weak_score, weak_parts = score_anti_overfit_tests(weak_tests)
    strong_score, strong_parts = score_anti_overfit_tests(strong_tests)

    assert strong_score > weak_score
    assert strong_score not in {0.0, 25.0, 50.0, 75.0, 100.0}
    assert weak_score not in {0.0, 25.0, 50.0, 75.0, 100.0}
    assert strong_parts["placebo"] > weak_parts["placebo"]
    assert strong_parts["half_life"] > weak_parts["half_life"]


def test_adversarial_score_is_continuous_and_temporal_ratio_sensitive():
    weak_tests = [
        AdversarialTestResult("Label Permutation", True, {"real_ic": 0.030, "perm_95th_abs": 0.026, "perm_mean_abs": 0.018}),
        AdversarialTestResult("Temporal Shuffle", False, {"real_ic_abs": 0.030, "shuffled_ic_abs_mean": 0.027, "ratio": 1.11}),
        AdversarialTestResult("Random Universe", True, {"consistency": 0.72, "subset_ic_mean": 0.029, "subset_ic_std": 0.011}),
        AdversarialTestResult("Noise Injection", True, {"base_ic_abs": 0.030, "noise_ics_abs": {"0.5": 0.017, "1.0": 0.008}, "retain_at_0.5": 0.57}),
    ]
    strong_tests = [
        AdversarialTestResult("Label Permutation", True, {"real_ic": 0.058, "perm_95th_abs": 0.010, "perm_mean_abs": 0.004}),
        AdversarialTestResult("Temporal Shuffle", True, {"real_ic_abs": 0.058, "shuffled_ic_abs_mean": 0.027, "ratio": 2.15}),
        AdversarialTestResult("Random Universe", True, {"consistency": 1.0, "subset_ic_mean": 0.056, "subset_ic_std": 0.003}),
        AdversarialTestResult("Noise Injection", True, {"base_ic_abs": 0.058, "noise_ics_abs": {"0.5": 0.045, "1.0": 0.025}, "retain_at_0.5": 0.78}),
    ]

    weak_score, weak_parts = score_adversarial_tests(weak_tests)
    strong_score, strong_parts = score_adversarial_tests(strong_tests)

    assert strong_score > weak_score
    assert strong_score not in {0.0, 25.0, 50.0, 75.0, 100.0}
    assert weak_score not in {0.0, 25.0, 50.0, 75.0, 100.0}
    assert strong_parts["temporal_shuffle"] > weak_parts["temporal_shuffle"]
    assert strong_parts["noise_injection"] > weak_parts["noise_injection"]
