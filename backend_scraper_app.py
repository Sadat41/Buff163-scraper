# backend_scraper_app.py
from flask import Flask, jsonify, request
import subprocess
import os
import json
import time
import requests
from datetime import datetime, timedelta, timezone
from playwright.sync_api import sync_playwright
import re
import html
import unicodedata

app = Flask(__name__)

# --- Configuration ---
# FIXED: Use the script's directory instead of current working directory
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GITHUB_REPO_PATH = SCRIPT_DIR  # This will be the directory where backend_scraper_app.py is located
JSON_OUTPUT_FILE_NAME = "item_overrides.json"
JSON_OUTPUT_FILE_PATH = os.path.join(GITHUB_REPO_PATH, JSON_OUTPUT_FILE_NAME)
GITHUB_JSON_URL = "https://sadat41.github.io/Buff163-scraper/item_overrides.json"

ITEMS_FILE = os.path.join(GITHUB_REPO_PATH, "items_to_scrape.txt")
MARKET_IDS_FILE = os.path.join(GITHUB_REPO_PATH, "marketids.json")

# Debug: Print the paths to verify they're correct
print(f"🔧 Script directory: {SCRIPT_DIR}")
print(f"🔧 Looking for marketids.json at: {MARKET_IDS_FILE}")
print(f"🔧 Looking for items_to_scrape.txt at: {ITEMS_FILE}")
print(f"🔧 Will save item_overrides.json to: {JSON_OUTPUT_FILE_PATH}")
STALE_THRESHOLD_DAYS = 7
YUAN_TO_USD_RATE = 0.13937312

def clean_item_name(item_name):
    """
    FIXED: Clean and normalize item names to prevent encoding issues.
    """
    if not item_name:
        return item_name
    
    try:
        # First, decode HTML entities if present
        cleaned = html.unescape(item_name)
        
        # Normalize Unicode characters (NFC normalization)
        cleaned = unicodedata.normalize('NFC', cleaned)
        
        # Remove any non-printable characters except spaces
        cleaned = ''.join(char for char in cleaned if char.isprintable() or char.isspace())
        
        # Clean up multiple spaces
        cleaned = ' '.join(cleaned.split())
        
        # Remove any remaining problematic characters that might cause encoding issues
        # Keep only ASCII letters, numbers, spaces, and common punctuation
        allowed_chars = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ()-|.:™®')
        cleaned = ''.join(char for char in cleaned if char in allowed_chars or ord(char) < 128)
        
        return cleaned.strip()
    except Exception as e:
        print(f"❌ Error cleaning item name '{item_name}': {e}")
        # Fallback: return ASCII-only version
        return ''.join(char for char in str(item_name) if ord(char) < 128).strip()

def get_items_to_scrape():
    """Reads the list of items from the text file."""
    try:
        with open(ITEMS_FILE, "r", encoding='utf-8') as f:
            items = []
            for line in f:
                item = line.strip()
                if item:
                    # Clean each item name as we read it
                    cleaned_item = clean_item_name(item)
                    items.append(cleaned_item)
            return items
    except FileNotFoundError:
        print(f"Error: {ITEMS_FILE} not found. Please create it.")
        return []

def load_existing_data():
    """FIXED: Loads existing data from both local file AND GitHub JSON."""
    local_data = {}
    github_data = {}
    
    # Try to load local file first
    try:
        with open(JSON_OUTPUT_FILE_PATH, "r", encoding='utf-8') as f:
            local_data = json.load(f)
            print(f"✅ Loaded {len(local_data)} items from local JSON file.")
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"⚠️ Could not load local JSON file: {e}")
    
    # Try to load from GitHub (this is what the extension actually uses)
    try:
        response = requests.get(GITHUB_JSON_URL, timeout=10)
        if response.status_code == 200:
            github_data = response.json()
            print(f"✅ Loaded {len(github_data)} items from GitHub JSON.")
        else:
            print(f"⚠️ GitHub JSON returned status {response.status_code}")
    except requests.RequestException as e:
        print(f"⚠️ Could not load GitHub JSON: {e}")
    
    # Merge data - GitHub data takes priority for consistency
    merged_data = local_data.copy()
    merged_data.update(github_data)
    
    print(f"📊 Total merged data: {len(merged_data)} items")
    return merged_data

