# backend_scraper_app.py
from flask import Flask, jsonify, request
import os
import json
import time
import tempfile
import shutil
from datetime import datetime, timedelta, timezone
from playwright.sync_api import sync_playwright
import re

app = Flask(__name__)

# --- Configuration ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GITHUB_REPO_PATH = SCRIPT_DIR  # This will be the directory where backend_scraper_app.py is located
JSON_OUTPUT_FILE_NAME = "item_overrides.json"
JSON_OUTPUT_FILE_PATH = os.path.join(GITHUB_REPO_PATH, JSON_OUTPUT_FILE_NAME)

ITEMS_FILE = os.path.join(GITHUB_REPO_PATH, "items_to_scrape.txt")
MARKET_IDS_FILE = os.path.join(GITHUB_REPO_PATH, "marketids.json")

# Debug: Print the paths to verify they're correct
print(f"🔧 Script directory: {SCRIPT_DIR}")
print(f"🔧 Looking for marketids.json at: {MARKET_IDS_FILE}")
print(f"🔧 Looking for items_to_scrape.txt at: {ITEMS_FILE}")
print(f"🔧 Will save item_overrides.json to: {JSON_OUTPUT_FILE_PATH}")
STALE_THRESHOLD_DAYS = 7
YUAN_TO_USD_RATE = 0.13937312

def get_items_to_scrape():
    """Reads the list of items from the text file."""
    try:
        with open(ITEMS_FILE, "r", encoding='utf-8') as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        print(f"Error: {ITEMS_FILE} not found. Please create it.")
        return []

def load_existing_data():
    """Loads existing data from local file only."""
    local_data = {}
    
    # Try to load local file
    try:
        if os.path.exists(JSON_OUTPUT_FILE_PATH):
            with open(JSON_OUTPUT_FILE_PATH, "r", encoding='utf-8') as f:
                local_data = json.load(f)
                print(f"✅ Loaded {len(local_data)} items from local JSON file.")
        else:
            print(f"⚠️ Local JSON file not found, starting with empty data.")
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"⚠️ Could not load local JSON file: {e}")
        local_data = {}
    
    print(f"📊 Total loaded data: {len(local_data)} items")
    return local_data

