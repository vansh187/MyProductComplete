"""
Regression tests for _ShoonyaApi's websocket URL derivation.

A plain `.replace("https://", "wss://")` silently leaves the scheme
untouched for anything that isn't an exact lowercase "https://" prefix
(different case, a bare host with no scheme, an already-wss URL) - NorenApi
then hands that URL straight to websocket-client, which rejects non-ws(s)
schemes with ValueError("scheme https is invalid"), causing the option-chain
feed to fail every reconnect attempt.
"""

import pytest

from marketengine.ShoonyaConnection import _ShoonyaApi

pytestmark = pytest.mark.skipif(
    _ShoonyaApi.__mro__[-2].__name__ == "object",
    reason="NorenRestApiOAuth not installed",
)


@pytest.mark.parametrize("api_url,expected", [
    ("https://api.shoonya.com/NorenWClientAPI/", "wss://api.shoonya.com/NorenWSAPI"),
    ("HTTPS://api.shoonya.com/NorenWClientAPI/", "wss://api.shoonya.com/NorenWSAPI"),
    ("http://api.shoonya.com/NorenWClientAPI/", "wss://api.shoonya.com/NorenWSAPI"),
    ("api.shoonya.com/NorenWClientAPI/", "wss://api.shoonya.com/NorenWSAPI"),
    ("wss://api.shoonya.com/NorenWSAPI/", "wss://api.shoonya.com/NorenWSAPI"),
])
def test_websocket_endpoint_always_forced_to_wss(api_url, expected):
    api = _ShoonyaApi(api_url)
    assert api._NorenApi__service_config["websocket_endpoint"] == expected