def load_market_ids():
    """Loads the market IDs from the JSON file."""
    try:
        with open(MARKET_IDS_FILE, "r", encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Error: {MARKET_IDS_FILE} not found or invalid. Error: {e}")
        return {}

def is_stale(timestamp_str):
    """FIXED: Checks if a timestamp is older than our threshold."""
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
    """FIXED: Saves data with proper encoding and validates the result."""
    try:
        # Clean all item names before saving
        cleaned_data = {}
        for item_name, item_data in data.items():
            cleaned_name = clean_item_name(item_name)
            cleaned_data[cleaned_name] = item_data
            
            # Log if name was changed
            if cleaned_name != item_name:
                print(f"🧹 Cleaned item name: '{item_name}' -> '{cleaned_name}'")
        
        # Save with explicit UTF-8 encoding and ensure_ascii=False for proper Unicode handling
        with open(JSON_OUTPUT_FILE_PATH, "w", encoding='utf-8') as f:
            json.dump(cleaned_data, f, indent=2, ensure_ascii=False)
        
        # Validate the saved file
        with open(JSON_OUTPUT_FILE_PATH, "r", encoding='utf-8') as f:
            validated_data = json.load(f)
            print(f"✅ Successfully saved and validated {len(validated_data)} items to {JSON_OUTPUT_FILE_NAME}")
            return True
    except Exception as e:
        print(f"❌ Error saving data: {e}")
        return False

def scrape_buff_price(item_name_with_phase, page, market_ids):
    """
    FIXED: Scrapes the price of an item from Buff.163.com with proper encoding handling.
    Returns a tuple (yuan_price, usd_price) or (None, None) on failure.
    """
    # Clean the input item name first
    item_name_with_phase = clean_item_name(item_name_with_phase)
    
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
        
        # FIXED: Set proper encoding headers for the page
        page.set_extra_http_headers({
            'Accept-Charset': 'utf-8',
            'Accept-Language': 'en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7'
        })
        
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
    """FIXED: Enhanced endpoint with better data management and encoding handling."""
    data = request.get_json()
    item_to_scrape = data.get('item') if data else None

    # Clean the item name if provided
    if item_to_scrape:
        item_to_scrape = clean_item_name(item_to_scrape)

    print(f"🎯 Received request to scrape: {item_to_scrape if item_to_scrape else 'all items'}")

    try:
        market_ids = load_market_ids()
        if not market_ids:
            return jsonify({"status": "error", "message": "Market IDs not loaded. Cannot scrape."}), 500

        # FIXED: Load existing data from both local and GitHub
        existing_data = load_existing_data()
        updated_data = existing_data.copy()
        scraped_item_data = None
        items_actually_scraped = 0

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                # FIXED: Set proper encoding for the browser context
                locale='en-US',
                extra_http_headers={
                    'Accept-Charset': 'utf-8'
                }
            )
            page = context.new_page()

            items_list = [item_to_scrape] if item_to_scrape else get_items_to_scrape()

            if not items_list:
                browser.close()
                return jsonify({"status": "error", "message": "No items to scrape configured."}), 500

            for item_key in items_list:
                # Clean item key
                item_key = clean_item_name(item_key)
                
                # FIXED: More robust staleness check
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

        # FIXED: Only save if we actually have data and something changed
        if updated_data and (items_actually_scraped > 0 or item_to_scrape):
            if save_data(updated_data):
                print(f"💾 Updated {JSON_OUTPUT_FILE_NAME} locally with {len(updated_data)} total items.")
                
                # FIXED: Enhanced Git operations with better error handling
                try:
                    # Check if there are actually changes to commit
                    git_status = subprocess.run(["git", "status", "--porcelain", JSON_OUTPUT_FILE_NAME], 
                                              capture_output=True, text=True, check=True)
                    
                    if git_status.stdout.strip():  # Only commit if there are changes
                        subprocess.run(["git", "add", JSON_OUTPUT_FILE_NAME], check=True)
                        
                        commit_message = f"Auto-update: Scraped {items_actually_scraped} items"
                        if item_to_scrape:
                            commit_message = f"Auto-update: Scraped '{item_to_scrape}'"
                        
                        subprocess.run(["git", "commit", "-m", commit_message], check=True)
                        print(f"📝 Changes committed locally: {commit_message}")
                        
                        # Optional: Auto-push (commented out for stability)
                        # subprocess.run(["git", "push"], check=True)
                        # print("🚀 Changes pushed to GitHub.")
                    else:
                        print("📄 No changes detected in JSON file, skipping commit.")
                        
                except subprocess.CalledProcessError as git_e:
                    print(f"❌ Git operation failed: {git_e}")
                except Exception as git_other_e:
                    print(f"❌ Unexpected Git error: {git_other_e}")
            else:
                return jsonify({"status": "error", "message": "Failed to save data locally."}), 500

        # FIXED: Better response messaging
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

    except subprocess.CalledProcessError as e:
        print(f"❌ Subprocess failed: {e}")
        return jsonify({"status": "error", "message": "Backend processing failed.", "details": str(e)}), 500
    except FileNotFoundError as e:
        print(f"❌ File not found: {e}")
        return jsonify({"status": "error", "message": f"Required file not found: {e.filename}"}), 500
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        return jsonify({"status": "error", "message": "An unexpected server error occurred.", "details": str(e)}), 500

