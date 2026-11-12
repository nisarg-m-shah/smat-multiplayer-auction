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

# How long the SOLD / UNSOLD result stays on every screen before the next player opens.
# The server holds the next player back for this long, so nobody loses bidding time behind the banner.
# Set to 0 to open the next player immediately.
RESULT_HOLD_SOLD = float(os.getenv("RESULT_HOLD_SOLD", "5.0"))
RESULT_HOLD_UNSOLD = float(os.getenv("RESULT_HOLD_UNSOLD", "3.5"))

# Late-bid grace. A bid is judged by when its request ARRIVED at the server, not by when the database work
# finished, and bids that arrive up to this many seconds after the deadline still count (covers phone latency
# and a press at 0.3s left). The player is sold only after the grace (plus a small processing slack) has passed.
BID_GRACE_SECONDS = float(os.getenv("BID_GRACE_SECONDS", "0.8"))
SETTLE_SLACK_SECONDS = float(os.getenv("SETTLE_SLACK_SECONDS", "0.25")) if BID_GRACE_SECONDS > 0 else 0.0

# When every franchise that can still bid has declared itself out, the clock is trimmed to this fraction of the
# timer (it is never lengthened), so anyone who changes their mind still has time to rejoin.
ALL_OUT_FRACTION = float(os.getenv("ALL_OUT_FRACTION", "0.5"))

# Important players get a longer clock, but only in rooms whose timer is at least this many seconds.
# The bonus is up to IMPORTANCE_TIMER_BONUS x the room timer for the pool's highest importance weight,
# scaled logarithmically so one extreme weight does not flatten everyone else.
IMPORTANCE_MIN_TIMER = int(os.getenv("IMPORTANCE_MIN_TIMER", "25"))
IMPORTANCE_TIMER_BONUS = float(os.getenv("IMPORTANCE_TIMER_BONUS", "0.5"))
