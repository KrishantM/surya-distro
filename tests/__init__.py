"""Tests never touch the network. Any request that reaches the real opener fails."""
from surya_distro import net


def _no_network(req, timeout):
    raise AssertionError(f"a test tried to reach the network: {req.get_method()} {req.full_url}")


net._open = _no_network
