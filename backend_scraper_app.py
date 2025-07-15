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
from apscheduler.schedulers.background import BackgroundScheduler
from filelock import FileLock # Import FileLock for safe concurrent file access
import logging 

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# --- Configuration ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GITHUB_REPO_PATH = SCRIPT_DIR
JSON_OUTPUT_FILE_NAME = "item_overrides.json"
JSON_OUTPUT_FILE_PATH = os.path.join(GITHUB_REPO_PATH, JSON_OUTPUT_FILE_NAME)
JSON_LOCK_FILE_PATH = os.path.join(tempfile.gettempdir(), f"{JSON_OUTPUT_FILE_NAME}.lock") # Use temp dir for lock file

ITEMS_FILE = os.path.join(GITHUB_REPO_PATH, "items_to_scrape.txt") # This file might become less relevant for automatic updates if we iterate all in item_overrides.json
MARKET_IDS_FILE = os.path.join(GITHUB_REPO_PATH, "marketids.json")

# Debug: Print the paths to verify they're correct
logger.info(f"🔧 Script directory: {SCRIPT_DIR}")
logger.info(f"🔧 Looking for marketids.json at: {MARKET_IDS_FILE}")
logger.info(f"🔧 Looking for items_to_scrape.txt at: {ITEMS_FILE}")
logger.info(f"🔧 Will save item_overrides.json to: {JSON_OUTPUT_FILE_PATH}")
logger.info(f"🔧 Lock file for item_overrides.json at: {JSON_LOCK_FILE_PATH}")
STALE_THRESHOLD_DAYS = 7
YUAN_TO_USD_RATE = 0.13937312
MAX_SCRAPE_RETRIES = 3 # No. of retries for failed scrapes
RETRY_DELAY_SECONDS = 5 # Delay between retries

def get_items_to_scrape():
    """Reads the list of items from the text file."""
    try:
        with open(ITEMS_FILE, "r", encoding='utf-8') as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        logger.error(f"Error: {ITEMS_FILE} not found. Please create it.")
        return []

def load_existing_data():
    """Loads existing data from local file only, with file lock."""
    local_data = {}
    lock = FileLock(JSON_LOCK_FILE_PATH)
    try:
        with lock: # Acquire lock before reading
            if os.path.exists(JSON_OUTPUT_FILE_PATH):
                with open(JSON_OUTPUT_FILE_PATH, "r", encoding='utf-8') as f:
                    local_data = json.load(f)
                    logger.info(f"✅ Loaded {len(local_data)} items from local JSON file.")
            else:
                logger.warning(f"⚠️ Local JSON file not found, starting with empty data.")
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.error(f"⚠️ Could not load local JSON file: {e}")
        local_data = {}
    finally:
        # lock is automatically released by 'with' statement
        pass
    
    logger.info(f"📊 Total loaded data: {len(local_data)} items")
    return local_data

