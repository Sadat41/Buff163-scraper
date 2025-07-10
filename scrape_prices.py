# scrape_prices.py
import json
import time
from datetime import datetime, timedelta, timezone
from playwright.sync_api import sync_playwright

# --- Configuration ---
ITEMS_FILE = "items_to_scrape.txt"
JSON_OUTPUT_FILE = "item_overrides.json"
STALE_THRESHOLD_DAYS = 7 # How old an entry can be before we re-scrape it

def get_items_to_scrape():
    """Reads the list of items from the text file."""
    with open(ITEMS_FILE, "r") as f:
        return [line.strip() for line in f if line.strip()]

def load_existing_data():
    """Loads the existing data from the JSON file."""
    try:
        with open(JSON_OUTPUT_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def is_stale(timestamp_str):
    """Checks if a timestamp is older than our threshold."""
    if not timestamp_str:
        return True
    last_updated = datetime.fromisoformat(timestamp_str)
    return datetime.now(timezone.utc) - last_updated > timedelta(days=STALE_THRESHOLD_DAYS)

def scrape_csfloat_price(playwright, item_name):
    """
    Navigates to CSFloat and scrapes the price for a given item.
    Note: CSS selectors can change. This may need maintenance.
    """
    print(f"Scraping for: {item_name}...")
    page = None
    browser = None
    try:
        browser = playwright.chromium.launch()
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/98.0.4758.102 Safari/537.36")
        page = context.new_page()

        # Construct the URL for searching
        url = f"https://csfloat.com/search?market_hash_name={item_name.replace(' ', '%20')}"
        
        # Navigate and wait for the network to be idle, indicating loading is likely complete
        page.goto(url, wait_until='networkidle', timeout=30000)

        # Wait for the price element to appear. THIS IS THE MOST FRAGILE PART.
        # This selector targets the first item card and finds the price inside it.
        # You may need to update this if the site's layout changes.
        price_selector = 'div[data-testid^="item-card-"] span.text-2xl.font-bold'
        page.wait_for_selector(price_selector, timeout=15000)
        
        price_text = page.locator(price_selector).first.inner_text()
        
        # Clean the price text (e.g., "$1,234.56" -> 1234.56)
        price_float = float(price_text.replace('$', '').replace(',', ''))
        
        print(f"  > Found price: ${price_float}")
        return price_float

    except Exception as e:
        print(f"  > Failed to scrape {item_name}: {e}")
        return None
    finally:
        if page:
            page.close()
        if browser:
            browser.close()


def main():
    items_to_scrape = get_items_to_scrape()
    data = load_existing_data()
    updated_count = 0

    with sync_playwright() as p:
        for item_name in items_to_scrape:
            # Check if the item needs updating
            item_data = data.get(item_name.lower())
            if item_data and not is_stale(item_data.get("lastUpdated")):
                print(f"Skipping fresh item: {item_name}")
                continue

            # Scrape the item
            price = scrape_csfloat_price(p, item_name)

            if price is not None:
                # Update data with new price and timestamp
                data[item_name.lower()] = {
                    "price": price,
                    "lastUpdated": datetime.now(timezone.utc).isoformat()
                }
                updated_count += 1
            
            # Be a good citizen and don't spam the server
            time.sleep(5) # 5-second delay between requests

    # Save the updated data back to the JSON file
    with open(JSON_OUTPUT_FILE, "w") as f:
        json.dump(data, f, indent=2)

    print(f"\nScraping complete. Updated {updated_count} items.")
    print(f"Data saved to {JSON_OUTPUT_FILE}")

if __name__ == "__main__":
    main()