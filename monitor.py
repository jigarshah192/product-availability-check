import requests
from bs4 import BeautifulSoup
import time
import os
import json
import re
import logging
import sys
from datetime import datetime, timezone, timedelta
from urllib.parse import quote_plus
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Force stdout to be unbuffered for Docker
sys.stdout.reconfigure(line_buffering=True) if hasattr(sys.stdout, 'reconfigure') else None

# Configuration from Environment Variables
# PRODUCTS format: [{"name": "Product Name", "url": "https://...", "out_of_stock_keywords": ["Coming Soon"]}]
# If PRODUCTS is not set, falls back to single PRODUCT_URL for backward compatibility
PRODUCTS_JSON = os.getenv("PRODUCTS", "")
PRODUCT_URL = os.getenv("PRODUCT_URL", "")

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CHECK_INTERVAL = int(os.getenv("INTERVAL", 300))
DAILY_REPORT_HOUR = int(os.getenv("DAILY_REPORT_HOUR", 11))  # Hour in IST (24-hour format)

# Default out-of-stock keywords if not specified per product
DEFAULT_OUT_OF_STOCK_KEYWORDS = ["Coming Soon", "Out of Stock", "Sold Out", "Notify Me", "Currently Unavailable"]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def get_products() -> list:
    """
    Get list of products to monitor.
    Supports both multi-product JSON config and single URL fallback.
    """
    if PRODUCTS_JSON:
        try:
            products = json.loads(PRODUCTS_JSON)
            if isinstance(products, list) and len(products) > 0:
                return products
        except json.JSONDecodeError as e:
            logger.error(f"Error parsing PRODUCTS JSON: {e}")
    
    # Fallback to single product URL
    if PRODUCT_URL:
        return [{
            "name": "Product",
            "url": PRODUCT_URL,
            "out_of_stock_keywords": DEFAULT_OUT_OF_STOCK_KEYWORDS
        }]
    
    logger.error("No products configured. Set PRODUCTS or PRODUCT_URL environment variable.")
    return []


def send_telegram_message(text: str):
    if not TOKEN or not CHAT_ID:
        logger.warning("Telegram TOKEN or CHAT_ID not set, skipping notification.")
        return
    url = (
        f"https://api.telegram.org/bot{TOKEN}/sendMessage?"
        f"chat_id={CHAT_ID}&text={quote_plus(text)}"
    )
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        logger.info("Telegram notification sent successfully")
    except Exception as e:
        logger.error(f"Error sending Telegram message: {e}")


def check_shopify_json_availability(html: str) -> tuple[bool, bool]:
    """
    Check Shopify product JSON for availability status.
    
    Returns:
        Tuple of (is_shopify_site, is_available)
    """
    # Look for Shopify product JSON data in various formats
    patterns = [
        # Pattern 1: mainProduct JSON (like in FastBundle)
        r'"available"\s*:\s*(true|false)',
        # Pattern 2: Product JSON in script tags
        r'"availableForSale"\s*:\s*(true|false)',
    ]
    
    for pattern in patterns:
        matches = re.findall(pattern, html, re.IGNORECASE)
        if matches:
            # If we find "available":true anywhere, consider it in stock
            # We check if ANY variant is available
            if 'true' in [m.lower() for m in matches]:
                return (True, True)
            # If all are false, it's out of stock
            return (True, False)
    
    # Also check for Shopify-specific variant availability
    # Look for pattern like "variants":[{"available":false}]
    variant_pattern = r'"variants"\s*:\s*\[(.*?)\]'
    variant_match = re.search(variant_pattern, html, re.DOTALL)
    if variant_match:
        variant_content = variant_match.group(1)
        # Check if any variant is available
        if '"available":true' in variant_content or '"available": true' in variant_content:
            return (True, True)
        if '"available":false' in variant_content or '"available": false' in variant_content:
            return (True, False)
    
    return (False, False)


def is_in_stock(html: bytes, out_of_stock_keywords: list = None) -> bool:
    """
    Check if product is in stock based on page content.
    
    Supports:
    - Shopify stores (checks JSON product data)
    - Generic sites (checks for keywords and add-to-cart buttons)
    
    Args:
        html: Raw HTML content of the product page
        out_of_stock_keywords: List of keywords that indicate out-of-stock status
    
    Returns:
        True if product appears to be in stock, False otherwise
    """
    if out_of_stock_keywords is None:
        out_of_stock_keywords = DEFAULT_OUT_OF_STOCK_KEYWORDS
    
    html_str = html.decode('utf-8', errors='ignore')
    soup = BeautifulSoup(html, "html.parser")
    
    # Method 1: Check Shopify JSON data (most reliable for Shopify sites)
    is_shopify, is_available = check_shopify_json_availability(html_str)
    if is_shopify:
        logger.debug(f"Shopify detected - Product available: {is_available}")
        return is_available
    
    # Method 2: Check for out-of-stock keywords in page text
    full_text = soup.get_text(separator=" ", strip=True)
    for keyword in out_of_stock_keywords:
        if keyword.lower() in full_text.lower():
            logger.debug(f"Found out-of-stock keyword: '{keyword}'")
            return False

    # Method 3: Look for add-to-cart button (fallback)
    add_to_cart = soup.find(
        lambda tag: tag.name in ["button", "input", "a"]
        and tag.get_text(strip=True).lower() in ["add to cart", "buy now", "add to bag"]
    )
    
    if add_to_cart:
        # Check if button is disabled
        if add_to_cart.get('disabled') or 'disabled' in add_to_cart.get('class', []):
            logger.debug("Add to cart button is disabled")
            return False
        return True
    
    # If no clear indicator, assume out of stock to avoid false positives
    logger.debug("No clear stock indicator found, assuming out of stock")
    return False


