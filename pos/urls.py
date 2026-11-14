from django.urls import path
from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("health/", views.health, name="health"),
    path("inventory/", views.inventory, name="inventory"),
    path("checkout/", views.checkout, name="checkout"),
    path("sales/<uuid:pk>/", views.receipt, name="receipt"),
    path("stock/receive/", views.stock_receipt, name="stock_receipt"),
    path("transfers/", views.transfers, name="transfers"),
    path("transfers/new/", views.dispatch, name="dispatch"),
    path("transfers/<uuid:pk>/receive/", views.receive_transfer, name="receive_transfer"),
    path("inventory/<int:location_id>/<int:product_id>/count/", views.reconcile, name="reconcile"),
    path("reports/", views.reports, name="reports"),
]
