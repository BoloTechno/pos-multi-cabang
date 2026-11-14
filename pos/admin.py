from django.contrib import admin
from .models import Location, Membership, Product, Stock, Movement, Operation, Sale, SaleLine, Transfer, Reconciliation

# Master-data administration is reserved for head office, not branch employees.
admin.site.has_permission = lambda request: request.user.is_active and request.user.is_superuser
admin.site.site_header = "Bolo POS · Head office"


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ["sku", "name", "price", "active"]
    search_fields = ["sku", "name"]


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = ["name", "kind", "active"]

    def get_readonly_fields(self, request, obj=None):
        return ["kind"] if obj else []


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ["user", "location", "role"]


class LedgerAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


for model in [Stock, Movement, Operation, Sale, SaleLine, Transfer, Reconciliation]:
    admin.site.register(model, LedgerAdmin)
