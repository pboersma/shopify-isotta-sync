import os
import asyncio
import json
import requests
from dotenv import load_dotenv
from playwright.async_api import async_playwright
import functions_framework

# Load environment variables from .env (optional in dev)
load_dotenv()

# Constants from environment
SHOPIFY_STORE_URL = os.getenv('SHOPIFY_STORE_URL')
ACCESS_TOKEN = os.getenv('SHOPIFY_ACCESS_TOKEN')
VENDOR_NAME = 'Isotta'
SEARCH_URL = "https://shop.isotta-srl.com/en/sherloc/result?q="

VERIFY_SSL = False
DRY_RUN = False
PRICE_THRESHOLD = 0.05

HEADERS = {
    "X-Shopify-Access-Token": ACCESS_TOKEN,
    "Content-Type": "application/json"
}


async def fetch_price(playwright, sku):
    browser = await playwright.chromium.launch(headless=True)
    page = await browser.new_page()

    try:
        await page.goto(f"{SEARCH_URL}{sku}", timeout=60000)

        try:
            await page.wait_for_selector('button.iubenda-cs-accept-btn', timeout=5000)
            await page.click('button.iubenda-cs-accept-btn')
        except:
            pass

        results_selector = '#sherlocWrapper > div > div.sherlocResultsBlock > div.sherlocResultsList > div'
        await page.wait_for_selector(results_selector, timeout=10000)
        results = await page.query_selector_all(results_selector)
        found = False

        for result in results:
            img = await result.query_selector('a > div.sherlocImgWrapper > img')
            if img:
                img_src = await img.get_attribute("src") or ""
                sku_lower = sku.lower()
                sku_prefix = sku_lower.split("-")[0]

                if sku_prefix in img_src.lower() or sku_prefix.replace("-", "_") in img_src.lower():
                    link = await result.query_selector('a')
                    if link:
                        await link.click()
                        found = True
                        break

        if not found:
            return None

        sku_selector = '#maincontent > div.columns > div > div.product-info-main > div.product-info-price > div.product-info-stock-sku > div.product.attribute.sku > div'
        await page.wait_for_selector(sku_selector, timeout=5000)
        sku_element = await page.query_selector(sku_selector)
        page_sku = await sku_element.inner_text() if sku_element else ""

        expected_prefix = sku.lower().split("-")[0]
        page_prefix = page_sku.lower().split("-")[0]
        if expected_prefix != page_prefix:
            return None

        await page.wait_for_selector('[id^="product-price-"] > span', timeout=10000)
        price_element = await page.query_selector('[id^="product-price-"] > span')
        return await price_element.inner_text() if price_element else None

    except Exception as e:
        print(f"Error: {e}")
        return None
    finally:
        await browser.close()


def get_all_products_by_vendor(vendor_name):
    products = []
    url = f"{SHOPIFY_STORE_URL}/admin/api/2025-04/products.json?vendor={vendor_name}&limit=250"

    while url:
        response = requests.get(url, headers=HEADERS, verify=VERIFY_SSL)
        response.raise_for_status()
        data = response.json()
        products.extend(data['products'])

        link_header = response.headers.get('Link')
        next_url = None
        if link_header:
            links = link_header.split(',')
            for link in links:
                if 'rel="next"' in link:
                    next_url = link[link.find('<') + 1:link.find('>')]
                    break
        url = next_url

    return products


def get_variant_id_from_products(products, sku):
    for product in products:
        for variant in product['variants']:
            if variant['sku'] == sku:
                return variant['id']
    return None


def update_variant_price(variant_id, new_price):
    if DRY_RUN:
        print(f"[DRY RUN] Would update variant {variant_id} to {new_price:.2f}")
        return

    url = f"{SHOPIFY_STORE_URL}/admin/api/2025-04/variants/{variant_id}.json"
    payload = {"variant": {"price": f"{new_price:.2f}"}}
    response = requests.put(url, json=payload, headers=HEADERS, verify=VERIFY_SSL)

    if response.status_code != 200:
        print(f"❌ Failed to update variant {variant_id}: {response.status_code} - {response.text}")


async def handle_request():
    products = get_all_products_by_vendor(VENDOR_NAME)
    print(f"Found {len(products)} products for vendor '{VENDOR_NAME}'")

    result_log = []

    async with async_playwright() as playwright:
        for product in products:
            for variant in product['variants']:
                sku = variant['sku']
                shopify_price = float(variant['price'])

                if not sku:
                    continue

                actual_price_text = await fetch_price(playwright, sku)
                if not actual_price_text:
                    continue

                try:
                    actual_price = float(str(actual_price_text).replace(',', '.').replace('€', '').strip())
                    difference = abs(actual_price - shopify_price) / shopify_price

                    if difference > PRICE_THRESHOLD:
                        variant_id = get_variant_id_from_products(products, sku)
                        if variant_id:
                            update_variant_price(variant_id, actual_price)
                            result_log.append({
                                "sku": sku,
                                "old_price": shopify_price,
                                "new_price": actual_price,
                                "updated": True
                            })
                        else:
                            result_log.append({
                                "sku": sku,
                                "error": "No matching variant ID",
                                "updated": False
                            })
                    else:
                        result_log.append({
                            "sku": sku,
                            "status": "No change needed",
                            "updated": False
                        })

                    await asyncio.sleep(10)
                except Exception as e:
                    result_log.append({"sku": sku, "error": str(e)})

    return result_log


@functions_framework.http
def sync(request):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    results = loop.run_until_complete(handle_request())

    return json.dumps(results), 200, {"Content-Type": "application/json"}
