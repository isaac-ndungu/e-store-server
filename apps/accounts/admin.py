"""Admin editors for the accounts app.

The custom ``User`` reuses Django's ``UserAdmin`` (which already handles
password hashing, staff flags, and password-change) adjusted for email login;
``Address`` gets a lightweight admin scoped by user.
"""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from apps.accounts.models import Address, User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    """Admin page for the email-login custom user model."""

    ordering = ["email"]
    list_display = ("email", "username", "phone_number", "phone_verified", "is_staff")
    search_fields = ("email", "username", "phone_number")
    fieldsets = DjangoUserAdmin.fieldsets + (
        ("Contact", {"fields": ("phone_number", "phone_verified")}),
    )
    add_fieldsets = DjangoUserAdmin.add_fieldsets + (
        (
            "Required",
            {"fields": ("email", "phone_number")},
        ),
    )


@admin.register(Address)
class AddressAdmin(admin.ModelAdmin):
    """Admin page for user delivery addresses, viewable by account."""

    list_display = ("recipient_name", "user", "county", "area_name", "is_default")
    search_fields = ("recipient_name", "county", "area_name")
    list_filter = ("county", "is_default")