def load_market_ids():
    """Loads the market IDs from the JSON file."""
    try:
        with open(MARKET_IDS_FILE, "r", encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Error: {MARKET_IDS_FILE} not found or invalid. Error: {e}")
        return {}

def is_stale(timestamp_str):
    """Checks if a timestamp is older than our threshold."""
    if not timestamp_str:
        return True
    
    try:
        last_updated = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
        current_time = datetime.now(timezone.utc)
        age_days = (current_time - last_updated).total_seconds() / (24 * 3600)
        
        print(f"DEBUG: Item age: {age_days:.2f} days (threshold: {STALE_THRESHOLD_DAYS} days)")
        return age_days > STALE_THRESHOLD_DAYS
    except (ValueError, TypeError) as e:
        print(f"DEBUG: Invalid timestamp '{timestamp_str}': {e}")
        return True

def save_data(data):
    """Atomically saves data to prevent corruption and validates the result."""
    try:
        # Create a temporary file in the same directory to ensure atomic write
        temp_dir = os.path.dirname(JSON_OUTPUT_FILE_PATH)
        temp_fd, temp_path = tempfile.mkstemp(dir=temp_dir, suffix='.json.tmp')
        
        try:
            # Write to temporary file
            with os.fdopen(temp_fd, 'w', encoding='utf-8') as temp_file:
                json.dump(data, temp_file, indent=2, ensure_ascii=False)
                temp_file.flush()
                os.fsync(temp_file.fileno())  # Force write to disk
            
            # Validate the temporary file by reading it back
            with open(temp_path, "r", encoding='utf-8') as f:
                validated_data = json.load(f)
                if len(validated_data) != len(data):
                    raise ValueError(f"Data validation failed: expected {len(data)} items, got {len(validated_data)}")
            
            # Atomically replace the original file
            if os.name == 'nt':  # Windows
                if os.path.exists(JSON_OUTPUT_FILE_PATH):
                    os.replace(temp_path, JSON_OUTPUT_FILE_PATH)
                else:
                    shutil.move(temp_path, JSON_OUTPUT_FILE_PATH)
            else:  # Unix/Linux/Mac
                os.replace(temp_path, JSON_OUTPUT_FILE_PATH)
            
            print(f"✅ Successfully saved and validated {len(validated_data)} items to {JSON_OUTPUT_FILE_NAME}")
            return True
            
        except Exception as e:
            # Clean up temporary file if something went wrong
            try:
                os.unlink(temp_path)
            except:
                pass
            raise e
            
    except Exception as e:
        print(f"❌ Error saving data: {e}")
        return False

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
        print(f"Error: Base item '{base_item_name}' not found in market IDs.")
        return None, None
    
    item_data = market_ids[base_item_name]

    if "buff" not in item_data:
        print(f"Warning: Buff ID not found for base item '{base_item_name}'.")
        return None, None

    buff_id = item_data["buff"]

    if phase_match and "buff_phase" in item_data:
        if phase_name in item_data["buff_phase"]:
            phase_tag_id = item_data["buff_phase"][phase_name]
            url_params = f"?from=market#tag_ids={phase_tag_id}"
            print(f"Found phase tag ID: {phase_tag_id} for '{phase_name}'.")
            print(f"Skipping '{item_name_with_phase}'. Phased items might require login.")
            return None, None
        else:
            print(f"Warning: Phase '{phase_name}' not found in buff_phase for '{base_item_name}'.")

    url = f"https://buff.163.com/goods/{buff_id}{url_params}"
    price_selector = 'td.t_Left strong.f_Strong' 
    
    try:
        print(f"Navigating to {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        print(f"Page loaded: {page.url}")

        page.wait_for_selector(price_selector, state='visible', timeout=60000)

        price_element = page.query_selector(price_selector)
        if price_element:
            price_text = price_element.inner_text()
            print(f"DEBUG: Raw price text: '{price_text}'")
            
            match = re.search(r'[\d,]+\.?\d*', price_text)
            if match:
                yuan_price_str = match.group(0).replace(',', '').strip()
                yuan_price = float(yuan_price_str)
                usd_price = round(yuan_price * YUAN_TO_USD_RATE, 2)
                print(f"DEBUG: Yuan price: {yuan_price}, USD price: {usd_price}")
                return yuan_price, usd_price
            else:
                print(f"Error: Could not parse price from '{price_text}'")
                return None, None
        else:
            print(f"Error: Price element not found with selector '{price_selector}'")
            return None, None
    except Exception as e:
        print(f"Error scraping {item_name_with_phase} from {url}: {e}")
        return None, None

@app.route('/scrape-prices', methods=['POST'])
def scrape_prices_endpoint():
    """Enhanced endpoint with robust data management (no GitHub integration)."""
    data = request.get_json()
    item_to_scrape = data.get('item') if data else None

    print(f"🎯 Received request to scrape: {item_to_scrape if item_to_scrape else 'all items'}")

    try:
        market_ids = load_market_ids()
        if not market_ids:
            return jsonify({"status": "error", "message": "Market IDs not loaded. Cannot scrape."}), 500

        # Load existing data from local file only
        existing_data = load_existing_data()
        updated_data = existing_data.copy()
        scraped_item_data = None
        items_actually_scraped = 0

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            items_list = [item_to_scrape] if item_to_scrape else get_items_to_scrape()

            if not items_list:
                browser.close()
                return jsonify({"status": "error", "message": "No items to scrape configured."}), 500

            for item_key in items_list:
                # Check if item needs scraping
                should_scrape = True
                if item_key in existing_data:
                    timestamp_str = existing_data[item_key].get("timestamp")
                    if timestamp_str and not is_stale(timestamp_str):
                        print(f"✅ Using fresh existing data for '{item_key}' (age: {timestamp_str})")
                        should_scrape = False
                        if item_key == item_to_scrape:
                            scraped_item_data = existing_data[item_key]
                    else:
                        print(f"🔄 Data for '{item_key}' is stale or missing timestamp, will scrape.")
                else:
                    print(f"🆕 No existing data for '{item_key}', will scrape.")

                if should_scrape:
                    print(f"🕷️ Scraping {item_key}...")
                    yuan_price, usd_price = scrape_buff_price(item_key, page, market_ids)
                    
                    if usd_price is not None:
                        current_timestamp = datetime.now(timezone.utc).isoformat()
                        updated_data[item_key] = {
                            "yuan_price": yuan_price,
                            "usd_price": usd_price,
                            "timestamp": current_timestamp
                        }
                        items_actually_scraped += 1
                        
                        if item_key == item_to_scrape:
                            scraped_item_data = updated_data[item_key]
                        
                        print(f"✅ Successfully scraped {item_key}: ${usd_price}")
                    else:
                        print(f"❌ Failed to scrape {item_key}")
                        # Don't update existing data if scraping fails
                
                # Be polite to Buff.163.com
                if should_scrape:
                    time.sleep(2)

            browser.close()

        # Only save if we actually have data and something changed
        if updated_data and (items_actually_scraped > 0 or item_to_scrape):
            if save_data(updated_data):
                print(f"💾 Updated {JSON_OUTPUT_FILE_NAME} locally with {len(updated_data)} total items.")
            else:
                return jsonify({"status": "error", "message": "Failed to save data locally."}), 500

        # Build response message
        if item_to_scrape:
            if scraped_item_data:
                response_message = f"✅ Successfully processed '{item_to_scrape}'. Data {'scraped' if items_actually_scraped > 0 else 'retrieved from cache'}."
            else:
                response_message = f"❌ Could not find or scrape data for '{item_to_scrape}'."
        else:
            response_message = f"✅ Processed {len(items_list)} items. Scraped {items_actually_scraped} new/stale items."

        return jsonify({
            "status": "success",
            "message": response_message,
            "data": scraped_item_data,
            "stats": {
                "total_items": len(updated_data),
                "items_scraped": items_actually_scraped,
                "items_from_cache": len(items_list) - items_actually_scraped
            }
        }), 200

    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        return jsonify({"status": "error", "message": "An unexpected server error occurred.", "details": str(e)}), 500

@app.route('/health', methods=['GET'])
def health_check():
    """Simple health check endpoint."""
    return jsonify({"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}), 200

@app.route('/data-status', methods=['GET'])
def data_status():
    """Endpoint to check current data status without scraping."""
    try:
        existing_data = load_existing_data()
        market_ids = load_market_ids()
        
        stats = {
            "total_items": len(existing_data),
            "market_ids_loaded": len(market_ids),
            "fresh_items": 0,
            "stale_items": 0,
            "missing_timestamp": 0
        }
        
        for item_name, item_data in existing_data.items():
            timestamp = item_data.get("timestamp")
            if not timestamp:
                stats["missing_timestamp"] += 1
            elif is_stale(timestamp):
                stats["stale_items"] += 1
            else:
                stats["fresh_items"] += 1
        
        return jsonify({
            "status": "success",
            "stats": stats,
            "local_file": JSON_OUTPUT_FILE_PATH
        }), 200
        
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    # For development purposes, run directly
    # In production, use a WSGI server like Gunicorn or uWSGI
    app.run(host='0.0.0.0', port=5002, debug=True)