@app.route('/health', methods=['GET'])
def health_check():
    """Simple health check endpoint."""
    return jsonify({"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}), 200

@app.route('/data-status', methods=['GET'])
def data_status():
    """FIXED: New endpoint to check current data status without scraping."""
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
            "github_url": GITHUB_JSON_URL,
            "local_file": JSON_OUTPUT_FILE_PATH
        }), 200
        
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/clean-data', methods=['POST'])
def clean_existing_data():
    """NEW: Endpoint to clean existing corrupted item names in the JSON file."""
    try:
        print("🧹 Starting data cleaning process...")
        
        # Load existing data
        existing_data = load_existing_data()
        
        if not existing_data:
            return jsonify({"status": "error", "message": "No existing data to clean."}), 400
        
        # Clean all item names
        cleaned_data = {}
        changes_made = 0
        
        for old_name, item_data in existing_data.items():
            cleaned_name = clean_item_name(old_name)
            cleaned_data[cleaned_name] = item_data
            
            if cleaned_name != old_name:
                changes_made += 1
                print(f"🧹 Cleaned: '{old_name}' -> '{cleaned_name}'")
        
        if changes_made > 0:
            # Save the cleaned data
            if save_data(cleaned_data):
                print(f"✅ Cleaned {changes_made} item names and saved data.")
                return jsonify({
                    "status": "success",
                    "message": f"Successfully cleaned {changes_made} item names.",
                    "changes_made": changes_made,
                    "total_items": len(cleaned_data)
                }), 200
            else:
                return jsonify({"status": "error", "message": "Failed to save cleaned data."}), 500
        else:
            return jsonify({
                "status": "success",
                "message": "No corrupted item names found. Data is already clean.",
                "changes_made": 0,
                "total_items": len(cleaned_data)
            }), 200
        
    except Exception as e:
        print(f"❌ Error during data cleaning: {e}")
        return jsonify({"status": "error", "message": f"Data cleaning failed: {str(e)}"}), 500

if __name__ == '__main__':
    # For development purposes, run directly
    # In production, use a WSGI server like Gunicorn or uWSGI
    app.run(host='0.0.0.0', port=5002, debug=True)