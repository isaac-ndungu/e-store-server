from django.urls import path

from apps.promotions.views import (
    AdminCouponDetailView,
    AdminCouponListCreateView,
    AdminDiscountDetailView,
    AdminDiscountListCreateView,
    CouponValidateView,
    VariantEffectivePriceView,
)

urlpatterns = [
    path(
        "promotions/variants/<int:variant_pk>/price/",
        VariantEffectivePriceView.as_view(),
        name="variant-effective-price",
    ),
    path(
        "promotions/coupons/validate/",
        CouponValidateView.as_view(),
        name="coupon-validate",
    ),
    # Admin CRUD
    path(
        "promotions/admin/discounts/",
        AdminDiscountListCreateView.as_view(),
        name="admin-discount-list-create",
    ),
    path(
        "promotions/admin/discounts/<int:pk>/",
        AdminDiscountDetailView.as_view(),
        name="admin-discount-detail",
    ),
    path(
        "promotions/admin/coupons/",
        AdminCouponListCreateView.as_view(),
        name="admin-coupon-list-create",
    ),
    path(
        "promotions/admin/coupons/<int:pk>/",
        AdminCouponDetailView.as_view(),
        name="admin-coupon-detail",
    ),
]
