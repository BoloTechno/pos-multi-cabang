import uuid
from django import forms
from django.forms import BaseFormSet, formset_factory
from .models import Location, Product
from .services import MAX_QUANTITY, locations_for


class CommandForm(forms.Form):
    key = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)


class SaleForm(CommandForm):
    branch = forms.ModelChoiceField(queryset=Location.objects.none())
    cash_received = forms.DecimalField(max_digits=16, decimal_places=2, min_value=0, label="Cash received")

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["branch"].queryset = locations_for(user).filter(kind="branch")


class SaleItemForm(forms.Form):
    product = forms.ModelChoiceField(queryset=Product.objects.filter(active=True))
    quantity = forms.IntegerField(min_value=1, max_value=MAX_QUANTITY)


class CartFormSet(BaseFormSet):
    def clean(self):
        if any(self.errors):
            return
        products = [f.cleaned_data["product"].pk for f in self.forms if f.cleaned_data]
        if len(products) != len(set(products)):
            raise forms.ValidationError("Use one row per product; combine duplicate quantities.")


SaleItems = formset_factory(SaleItemForm, formset=CartFormSet, extra=3, min_num=1, validate_min=True,
                           max_num=20, validate_max=True, absolute_max=20)


class ReceiptForm(CommandForm):
    location = forms.ModelChoiceField(queryset=Location.objects.none(), label="Warehouse")
    product = forms.ModelChoiceField(queryset=Product.objects.filter(active=True))
    quantity = forms.IntegerField(min_value=1, max_value=MAX_QUANTITY)
    reason = forms.CharField(max_length=250, label="Supplier / delivery reference")

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["location"].queryset = locations_for(user, manage=True).filter(kind="warehouse")


class TransferForm(CommandForm):
    source = forms.ModelChoiceField(queryset=Location.objects.none(), label="From warehouse")
    destination = forms.ModelChoiceField(queryset=Location.objects.filter(active=True, kind="branch"), label="To branch")
    product = forms.ModelChoiceField(queryset=Product.objects.filter(active=True))
    quantity = forms.IntegerField(min_value=1, max_value=MAX_QUANTITY)

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["source"].queryset = locations_for(user, manage=True).filter(kind="warehouse")


class ReconcileForm(CommandForm):
    expected_version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    counted = forms.IntegerField(min_value=0, max_value=MAX_QUANTITY, label="Physical quantity counted")
    reason = forms.CharField(max_length=250, widget=forms.Textarea(attrs={"rows": 3}), label="Count notes / reason")


class ReportForm(forms.Form):
    branch = forms.ModelChoiceField(queryset=Location.objects.none(), required=False, empty_label="All permitted branches")
    start = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    end = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["branch"].queryset = locations_for(user).filter(kind="branch")

    def clean(self):
        data = super().clean()
        if data.get("start") and data.get("end") and data["start"] > data["end"]:
            raise forms.ValidationError("Start date must be on or before end date.")
        if data.get("end") and data["end"].year == 9999:
            raise forms.ValidationError("Choose an end date before year 9999.")
        return data
