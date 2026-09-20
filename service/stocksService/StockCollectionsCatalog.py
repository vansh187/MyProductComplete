"""
Static tile metadata for the Explore page's "Collections" row - same role
as mutualfunds/collections_config.py's MFCollectionsCatalog, but for stocks.
Built once, per-instance, from whatever sectors the injected watchlist
actually contains, so it never drifts out of sync with the data the app
actually has (rather than a hand-maintained list that could reference a
sector nothing in the watchlist has).
"""


class StockCollectionsCatalog:

    _ICON_BY_SECTOR = {
        "banking": "landmark",
        "it": "cpu",
        "fmcg": "shopping-cart",
        "auto": "car",
        "pharma": "pill",
        "energy": "flame",
        "metal": "hammer",
        "finance": "wallet",
        "telecom": "phone",
        "infrastructure": "building",
        "cement": "building-2",
        "power": "zap",
        "mining": "pickaxe",
        "paints": "palette",
        "consumer": "shopping-bag",
    }
    _DEFAULT_ICON = "briefcase"

    def __init__(self, watchlist_stocks: list[dict]):
        self._watchlist_stocks = watchlist_stocks

    def all(self) -> list[dict]:
        tiles = [{"key": "nifty50", "title": "Nifty 50", "icon_hint": "trending-up"}]

        sectors = sorted({
            str(stock.get("sector")).strip()
            for stock in self._watchlist_stocks
            if stock.get("sector")
        })
        for sector in sectors:
            icon = self._ICON_BY_SECTOR.get(sector.lower(), self._DEFAULT_ICON)
            tiles.append({
                "key": sector.lower().replace(" ", "-"),
                "title": sector,
                "icon_hint": icon,
            })
        return tiles
