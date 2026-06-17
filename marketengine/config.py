import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # Toggle between: "ALPHAVANTAGE" or "KITECONNECT"
    MARKET_PROVIDER = os.getenv("MARKET_PROVIDER", "ALPHAVANTAGE")
    
    # API Credentials
    ALPHA_VANTAGE_KEY = os.getenv("ALPHA_VANTAGE_KEY", "YOUR_FREE_KEY")
    """KITE_API_KEY = os.getenv("KITE_API_KEY", "YOUR_KITE_KEY")
    KITE_ACCESS_TOKEN = os.getenv("KITE_ACCESS_TOKEN", "YOUR_KITE_TOKEN")"""
    
    # Redis Configuration
    #REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")