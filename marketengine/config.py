import os
from dotenv import load_dotenv

# This looks for the .env file and injects its variables directly into your system's memory
load_dotenv() 

class Config:
    MARKET_PROVIDER = os.getenv("MARKET_PROVIDER", "ALPHAVANTAGE")
    ALPHA_VANTAGE_KEY = os.getenv("ALPHA_VANTAGE_KEY")
    