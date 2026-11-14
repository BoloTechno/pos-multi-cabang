import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class Location(models.Model):
    class Kind(models.TextChoices):
        WAREHOUSE = "warehouse", "Central warehouse"
        BRANCH = "branch", "Branch"

    name = models.CharField(max_length=100, unique=True)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Membership(models.Model):
    class Role(models.TextChoices):
        MANAGER = "manager", "Manager"
        CASHIER = "cashier", "Cashier"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    location = models.ForeignKey(Location, on_delete=models.PROTECT)
    role = models.CharField(max_length=10, choices=Role.choices)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "location"], name="unique_membership")]


class Product(models.Model):
    sku = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=120)
    price = models.DecimalField(max_digits=14, decimal_places=2)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sku"]
        constraints = [models.CheckConstraint(condition=Q(price__gte=0), name="nonnegative_price")]

    def __str__(self):
        return f"{self.sku} · {self.name}"


class Stock(models.Model):
    location = models.ForeignKey(Location, on_delete=models.PROTECT)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField(default=0)
    version = models.PositiveBigIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["location", "product"], name="unique_stock"),
                       models.CheckConstraint(condition=Q(quantity__gte=0), name="nonnegative_stock")]


class Operation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    kind = models.CharField(max_length=20)
    fingerprint = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)


class Movement(models.Model):
    operation = models.ForeignKey(Operation, on_delete=models.PROTECT)
    location = models.ForeignKey(Location, on_delete=models.PROTECT)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    delta = models.IntegerField()
    balance_after = models.PositiveIntegerField()
    note = models.CharField(max_length=250)

    class Meta:
        ordering = ["-operation__created_at", "-id"]
        constraints = [models.UniqueConstraint(fields=["operation", "location", "product"], name="unique_movement")]


class Sale(models.Model):
    operation = models.OneToOneField(Operation, primary_key=True, on_delete=models.PROTECT)
    branch = models.ForeignKey(Location, on_delete=models.PROTECT)
    total = models.DecimalField(max_digits=16, decimal_places=2)
    cash_received = models.DecimalField(max_digits=16, decimal_places=2)

    @property
    def change(self):
        return self.cash_received - self.total


class SaleLine(models.Model):
    sale = models.ForeignKey(Sale, related_name="lines", on_delete=models.PROTECT)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=14, decimal_places=2)
    product_name = models.CharField(max_length=120)
    sku = models.CharField(max_length=40)

    @property
    def subtotal(self):
        return self.quantity * self.unit_price


class Transfer(models.Model):
    class Status(models.TextChoices):
        IN_TRANSIT = "in_transit", "In transit"
        RECEIVED = "received", "Received"

    operation = models.OneToOneField(Operation, primary_key=True, on_delete=models.PROTECT)
    source = models.ForeignKey(Location, related_name="outgoing", on_delete=models.PROTECT)
    destination = models.ForeignKey(Location, related_name="incoming", on_delete=models.PROTECT)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.IN_TRANSIT)
    receipt_operation = models.OneToOneField(Operation, null=True, blank=True, related_name="received_transfer", on_delete=models.PROTECT)


class Reconciliation(models.Model):
    operation = models.OneToOneField(Operation, primary_key=True, on_delete=models.PROTECT)
    location = models.ForeignKey(Location, on_delete=models.PROTECT)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    expected = models.PositiveIntegerField()
    counted = models.PositiveIntegerField()
    reason = models.CharField(max_length=250)

    @property
    def variance(self):
        return self.counted - self.expected
