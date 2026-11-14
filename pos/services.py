"""All stock writes go through atomic commands; PostgreSQL locks locations in ID order."""
import hashlib
import json
from decimal import Decimal, InvalidOperation

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from .models import Location, Membership, Movement, Operation, Product, Reconciliation, Sale, SaleLine, Stock, Transfer

MAX_QUANTITY = 1_000_000_000


def locations_for(user, manage=False):
    if not user.is_authenticated or not user.is_active:
        return Location.objects.none()
    locations = Location.objects.filter(active=True)
    if user.is_superuser:
        return locations
    memberships = Membership.objects.filter(user=user)
    if manage:
        memberships = memberships.filter(role=Membership.Role.MANAGER)
    return locations.filter(id__in=memberships.values("location_id"))


def _authorize(user, location, manage=False):
    if not locations_for(user, manage=manage).filter(pk=location.pk).exists():
        raise PermissionDenied("You do not have access to this location or action.")


def _quantity(value, allow_zero=False):
    if type(value) is not int or not (0 if allow_zero else 1) <= value <= MAX_QUANTITY:
        raise ValidationError("Quantity must be a whole number within the supported range.")


def _locations(*ids):
    rows = {row.pk: row for row in Location.objects.select_for_update().filter(pk__in=ids).order_by("pk")}
    if len(rows) != len(set(ids)) or any(not row.active for row in rows.values()):
        raise ValidationError("Choose active locations.")
    return rows


def _product(product_id):
    try:
        return Product.objects.get(pk=product_id, active=True)
    except Product.DoesNotExist:
        raise ValidationError("Choose an active product.") from None


def _operation(key, user, kind, payload):
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    op, created = Operation.objects.get_or_create(id=key, defaults={"actor": user, "kind": kind, "fingerprint": fingerprint})
    if (op.actor_id, op.kind, op.fingerprint) != (user.pk, kind, fingerprint):
        raise ValidationError("This submission ID was already used for different data. Reload the form.")
    return op, created


def _move(op, location, product, delta, note):
    stock, _ = Stock.objects.get_or_create(location=location, product=product)
    updated = stock.quantity + delta
    if updated < 0:
        raise ValidationError(f"Insufficient stock for {product.sku} at {location.name}.")
    if updated > MAX_QUANTITY:
        raise ValidationError("Resulting stock exceeds the supported range.")
    stock.quantity = updated
    stock.version += 1
    stock.save(update_fields=["quantity", "version"])
    Movement.objects.create(operation=op, location=location, product=product, delta=delta,
                            balance_after=updated, note=note)


@transaction.atomic
def receive_stock(*, user, location_id, product_id, quantity, reason, key):
    location = _locations(location_id)[location_id]
    _authorize(user, location, manage=True)
    if location.kind != Location.Kind.WAREHOUSE:
        raise ValidationError("External stock receipts go to a central warehouse.")
    _quantity(quantity)
    if not reason.strip() or len(reason) > 250:
        raise ValidationError("Provide a receipt reference or reason (up to 250 characters).")
    product = _product(product_id)
    op, created = _operation(key, user, "stock_receipt", [location_id, product_id, quantity, reason])
    if created:
        _move(op, location, product, quantity, reason)
    return op


@transaction.atomic
def create_sale(*, user, branch_id, items, cash_received, key):
    branch = _locations(branch_id)[branch_id]
    _authorize(user, branch)
    if branch.kind != Location.Kind.BRANCH:
        raise ValidationError("Sales can only be recorded at a branch.")
    if not 1 <= len(items) <= 20 or len({p for p, q in items}) != len(items):
        raise ValidationError("Add 1–20 distinct products.")
    for _, quantity in items:
        _quantity(quantity)
    try:
        cash = Decimal(str(cash_received))
        if not cash.is_finite() or cash < 0 or cash > Decimal("99999999999999.99") or cash.as_tuple().exponent < -2:
            raise ValueError
    except (InvalidOperation, ValueError):
        raise ValidationError("Enter a valid cash amount with up to two decimal places.") from None
    op, created = _operation(key, user, "sale", [branch_id, sorted(items), str(cash.quantize(Decimal(".01")))])
    if not created:
        return op.sale
    priced = [(_product(product_id), quantity) for product_id, quantity in items]
    total = sum((p.price * q for p, q in priced), Decimal("0.00"))
    if total > Decimal("99999999999999.99"):
        raise ValidationError("Sale total exceeds the supported range.")
    if cash < total:
        raise ValidationError(f"Cash received must cover the total ({total:,.2f}).")
    sale = Sale.objects.create(operation=op, branch=branch, total=total, cash_received=cash)
    for product, quantity in priced:
        _move(op, branch, product, -quantity, "Cash sale")
        SaleLine.objects.create(sale=sale, product=product, quantity=quantity, unit_price=product.price,
                                product_name=product.name, sku=product.sku)
    return sale


@transaction.atomic
def dispatch_transfer(*, user, source_id, destination_id, product_id, quantity, key):
    rows = _locations(source_id, destination_id)
    source, destination = rows[source_id], rows[destination_id]
    _authorize(user, source, manage=True)
    if source.kind != Location.Kind.WAREHOUSE or destination.kind != Location.Kind.BRANCH:
        raise ValidationError("Transfers must go from a central warehouse to a branch.")
    _quantity(quantity)
    product = _product(product_id)
    op, created = _operation(key, user, "dispatch", [source_id, destination_id, product_id, quantity])
    if not created:
        return op.transfer
    transfer = Transfer.objects.create(operation=op, source=source, destination=destination, product=product, quantity=quantity)
    _move(op, source, product, -quantity, f"Dispatched to {destination.name}")
    return transfer


@transaction.atomic
def receive_transfer(*, user, transfer_id, key):
    transfer = Transfer.objects.get(pk=transfer_id)
    rows = _locations(transfer.source_id, transfer.destination_id)
    destination = rows[transfer.destination_id]
    _authorize(user, destination, manage=True)
    transfer = Transfer.objects.select_for_update().get(pk=transfer_id)
    op, created = _operation(key, user, "transfer_receipt", [str(transfer_id)])
    if not created:
        return transfer
    if transfer.status != Transfer.Status.IN_TRANSIT:
        raise ValidationError("This transfer has already been received.")
    # Historical transfers can be received even if the product has since been retired.
    _move(op, destination, transfer.product, transfer.quantity, f"Received from {transfer.source.name}")
    transfer.status = Transfer.Status.RECEIVED
    transfer.receipt_operation = op
    transfer.save(update_fields=["status", "receipt_operation"])
    return transfer


@transaction.atomic
def reconcile_stock(*, user, location_id, product_id, counted, expected_version, reason, key):
    location = _locations(location_id)[location_id]
    _authorize(user, location, manage=True)
    _quantity(counted, allow_zero=True)
    if not reason.strip() or len(reason) > 250:
        raise ValidationError("Explain the count adjustment (up to 250 characters).")
    # Retired products still need physical counts and stock write-offs.
    try:
        product = Product.objects.get(pk=product_id)
    except Product.DoesNotExist:
        raise ValidationError("Product not found.") from None
    op, created = _operation(key, user, "reconciliation", [location_id, product_id, counted, expected_version, reason])
    if not created:
        return op.reconciliation
    stock, _ = Stock.objects.get_or_create(location=location, product=product)
    if stock.version != expected_version:
        raise ValidationError("Stock changed since this count was opened. Reload and recount before submitting.")
    count = Reconciliation.objects.create(operation=op, location=location, product=product,
                                          expected=stock.quantity, counted=counted, reason=reason)
    _move(op, location, product, counted - stock.quantity, reason)
    return count
