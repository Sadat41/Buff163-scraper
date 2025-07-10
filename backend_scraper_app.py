# backend_scraper_app.py
from flask import Flask, jsonify, request
import subprocess
import os
import json
import time
from datetime import datetime, timedelta, timezone
from playwright.sync_api import sync_playwright # Ensure Playwright is installed and browsers are installed
import re # REQUIRED for regex in scrape_buff_price

app = Flask(__name__)

# --- Configuration ---
# IMPORTANT: Adjust these paths to your local setup
# Path to your Buff163-scraper repository cloned locally
# This should be the directory where this backend_scraper_app.py resides,
# and where your item_overrides.json, items_to_scrape.txt, marketids.json are.
GITHUB_REPO_PATH = os.getcwd() # This automatically sets it to the current directory of this script

# Path to the JSON output file within the repository
JSON_OUTPUT_FILE_NAME = "item_overrides.json"
JSON_OUTPUT_FILE_PATH = os.path.join(GITHUB_REPO_PATH, JSON_OUTPUT_FILE_NAME)

# Make sure these match the ones in your scrape_prices.py if they are also used there
ITEMS_FILE = os.path.join(GITHUB_REPO_PATH, "items_to_scrape.txt")
MARKET_IDS_FILE = os.path.join(GITHUB_REPO_PATH, "marketids.json")
STALE_THRESHOLD_DAYS = 7
YUAN_TO_USD_RATE = 0.13937312 # This should be consistent or fetched dynamically if possible

def get_items_to_scrape():
    """Reads the list of items from the text file."""
    try:
        with open(ITEMS_FILE, "r", encoding='utf-8') as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        print(f"Error: {ITEMS_FILE} not found. Please create it.")
        return []

