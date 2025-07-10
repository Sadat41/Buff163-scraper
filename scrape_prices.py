# scrape_prices.py
import json
import time
from datetime import datetime, timedelta, timezone
from playwright.sync_api import sync_playwright
import re

# --- Configuration ---
ITEMS_FILE = "items_to_scrape.txt"
JSON_OUTPUT_FILE = "item_overrides.json"
MARKET_IDS_FILE = "marketids.json"  # File for Buff.163.com item IDs
STALE_THRESHOLD_DAYS = 7  # How old an entry can be before we re-scrape it
YUAN_TO_USD_RATE = 0.13937312  # Given conversion rate

def get_items_to_scrape():
    """Reads the list of items from the text file."""
    with open(ITEMS_FILE, "r", encoding='utf-8') as f: # Added encoding='utf-8' for robust file reading
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
        with open(MARKET_IDS_FILE, "r", encoding='utf-8') as f: # Added encoding='utf-8' for robust file reading
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Error loading {MARKET_IDS_FILE}: {e}") # Improved error message
        return {}

def is_stale(timestamp_str):
    """Checks if a timestamp is older than our threshold."""
    if not timestamp_str:
        return True
    last_updated = datetime.fromisoformat(timestamp_str)
    # Ensure current_time is timezone-aware if last_updated is
    current_time = datetime.now(timezone.utc).astimezone(last_updated.tzinfo) if last_updated.tzinfo else datetime.now()
    return (current_time - last_updated) > timedelta(days=STALE_THRESHOLD_DAYS)

def save_data(data):
    """Saves the updated data to the JSON file."""
    with open(JSON_OUTPUT_FILE, "w") as f:
        json.dump(data, f, indent=2)

def scrape_buff_price(item_name_with_phase, browser, market_ids):
    """
    Scrapes the price of an item from Buff.163.com, handling phases.
    Returns a tuple (yuan_price, usd_price) or (None, None) on failure.
    """
    base_item_name = item_name_with_phase
    phase_tag_id = None
    url_params = ""

    # Check if the item name contains phase information (e.g., " - Phase 1")
    phase_pattern = r'^(.*)\s*-\s*(Phase\s*\d|Ruby|Sapphire|Black Pearl)$'
    phase_match = re.search(phase_pattern, item_name_with_phase, re.IGNORECASE)

    if phase_match:
        base_item_name = phase_match.group(1).strip()
        phase_name = phase_match.group(2).strip()
        print(f"Detected base item: '{base_item_name}', Phase: '{phase_name}'")

    if base_item_name not in market_ids:
        print(f"Error: Base item '{base_item_name}' not found in market IDs. Please ensure the name matches exactly.")
        return None, None
    
    item_data = market_ids[base_item_name]

    if "buff" not in item_data:
        print(f"Warning: Buff ID not found for base item '{base_item_name}'. Skipping.")
        return None, None

    buff_id = item_data["buff"]

    # Handle phased items and potential login requirement
    if phase_match and "buff_phase" in item_data:
        if phase_name in item_data["buff_phase"]:
            phase_tag_id = item_data["buff_phase"][phase_name]
            url_params = f"?from=market#tag_ids={phase_tag_id}"
            print(f"Found phase tag ID: {phase_tag_id} for '{phase_name}'.")
            print(f"Skipping '{item_name_with_phase}'. Phased items might require login to scrape correctly. Please handle manually for now.")
            return None, None
        else:
            print(f"Warning: Phase '{phase_name}' not found in buff_phase for '{base_item_name}'. Proceeding without phase tag.")

    url = f"https://buff.163.com/goods/{buff_id}{url_params}"
    # Updated price selector to target the lowest sell order price
    price_selector = 'td.t_Left strong.f_Strong'

    try:
        page = browser.new_page()
        print(f"Navigating to {url}")
        page.goto(url, wait_until="domcontentloaded")

        # Optional: Add screenshot for debugging if needed (uncomment to activate)
        # page.screenshot(path=f"debug_page_{buff_id}_{phase_name}.png" if phase_name else f"debug_page_{buff_id}.png")

        page.wait_for_selector(price_selector, state='visible', timeout=30000)

        price_element = page.query_selector(price_selector)
        if price_element:
            price_text = price_element.inner_text() # Use inner_text for direct content
            
            # Use regex to extract only the numeric part for Yuan price
            match = re.search(r'[\d,]+\.?\d*', price_text)
            if match:
                yuan_price_str = match.group(0).replace(',', '').strip()
                yuan_price = float(yuan_price_str)
                usd_price = round(yuan_price * YUAN_TO_USD_RATE, 2)
                return yuan_price, usd_price
            else:
                print(f"Error: Could not parse Yuan price from '{price_text}' for {item_name_with_phase} at {url}.")
                return None, None
        else:
            print(f"Error: Price element not found for {item_name_with_phase} at {url}.")
            return None, None
    except Exception as e:
        print(f"An error occurred while scraping {item_name_with_phase} from {url}: {e}")
        return None, None
    finally:
        if 'page' in locals():
            page.close()


