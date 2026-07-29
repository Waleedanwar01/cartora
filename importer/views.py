import csv, json
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST
from .services import extract_product, extract_collection, import_health, product_rows

def dashboard(request):
    return render(request, "importer/dashboard.html")

@require_POST
def preview(request):
    url = request.POST.get("url", "").strip()
    mode = request.POST.get("mode", "product")
    if not url.startswith(("https://", "http://")):
        return JsonResponse({"error": "Enter a full http(s) product URL."}, status=400)
    try:
        if mode == "collection":
            products, failures = extract_collection(url)
            return JsonResponse({"products": products, "failures": failures, "health": import_health(products)})
        products = [extract_product(url)]
        return JsonResponse({"products": products, "failures": [], "health": import_health(products)})
    except Exception as exc:
        return JsonResponse({"error": f"Could not read this product: {exc}"}, status=422)

@require_POST
def export_csv(request, platform):
    if platform not in {"shopify", "woocommerce"}: return HttpResponse(status=404)
    products = json.loads(request.POST["products"])
    selected = set(json.loads(request.POST.get("fields", "[]")))
    if platform == "shopify":
        rename = {"Body (HTML)": "Description", "Variant SKU": "SKU", "Variant Price": "Price", "Variant Compare At Price": "Compare-at price", "Image Src": "Product image URL", "SEO Title": "SEO title", "SEO Description": "SEO description", "Option1 Name": "Option1 name", "Option1 Value": "Option1 value", "Option2 Name": "Option2 name", "Option2 Value": "Option2 value", "Option3 Name": "Option3 name", "Option3 Value": "Option3 value"}
        selected = {rename.get(field, field) for field in selected}
    # Keep an original/compare price together with the displayed price.
    if platform == "shopify" and "Variant Price" in selected:
        selected.update({"Price", "Compare-at price"})
    if platform == "woocommerce" and "Regular price" in selected:
        selected.add("Sale price")
    rows = [row for product in products for row in product_rows(product, platform)]
    # Checkboxes must always be honoured, including when a user deselects every optional field.
    structural = {"URL handle", "Name", "Title", "Type", "Parent", "Published", "Status", "Published on online store"}
    rows = [{key: value for key, value in row.items() if key in selected or key in structural} for row in rows]
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{platform}-import.csv"'
    if not rows: return HttpResponse("No products to export", status=400)
    writer = csv.DictWriter(response, fieldnames=rows[0].keys(), extrasaction="ignore")
    writer.writeheader(); writer.writerows(rows)
    return response
