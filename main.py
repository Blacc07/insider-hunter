import os
import json
import requests
from datetime import datetime

# --- CONFIGURATION ---
DUNE_API_KEY = os.environ.get("DUNE_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Dune Query IDs (Read from environment variables)
DUNE_SOLANA_QUERY_ID = os.environ.get("DUNE_SOLANA_QUERY_ID")
DUNE_EVM_QUERY_ID = os.environ.get("DUNE_EVM_QUERY_ID")
# File to store processed leads for deduplication
STATE_FILE = "processed_leads.json"

def fetch_dune_results(query_id):
    """Fetches results from a Dune Analytics query."""
    url = f"https://api.dune.com/api/v1/query/{query_id}/results"
    headers = {"x-dune-api-key": DUNE_API_KEY}
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json().get("result", {}).get("rows", [])

def fetch_defillama():
    """Fetches all protocols from DefiLlama."""
    url = "https://api.llama.fi/protocols"
    response = requests.get(url)
    response.raise_for_status()
    return response.json()

def normalize_leads(solana_data, evm_data, llama_data):
    """Merges and normalizes data from all sources."""
    leads = []
    
    # 1. Solana (Pump.fun)
    for row in solana_data[:10]: # Limit to 10
        leads.append({
            "source": "Dune_Solana",
            "slug": row.get("tx_hash"),
            "name": row.get("token_name", "Unknown"),
            "symbol": row.get("token_symbol", "???"),
            "chain": "Solana",
            "website": f"https://pump.fun/{row.get('token_address')}",
            "mcap": None
        })

    # 2. EVM (Base/BSC)
    for row in evm_data[:10]: # Limit to 10
        leads.append({
            "source": "Dune_EVM",
            "slug": row.get("tx_hash"),
            "name": "New EVM Deployment",
            "symbol": "N/A",
            "chain": row.get("chain"),
            "website": None, # No website for raw contract deployments
            "mcap": None
        })

    # 3. DefiLlama (Established)
    target_chains = ["Solana", "Base", "Binance"]
    for proto in llama_data:
        chains = proto.get("chains", [])
        has_chain = any(c in target_chains for c in chains)
        mcap = proto.get("mcap")
        
        # Filter: Target chain, Market Cap $100k - $20M, has URL
        if has_chain and mcap and 100000 <= mcap <= 20000000 and proto.get("url"):
            leads.append({
                "source": "DefiLlama",
                "slug": proto.get("slug"),
                "name": proto.get("name"),
                "symbol": "N/A",
                "chain": next((c for c in chains if c in target_chains), "Unknown"),
                "website": proto.get("url"),
                "mcap": mcap
            })
            
    # Take top 5 from DefiLlama to balance the list
    llama_leads = [l for l in leads if l["source"] == "DefiLlama"][:5]
    other_leads = [l for l in leads if l["source"] != "DefiLlama"]
    
    return other_leads + llama_leads

def load_processed_slugs():
    """Loads previously processed slugs from the state file."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return []

def save_processed_slugs(slugs):
    """Saves processed slugs to the state file."""
    with open(STATE_FILE, "w") as f:
        json.dump(slugs, f)

def send_telegram_message(lead):
    """Sends a formatted message to Telegram."""
    mcap_str = f"${int(lead['mcap']):,}" if lead['mcap'] else "New/Unknown"
    website_str = lead['website'] if lead['website'] else "Manual Search Required"
    
    text = (
        f"🚨 *New Vibe Trading Lead*\n\n"
        f"📛 *Project*: {lead['name']} ({lead['symbol']})\n"
        f"⛓️ *Chain*: {lead['chain']}\n"
        f"💰 *Market Cap*: {mcap_str}\n"
        f"🔗 *Website*: {website_str}\n\n"
        f" *Vibe Pitch*: \"Instead of dumping treasury, deposit tokens to Vibe's vault to list a perp for free, back OI, and earn trading fees forever.\""
    )
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }
    requests.post(url, json=payload)

def main():
    print("Starting Vibe Trading Lead Gen Pipeline...")
    
    # 1. Fetch Data
    solana_data = fetch_dune_results(DUNE_SOLANA_QUERY_ID)
    evm_data = fetch_dune_results(DUNE_EVM_QUERY_ID)
    llama_data = fetch_defillama()
    
    # 2. Normalize
    all_leads = normalize_leads(solana_data, evm_data, llama_data)
    print(f"Normalized {len(all_leads)} potential leads.")
    
    # 3. Deduplicate
    processed_slugs = load_processed_slugs()
    new_leads = [lead for lead in all_leads if lead["slug"] not in processed_slugs]
    print(f"Found {len(new_leads)} new leads.")
    
    # 4. Process and Send
    for lead in new_leads:
        send_telegram_message(lead)
        processed_slugs.append(lead["slug"])
        
    # 5. Save State
    save_processed_slugs(processed_slugs)
    print("Pipeline finished successfully.")

if __name__ == "__main__":
    main()
