import csv
import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q, Sum
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import services
from .forms import CommandForm, ReceiptForm, ReconcileForm, ReportForm, SaleForm, SaleItems, TransferForm
from .models import Location, Movement, Product, Reconciliation, Sale, SaleLine, Stock, Transfer


def _errors(form, exc):
    form.add_error(None, exc)


@login_required
def dashboard(request):
    locations = services.locations_for(request.user)
    sales = Sale.objects.filter(branch__in=locations)
    today = timezone.localdate()
    return render(request, "pos/dashboard.html", {
        "locations": locations,
        "today_sales": sales.filter(operation__created_at__date=today).aggregate(n=Sum("total"))["n"] or Decimal("0.00"),
        "today_count": sales.filter(operation__created_at__date=today).count(),
        "stock_units": Stock.objects.filter(location__in=locations).aggregate(n=Sum("quantity"))["n"] or 0,
        "in_transit": Transfer.objects.filter(Q(source__in=locations) | Q(destination__in=locations), status="in_transit").count(),
        "recent": sales.select_related("branch", "operation__actor").order_by("-operation__created_at")[:8],
    })


@login_required
def inventory(request):
    locations = services.locations_for(request.user)
    location_id = request.GET.get("location")
    if location_id and not location_id.isdecimal():
        raise Http404("Invalid location")
    location = get_object_or_404(locations, pk=location_id) if location_id else locations.first()
    rows = []
    if location:
        products = Product.objects.all()
        query = request.GET.get("q", "").strip()
        if query:
            products = products.filter(Q(sku__icontains=query) | Q(name__icontains=query))
        page = Paginator(products, 30).get_page(request.GET.get("page"))
        balances = {s.product_id: s for s in Stock.objects.filter(location=location, product__in=page.object_list)}
        rows = [{"product": p, "quantity": balances[p.pk].quantity if p.pk in balances else 0} for p in page]
    else:
        page = None
    return render(request, "pos/inventory.html", {"locations": locations, "location": location, "rows": rows,
        "page": page, "can_manage": bool(location and services.locations_for(request.user, manage=True).filter(pk=location.pk).exists()),
        "movements": Movement.objects.filter(location=location).select_related("product", "operation__actor")[:20] if location else []})


@login_required
def checkout(request):
    form = SaleForm(request.POST or None, user=request.user)
    items = SaleItems(request.POST or None, prefix="items")
    if request.method == "POST":
        form_valid, items_valid = form.is_valid(), items.is_valid()
        if form_valid and items_valid:
            d = form.cleaned_data
            try:
                sale = services.create_sale(user=request.user, branch_id=d["branch"].pk, cash_received=d["cash_received"],
                    items=[(f.cleaned_data["product"].pk, f.cleaned_data["quantity"]) for f in items if f.cleaned_data], key=d["key"])
                return redirect("receipt", pk=sale.pk)
            except ValidationError as exc:
                _errors(form, exc)
    return render(request, "pos/checkout.html", {"form": form, "items": items,
        "products": [{"id": p.pk, "price": str(p.price)} for p in Product.objects.filter(active=True)]})


@login_required
def receipt(request, pk):
    sale = get_object_or_404(Sale.objects.select_related("branch", "operation__actor"), pk=pk,
                             branch__in=services.locations_for(request.user))
    return render(request, "pos/receipt.html", {"sale": sale})


@login_required
def stock_receipt(request):
    form = ReceiptForm(request.POST or None, user=request.user)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            services.receive_stock(user=request.user, location_id=d["location"].pk, product_id=d["product"].pk,
                                   quantity=d["quantity"], reason=d["reason"], key=d["key"])
            messages.success(request, "Warehouse stock received and recorded in the ledger.")
            return redirect("inventory")
        except ValidationError as exc:
            _errors(form, exc)
    return render(request, "pos/form.html", {"form": form, "title": "Receive warehouse stock",
        "description": "Record an external delivery into the central warehouse.", "button": "Record receipt"})


@login_required
def transfers(request):
    locations = services.locations_for(request.user)
    managed = services.locations_for(request.user, manage=True)
    queryset = Transfer.objects.filter(Q(source__in=locations) | Q(destination__in=locations)).select_related(
        "source", "destination", "product", "operation", "receipt_operation")
    page = Paginator(queryset.order_by("-operation__created_at"), 30).get_page(request.GET.get("page"))
    return render(request, "pos/transfers.html", {"page": page, "receive_locations": list(managed.values_list("pk", flat=True)),
                                                 "receive_key": uuid.uuid4()})


