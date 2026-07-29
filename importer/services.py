"""Product extraction and marketplace export adapters.

Respect the source site's terms, robots.txt, rate limits and intellectual-property rights.
For production, add per-domain adapters/API integrations and a background job queue.
"""
import json
import re
from itertools import product as cartesian_product
from urllib.parse import urljoin, urlsplit, urlunsplit
import requests
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "ProductBridge/0.1 (+contact@example.com)"}

def _money(value):
    if isinstance(value, dict):
        value = value.get("amount") or value.get("price") or value.get("value") or ""
    return str(value or "").replace("$", "").replace(",", "").strip()

def _json_description(value):
    """Find description/body_html in nested Next.js/theme product payloads."""
    if isinstance(value, dict):
        for key in ("description", "body_html", "productDescription", "short_description"):
            text = value.get(key)
            if isinstance(text, str) and len(text.strip()) > 20:
                return BeautifulSoup(text, "lxml").get_text(" ", strip=True)
        for child in value.values():
            result = _json_description(child)
            if result: return result
    elif isinstance(value, list):
        for child in value:
            result = _json_description(child)
            if result: return result
    return ""

def _canonical_image(url):
    """The same CDN asset often appears with different width/cache query strings."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))

def _image_src(node, page_url):
    srcset = node.get("data-srcset") or node.get("srcset")
    if srcset:
        # Select the largest advertised responsive rendition.
        candidates = [part.strip().split()[0] for part in srcset.split(",") if part.strip()]
        if candidates: return _canonical_image(urljoin(page_url, candidates[-1]))
    src = node.get("data-zoom-image") or node.get("data-src") or node.get("src")
    return _canonical_image(urljoin(page_url, src)) if src else ""

def _json_images(value, page_url):
    """Find gallery assets in common embedded product JSON shapes."""
    found = []
    def visit(item, in_media=False):
        if isinstance(item, str):
            if in_media and re.search(r"\.(?:jpg|jpeg|png|webp|gif)(?:\?|$)", item, re.I):
                found.append(_canonical_image(urljoin(page_url, item)))
        elif isinstance(item, list):
            for child in item: visit(child, in_media)
        elif isinstance(item, dict):
            for key, child in item.items():
                lower = key.lower()
                if lower in {"images", "media", "gallery", "featured_image", "featured_media"}:
                    visit(child, True)
                elif in_media and lower in {"src", "url", "original_src", "url_zoom", "large"}:
                    visit(child, True)
    visit(value)
    return found

def extract_product(url):
    parsed_url = urlsplit(url)
    # WooCommerce's public Store API is more reliable than rendered HTML when available.
    # It is used only for the product page the user supplied.
    woo_product = None
    if "/shop/" in parsed_url.path or "/product/" in parsed_url.path:
        slug = parsed_url.path.rstrip("/").split("/")[-1]
        try:
            api = urlunsplit((parsed_url.scheme, parsed_url.netloc, "/wp-json/wc/store/v1/products", f"slug={slug}", ""))
            api_response = requests.get(api, headers=HEADERS, timeout=15)
            api_payload = api_response.json() if api_response.ok else []
            woo_product = api_payload[0] if isinstance(api_payload, list) and api_payload else None
        except (requests.RequestException, ValueError, IndexError):
            woo_product = None
    response = requests.get(url, headers=HEADERS, timeout=15)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "lxml")
    data = {"source_url": url, "title": "", "description": "", "price": "", "compare_at_price": "", "images": [], "sku": "", "vendor": "", "categories": "", "tags": "", "seo_title": "", "seo_description": "", "product_type": "", "weight": "", "barcode": "", "variants": []}
    if woo_product:
        prices = woo_product.get("prices", {})
        raw_price = prices.get("price", "")
        minor_unit = int(prices.get("currency_minor_unit", 0) or 0)
        price = str(raw_price)
        if minor_unit and price.isdigit(): price = str(int(price) / (10 ** minor_unit))
        data.update({"title": woo_product.get("name", ""), "description": BeautifulSoup(woo_product.get("description") or woo_product.get("short_description") or "", "lxml").get_text(" ", strip=True), "price": price, "sku": woo_product.get("sku", ""), "categories": ", ".join(x.get("name", "") for x in woo_product.get("categories", []) if x.get("name")), "tags": ", ".join(x.get("name", "") for x in woo_product.get("tags", []) if x.get("name")), "images": [_canonical_image(x.get("src", "")) for x in woo_product.get("images", []) if x.get("src")]})
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or "{}")
            items = payload if isinstance(payload, list) else [payload]
            for item in items:
                if item.get("@type") in ("Product", ["Product"]):
                    offers = item.get("offers", {})
                    if isinstance(offers, list): offers = offers[0]
                    data.update({"title": item.get("name", "") or data["title"], "description": item.get("description", "") or data["description"],
                                 "price": _money(offers.get("price")) or data["price"], "sku": item.get("sku", "") or data["sku"],
                                 "vendor": item.get("brand", {}).get("name", "") if isinstance(item.get("brand"), dict) else item.get("brand", ""),
                                 "categories": item.get("category", "") or ""})
                    # Product JSON-LD normally exposes the active price only; a visual old price is read below.
                    data["compare_at_price"] = ""
                    image = item.get("image", [])
                    data["images"] = [_canonical_image(urljoin(url, x)) for x in (image if isinstance(image, list) else [image]) if x]
                    for variant in item.get("hasVariant", []) or []:
                        offer = variant.get("offers", {})
                        if isinstance(offer, list): offer = offer[0] if offer else {}
                        props = variant.get("additionalProperty", []) or []
                        options = {str(x.get("name", "Option")): str(x.get("value", "")) for x in props if isinstance(x, dict) and x.get("value")}
                        data["variants"].append({"title": variant.get("name", ""), "sku": variant.get("sku", ""), "barcode": variant.get("gtin13", variant.get("gtin", "")), "price": _money(offer.get("price")) or data["price"], "compare_at_price": "", "weight": variant.get("weight", ""), "options": options, "image": ""})
                    break
        except (ValueError, AttributeError, TypeError):
            pass
    fallback_title = (soup.select_one("h1") or soup.title).get_text(" ", strip=True) if (soup.select_one("h1") or soup.title) else "Untitled product"
    data["title"] = data["title"] or fallback_title
    # Use the real product description area before falling back to a generic page meta tag.
    description_node = soup.select_one('[itemprop="description"], [data-product-description], .product-description, .product__description, .product-single__description, .woocommerce-product-details__short-description, .woocommerce-Tabs-panel--description, #tab-description, #description')
    if description_node:
        data["description"] = data["description"] or description_node.get_text(" ", strip=True)
    meta = soup.select_one('meta[name="description"]')
    data["description"] = data["description"] or (meta.get("content", "") if meta else "")
    seo_title = soup.select_one('meta[property="og:title"]')
    data["seo_title"] = seo_title.get("content", "") if seo_title else data["title"]
    data["seo_description"] = data["description"]
    image = soup.select_one('meta[property="og:image"]')
    if not data["images"]:
        data["images"] = [urljoin(url, image["content"])] if image and image.get("content") else []
    # Extend structured images with common product-gallery markup.
    for node in soup.select('[data-product-image] img, [data-media-id] img, .product-gallery img, .woocommerce-product-gallery img, .product__media img, .product__media-item img, .product-single__media img, .fotorama__img, [itemprop="image"][src], [itemprop="image"] img'):
        src = _image_src(node, url)
        if src and not src.startswith("data:"):
            data["images"].append(src)
    # Common metadata fallbacks when a site does not publish Product JSON-LD.
    def meta_value(*selectors):
        for selector in selectors:
            node = soup.select_one(selector)
            if node and node.get("content"): return node["content"].strip()
        return ""
    data["price"] = data["price"] or _money(meta_value('meta[property="product:price:amount"]', 'meta[itemprop="price"]', 'meta[name="price"]'))
    data["vendor"] = data["vendor"] or meta_value('meta[property="product:brand"]', 'meta[name="brand"]', 'meta[itemprop="brand"]')
    data["sku"] = data["sku"] or meta_value('meta[itemprop="sku"]', 'meta[name="sku"]', 'meta[property="product:retailer_item_id"]')
    data["categories"] = data["categories"] or meta_value('meta[property="product:category"]', 'meta[name="category"]')
    crumbs = [x.get_text(" ", strip=True) for x in soup.select('[aria-label="Breadcrumb"] a, .breadcrumb a, .breadcrumbs a')]
    if not data["categories"] and crumbs: data["categories"] = ", ".join(crumbs[1:-1] or crumbs[:-1])
    data["tags"] = data["tags"] or meta_value('meta[name="keywords"]')
    # If a source provides categories but no separate tags, carry the category as a
    # searchable Shopify tag instead of leaving the field unusably empty.
    data["tags"] = data["tags"] or data["categories"]
    # Common visual old-price patterns. This is intentionally separate from sale price.
    old = soup.select_one('[class*="compare" i], [class*="old-price" i], [class*="was-price" i], .price del, del.price')
    data["compare_at_price"] = data["compare_at_price"] or _money(old.get_text(" ", strip=True) if old else "")
    # Deduplicate canonical image URLs while preserving source order.
    data["images"] = list(dict.fromkeys(_canonical_image(x) for x in data["images"] if x))
    # Many ecommerce themes embed a Shopify-style product JSON object with variants.
    if not data["variants"]:
        for script in soup.select('script[type="application/json"]'):
            try:
                payload = json.loads(script.string or "{}")
                # Shopify/theme product payloads commonly keep the full HTML here.
                product_payload = payload.get("product", payload) if isinstance(payload, dict) else {}
                if isinstance(product_payload, dict):
                    embedded_description = product_payload.get("description") or product_payload.get("body_html") or ""
                    if embedded_description and not data["description"]:
                        data["description"] = BeautifulSoup(str(embedded_description), "lxml").get_text(" ", strip=True)
                data["images"].extend(_json_images(payload, url))
                variants = payload.get("variants", []) if isinstance(payload, dict) else []
                if variants and isinstance(variants, list):
                    for v in variants:
                        if not isinstance(v, dict): continue
                        options = {f"Option {n}": str(v.get(f"option{n}", "")) for n in range(1, 4) if v.get(f"option{n}")}
                        image = v.get("featured_image") or v.get("image") or {}
                        if isinstance(image, dict): image = image.get("src", "")
                        data["variants"].append({"title": v.get("title", ""), "sku": v.get("sku", ""), "barcode": v.get("barcode", ""), "price": _money(v.get("price")) or data["price"], "compare_at_price": _money(v.get("compare_at_price")), "weight": v.get("weight", ""), "options": options, "image": _canonical_image(urljoin(url, image)) if image else ""})
                    break
            except (ValueError, AttributeError, TypeError): pass
    # A number of themes place `variants: [...]` inside a normal JavaScript block,
    # rather than an application/json script. Decode that embedded array safely.
    if not data["variants"]:
        decoder = json.JSONDecoder()
        for script in soup.select('script:not([src])'):
            text = script.string or script.get_text() or ""
            marker = re.search(r'["\']variants["\']\s*:\s*\[', text)
            if not marker: continue
            try:
                raw, _ = decoder.raw_decode(text[marker.end() - 1:])
                if not isinstance(raw, list): continue
                for v in raw:
                    if not isinstance(v, dict): continue
                    options = {f"Option {n}": str(v.get(f"option{n}", "")) for n in range(1, 4) if v.get(f"option{n}")}
                    image = v.get("featured_image") or v.get("image") or ""
                    if isinstance(image, dict): image = image.get("src", "")
                    data["variants"].append({"title": v.get("title", ""), "sku": v.get("sku", ""), "barcode": v.get("barcode", ""), "price": _money(v.get("price")) or data["price"], "compare_at_price": _money(v.get("compare_at_price")), "weight": v.get("weight", ""), "options": options, "image": _canonical_image(urljoin(url, image)) if image else ""})
                if data["variants"]: break
            except (ValueError, TypeError): continue
    # Theme-agnostic fallback: native size/color selectors used by many storefronts.
    # JSON remains preferred because it contains only valid option combinations.
    if not data["variants"]:
        option_groups = []
        for select in soup.select('form select'):
            name = select.get('name', '')
            if any(word in name.lower() for word in ('quantity', 'country', 'state')): continue
            label = soup.select_one(f'label[for="{select.get("id", "")}"]')
            option_name = label.get_text(" ", strip=True) if label else re.sub(r'^(options?\[|attribute_?)|\]$', '', name, flags=re.I).strip() or "Variant"
            choices = []
            for option in select.select('option[value]'):
                text = option.get_text(" ", strip=True)
                if not text or option.has_attr('disabled') or option.get('value') in {'', '0'}: continue
                choices.append({"value": text, "sku": option.get('data-sku', ''), "price": _money(option.get('data-price', '')), "barcode": option.get('data-barcode', '')})
            if choices: option_groups.append((option_name, choices))
        # Avoid creating hundreds of uncertain combinations from an unusual form.
        combination_count = 1
        for _, choices in option_groups: combination_count *= len(choices)
        if option_groups and combination_count <= 150:
            for combination in cartesian_product(*(choices for _, choices in option_groups)):
                options = {option_groups[i][0]: choice["value"] for i, choice in enumerate(combination)}
                data["variants"].append({"title": " / ".join(options.values()), "sku": next((x["sku"] for x in combination if x["sku"]), ""), "barcode": next((x["barcode"] for x in combination if x["barcode"]), ""), "price": next((x["price"] for x in combination if x["price"]), data["price"]), "compare_at_price": "", "weight": "", "options": options, "image": ""})
    # JSON used by some themes has no variants but still holds a complete media gallery.
    for script in soup.select('script[type="application/json"]'):
        try:
            payload = json.loads(script.string or "{}")
            data["images"].extend(_json_images(payload, url))
            data["description"] = data["description"] or _json_description(payload)
            product_payload = payload.get("product", payload) if isinstance(payload, dict) else {}
            if isinstance(product_payload, dict) and not data["description"]:
                embedded_description = product_payload.get("description") or product_payload.get("body_html") or ""
                if embedded_description:
                    data["description"] = BeautifulSoup(str(embedded_description), "lxml").get_text(" ", strip=True)
        except (ValueError, AttributeError, TypeError): pass
    data["images"] = list(dict.fromkeys(_canonical_image(x) for x in data["images"] if x))
    return data

def extract_collection(url, limit=None):
    """Extract only cards from the requested collection grid—not menus/recommendations."""
    response = requests.get(url, headers=HEADERS, timeout=15)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "lxml")
    links, seen = [], set()
    # E-commerce themes use one of these wrappers for the actual collection items.
    # Deliberately do not fall back to every page link: that captures navigation and related products.
    grid_candidates = soup.select('[data-product-grid], #product-grid, .product-grid, .collection-products, .collection__products, [data-collection-products], .products-grid, ul.products, .products')
    # A theme may apply .product-grid to every card. Pick the container holding most product URLs.
    def product_link_count(node):
        return len({urljoin(url, a.get("href", "")).split("#")[0] for a in node.select("a[href]") if re.search(r"/(products?|product)/", a.get("href", ""), re.I)})
    grid = max(grid_candidates, key=product_link_count, default=None)
    if not grid:
        # Schema.org ItemList is the least ambiguous fallback for a collection page.
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                payload = json.loads(script.string or "{}")
                candidates = payload if isinstance(payload, list) else [payload]
                for item in candidates:
                    if item.get("@type") == "ItemList":
                        for entry in item.get("itemListElement", []):
                            target = entry.get("url") or entry.get("item", {}).get("url", "")
                            if target: links.append(urljoin(url, target))
                        break
            except (ValueError, AttributeError, TypeError): pass
    else:
        for a in grid.select("a[href]"):
            href = urljoin(url, a["href"]).split("#")[0]
            if href != url and re.search(r"/(products?|product)/", href, re.I) and href not in seen:
                links.append(href); seen.add(href)
                if limit and len(links) >= limit: break
    # Deduplicate while retaining collection-grid ordering.
    links = list(dict.fromkeys(links))
    products, failures = [], []
    for link in links:
        try: products.append(extract_product(link))
        except Exception: failures.append(link)
    return products, failures

def import_health(products):
    """Non-blocking quality checks shown before an export is created."""
    seen, report = set(), []
    for product in products:
        issues = []
        if not product.get("title"): issues.append("Missing title")
        if not product.get("price"): issues.append("No price found")
        if not product.get("images"): issues.append("No image found")
        if product.get("source_url") in seen: issues.append("Duplicate source URL")
        seen.add(product.get("source_url"))
        report.append({"ready": not issues, "issues": issues, "image_count": len(product.get("images", []))})
    return report

def product_rows(product, platform):
    images = product.get("images") or [""]
    variants = product.get("variants") or [{}]
    # Some stores expose variant titles but omit option keys. Shopify still needs an
    # option name/value pair, so preserve those variants as a Size option.
    if len(variants) > 1 and not any(v.get("options") for v in variants):
        for variant in variants:
            if variant.get("title"):
                variant["options"] = {"Size": variant["title"]}
    # Do not emit invalid duplicate variants. Shopify requires every variant to be
    # meaningfully distinct (different option values or SKU).
    unique_variants, seen_variants = [], set()
    for variant in variants:
        key = (variant.get("sku", ""), tuple(sorted((variant.get("options") or {}).items())), variant.get("title", ""))
        if key not in seen_variants:
            unique_variants.append(variant); seen_variants.add(key)
    variants = unique_variants
    if platform == "shopify":
        rows = []
        handle = product["title"].lower().replace(" ", "-")[:100]
        for index, variant in enumerate(variants):
            options = list((variant.get("options") or {}).items())[:3]
            row = {"Title": product["title"] if index == 0 else "", "URL handle": handle, "Description": product["description"] if index == 0 else "", "Vendor": product["vendor"] if index == 0 else "", "Product category": product.get("categories", "") if index == 0 else "", "Type": product.get("product_type", "") if index == 0 else "", "Tags": product.get("tags", "") if index == 0 else "", "Published on online store": "TRUE", "Status": "Draft", "SKU": variant.get("sku") or product["sku"], "Barcode": variant.get("barcode", ""), "Price": variant.get("price") or product["price"], "Compare-at price": variant.get("compare_at_price") or product.get("compare_at_price", ""), "Weight value (grams)": variant.get("weight", product.get("weight", "")), "Weight unit for display": "g", "Requires shipping": "TRUE", "Fulfillment service": "manual", "Product image URL": images[0] if index == 0 else "", "Variant image URL": variant.get("image", ""), "SEO title": product.get("seo_title", "") if index == 0 else "", "SEO description": product.get("seo_description", "") if index == 0 else ""}
            for number in range(1, 4):
                name, value = options[number - 1] if len(options) >= number else ("", "")
                row[f"Option{number} name"], row[f"Option{number} value"] = name, value
            rows.append(row)
        # Image-only rows attach the remaining gallery images without duplicating the primary image.
        used = {r["Product image URL"] for r in rows}
        for image in images:
            if image and image not in used:
                rows.append({**{key: "" for key in rows[0]}, "URL handle": handle, "Product image URL": image})
        return rows
    # WooCommerce treats the regular price as original and sale price as current.
    if len(product.get("variants") or []) > 0:
        handle = product["title"].lower().replace(" ", "-")[:100]
        parent_sku = product["sku"] or handle or "parent"
        parent = {"Type": "variable", "SKU": parent_sku, "Name": product["title"], "Published": 0, "Regular price": "", "Sale price": "", "Description": product["description"], "Images": ", ".join(images), "Categories": product.get("categories", ""), "Tags": product.get("tags", ""), "Meta: _vendor": product.get("vendor", ""), "Meta: yoast_wpseo_title": product.get("seo_title", ""), "Meta: yoast_wpseo_metadesc": product.get("seo_description", ""), "Parent": "", "Attribute 1 name": "", "Attribute 1 value(s)": ""}
        rows = [parent]
        for v in product["variants"]:
            options = list((v.get("options") or {}).items())
            rows.append({"Type": "variation", "SKU": v.get("sku", ""), "Name": v.get("title", ""), "Published": 0, "Regular price": v.get("compare_at_price") or v.get("price") or product["price"], "Sale price": v.get("price") if v.get("compare_at_price") else "", "Description": "", "Images": v.get("image", ""), "Categories": "", "Tags": "", "Meta: _vendor": "", "Meta: yoast_wpseo_title": "", "Meta: yoast_wpseo_metadesc": "", "Parent": parent_sku, "Attribute 1 name": options[0][0] if options else "", "Attribute 1 value(s)": options[0][1] if options else ""})
        return rows
    return [{"Type": "simple", "SKU": product["sku"], "Name": product["title"], "Published": 0, "Regular price": product.get("compare_at_price") or product["price"], "Sale price": product["price"] if product.get("compare_at_price") else "", "Description": product["description"], "Images": ", ".join(images), "Categories": product.get("categories", ""), "Tags": product.get("tags", ""), "Meta: _vendor": product.get("vendor", ""), "Meta: yoast_wpseo_title": product.get("seo_title", ""), "Meta: yoast_wpseo_metadesc": product.get("seo_description", "")}]
