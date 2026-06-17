from marketengine.dataproviders.AlphaVantageProvider import AlphaVantageProvider
from marketengine.core.MarketEngine import MarketEngine


def test_market_engine():

    provider = AlphaVantageProvider(
        api_key="50NE5QDISSMMAXMT"
    )

    engine = MarketEngine(provider)

    symbols = ["IBM"]

    print("Starting Market Engine Test...")
    print("=" * 50)

    engine.run(symbols)


test_market_engine()