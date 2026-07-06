"""Direct-API runner: cost math and CLIResult-shape parity (no network)."""
from types import SimpleNamespace
from kalshibot.decision.anthropic_api import AnthropicRunner


def test_cost_computation_haiku_rates():
    usage = SimpleNamespace(input_tokens=3000, output_tokens=150,
                            cache_read_input_tokens=0, cache_creation_input_tokens=0)
    # 3000*$1/1M + 150*$5/1M = 0.003 + 0.00075
    assert AnthropicRunner._cost(usage) == 0.00375


def test_cost_includes_cache_tokens():
    usage = SimpleNamespace(input_tokens=1000, output_tokens=100,
                            cache_read_input_tokens=2000, cache_creation_input_tokens=500)
    cost = AnthropicRunner._cost(usage)
    # 1000e-6 + 100*5e-6 + 2000*0.1e-6 + 500*1.25e-6
    assert round(cost, 6) == round(0.001 + 0.0005 + 0.0002 + 0.000625, 6)
