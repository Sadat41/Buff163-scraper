# scrape_prices.py
import json
import time
from datetime import datetime, timedelta, timezone
from playwright.sync_api import sync_playwright
import re

# --- Configuration ---
ITEMS_FILE = "items_to_scrape.txt"
JSON_OUTPUT_FILE = "item_overrides.json"
MARKET_IDS_FILE = "marketids.json"
STALE_THRESHOLD_DAYS = 7 # How old an entry can be before we re-scrape it
YUAN_TO_USD_RATE = 0.13937312 # Given conversion rate

def get_items_to_scrape():
    """Reads the list of items from the text file."""
    with open(ITEMS_FILE, "r", encoding='utf-8') as f:
        return [line.strip() for line in f if line.strip()]

def load_existing_data():
    """Loads the existing data from the JSON file."""
    try:
        with open(JSON_OUTPUT_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def load_market_ids():
    """Loads the market IDs from the JSON file."""
    try:
        with open(MARKET_IDS_FILE, "r", encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Error loading {MARKET_IDS_FILE}: {e}")
        return {}

def is_stale(timestamp_str):
    """Checks if a timestamp is older than our threshold."""
    if not timestamp_str:
        return True
    last_updated = datetime.fromisoformat(timestamp_str)
    current_time = datetime.now(timezone.utc).astimezone(last_updated.tzinfo) if last_updated.tzinfo else datetime.now()
    return (current_time - last_updated) > timedelta(days=STALE_THRESHOLD_DAYS)

def save_data(data):
    """Saves the updated data to the JSON file."""
    with open(JSON_OUTPUT_FILE, "w") as f:
        json.dump(data, f, indent=2)

def scrape_buff_price(item_name, browser, market_ids):
    """Scrapes the price of an item from Buff.163.com.
    Returns a tuple (yuan_price, usd_price) or (None, None) on failure.
    """
    if item_name not in market_ids:
        print(f"Error: Item '{item_name}' not found in market IDs. Please ensure the name matches exactly.")
        return None, None
    
    if "buff" not in market_ids[item_name]:
        print(f"Warning: Buff ID not found for '{item_name}'. Skipping.")
        return None, None

    buff_id = market_ids[item_name]["buff"]
    url = f"https://buff.163.com/goods/{buff_id}"
    
    # *** UPDATED PRICE SELECTOR ***
    # This targets a strong tag with class f_Strong, specifically within a td with class t_Left.
    price_selector = 'td.t_Left strong.f_Strong' 

    try:
        page = browser.new_page()
        print(f"Navigating to {url}")
        page.goto(url, wait_until="domcontentloaded") 

        # Keep headless=False for now to visually confirm the correct price is selected
        # page.screenshot(path=f"debug_page_{buff_id}.png") 

        page.wait_for_selector(price_selector, state='visible', timeout=30000) 

        price_element = page.query_selector(price_selector)
        if price_element:
            price_text = price_element.inner_text()
            
            # Use regex to extract only the numeric part for Yuan price
            # It will now correctly parse '¥ 21.2'
            match = re.search(r'[\d,]+\.?\d*', price_text)
            if match:
                yuan_price_str = match.group(0).replace(',', '').strip()
                yuan_price = float(yuan_price_str)
                usd_price = round(yuan_price * YUAN_TO_USD_RATE, 2)
                return yuan_price, usd_price
            else:
                print(f"Error: Could not parse Yuan price from '{price_text}' for {item_name} at {url}.")
                return None, None
        else:
            print(f"Error: Price element not found for {item_name} at {url}.")
            return None, None
    except Exception as e:
        print(f"An error occurred while scraping {item_name} from {url}: {e}")
        return None, None
    finally:
        if 'page' in locals():
            page.close()


def main():
    items_to_scrape = get_items_to_scrape()
    existing_data = load_existing_data()
    market_ids = load_market_ids()

    if not market_ids:
        print("Market IDs not loaded. Exiting.")
        return

    with sync_playwright() as p:
        # Keep headless=False for now to observe the browser and confirm the correct price is scraped
        browser = p.chromium.launch(headless=False) 
        try:
            # --- Automated Scraping for all items in items_to_scrape.txt ---
            print("--- Starting automated scraping of all items ---")
            for item in items_to_scrape:
                print(f"Processing item: {item}")
                last_updated = existing_data.get(item, {}).get("last_updated")

                if last_updated and not is_stale(last_updated):
                    print(f"Skipping {item}: data is not stale (last updated: {last_updated}).")
                    continue

                yuan_price, usd_price = scrape_buff_price(item, browser, market_ids)
                
                if usd_price is not None:
                    current_time_utc = datetime.now(timezone.utc).isoformat()
                    existing_data[item] = {
                        "price_usd": usd_price,
                        "last_updated": current_time_utc
                    }
                    save_data(existing_data)
                
                time.sleep(5)
            print("--- Automated scraping complete ---")
            
            # --- Interactive Mode ---
            print("\n--- Entering interactive price check mode ---")
            print("Type 'exit' to quit.")
            while True:
                user_input = input("Enter item name to check price: ").strip()
                if user_input.lower() == 'exit':
                    break
                
                found_item_key = None
                for key in market_ids:
                    if key.lower().strip() == user_input.lower():
                        found_item_key = key
                        break
                
                if found_item_key:
                    yuan_price, usd_price = scrape_buff_price(found_item_key, browser, market_ids)
                    if usd_price is not None:
                        print(f"Price for '{found_item_key}': ¥ {yuan_price} (${usd_price} USD)")
                    else:
                        print(f"Could not retrieve price for '{found_item_key}'. Check console for errors.")
                else:
                    print(f"Item '{user_input}' not found in market IDs. Please check spelling.")
                
                print("-" * 30)
                time.sleep(1)

        finally:
            browser.close()
    print("Program finished.")

if __name__ == "__main__":
    main()