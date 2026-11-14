import os
import uuid
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from pos.models import Location, Membership, Product
from pos.services import create_sale, dispatch_transfer, receive_stock, receive_transfer


def key(label):
    return uuid.uuid5(uuid.NAMESPACE_URL, "bolo-pos-demo/" + label)


class Command(BaseCommand):
    help = "Seed a development workspace. Requires DEBUG=true and DEMO_PASSWORD. Safe to rerun."

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Demo seeding is disabled outside development.")
        password = os.environ.get("DEMO_PASSWORD", "")
        if not password:
            raise CommandError("Set DEMO_PASSWORD to a strong development-only password.")
        validate_password(password)
        User = get_user_model()
        owner, created = User.objects.get_or_create(username="owner", defaults={"is_staff": True, "is_superuser": True})
        if created:
            owner.set_password(password)
            owner.save()
        elif not owner.is_superuser:
            raise CommandError("Existing 'owner' is not a superuser; refusing to change privileges.")
        warehouse, _ = Location.objects.get_or_create(name="Central warehouse", defaults={"kind": "warehouse"})
        branches = [Location.objects.get_or_create(name=name, defaults={"kind": "branch"})[0]
                    for name in ["North branch", "South branch"]]
        for username, location, role in [("north-manager", branches[0], "manager"),
                                         ("north-cashier", branches[0], "cashier"),
                                         ("south-manager", branches[1], "manager")]:
            user, created = User.objects.get_or_create(username=username)
            if created:
                user.set_password(password)
                user.save()
            Membership.objects.get_or_create(user=user, location=location, defaults={"role": role})
        for sku, name, price in [("COF-001", "House blend coffee · 250g", "65000"),
                                  ("TEA-002", "Jasmine tea · 100g", "32000"),
                                  ("MUG-003", "Everyday ceramic mug", "45000")]:
            product, _ = Product.objects.get_or_create(sku=sku, defaults={"name": name, "price": Decimal(price)})
            receive_stock(user=owner, location_id=warehouse.pk, product_id=product.pk,
                          quantity=200, reason="Demo opening delivery", key=key(sku + "-opening"))
            for branch in branches:
                transfer = dispatch_transfer(user=owner, source_id=warehouse.pk, destination_id=branch.pk,
                    product_id=product.pk, quantity=40, key=key(sku + branch.name + "-dispatch"))
                receive_transfer(user=owner, transfer_id=transfer.pk, key=key(sku + branch.name + "-received"))
                create_sale(user=owner, branch_id=branch.pk, items=[(product.pk, 2)],
                            cash_received=product.price * 2, key=key(sku + branch.name + "-sale"))
        self.stdout.write(self.style.SUCCESS("Demo ready: owner, north-manager, north-cashier, south-manager. Existing passwords unchanged."))