def load_market_ids():
    """Loads the market IDs from the JSON file."""
    try:
        with open(MARKET_IDS_FILE, "r", encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.error(f"Error: {MARKET_IDS_FILE} not found or invalid. Error: {e}")
        return {}

def is_stale(timestamp_str):
    """Checks if a timestamp is older than our threshold."""
    if not timestamp_str:
        logger.debug("DEBUG: Timestamp is empty, considering stale.")
        return True
    
    try:
        # Ensure timezone-aware comparison
        last_updated = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
        current_time = datetime.now(timezone.utc)
        age_seconds = (current_time - last_updated).total_seconds()
        age_days = age_seconds / (24 * 3600)
        
        logger.debug(f"DEBUG: Last updated: {last_updated.isoformat()}, Current time: {current_time.isoformat()}")
        logger.debug(f"DEBUG: Item age: {age_days:.2f} days (threshold: {STALE_THRESHOLD_DAYS} days)")
        return age_days > STALE_THRESHOLD_DAYS
    except (ValueError, TypeError) as e:
        logger.error(f"DEBUG: Invalid timestamp '{timestamp_str}': {e}")
        return True

def save_data_atomic(data):
    """Atomically saves data to prevent corruption and validates the result."""
    # NOTE: This function assumes the caller already holds the file lock
    try:
        # a temporary file in the same directory to ensure atomic write
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
            
            logger.info(f"✅ Successfully saved and validated {len(validated_data)} items to {JSON_OUTPUT_FILE_NAME}")
            return True
            
        except Exception as e:
            # Clean up temporary file if something went wrong
            try:
                os.unlink(temp_path)
            except Exception as cleanup_e:
                logger.error(f"Error cleaning up temp file {temp_path}: {cleanup_e}")
            raise e
            
    except Exception as e:
        logger.error(f"❌ Error saving data: {e}")
        return False

def update_item_data_safely(item_key, yuan_price, usd_price):
    """
    Safely updates a single item's data using file locking.
    This prevents race conditions when multiple requests try to update simultaneously.
    """
    lock = FileLock(JSON_LOCK_FILE_PATH)
    try:
        with lock:
            # Load the latest data while holding the lock
            existing_data = {}
            if os.path.exists(JSON_OUTPUT_FILE_PATH):
                with open(JSON_OUTPUT_FILE_PATH, "r", encoding='utf-8') as f:
                    existing_data = json.load(f)
            
            # Update the specific item
            current_timestamp = datetime.now(timezone.utc).isoformat()
            existing_data[item_key] = {
                "yuan_price": yuan_price,
                "usd_price": usd_price,
                "timestamp": current_timestamp
            }
            
            # Save the updated data
            success = save_data_atomic(existing_data)
            if success:
                logger.info(f"💾 Successfully updated {item_key} in {JSON_OUTPUT_FILE_NAME} (total: {len(existing_data)} items)")
            return success, existing_data
            
    except Exception as e:
        logger.error(f"❌ Error updating item data for {item_key}: {e}")
        return False, {}

# retry logic
def scrape_buff_price(item_name_with_phase, page, market_ids):
    """
    Scrapes the price of an item from Buff.163.com, handling phases.
    Includes retry logic for transient failures.
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
        logger.info(f"Detected base item: '{base_item_name}', Phase: '{phase_name}'")

    # This ensures consistency for both requested scrapes and scheduled scrapes.
    corrected_item_name_for_lookup = re.sub(r'(\s(?:Doppler|Gamma Doppler))\s(Phase\s*\d|Ruby|Sapphire|Emerald|Black Pearl)', r'\1 - \2', base_item_name, flags=re.IGNORECASE)
    if corrected_item_name_for_lookup != base_item_name:
        logger.info(f"🔧 Corrected item name for market ID lookup: from '{base_item_name}' to '{corrected_item_name_for_lookup}'")
        base_item_name = corrected_item_name_for_lookup 

    if base_item_name not in market_ids:
        logger.error(f"Error: Base item '{base_item_name}' not found in market IDs.")
        return None, None
    
    item_data = market_ids[base_item_name]

    if "buff" not in item_data:
        logger.warning(f"Warning: Buff ID not found for base item '{base_item_name}'.")
        return None, None

    buff_id = item_data["buff"]

    if phase_match and "buff_phase" in item_data:
        if phase_name in item_data["buff_phase"]:
            phase_tag_id = item_data["buff_phase"][phase_name]
            url_params = f"?from=market#tag_ids={phase_tag_id}"
            logger.info(f"Found phase tag ID: {phase_tag_id} for '{phase_name}'.")
            logger.warning(f"Skipping '{item_name_with_phase}'. Phased items might require login.")
            return None, None
        else:
            logger.warning(f"Warning: Phase '{phase_name}' not found in buff_phase for '{base_item_name}'.")

    url = f"https://buff.163.com/goods/{buff_id}{url_params}"
    price_selector = 'td.t_Left strong.f_Strong' 
    
    for attempt in range(MAX_SCRAPE_RETRIES): # Retry loop
        try:
            logger.info(f"Attempt {attempt + 1}/{MAX_SCRAPE_RETRIES}: Navigating to {url}")
            # Increased timeout for page navigation and selector wait slightly
            page.goto(url, wait_until="domcontentloaded", timeout=75000) 
            logger.info(f"Page loaded: {page.url}")

            # Wait for the price selector, allowing more time
            page.wait_for_selector(price_selector, state='visible', timeout=75000)

            price_element = page.query_selector(price_selector)
            if price_element:
                price_text = price_element.inner_text()
                logger.debug(f"DEBUG: Raw price text: '{price_text}'")
                
                match = re.search(r'[\d,]+\.?\d*', price_text)
                if match:
                    yuan_price_str = match.group(0).replace(',', '').strip()
                    yuan_price = float(yuan_price_str)
                    usd_price = round(yuan_price * YUAN_TO_USD_RATE, 2)
                    logger.debug(f"DEBUG: Yuan price: {yuan_price}, USD price: {usd_price}")
                    return yuan_price, usd_price
                else:
                    logger.warning(f"Error: Could not parse price from '{price_text}' on {url}")
            else:
                logger.warning(f"Error: Price element not found with selector '{price_selector}' on {url}")
        except Exception as e:
            logger.error(f"Error scraping {item_name_with_phase} from {url} (Attempt {attempt + 1}): {e}")
            if attempt < MAX_SCRAPE_RETRIES - 1:
                logger.info(f"Retrying in {RETRY_DELAY_SECONDS} seconds...")
                time.sleep(RETRY_DELAY_SECONDS)
            else:
                logger.error(f"Max retries reached for {item_name_with_phase}.")
    
    return None, None # Return None, None if all retries fail

# perform_scheduled_price_update to iterate through ALL existing_data
def perform_scheduled_price_update():
    """
    Function to be called by the scheduler to check and update stale prices.
    Iterates through all items in item_overrides.json to check for staleness.
    """
    logger.info(f"\n--- Running scheduled price update at {datetime.now(timezone.utc)} ---")
    try:
        market_ids = load_market_ids()
        if not market_ids:
            logger.error("Scheduled update: Market IDs not loaded. Skipping update.")
            return

        existing_data = load_existing_data()
        items_actually_scraped = 0
        
        stale_items_to_scrape = []
        for item_key, item_details in existing_data.items():
            timestamp_str = item_details.get("timestamp")
            if is_stale(timestamp_str):
                stale_items_to_scrape.append(item_key)
        
        if not stale_items_to_scrape:
            logger.info("No stale items found in item_overrides.json. No scraping needed.")
            return

        logger.info(f"Found {len(stale_items_to_scrape)} stale items. Will attempt to scrape.")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            for item_key in stale_items_to_scrape:
                logger.info(f"🕷️ Scheduled scrape for {item_key}...")
                yuan_price, usd_price = scrape_buff_price(item_key, page, market_ids)
                
                if usd_price is not None:
                    success, _ = update_item_data_safely(item_key, yuan_price, usd_price)
                    if success:
                        items_actually_scraped += 1
                        logger.info(f"✅ Successfully scraped {item_key}: ${usd_price} (scheduled update)")
                    else:
                        logger.error(f"❌ Failed to save scraped data for {item_key}")
                else:
                    logger.warning(f"❌ Failed to scrape {item_key} (scheduled update). Keeping old data if it exists.")
                
                # Sleep between requests to avoid overwhelming the server
                time.sleep(2)

            browser.close()

        logger.info(f"💾 Scheduled update completed. Scraped {items_actually_scraped} items.")

    except Exception as e:
        logger.exception(f"❌ Unexpected error during scheduled price update:") 

@app.route('/scrape-prices', methods=['POST'])
def scrape_prices_endpoint():
    """Enhanced endpoint with robust data management and race condition prevention."""
    data = request.get_json()
    item_to_scrape = data.get('item') if data else None

    logger.info(f"🎯 Received request to scrape: {item_to_scrape if item_to_scrape else 'all items'}")

    try:
        market_ids = load_market_ids()
        if not market_ids:
            return jsonify({"status": "error", "message": "Market IDs not loaded. Cannot scrape."}), 500

        scraped_item_data = None
        items_actually_scraped = 0

        # The /scrape-prices endpoint will still use items_to_scrape.txt if no specific item is requested
        items_list = [item_to_scrape] if item_to_scrape else get_items_to_scrape()

        if not items_list:
            # If no items are in items_to_scrape.txt AND no specific item was requested, it's an error.
            return jsonify({"status": "error", "message": "No items to scrape configured or requested."}), 500

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            for item_key_raw in items_list:
                # Apply item name correction here for consistency before checking existing data or scraping
                item_key = re.sub(r'(\s(?:Doppler|Gamma Doppler))\s(Phase\s*\d|Ruby|Sapphire|Emerald|Black Pearl)', r'\1 - \2', item_key_raw, flags=re.IGNORECASE)
                if item_key_raw != item_key:
                    logger.info(f"🔧 Corrected item name for request: from '{item_key_raw}' to '{item_key}'")

                # Check if item needs scraping by loading fresh data each time
                should_scrape = True
                existing_data = load_existing_data()
                
                if item_key in existing_data:
                    timestamp_str = existing_data[item_key].get("timestamp")
                    if timestamp_str and not is_stale(timestamp_str):
                        logger.info(f"✅ Using fresh existing data for '{item_key}' (age: {timestamp_str})")
                        should_scrape = False
                        if item_to_scrape:
                            scraped_item_data = existing_data[item_key]
                    else:
                        logger.info(f"🔄 Data for '{item_key}' is stale or missing timestamp, will scrape.")
                else:
                    logger.info(f"🆕 No existing data for '{item_key}', will scrape.")

                if should_scrape:
                    logger.info(f"🕷️ Scraping {item_key}...")
                    yuan_price, usd_price = scrape_buff_price(item_key, page, market_ids)
                    
                    if usd_price is not None:
                        # Use the safe update function to prevent race conditions
                        success, updated_data = update_item_data_safely(item_key, yuan_price, usd_price)
                        
                        if success:
                            items_actually_scraped += 1
                            
                            # Only set scraped_item_data if this was the specifically requested item
                            if item_key == item_to_scrape: 
                                scraped_item_data = updated_data[item_key]
                            
                            logger.info(f"✅ Successfully scraped {item_key}: ${usd_price}")
                        else:
                            logger.error(f"❌ Failed to save scraped data for {item_key}")
                    else:
                        logger.warning(f"❌ Failed to scrape {item_key}. Keeping old data if it exists.")
                
                if should_scrape:
                    time.sleep(2)

            browser.close()

        # Get final stats
        final_data = load_existing_data()

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
                "total_items": len(final_data),
                "items_scraped": items_actually_scraped,
                "items_from_cache": len(items_list) - items_actually_scraped
            }
        }), 200

    except Exception as e:
        logger.exception(f"❌ Unexpected error in /scrape-prices endpoint:") # Use exception for full traceback
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
        logger.exception(f"❌ Error in /data-status endpoint:")
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    
    scheduler = BackgroundScheduler()
    # Schedule perform_scheduled_price_update to run every 5 minutes
    scheduler.add_job(perform_scheduled_price_update, 'interval', minutes=30)
    scheduler.start()
    logger.info("✨ Scheduler started for automatic price updates.")

    # For development purposes, run directly
    # Better to use a WSGI server like Gunicorn or uWSGI
    app.run(host='0.0.0.0', port=5002, debug=True, use_reloader=False) 
    # use_reloader=False is crucial when using APScheduler with Flask's debug mode
    # as it prevents the app from starting twice and thus the scheduler from running twice.