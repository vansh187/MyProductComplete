"""
Shoonya WebSocket feed types (NorenRestApiPy FeedType values), shared by
ShoonyaConnection (frame building) and ShoonyaOptionFeed (ref-counting).
"""

TOUCHLINE_FEED = 1   # 't' subscription -> 'tk' / 'tf' frames (best bid/ask, LTP, OHLC, OI)
DEPTH_FEED = 2       # 'd' subscription -> 'dk' / 'df' frames (5-level order book)
DEPTH_FRAME_TYPES = ("dk", "df")
