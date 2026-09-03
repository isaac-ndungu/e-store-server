"""API views for the promotions app.

Split into public views (``AllowAny``) — the effective-price lookup and coupon
validation — and admin CRUD views (``IsAdminUser``) for discounts and coupons.
Every view delegates to a service or selector and stays thin: parse input,
call service, return response.
"""

from rest_framework import generics, permissions
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.promotions.selectors import list_coupons, list_discounts
from apps.promotions.serializers import (
    CouponSerializer,
    CouponValidationResultSerializer,
    CouponValidationSerializer,
    DiscountSerializer,
    EffectivePriceResultSerializer,
)
from apps.promotions.services import (
    get_effective_price,
    validate_coupon_code,
)

# Public views


class VariantEffectivePriceView(APIView):
    """Return the effective price of a product variant (public).

    The storefront calls this to display the discounted price it should show
    and charge. The amount is recomputed server-side from the current variant
    price and active promotions — never from a client-supplied value.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public_catalog"

    def get(self, request, variant_pk):
        """Return the effective price breakdown for a variant.

        Args:
            request: the GET request.
            variant_pk (int): the product variant id.

        Returns:
            Response: the effective-price data, or 404 for an unknown variant.
        """
        from apps.catalog.models import ProductVariant

        variant = ProductVariant.objects.filter(pk=variant_pk).first()
        if variant is None:
            raise NotFound("No such variant.")
        price_data = get_effective_price(variant)
        serializer = EffectivePriceResultSerializer(price_data)
        return Response(serializer.data)


class CouponValidateView(APIView):
    """Validate a coupon code and return its discount metadata (public).

    Checks intrinsic validity (active, in window, usage limits) and, when a
    ``subtotal`` is supplied, the coupon's minimum order value. Product and
    category restrictions are evaluated later at checkout against the cart.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public"

    def post(self, request):
        """Validate the submitted coupon code.

        Args:
            request: the POST request.

        Returns:
            Response: the validation result.
        """
        input_serializer = CouponValidationSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        user = request.user if request.user.is_authenticated else None
        result = validate_coupon_code(
            input_serializer.validated_data["code"],
            user=user,
            subtotal=input_serializer.validated_data.get("subtotal"),
        )
        serializer = CouponValidationResultSerializer(result)
        return Response(serializer.data)


# Admin views


class AdminDiscountListCreateView(generics.ListCreateAPIView):
    """List all discounts or create a new one (admin only)."""

    permission_classes = [permissions.IsAdminUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"
    serializer_class = DiscountSerializer

    def get_queryset(self):
        """Return discounts with their scope relations pre-fetched."""
        return list_discounts().prefetch_related(
            "variants", "products", "categories", "brands"
        )


class AdminDiscountDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update, or delete a discount (admin only)."""

    permission_classes = [permissions.IsAdminUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"
    serializer_class = DiscountSerializer

    def get_queryset(self):
        """Return discounts with their scope relations pre-fetched."""
        return list_discounts().prefetch_related(
            "variants", "products", "categories", "brands"
        )


class AdminCouponListCreateView(generics.ListCreateAPIView):
    """List all coupons or create a new one (admin only)."""

    permission_classes = [permissions.IsAdminUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"
    serializer_class = CouponSerializer

    def get_queryset(self):
        """Return coupons with their restrictions pre-fetched."""
        return list_coupons().prefetch_related("applies_to_products")


class AdminCouponDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Retrieve, update, or delete a coupon (admin only)."""

    permission_classes = [permissions.IsAdminUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin"
    serializer_class = CouponSerializer

    def get_queryset(self):
        """Return coupons with their restrictions pre-fetched."""
        return list_coupons().prefetch_related("applies_to_products")