@login_required
def dispatch(request):
    form = TransferForm(request.POST or None, user=request.user)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            services.dispatch_transfer(user=request.user, source_id=d["source"].pk, destination_id=d["destination"].pk,
                                       product_id=d["product"].pk, quantity=d["quantity"], key=d["key"])
            messages.success(request, "Stock dispatched. The branch must confirm receipt before it becomes available for sale.")
            return redirect("transfers")
        except ValidationError as exc:
            _errors(form, exc)
    return render(request, "pos/form.html", {"form": form, "title": "Distribute warehouse stock",
        "description": "Dispatch one product shipment to a branch. Stock stays in transit until received.", "button": "Dispatch stock"})


@login_required
@require_POST
def receive_transfer(request, pk):
    get_object_or_404(Transfer, pk=pk, destination__in=services.locations_for(request.user, manage=True))
    form = CommandForm(request.POST)
    if form.is_valid():
        try:
            services.receive_transfer(user=request.user, transfer_id=pk, key=form.cleaned_data["key"])
            messages.success(request, "Transfer received. Branch stock is now available.")
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
    else:
        messages.error(request, "Invalid submission. Reload and try again.")
    return redirect("transfers")


@login_required
def reconcile(request, location_id, product_id):
    location = get_object_or_404(services.locations_for(request.user, manage=True), pk=location_id)
    product = get_object_or_404(Product, pk=product_id)
    stock = Stock.objects.filter(location=location, product=product).first()
    form = ReconcileForm(request.POST or None, initial={"expected_version": stock.version if stock else 0})
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            count = services.reconcile_stock(user=request.user, location_id=location.pk, product_id=product.pk,
                counted=d["counted"], expected_version=d["expected_version"], reason=d["reason"], key=d["key"])
            messages.success(request, f"Count recorded. Adjustment: {count.variance:+d} units.")
            return redirect(f"/inventory/?location={location.pk}")
        except ValidationError as exc:
            _errors(form, exc)
    return render(request, "pos/form.html", {"form": form, "title": "Reconcile physical stock",
        "description": f"{location.name} · {product} · System quantity: {stock.quantity if stock else 0}. Count only on-hand units; exclude shipments in transit.",
        "button": "Record count & adjustment"})


def _csv_safe(value):
    text = str(value)
    return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r", "\n")) else text


@login_required
def reports(request):
    today = timezone.localdate()
    form = ReportForm(request.GET if request.GET else None, user=request.user,
                      initial={"start": today.replace(day=1), "end": today})
    rows = []
    if not form.is_bound:
        form = ReportForm({"start": today.replace(day=1), "end": today}, user=request.user)
    if form.is_valid():
        d = form.cleaned_data
        branches = services.locations_for(request.user).filter(kind="branch")
        if d["branch"]:
            branches = branches.filter(pk=d["branch"].pk)
        start = timezone.make_aware(datetime.combine(d["start"], time.min))
        end = timezone.make_aware(datetime.combine(d["end"] + timedelta(days=1), time.min))
        for branch in branches:
            sales = Sale.objects.filter(branch=branch, operation__created_at__gte=start, operation__created_at__lt=end)
            counts = Reconciliation.objects.filter(location=branch, operation__created_at__gte=start, operation__created_at__lt=end)
            receipts = Transfer.objects.filter(destination=branch, status="received",
                receipt_operation__created_at__gte=start, receipt_operation__created_at__lt=end)
            rows.append({"branch": branch.name, "sales": sales.count(),
                "revenue": sales.aggregate(n=Sum("total"))["n"] or Decimal("0.00"),
                "units_sold": SaleLine.objects.filter(sale__in=sales).aggregate(n=Sum("quantity"))["n"] or 0,
                "received": receipts.aggregate(n=Sum("quantity"))["n"] or 0,
                "adjustment": sum(c.variance for c in counts),
                "on_hand": Stock.objects.filter(location=branch).aggregate(n=Sum("quantity"))["n"] or 0})
        if request.GET.get("format") == "csv":
            response = HttpResponse(content_type="text/csv")
            response["Content-Disposition"] = 'attachment; filename="branch-report.csv"'
            writer = csv.writer(response)
            writer.writerow(["Branch", "Sales", "Revenue", "Units sold", "Units received", "Count adjustment", "Current on hand"])
            for row in rows:
                writer.writerow([_csv_safe(row["branch"]), row["sales"], f"{row['revenue']:.2f}", row["units_sold"], row["received"], row["adjustment"], row["on_hand"]])
            return response
    total = sum((r["revenue"] for r in rows), Decimal("0.00"))
    return render(request, "pos/reports.html", {"form": form, "rows": rows, "total": total})


def health(request):
    return HttpResponse("ok", content_type="text/plain")
