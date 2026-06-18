import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # Toggle between: "ALPHAVANTAGE" or "KITECONNECT" or Breeze
    MARKET_PROVIDER = os.getenv("MARKET_PROVIDER", "ALPHAVANTAGE")
    
    # API Credentials
    ALPHA_VANTAGE_KEY = os.getenv("ALPHA_VANTAGE_KEY", "YOUR_FREE_KEY")
    """KITE_API_KEY = os.getenv("KITE_API_KEY", "YOUR_KITE_KEY")
    KITE_ACCESS_TOKEN = os.getenv("KITE_ACCESS_TOKEN", "YOUR_KITE_TOKEN")"""
    
    BREEZE_API_KEY=os.getenv("BREEZE_API_KEY")
    BREEZE_SESSION_TOKEN=os.getenv("")
    BREEZE_SECRET_KEY=os.getenv("BREEZE_SECRET_KEY")
    BREEZE_SESSION_TOKEN=os.getenv("BREEZE_SESSION_TOKEN")

    # Redis Configuration
    #REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")