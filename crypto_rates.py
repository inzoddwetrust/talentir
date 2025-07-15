import aiohttp

API_ENDPOINTS = {
    "coingecko": "https://api.coingecko.com/api/v3/simple/price?ids=binancecoin,ethereum,tron&vs_currencies=usd",
    "binance": "https://api.binance.com/api/v3/ticker/price"
}


# Обновление курсов через CoinGecko API
async def fetch_from_coingecko():
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(API_ENDPOINTS["coingecko"]) as response:
                data = await response.json()
                return {
                    "BNB": data["binancecoin"]["usd"],
                    "ETH": data["ethereum"]["usd"],
                    "TRX": data["tron"]["usd"]
                }
        except Exception as e:
            print(f"Error fetching from CoinGecko: {e}")
            return None


# Обновление курсов через Binance API
async def fetch_from_binance():
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(API_ENDPOINTS["binance"]) as response:
                data = await response.json()
                prices = {item["symbol"]: float(item["price"]) for item in data}
                return {
                    "BNB": prices.get("BNBUSDT"),
                    "ETH": prices.get("ETHUSDT"),
                    "TRX": prices.get("TRXUSDT")
                }
        except Exception as e:
            print(f"Error fetching from Binance: {e}")
            return None