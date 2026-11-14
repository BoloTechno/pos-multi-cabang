import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, close_old_connections, transaction
from django.test import Client, TestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone

from pos import services
from pos.models import Location, Membership, Movement, Operation, Product, Reconciliation, Sale, Stock, Transfer


class POSFixture:
    def setup_data(self):
        User = get_user_model()
        self.owner = User.objects.create_superuser("owner", password="Strong-test-password-123")
        self.manager = User.objects.create_user("north-manager", password="Strong-test-password-123")
        self.cashier = User.objects.create_user("north-cashier", password="Strong-test-password-123")
        self.south_user = User.objects.create_user("south-manager", password="Strong-test-password-123")
        self.warehouse = Location.objects.create(name="Warehouse", kind="warehouse")
        self.north = Location.objects.create(name="North", kind="branch")
        self.south = Location.objects.create(name="South", kind="branch")
        Membership.objects.create(user=self.manager, location=self.north, role="manager")
        Membership.objects.create(user=self.cashier, location=self.north, role="cashier")
        Membership.objects.create(user=self.south_user, location=self.south, role="manager")
        self.coffee = Product.objects.create(sku="COF", name="Coffee", price="10.10")
        self.tea = Product.objects.create(sku="TEA", name="Tea", price="2.50")
        services.receive_stock(user=self.owner, location_id=self.warehouse.pk, product_id=self.coffee.pk,
                               quantity=100, reason="Delivery 001", key=uuid.uuid4())
        self.ship(self.coffee, 20, self.north)
        self.ship(self.coffee, 10, self.south)

    def ship(self, product, quantity, branch, receive=True):
        transfer = services.dispatch_transfer(user=self.owner, source_id=self.warehouse.pk, destination_id=branch.pk,
                                              product_id=product.pk, quantity=quantity, key=uuid.uuid4())
        if receive:
            services.receive_transfer(user=self.owner, transfer_id=transfer.pk, key=uuid.uuid4())
        return transfer

    def quantity(self, location, product=None):
        return Stock.objects.get(location=location, product=product or self.coffee).quantity

    def sale(self, **overrides):
        args = dict(user=self.cashier, branch_id=self.north.pk, items=[(self.coffee.pk, 2)], cash_received=Decimal("25.00"), key=uuid.uuid4())
        args.update(overrides)
        return services.create_sale(**args)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class StockTests(POSFixture, TestCase):
    def setUp(self):
        self.setup_data()

    def test_decimal_sale_snapshots_price_and_change(self):
        sale = self.sale()
        self.assertEqual(sale.total, Decimal("20.20"))
        self.assertEqual(sale.change, Decimal("4.80"))
        self.assertEqual(self.quantity(self.north), 18)
        Product.objects.filter(pk=self.coffee.pk).update(price="99.99", name="New name")
        line = sale.lines.get()
        self.assertEqual(line.unit_price, Decimal("10.10"))
        self.assertEqual(line.product_name, "Coffee")

    def test_sale_retry_does_not_deduct_twice_and_rejects_changed_payload(self):
        key = uuid.uuid4()
        first = self.sale(key=key)
        self.assertEqual(self.sale(key=key).pk, first.pk)
        with self.assertRaises(ValidationError):
            self.sale(key=key, items=[(self.coffee.pk, 1)])
        self.assertEqual(self.quantity(self.north), 18)
        self.assertEqual(Sale.objects.count(), 1)

    def test_insufficient_second_line_rolls_back_entire_sale(self):
        before = Operation.objects.count()
        with self.assertRaises(ValidationError):
            self.sale(items=[(self.coffee.pk, 1), (self.tea.pk, 1)])
        self.assertEqual(self.quantity(self.north), 20)
        self.assertEqual(Operation.objects.count(), before)
        self.assertFalse(Sale.objects.exists())
        self.assertFalse(Stock.objects.filter(location=self.north, product=self.tea).exists())

    def test_underpayment_negative_fractional_and_duplicate_items_rejected(self):
        for override in [dict(cash_received="1"), dict(cash_received="NaN"), dict(cash_received="1.001"),
                         dict(items=[(self.coffee.pk, -1)]), dict(items=[(self.coffee.pk, 1.5)]),
                         dict(items=[]), dict(items=[(self.coffee.pk, 1), (self.coffee.pk, 1)])]:
            with self.subTest(override=override), self.assertRaises(ValidationError):
                self.sale(**override)
        self.assertEqual(self.quantity(self.north), 20)

    def test_cashier_cannot_use_other_branch_or_reconcile(self):
        with self.assertRaises(PermissionDenied):
            self.sale(branch_id=self.south.pk)
        with self.assertRaises(PermissionDenied):
            services.reconcile_stock(user=self.cashier, location_id=self.north.pk, product_id=self.coffee.pk,
                                     counted=10, expected_version=1, reason="Count", key=uuid.uuid4())

    def test_dispatch_conserves_stock_through_in_transit_and_receipt(self):
        before = self.quantity(self.warehouse) + self.quantity(self.north) + self.quantity(self.south)
        transfer = self.ship(self.coffee, 5, self.north, receive=False)
        self.assertEqual(self.quantity(self.north), 20)
        self.assertEqual(self.quantity(self.warehouse) + self.quantity(self.north) + self.quantity(self.south) + transfer.quantity, before)
        key = uuid.uuid4()
        services.receive_transfer(user=self.manager, transfer_id=transfer.pk, key=key)
        services.receive_transfer(user=self.manager, transfer_id=transfer.pk, key=key)
        with self.assertRaises(ValidationError):
            services.receive_transfer(user=self.manager, transfer_id=transfer.pk, key=uuid.uuid4())
        self.assertEqual(self.quantity(self.north), 25)
        self.assertEqual(self.quantity(self.warehouse) + self.quantity(self.north) + self.quantity(self.south), before)

    def test_transfer_requires_source_manager_and_destination_manager(self):
        with self.assertRaises(PermissionDenied):
            services.dispatch_transfer(user=self.manager, source_id=self.warehouse.pk, destination_id=self.north.pk,
                                       product_id=self.coffee.pk, quantity=1, key=uuid.uuid4())
        transfer = self.ship(self.coffee, 1, self.south, receive=False)
        with self.assertRaises(PermissionDenied):
            services.receive_transfer(user=self.manager, transfer_id=transfer.pk, key=uuid.uuid4())

    def test_dispatch_retry_and_oversell(self):
        args = dict(user=self.owner, source_id=self.warehouse.pk, destination_id=self.north.pk,
                    product_id=self.coffee.pk, quantity=3, key=uuid.uuid4())
        first = services.dispatch_transfer(**args)
        self.assertEqual(services.dispatch_transfer(**args).pk, first.pk)
        with self.assertRaises(ValidationError):
            services.dispatch_transfer(**(args | {"quantity": 999, "key": uuid.uuid4()}))
        self.assertEqual(self.quantity(self.warehouse), 67)

    def test_reconciliation_records_variance_and_zero_count(self):
        key = uuid.uuid4()
        args = dict(user=self.manager, location_id=self.north.pk, product_id=self.coffee.pk,
                    counted=0, expected_version=1, reason="Damaged stock verified", key=key)
        count = services.reconcile_stock(**args)
        self.assertEqual(count.expected, 20)
        self.assertEqual(count.variance, -20)
        self.assertEqual(services.reconcile_stock(**args).pk, count.pk)
        self.assertEqual(self.quantity(self.north), 0)
        self.assertEqual(Movement.objects.get(operation_id=count.pk).delta, -20)

    def test_stale_count_rejected_even_when_quantity_returns_to_original(self):
        self.sale(items=[(self.coffee.pk, 1)])
        self.ship(self.coffee, 1, self.north)
        self.assertEqual(self.quantity(self.north), 20)
        with self.assertRaisesMessage(ValidationError, "Stock changed"):
            services.reconcile_stock(user=self.manager, location_id=self.north.pk, product_id=self.coffee.pk,
                                     counted=10, expected_version=1, reason="Count", key=uuid.uuid4())
        self.assertFalse(Reconciliation.objects.exists())

    def test_receipt_idempotency_and_warehouse_only(self):
        args = dict(user=self.owner, location_id=self.warehouse.pk, product_id=self.tea.pk,
                    quantity=5, reason="Supplier 002", key=uuid.uuid4())
        services.receive_stock(**args)
        services.receive_stock(**args)
        self.assertEqual(self.quantity(self.warehouse, self.tea), 5)
        with self.assertRaises(ValidationError):
            services.receive_stock(**(args | {"location_id": self.north.pk, "key": uuid.uuid4()}))

    def test_inactive_locations_and_products_cannot_sell(self):
        Product.objects.filter(pk=self.coffee.pk).update(active=False)
        with self.assertRaises(ValidationError):
            self.sale()
        Product.objects.filter(pk=self.coffee.pk).update(active=True)
        Location.objects.filter(pk=self.north.pk).update(active=False)
        with self.assertRaises(ValidationError):
            self.sale()

    def test_stock_database_constraint_rejects_negative(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Stock.objects.filter(location=self.north).update(quantity=-1)

    def test_ledger_sums_match_on_hand_after_operations(self):
        self.sale()
        self.ship(self.coffee, 4, self.north)
        for stock in Stock.objects.all():
            from django.db.models import Sum
            total = Movement.objects.filter(location=stock.location, product=stock.product).aggregate(n=Sum("delta"))["n"]
            self.assertEqual(stock.quantity, total)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
                   STORAGES={"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
                             "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}})
class ViewTests(POSFixture, TestCase):
    def setUp(self):
        self.setup_data()
        self.client.force_login(self.cashier)

    def test_pages_and_empty_states_render(self):
        for user in [self.owner, self.manager, self.cashier]:
            self.client.force_login(user)
            for url in ["/", "/inventory/", "/checkout/", "/transfers/", "/reports/", "/transfers/new/", "/stock/receive/"]:
                with self.subTest(user=user.username, url=url):
                    self.assertEqual(self.client.get(url).status_code, 200)

    def test_anonymous_user_redirected_and_csrf_required(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.get("/inventory/").status_code, 302)
        client.force_login(self.cashier)
        self.assertEqual(client.post("/checkout/", {}).status_code, 403)

    def test_reports_scope_revenue_and_time_boundaries(self):
        north_sale = self.sale()
        self.sale(user=self.south_user, branch_id=self.south.pk)
        old_sale = self.sale(items=[(self.coffee.pk, 1)])
        Operation.objects.filter(pk=old_sale.pk).update(created_at=timezone.now() - timedelta(days=3))
        today = timezone.localdate().isoformat()
        response = self.client.get("/reports/", {"start": today, "end": today})
        self.assertEqual(response.context["total"], north_sale.total)
        self.assertEqual(len(response.context["rows"]), 1)
        self.assertEqual(response.context["rows"][0]["units_sold"], 2)
        csv = self.client.get("/reports/", {"start": today, "end": today, "format": "csv"})
        self.assertContains(csv, "North,1,20.20,2")
        self.assertNotContains(csv, "South")

    def test_report_denies_cross_branch_filter_and_invalid_date(self):
        today = timezone.localdate().isoformat()
        for params in [{"branch": self.south.pk, "start": today, "end": today},
                       {"start": "bad", "end": today}, {"start": today, "end": "2000-01-01"},
                       {"start": today, "end": "9999-12-31"}]:
            response = self.client.get("/reports/", params)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context["form"].errors)
            self.assertEqual(response.context["rows"], [])

    def test_cannot_read_other_branch_inventory_receipt_or_count(self):
        sale = self.sale(user=self.south_user, branch_id=self.south.pk)
        for url in [f"/inventory/?location={self.south.pk}", reverse("receipt", args=[sale.pk]),
                    reverse("reconcile", args=[self.north.pk, self.coffee.pk]), "/inventory/?location=invalid"]:
            self.assertEqual(self.client.get(url).status_code, 404)

    def test_admin_ledger_not_editable_and_staff_cashier_denied(self):
        self.cashier.is_staff = True
        self.cashier.save()
        self.assertEqual(self.client.get("/admin/").status_code, 302)
        self.client.force_login(self.owner)
        stock = Stock.objects.get(location=self.north, product=self.coffee)
        self.assertEqual(self.client.post(f"/admin/pos/stock/{stock.pk}/change/", {"quantity": 999}).status_code, 403)

    def test_checkout_post_and_duplicate_browser_submission(self):
        payload = {"key": str(uuid.uuid4()), "branch": self.north.pk, "cash_received": "25.00",
                   "items-TOTAL_FORMS": "1", "items-INITIAL_FORMS": "0", "items-MIN_NUM_FORMS": "1",
                   "items-MAX_NUM_FORMS": "20", "items-0-product": self.coffee.pk, "items-0-quantity": "2"}
        first = self.client.post("/checkout/", payload)
        self.assertEqual(first.status_code, 302)
        self.assertEqual(self.client.post("/checkout/", payload).url, first.url)
        self.assertEqual(self.quantity(self.north), 18)
        self.assertContains(self.client.get(first.url), "4.80")

    def test_transfer_confirmation_is_post_only(self):
        transfer = self.ship(self.coffee, 1, self.north, receive=False)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(reverse("receive_transfer", args=[transfer.pk])).status_code, 405)

    def test_csv_neutralizes_spreadsheet_formulas(self):
        self.north.name = "=DANGEROUS()"
        self.north.save()
        today = timezone.localdate().isoformat()
        response = self.client.get("/reports/", {"start": today, "end": today, "format": "csv"})
        self.assertContains(response, "'=DANGEROUS()")

    def test_no_membership_has_empty_workspace_and_cannot_submit_sale(self):
        user = get_user_model().objects.create_user("unassigned")
        self.client.force_login(user)
        for url in ["/", "/inventory/", "/checkout/", "/reports/", "/transfers/"]:
            self.assertEqual(self.client.get(url).status_code, 200)
        with self.assertRaises(PermissionDenied):
            self.sale(user=user)

    def test_count_form_rejects_intervening_sale(self):
        self.client.force_login(self.manager)
        url = reverse("reconcile", args=[self.north.pk, self.coffee.pk])
        response = self.client.get(url)
        version = response.context["form"]["expected_version"].value()
        self.sale(items=[(self.coffee.pk, 1)])
        response = self.client.post(url, {"key": str(uuid.uuid4()), "expected_version": version,
                                          "counted": 18, "reason": "Shelf count"})
        self.assertContains(response, "Stock changed")
        self.assertEqual(self.quantity(self.north), 19)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ConcurrencyTests(POSFixture, TransactionTestCase):
    def setUp(self):
        self.setup_data()

    @skipUnlessDBFeature("has_select_for_update")
    def test_two_cashiers_cannot_oversell_last_units(self):
        barrier = Barrier(2)

        def checkout():
            close_old_connections()
            try:
                user = get_user_model().objects.get(pk=self.cashier.pk)
                barrier.wait(timeout=10)
                services.create_sale(user=user, branch_id=self.north.pk, items=[(self.coffee.pk, 15)],
                                     cash_received="200.00", key=uuid.uuid4())
                return "sold"
            except ValidationError:
                return "insufficient"
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: checkout(), range(2)))
        self.assertCountEqual(results, ["sold", "insufficient"])
        self.assertEqual(self.quantity(self.north), 5)
        self.assertEqual(Sale.objects.count(), 1)

    @skipUnlessDBFeature("has_select_for_update")
    def test_parallel_transfer_receipts_credit_stock_once(self):
        transfer = self.ship(self.coffee, 5, self.north, receive=False)
        barrier = Barrier(2)

        def receive():
            close_old_connections()
            try:
                user = get_user_model().objects.get(pk=self.manager.pk)
                barrier.wait(timeout=10)
                services.receive_transfer(user=user, transfer_id=transfer.pk, key=uuid.uuid4())
                return "received"
            except ValidationError:
                return "already_received"
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: receive(), range(2)))
        self.assertCountEqual(results, ["received", "already_received"])
        self.assertEqual(self.quantity(self.north), 25)