def run_automated_scrape(playwright_instance, items_to_scrape, market_ids, existing_data):
    """Performs the automated scraping of all items from the list."""
    updated_count = 0
    browser = playwright_instance.chromium.launch(headless=True) # Set to headless=True for automated runs
    try:
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
            
            time.sleep(5) # Delay to avoid overwhelming the server
        print("--- Automated scraping complete ---")
    finally:
        browser.close()
    return updated_count

def run_interactive_check(playwright_instance, market_ids):
    """Allows interactive checking of item prices."""
    browser = playwright_instance.chromium.launch(headless=False) # Keep headless=False for interactive mode
    try:
        print("\n--- Entering interactive price check mode ---")
        print("Type 'exit' to quit.")
        while True:
            user_input = input("Enter item name to check price (e.g., '★ Bayonet | Doppler (Factory New) - Phase 1'): ").strip()
            if user_input.lower() == 'exit':
                break
            
            found_item_key = None
            # First, check if the full user_input exists as a key in market_ids
            if user_input in market_ids:
                found_item_key = user_input
            else:
                # If not, try to match the base name if it's a phased item
                phase_pattern = r'^(.*)\s*-\s*(Phase\s*\d|Ruby|Sapphire|Black Pearl)$'
                phase_match = re.search(phase_pattern, user_input, re.IGNORECASE)
                if phase_match:
                    base_name_from_input = phase_match.group(1).strip()
                    if base_name_from_input in market_ids:
                        found_item_key = user_input # Keep the full string, scrape_buff_price will parse it
                else: # If not a phased item pattern, try case-insensitive match for existing keys
                     for key in market_ids:
                        if key.lower().strip() == user_input.lower():
                            found_item_key = key
                            break

            if found_item_key:
                yuan_price, usd_price = scrape_buff_price(found_item_key, browser, market_ids)
                if usd_price is not None:
                    print(f"Price for '{found_item_key}': ¥ {yuan_price} (${usd_price} USD)") # Display both
                else:
                    print(f"Could not retrieve price for '{found_item_key}'. Check console for errors.")
            else:
                print(f"Item '{user_input}' not found in market IDs. Please ensure the name matches exactly or check your spelling. If it's a phased item, use the format 'Base Item Name - Phase X'.")
            
            print("-" * 30)
            time.sleep(1) # Small delay before next prompt

    finally:
        browser.close()


def main():
    """Main function to run the scraper."""
    items_to_scrape = get_items_to_scrape()
    existing_data = load_existing_data()
    market_ids = load_market_ids()

    if not market_ids:
        print("Market IDs not loaded. Exiting.")
        return

    with sync_playwright() as p:
        # Run automated scrape
        run_automated_scrape(p, items_to_scrape, market_ids, existing_data)
        
        # Then, offer interactive mode (optional, for debugging/manual checks)
        # You might want to remove or comment out this line when integrating into an extension
        run_interactive_check(p, market_ids)
    
    print("Program finished.")

if __name__ == "__main__":
    main()