def load_existing_data():
    """Loads the existing data from the JSON file."""
    try:
        with open(JSON_OUTPUT_FILE_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def load_market_ids():
    """Loads the market IDs from the JSON file."""
    try:
        with open(MARKET_IDS_FILE, "r", encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Error: {MARKET_IDS_FILE} not found or invalid. Please ensure it exists and is valid JSON. Error: {e}")
        return {}

def is_stale(timestamp_str):
    """Checks if a timestamp is older than our threshold."""
    if not timestamp_str:
        return True
    last_updated = datetime.fromisoformat(timestamp_str)
    # Ensure timezone awareness for comparison
    current_time = datetime.now(timezone.utc).astimezone(last_updated.tzinfo) if last_updated.tzinfo else datetime.now(timezone.utc)
    return (current_time - last_updated) > timedelta(days=STALE_THRESHOLD_DAYS)

def save_data(data):
    """Saves the updated data to the JSON file."""
    with open(JSON_OUTPUT_FILE_PATH, "w") as f:
        json.dump(data, f, indent=2)

def scrape_buff_price(item_name_with_phase, page, market_ids):
    """
    Scrapes the price of an item from Buff.163.com, handling phases.
    Returns a tuple (yuan_price, usd_price) or (None, None) on failure.
    """
    base_item_name = item_name_with_phase
    phase_tag_id = None
    url_params = ""

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

    if phase_match and "buff_phase" in item_data:
        if phase_name in item_data["buff_phase"]:
            phase_tag_id = item_data["buff_phase"][phase_name]
            url_params = f"?from=market#tag_ids={phase_tag_id}"
            print(f"Found phase tag ID: {phase_tag_id} for '{phase_name}'.")
            print(f"Skipping '{item_name_with_phase}'. Phased items might require login to scrape correctly or may not be listed.")
            return None, None
        else:
            print(f"Warning: Phase '{phase_name}' not found in buff_phase for '{base_item_name}'. Proceeding without phase tag.")

    url = f"https://buff.163.com/goods/{buff_id}{url_params}"
    
    # === CRITICAL: This is the selector that needs to be accurate ===
    # Based on past experience, this selector 'td.t_Left strong.f_Strong' can become outdated.
    # You MUST verify this selector by inspecting the page on Buff.163.com for an item like AWP Gungnir (https://buff.163.com/goods/776029).
    # Update it if necessary to accurately target the main item price (e.g., ¥ 72240).
    price_selector = 'td.t_Left strong.f_Strong' 
    
    try:
        print(f"Navigating to {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=60000) # Increased timeout
        print(f"Page loaded: {page.url}")

        # Wait for the price element to be visible
        page.wait_for_selector(price_selector, state='visible', timeout=60000) # Increased timeout

        price_element = page.query_selector(price_selector)
        if price_element:
            price_text = price_element.inner_text()
            print(f"DEBUG: Raw price text found by selector: '{price_text}'") # <-- DEBUG PRINT
            
            # Use regex to robustly extract the numerical part (handles commas, decimals)
            match = re.search(r'[\d,]+\.?\d*', price_text)
            if match:
                yuan_price_str = match.group(0).replace(',', '').strip()
                yuan_price = float(yuan_price_str)
                usd_price = round(yuan_price * YUAN_TO_USD_RATE, 2)
                print(f"DEBUG: Parsed Yuan price: {yuan_price}, Calculated USD price: {usd_price}") # <-- DEBUG PRINT
                return yuan_price, usd_price
            else:
                print(f"Error: Could not parse Yuan price from raw text '{price_text}' for {item_name_with_phase} at {url}.")
                return None, None
        else:
            print(f"Error: Price element NOT FOUND with selector '{price_selector}' for {item_name_with_phase} at {url}. Page URL: {page.url}")
            return None, None
    except Exception as e:
        print(f"An error occurred while scraping {item_name_with_phase} from {url}: {e}")
        return None, None


@app.route('/scrape-prices', methods=['POST'])
def scrape_prices_endpoint():
    """
    API endpoint to trigger the scraper.
    Expects a JSON body with an optional 'item' field to scrape a specific item,
    or triggers a full scrape if 'item' is not provided.
    """
    data = request.get_json()
    item_to_scrape = data.get('item') if data else None

    print(f"Received request to scrape: {item_to_scrape if item_to_scrape else 'all items'}")

    try:
        market_ids = load_market_ids()
        if not market_ids:
            return jsonify({"status": "error", "message": "Market IDs not loaded. Cannot scrape."}), 500

        existing_data = load_existing_data()
        updated_data = existing_data.copy()
        scraped_item_data = None # To return specific item data if requested

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True) # Run headless for backend
            page = browser.new_page() # Use a single page for efficiency in this context

            items_list = [item_to_scrape] if item_to_scrape else get_items_to_scrape()

            if not items_list:
                browser.close()
                return jsonify({"status": "error", "message": "No items to scrape configured."}), 500

            for item_key in items_list:
                # Check if data is stale or missing
                is_stale_data = True
                if item_key in existing_data:
                    timestamp_str = existing_data[item_key].get("timestamp")
                    if timestamp_str:
                        is_stale_data = is_stale(timestamp_str)

                if is_stale_data:
                    print(f"Scraping {item_key} (stale/missing data)...")
                    yuan_price, usd_price = scrape_buff_price(item_key, page, market_ids)
                    if usd_price is not None:
                        updated_data[item_key] = {
                            "yuan_price": yuan_price,
                            "usd_price": usd_price,
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        }
                        if item_key == item_to_scrape:
                            scraped_item_data = updated_data[item_key]
                        print(f"Successfully scraped {item_key}")
                    else:
                        print(f"Failed to scrape {item_key}")
                else:
                    print(f"Using fresh existing data for {item_key}")
                    if item_key == item_to_scrape:
                        scraped_item_data = existing_data[item_key]
                
                time.sleep(2) # Delay between item scrapes to be polite to Buff.163.com

            browser.close() # Close browser after all items are processed

        # Save updated data to the JSON file
        save_data(updated_data)
        print(f"Updated {JSON_OUTPUT_FILE_NAME} locally.")

        try:
            subprocess.run(["git", "add", JSON_OUTPUT_FILE_NAME], check=True)
            subprocess.run(["git", "commit", "-m", "Automated update of item_overrides.json (local commit)"], check=True)
            print("Changes committed locally.")
            # Removed automatic git push from here to avoid instability
            # subprocess.run(["git", "push"], check=True)
            # print("Changes pushed to GitHub.")
        except subprocess.CalledProcessError as git_e:
            print(f"❌ Git commit failed: {git_e.stderr}")
        except Exception as git_other_e:
            print(f"❌ An unexpected error occurred during Git commit: {git_other_e}")

        response_message = "Scraper executed and local JSON updated."
        if item_to_scrape:
            response_message = f"Scraped data for '{item_to_scrape}'. Local JSON updated."
        response_message += " GitHub push needs to be done separately."

        return jsonify({
            "status": "success",
            "message": response_message,
            "data": scraped_item_data
        }), 200

    except subprocess.CalledProcessError as e:
        print(f"Git or Scraper command failed: {e.stderr}")
        return jsonify({"status": "error", "message": "Backend processing failed.", "details": e.stderr}), 500
    except FileNotFoundError as e:
        print(f"File not found error: {e}")
        return jsonify({"status": "error", "message": f"Required file not found: {e.filename}", "details": str(e)}), 500
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        return jsonify({"status": "error", "message": "An unexpected server error occurred.", "details": str(e)}), 500

if __name__ == '__main__':
    # For development purposes, run directly
    # In production, use a WSGI server like Gunicorn or uWSGI
    app.run(host='0.0.0.0', port=5002, debug=True) # Runs on port 5002 to avoid conflict with default 5000