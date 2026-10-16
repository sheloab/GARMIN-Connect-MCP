import os

import pytest

from mcp_garmin_connect.tools import get_recovery


@pytest.mark.skipif(os.getenv("GARMIN_LIVE_TEST") != "1", reason="live Garmin test disabled")
def test_live_recovery() -> None:
    result = get_recovery()
    assert "training_readiness" in result
