from dotenv import load_dotenv
import os

import asyncio
import requests
from playwright.async_api import async_playwright

load_dotenv()

# SHOPIFY_STORE_URL = 'https://shop.droommotor.nl'
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

        # Accept cookies if needed
        try:
            await page.wait_for_selector('button.iubenda-cs-accept-btn', timeout=5000)
            await page.click('button.iubenda-cs-accept-btn')
        except:
            pass

        # Wait for results list
        results_selector = '#sherlocWrapper > div > div.sherlocResultsBlock > div.sherlocResultsList > div'
        await page.wait_for_selector(results_selector, timeout=10000)

        results = await page.query_selector_all(results_selector)
        found = False

        for result in results:
            img = await result.query_selector('a > div.sherlocImgWrapper > img')

            if img:
                img_src = await img.get_attribute("src") or ""
                img_src_lower = img_src.lower()
                sku_lower = sku.lower()
                sku_prefix = sku_lower.split("-")[0]  # take only prefix before dash

                # ✅ Check for either prefix or prefix with underscores
                if sku_prefix in img_src_lower or sku_prefix.replace("-", "_") in img_src_lower:
                    link = await result.query_selector('a')
                    if link:
                        await link.click()
                        found = True
                        break

        if not found:
            print(f"❌ No matching result found for SKU {sku}. Skipping.")
            return None

        
        # ✅ Final check on product page for SKU match
        sku_selector = '#maincontent > div.columns > div > div.product-info-main > div.product-info-price > div.product-info-stock-sku > div.product.attribute.sku > div'
        await page.wait_for_selector(sku_selector, timeout=5000)
        sku_element = await page.query_selector(sku_selector)
        page_sku = await sku_element.inner_text() if sku_element else ""

        expected_prefix = sku.lower().split("-")[0]
        page_prefix = page_sku.lower().split("-")[0]

        if expected_prefix != page_prefix:
            print(f"❌ SKU mismatch on product page. Expected prefix {expected_prefix}, found {page_prefix}. Skipping.")
            return None

        # ✅ If confirmed, scrape price
        await page.wait_for_selector('[id^="product-price-"] > span', timeout=10000)
        price_element = await page.query_selector('[id^="product-price-"] > span')
        price = await price_element.inner_text() if price_element else None

        return price

    except Exception as e:
        print(f"Error fetching price for SKU {sku}: {e}")
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
        print(f"💡 [DRY RUN] Would update variant {variant_id} to price {new_price:.2f}")
        return

    url = f"{SHOPIFY_STORE_URL}/admin/api/2025-04/variants/{variant_id}.json"
    payload = {"variant": {"price": f"{new_price:.2f}"}}
    response = requests.put(url, json=payload, headers=HEADERS, verify=VERIFY_SSL)

    if response.status_code == 200:
        print(f"✅ Updated variant {variant_id} to {new_price:.2f}")
    else:
        print(f"❌ Failed to update variant {variant_id}: {response.status_code} - {response.text}")

async def main():
    products = get_all_products_by_vendor(VENDOR_NAME)
    print(f"Found {len(products)} products for vendor '{VENDOR_NAME}'")

    async with async_playwright() as playwright:
        for product in products:
            for variant in product['variants']:
                sku = variant['sku']
                shopify_price = float(variant['price'])

                if not sku:
                    continue

                print(f"\nChecking SKU {sku} (Shopify price: {shopify_price})")
                actual_price_text = await fetch_price(playwright, sku)
                if not actual_price_text:
                    continue

                try:
                    actual_price = float(str(actual_price_text).replace(',', '.').replace('€', '').strip())
                    print(f"Isotta price: {actual_price}")
                    difference = abs(actual_price - shopify_price) / shopify_price

                    if difference > PRICE_THRESHOLD:
                        print(f"⚠️  Price discrepancy > {PRICE_THRESHOLD*100:.1f}%: External {actual_price}, Shopify {shopify_price}")

                        # ✅ SAFEST: lookup variant ID only inside your vendor's products
                        variant_id = get_variant_id_from_products(products, sku)
                        if variant_id:
                            update_variant_price(variant_id, actual_price)
                        else:
                            print(f"❌ No matching variant ID found for SKU {sku} within vendor's products.")
                    else:
                        print(f"✅ Prices within range. No update.")
                except Exception as e:
                    print(f"Could not compare or update SKU {sku}: {e}")

                # 👇 Add 10 second delay between website scrapes
                await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(main())