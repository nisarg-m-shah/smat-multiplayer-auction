import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./smat_auction.db")
APP_SECRET = os.getenv("APP_SECRET", "development-only-change-me")
AUCTION_SOURCE = os.getenv("AUCTION_SOURCE", "./data/Auction.py")

# Change this one value if you want a different default maximum squad size.
MAX_SQUAD = int(os.getenv("MAX_SQUAD", "25"))
MIN_SQUAD = 18
DEFAULT_PURSE = 5000
DEFAULT_BASE_PRICE = 20
MINIMUM_BID = 10
