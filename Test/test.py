from pathlib import Path
import sys

from kiteconnect import KiteConnect
from upstox_client import ApiClient, Configuration
from upstox_client.api import user_api

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import get_access_token, get_api_key, get_upstox_api_key, get_upstox_access_token  # Assuming config has these functions

# Zerodha Kite
kite = KiteConnect(api_key=get_api_key())
kite.set_access_token(get_access_token())

print("Fetching Zerodha profile...")
print(kite.profile())

# Upstox
config = Configuration()
config.api_key['Authorization'] = get_upstox_access_token()
api_client = ApiClient(config)
upstox = user_api.UserApi(api_client)

print("Fetching Upstox profile...")
print(upstox.get_profile())
