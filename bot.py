import requests
import os
import time
from datetime import datetime

# --- CONFIGURATION ---
ALCHEMY_API_KEY = os.getenv("ALCHEMY_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

MAX_AGE_MINUTES = 10
MIN_LIQUIDITY = 5000

def get_fresh_pools():
    """Fetches the newest pools on Base network."""
    url = "https://api.geckoterminal.com/api/v2/networks/base/new_pools"
    response = requests.get(url).json()
    fresh_pools = []
    now = time.time() * 1000
    
    for pool in response.get("data", [])[:10]:
        attrs = pool.get("attributes", {})
        rel = pool.get("relationships", {})
        
        # Calculate age
        created_at_str = attrs.get("pool_created_at", "1970-01-01T00:00:00Z")
        created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00")).timestamp() * 1000
        age_minutes = (now - created_at) / 60000
        
        # FIX: Handle None values properly
        liquidity_val = attrs.get("reserve_in_usd")
        liquidity = float(liquidity_val) if liquidity_val is not None else 0.0
        
        token_address = rel.get("base_token", {}).get("data", {}).get("id", "").replace("base_", "")
        token_name = attrs.get("name", "").split("/")[0]
        
        if age_minutes <= MAX_AGE_MINUTES and liquidity >= MIN_LIQUIDITY and token_address:
            fresh_pools.append({
                "token_address": token_address,
                "token_name": token_name,
                "pool_address": pool["id"].replace("base_", ""),
                "liquidity": liquidity,
                "dex_url": f"https://dexscreener.com/base/{token_address}"
            })
    return fresh_pools[:3]

def get_first_buyers(token_address, pool_address):
    """Fetches the first 3 buyers of the token."""
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "alchemy_getAssetTransfers",
        "params": [{
            "fromBlock": "0x0", "toBlock": "latest", "category": ["erc20"],
            "contractAddresses": [token_address], "order": "asc",
            "maxCount": "0xa", "excludeZeroValue": True
        }]
    }
    url = f"https://base-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}"
    response = requests.post(url, json=payload).json()
    
    buyers = []
    for t in response.get("result", {}).get("transfers", [])[:3]:
        buyer = t.get("to", "").lower()
        if buyer and buyer != "0x0000000000000000000000000000000000000000" and buyer != pool_address.lower():
            buyers.append(buyer)
    return buyers

def check_wallet_experience(wallet):
    """Checks if a wallet is a 'Newbie' or 'Experienced'."""
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "alchemy_getAssetTransfers",
        "params": [{
            "fromBlock": "0x0", "toBlock": "latest", "category": ["erc20"],
            "fromAddress": wallet, "maxCount": "0x1"
        }]
    }
    url = f"https://base-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}"
    response = requests.post(url, json=payload).json()
    transfers = response.get("result", {}).get("transfers", [])
    
    is_known = len(transfers) > 0 
    return is_known

def send_alert(message):
    """Sends the formatted message to Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID, 
        "text": message, 
        "parse_mode": "HTML", 
        "disable_web_page_preview": True
    })

def main():
    print(f"[{datetime.now()}] 🚀 Starting Insider Hunter scan...")
    pools = get_fresh_pools()
    
    if not pools:
        print("No fresh pools matching criteria found.")
        return

    for pool in pools:
        print(f"🔍 Checking buyers for ${pool['token_name']}...")
        buyers = get_first_buyers(pool["token_address"], pool["pool_address"])
        
        for buyer in buyers:
            is_known = check_wallet_experience(buyer)
            
            if is_known:
                emoji, status = "️", "Known Insider Detected"
            else:
                emoji, status = "🆕", "New Insider Detected"
            
            message = (
                f"{emoji} <b>{status}</b>\n\n"
                f"<b>Token:</b> ${pool['token_name']}\n"
                f"<b>Wallet:</b> <code>{buyer}</code>\n"
                f"<b>Pool Liq:</b> ${int(pool['liquidity'])}\n\n"
                f"🔗 <a href='{pool['dex_url']}'>DexScreener</a> | <a href='https://basescan.org/address/{buyer}'>BaseScan</a>"
            )
            
            print(f"📤 Sending alert for {buyer}...")
            send_alert(message)
            time.sleep(1.5)

    print("✅ Scan complete.")

if __name__ == "__main__":
    main()