def check_stock(product: dict) -> bool:
    """
    Check stock status for a single product.
    
    Args:
        product: Product dict with 'name', 'url', and optional 'out_of_stock_keywords'
    
    Returns:
        True if product is in stock, False otherwise
    """
    name = product.get("name", "Unknown Product")
    url = product.get("url")
    keywords = product.get("out_of_stock_keywords", DEFAULT_OUT_OF_STOCK_KEYWORDS)
    
    if not url:
        logger.error(f"No URL configured for product: {name}")
        return False
    
    try:
        logger.info(f"Checking: {name}")
        response = requests.get(url, headers=HEADERS, timeout=15)
        response.raise_for_status()

        if is_in_stock(response.content, keywords):
            msg = f"✅ {name} is IN STOCK!\n🔗 {url}"
            send_telegram_message(msg)
            logger.info(f"✅ {name} is IN STOCK!")
            return True
        else:
            logger.info(f"❌ {name} - Not in stock")
    except Exception as e:
        logger.error(f"Error checking {name}: {e}")
    return False


def check_all_products() -> dict:
    """
    Check stock status for all configured products.
    
    Returns:
        Dict with product names as keys and stock status (bool) as values
    """
    products = get_products()
    if not products:
        return {}
    
    results = {}
    for product in products:
        name = product.get("name", "Unknown")
        results[name] = check_stock(product)
    
    return results


# IST timezone (UTC+5:30)
IST = timezone(timedelta(hours=5, minutes=30))

def get_current_ist_time():
    """Get current time in IST timezone."""
    return datetime.now(IST)


def should_send_daily_report(last_report_date: str) -> bool:
    """
    Check if daily report should be sent.
    Returns True if current time is at DAILY_REPORT_HOUR and report hasn't been sent today.
    """
    now = get_current_ist_time()
    today_str = now.strftime("%Y-%m-%d")
    
    # Check if it's the right hour and we haven't sent today's report
    if now.hour == DAILY_REPORT_HOUR and last_report_date != today_str:
        return True
    return False


def send_daily_report():
    """
    Send a daily summary of all monitored products to Telegram.
    """
    products = get_products()
    if not products:
        return
    
    now = get_current_ist_time()
    date_str = now.strftime("%d %b %Y")
    
    # Build the report message
    report = f"📊 Daily Monitoring Report\n"
    report += f"📅 {date_str} | 🕐 {now.strftime('%I:%M %p')} IST\n"
    report += f"{'─' * 25}\n\n"
    report += f"📋 Monitoring {len(products)} product(s):\n\n"
    
    for i, product in enumerate(products, 1):
        name = product.get("name", "Unknown")
        url = product.get("url", "")
        
        # Check current stock status
        try:
            response = requests.get(url, headers=HEADERS, timeout=15)
            html_str = response.content.decode('utf-8', errors='ignore')
            is_shopify, is_available = check_shopify_json_availability(html_str)
            
            if is_shopify:
                status = "✅ In Stock" if is_available else "❌ Out of Stock"
            else:
                # Fallback for non-Shopify sites
                status = "❓ Unknown"
        except Exception:
            status = "⚠️ Error checking"
        
        report += f"{i}. {name}\n"
        report += f"   {status}\n"
        report += f"   🔗 {url}\n\n"
    
    report += f"{'─' * 25}\n"
    report += f"🔄 Checking every {CHECK_INTERVAL // 60} min"
    
    send_telegram_message(report)
    logger.info(f"Daily report sent at {now.strftime('%Y-%m-%d %H:%M:%S')} IST")


if __name__ == "__main__":
    products = get_products()
    
    logger.info("=" * 50)
    logger.info("PRODUCT AVAILABILITY MONITOR STARTED")
    logger.info("=" * 50)
    logger.info(f"Monitoring {len(products)} product(s)")
    for p in products:
        logger.info(f"  - {p.get('name', 'Unknown')}: {p.get('url', 'No URL')}")
    logger.info(f"Check interval: {CHECK_INTERVAL} seconds")
    logger.info(f"Daily report: {DAILY_REPORT_HOUR}:00 IST")
    logger.info(f"Telegram: {'Configured' if TOKEN and CHAT_ID else 'Not configured'}")
    logger.info("=" * 50)
    
    # Track last daily report date to avoid duplicates
    last_report_date = ""
    check_count = 0
    
    while True:
        now = get_current_ist_time()
        check_count += 1
        logger.info(f"[Check #{check_count}] Starting at {now.strftime('%Y-%m-%d %H:%M:%S')} IST")
        
        # Check if daily report should be sent
        if should_send_daily_report(last_report_date):
            send_daily_report()
            last_report_date = now.strftime("%Y-%m-%d")
        
        # Check all products for stock
        check_all_products()
        logger.info(f"[Check #{check_count}] Complete. Next check in {CHECK_INTERVAL} seconds")
        time.sleep(CHECK_INTERVAL)

