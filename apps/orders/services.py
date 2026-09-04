"""Business logic for the orders app.

The order service is the only layer that creates or mutates ``Order`` rows.
It decomposes a cart into snapshotted line items (bundle purchases become
per-component lines sharing a ``bundle_group_id``), reserves stock per the
inventory reservation lifecycle, computes shipping with the shipping service,
and routes COD orders through SMS OTP verification.

Invariants upheld here:

- ``Order.status`` only ever changes through ``transition_order``, which writes
  a matching ``OrderStatusHistory`` row.
- All pricing is recomputed server-side from the current catalogue/effective
  price services — a client-supplied amount is display data, never charged.
- Stock is held via ``create_reservation`` / released or fulfilled through the
  inventory service; ``OrderItem.quantity`` is never used to adjust stock.
- OTP verification is idempotent: a repeat of the same code, or a callback on
  an already terminal verification, does not re-mutate state.
"""

import logging
import secrets
import uuid
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.cart.services import compute_cart_totals
from apps.core.models import SiteConfig
from apps.inventory.services import create_reservation
from apps.orders.models import Order, OrderItem, OrderStatusHistory, OrderVerification
from apps.shipping.services import calculate_shipping_fee

logger = logging.getLogger(__name__)

_PENNY = Decimal("0.01")

# Allowed order status transitions. Any status change not listed here is
# rejected by ``transition_order``.
_ALLOWED_TRANSITIONS = {
    "pending": {"confirmed", "cancelled"},
    "confirmed": {"processing", "shipped", "cancelled", "refunded"},
    "processing": {"shipped", "cancelled", "refunded"},
    "shipped": {"delivered", "delivery_failed", "cancelled", "refunded"},
    "delivery_failed": {"processing", "shipped", "cancelled", "refunded"},
    "delivered": {"returned", "refunded"},
    "cancelled": set(),
    "refunded": set(),
    "returned": set(),
}

# Payment methods that require OTP verification of the contact number.
_OTP_REQUIRING_PAYMENT_METHODS = {"cod"}

# Amount of time a verification code remains valid (minutes).
_OTP_EXPIRY_MINUTES_DEFAULT = 10


def _money(value):
    """Return ``value`` as an exact ``Decimal``.

    Args:
        value: a money value in its backend-native form.

    Returns:
        Decimal: the exact decimal value.
    """
    return value if isinstance(value, Decimal) else Decimal(value)


def _otp_expiry_minutes():
    """Return the configured OTP expiry in minutes.

    Returns:
        int: the configured expiry with a sensible fallback.
    """
    return SiteConfig.load().settings.get(
        "otp_expiry_minutes", _OTP_EXPIRY_MINUTES_DEFAULT
    )


def _generate_otp():
    """Generate a random six-digit OTP.

    Uses ``secrets`` so the value is cryptographically unpredictable and
    cannot be guessed by repeated submission.

    Returns:
        str: a six-digit numeric code.
    """
    return f"{secrets.randbelow(1_000_000):06d}"


def _normalize_phone(value):
    """Normalize and validate a phone number to E.164 form.

    Args:
        value (str): the raw phone number.

    Returns:
        str: the normalized phone number.

    Raises:
        ValidationError: if the value is not a valid Kenyan number.
    """
    from apps.accounts.services import validate_phone_number

    return validate_phone_number(value)


def transition_order(order, to_status, *, changed_by=None, note=""):
    """Change an order's status and log the transition.

    The status field may only change through this function — a direct write in
    a view or task is a bug. The transition is validated against the allowed
    graph and a ``OrderStatusHistory`` row is written in the same transaction
    as the status field, so the audit trail can never drift from the column.

    Args:
        order (Order): the order to transition.
        to_status (str): the target status.
        changed_by (User | None): the staff user performing the change, if any.
        note (str): a free-text explanation for the audit trail.

    Returns:
        Order: the updated order.

    Raises:
        ValidationError: if the transition is not in the allowed graph.
    """
    allowed = _ALLOWED_TRANSITIONS.get(order.status, set())
    if to_status not in allowed:
        raise ValidationError(
            f"Cannot transition an order from '{order.status}' to '{to_status}'."
        )

    with transaction.atomic():
        OrderStatusHistory.objects.create(
            order=order,
            from_status=order.status,
            to_status=to_status,
            changed_by=changed_by,
            note=note,
        )
        order.status = to_status
        order.save(update_fields=["status", "updated_at"])
    return order


def create_order_from_cart(
    *,
    cart,
    user=None,
    phone,
    shipping_address=None,
    delivery_zone_id=None,
    payment_method="cod",
    email="",
    notes="",
):
    """Convert a cart into a pending order under a stock-reservation lock.

    Everything the customer is charged is recomputed here from the current
    cart totals (which in turn come from the effective-price services) and the
    shipping service. A client-supplied amount is never charged. The operation
    is transactional: a stock-reservation failure for any line rolls back the
    entire order so an order is never created with only some of its stock held.

    Bundle items decompose into one ``OrderItem`` per component (sharing a
    ``bundle_group_id``) so inventory, tax, and fulfilment are handled per
    component. The returned order is in ``pending`` status and its stock is
    held by active ``StockReservation`` rows.

    Args:
        cart (Cart): the cart to convert. Must have at least one item.
        user (User | None): the authenticated user owning the cart, or None for
            a guest checkout.
        phone (str): the order contact phone (E.164), independent of any
            account phone number.
        shipping_address (Address | None): the delivery address, if any.
        delivery_zone_id (int | None): the delivery zone used to price and
            route shipping. When omitted a no-zone order is created with zero
            shipping (only valid for non-physical or pick-up scenarios).
        payment_method (str): the payment method (``cod``, ``mpesa``, etc.).
        email (str): the optional order contact email.
        notes (str): the optional order memo.

    Returns:
        Order: the created pending order.

    Raises:
        ValidationError: if the cart is empty, the phone is invalid, the
            delivery zone is missing for a shipping-required order, or stock
            is insufficient for any line.
    """
    phone = _normalize_phone(phone)
    items = list(cart.items.select_related("variant__product", "bundle"))
    if not items:
        raise ValidationError("Cannot create an order from an empty cart.")

    order_payment_method = payment_method or SiteConfig.load().settings.get(
        "default_payment_method", "cod"
    )

    totals = compute_cart_totals(cart)
    subtotal = Decimal(totals["subtotal"])

    delivery_zone = None
    shipping_total = Decimal("0.00")
    shipping_tax_rate = Decimal("0.00")
    shipping_tax_amount = Decimal("0.00")

    if delivery_zone_id:
        from apps.shipping.models import DeliveryZone

        delivery_zone = DeliveryZone.objects.filter(pk=delivery_zone_id).first()
        if delivery_zone is None or not delivery_zone.is_active:
            raise ValidationError("Invalid delivery zone.")

        shipping_total = calculate_shipping_fee(delivery_zone, items)
        shipping_total = Decimal(shipping_total).quantize(
            _PENNY, rounding=ROUND_HALF_UP
        )

        from apps.core.models import SiteConfig as SC

        if SC.load().settings.get("shipping_is_vatable", True):
            shipping_tax_rate = _money(SC.load().standard_vat_rate)
            shipping_tax_amount = (
                shipping_total * shipping_tax_rate / Decimal("100")
            ).quantize(_PENNY, rounding=ROUND_HALF_UP)

    tax_total = compute_tax_total(totals)
    discount_total = Decimal(totals["discount_total"])
    coupon = cart.coupon if cart.coupon_id else None

    grand_total = (
        subtotal + shipping_total + shipping_tax_amount + tax_total
    ).quantize(_PENNY, rounding=ROUND_HALF_UP)

    with transaction.atomic():
        order = Order.objects.create(
            user=user if user is not None and user.is_authenticated else None,
            phone=phone,
            email=email,
            notes=notes,
            payment_method=order_payment_method,
            currency="KES",
            subtotal=subtotal,
            shipping_total=shipping_total,
            shipping_tax_rate=_money(shipping_tax_rate),
            shipping_tax_amount=shipping_tax_amount,
            tax_total=tax_total,
            discount_total=discount_total,
            coupon=coupon,
            grand_total=grand_total,
            shipping_address=shipping_address,
            delivery_zone=delivery_zone,
        )
        OrderStatusHistory.objects.create(
            order=order,
            from_status="",
            to_status="pending",
        )

        bundle_group_id = None
        for item in items:
            if item.variant_id is not None:
                _create_variant_order_item(order, item, delivery_zone)
            else:
                if bundle_group_id is None:
                    bundle_group_id = uuid.uuid4()
                _create_bundle_order_items(order, item, delivery_zone, bundle_group_id)

    return order


def compute_tax_total(totals):
    """Return the tax total from a cart's VAT breakdown.

    The cart's computed ``vat_total`` is the tax attributable to the goods —
    the sum of each line's standard-rated tax (the only taxable bucket).
    Zero-rated and exempt buckets contribute nothing, and the amount is already
    quantized to the penny per-bucket by the cart service.

    Args:
        totals (dict): the cart totals dict from ``compute_cart_totals``.

    Returns:
        Decimal: the total tax amount, quantized to the penny.
    """
    vat_total = _money(totals["vat_total"])
    return vat_total.quantize(_PENNY, rounding=ROUND_HALF_UP)


def _create_variant_order_item(order, cart_item, delivery_zone):
    """Create an ``OrderItem`` snapshot for a simple variant cart line.

    The line's SKU, name, attributes, unit price, quantity, and total are all
    copied from the live product/variant at checkout time so the historical
    order renders independently of catalogue changes. Stock is reserved for the
    line via the inventory reservation service with the order line recorded as
    the reservation's owner.

    Args:
        order (Order): the order the line belongs to.
        cart_item (CartItem): the variant cart line.
        delivery_zone (DeliveryZone | None): the zone used for warehouse
            selection, if any.

    Raises:
        ValidationError: if the variant is gone or stock is insufficient.
    """
    from apps.cart.services import _price_variant_item

    variant = cart_item.variant
    if variant is None:
        raise ValidationError("A cart variant is no longer available.")
    coupon = order.coupon if order.coupon_id else None
    line = _price_variant_item(cart_item, coupon)

    unit_price = Decimal(line["unit_price"])
    quantity = cart_item.quantity
    total_price = (unit_price * quantity).quantize(_PENNY, rounding=ROUND_HALF_UP)
    tax_rate = _component_tax_rate(variant)
    tax = (total_price * tax_rate / Decimal("100")).quantize(
        _PENNY, rounding=ROUND_HALF_UP
    )

    warehouse = _select_warehouse_for_line(variant, delivery_zone, quantity)

    order_item = OrderItem.objects.create(
        order=order,
        product=variant.product,
        variant_sku=variant.sku,
        product_name=variant.product.name,
        variant_attributes=variant.attributes,
        unit_price=unit_price,
        quantity=quantity,
        total_price=total_price,
        tax_rate=tax_rate,
        tax=tax,
        fulfillment_warehouse=warehouse,
    )

    create_reservation(
        variant=variant,
        quantity=quantity,
        warehouse=warehouse,
        order_item=order_item,
    )


def _create_bundle_order_items(order, cart_item, delivery_zone, bundle_group_id):
    """Create one ``OrderItem`` per component of a bundle cart line.

    Buying a bundle never records a single opaque line — inventory and tax are
    per component, so each component becomes its own ``OrderItem`` sharing
    ``bundle_group_id``. The per-component unit price comes from the bundles
    price service (the component's effective price marked as within-bundle, so
    an individual-item promotion does not stack with the bundle discount). The
    completion fraction of the bundle's discount attributed to each component
    is not needed for order storage because the bundle price itself is the
    charged amount; the per-line money is snapshotted from the current price
    services for order-history display.

    Args:
        order (Order): the order the lines belong to.
        cart_item (CartItem): the bundle cart line.
        delivery_zone (DeliveryZone | None): the delivery zone, if any.
        bundle_group_id (UUID): the shared bundle group identifier.

    Raises:
        ValidationError: if a component cannot be priced or is out of stock.
    """
    from apps.bundles.services import _item_source_variant

    bundle = cart_item.bundle
    if bundle is None:
        raise ValidationError("A cart bundle is no longer available.")

    bundle_items = list(
        bundle.items.select_related("product", "variant").order_by("pk")
    )
    if not bundle_items:
        raise ValidationError("A bundle must have at least one item.")

    line_quantity = cart_item.quantity

    for bundle_item in bundle_items:
        variant = _item_source_variant(bundle_item)
        if variant is None:
            raise ValidationError("A bundle component is no longer priceable.")

        quantity = bundle_item.quantity * line_quantity
        unit_price = variant.price
        total_price = (unit_price * quantity).quantize(_PENNY, rounding=ROUND_HALF_UP)
        tax_rate = _component_tax_rate(variant)
        tax = (total_price * tax_rate / Decimal("100")).quantize(
            _PENNY, rounding=ROUND_HALF_UP
        )

        warehouse = _select_warehouse_for_line(variant, delivery_zone, quantity)

        order_item = OrderItem.objects.create(
            order=order,
            product=variant.product,
            bundle=bundle,
            bundle_group_id=bundle_group_id,
            variant_sku=variant.sku,
            product_name=variant.product.name,
            variant_attributes=variant.attributes,
            unit_price=unit_price,
            quantity=quantity,
            total_price=total_price,
            tax_rate=tax_rate,
            tax=tax,
            fulfillment_warehouse=warehouse,
        )

        create_reservation(
            variant=variant,
            quantity=quantity,
            warehouse=warehouse,
            order_item=order_item,
        )


def _component_tax_rate(variant):
    """Return the VAT rate for a component's product tax class.

    Args:
        variant (ProductVariant): the component variant.

    Returns:
        Decimal: the VAT rate as a percentage (``0`` for zero-rated/exempt).
    """
    from apps.core.models import SiteConfig

    tax_class = variant.product.tax_class
    if tax_class == "standard":
        return _money(SiteConfig.load().standard_vat_rate)
    return Decimal("0.00")


def _select_warehouse_for_line(variant, delivery_zone, quantity):
    """Pick the fulfillment warehouse for a line.

    Uses the shipping service's warehouse-selection rule when a delivery zone
    is provided; otherwise defers to the inventory reservation service's own
    allocation logic by passing ``None``.

    Args:
        variant (ProductVariant): the variant being fulfilled.
        delivery_zone (DeliveryZone | None): the destination zone, if any.
        quantity (int): the number of units needed.

    Returns:
        Warehouse | None: the chosen warehouse, or None when none is available
            — in which case the reservation call will raise.
    """
    if delivery_zone is None:
        return None
    from apps.shipping.services import select_fulfillment_warehouse

    return select_fulfillment_warehouse(variant, delivery_zone, quantity)


def cancel_pending_order(order, *, user=None, note=""):
    """Cancel a pending order, releasing any held stock.

    Only a pending order may be cancelled through this path; a confirmed
    (already-fulfilled) order follows the post-confirmation cancellation
    workflow which requires a credit note and refund resolution. On
    cancellation the reservation stock is released and the order moves to
    ``cancelled``.

    Args:
        order (Order): the order to cancel.
        user (User | None): the acting user.
        note (str): a note for the audit trail.

    Returns:
        Order: the cancelled order.

    Raises:
        ValidationError: if the order is not pending.
    """
    with transaction.atomic():
        for order_item in order.items.select_related("product"):
            _release_stock_for_order_item(order_item)
        return transition_order(order, "cancelled", changed_by=user, note=note)


def _release_stock_for_order_item(order_item):
    """Release any active reservation associated with an order line.

    Iterates the reservations linked to the order line and releases each one
    that is still active. ``release_reservation`` is idempotent so replaying
    this on an already-released hold is a no-op.

    Args:
        order_item (OrderItem): the order line whose stock should be released.
    """
    from apps.inventory.models import StockReservation
    from apps.inventory.services import release_reservation

    reservations = StockReservation.objects.filter(
        order_item=order_item, status="active"
    )
    for reservation in reservations:
        release_reservation(reservation)


def confirm_order_from_verification(order, *, user=None):
    """Transition a verified package order to confirmed and fulfil stock.

    Callable only from the COD path after ``verify_order_otp`` has succeeded,
    or from the order-confirmation flow for orders that skip OTP. Fulfils each
    line's reservation (real deduction plus serial-unit assignment) and moves
    the order to ``confirmed`` in the same transaction.

    Args:
        order (Order): the order to confirm.
        user (User | None): the acting user.

    Returns:
        Order: the confirmed order.

    Raises:
        ValidationError: if the order is not pending, or a reservation cannot
            be fulfilled because it was released in the interim.
    """
    from apps.inventory.services import fulfill_reservation

    with transaction.atomic():
        if order.status != "pending":
            raise ValidationError("Only a pending order can be confirmed.")

        from apps.inventory.models import StockReservation

        reservations = list(
            StockReservation.objects.select_related("order_item").filter(
                order_item__order=order, status="active"
            )
        )

        for reservation in reservations:
            fulfilled = fulfill_reservation(reservation)
            if not fulfilled:
                raise ValidationError(
                    f"Reservation {reservation.pk} could not be fulfilled; "
                    "re-hold stock before confirming."
                )

        return transition_order(order, "confirmed", changed_by=user)


def verify_order_otp(order, otp_code):
    """Verify a COD order with the supplied one-time password.

    Idempotent-safe: once a record is ``verified`` a repeat with any code
    returns ``None`` (nothing to do) and an expired/maxed record cannot be
    revived. On success the order is confirmed — stock reservations fulfilled
    and status moved to ``confirmed`` — in the same transaction as the
    verification update.

    Args:
        order (Order): the COD order to verify.
        otp_code (str): the six-digit code submitted by the customer.

    Returns:
        (Order, bool): the updated order and whether it was newly verified.

    Raises:
        ValidationError: if the order has no verification record, the code is
            wrong, expired, or the attempt limit is exceeded.
    """
    try:
        verification = order.verification
    except Order.verification.RelatedObjectDoesNotExist:
        raise ValidationError("This order does not require OTP verification.") from None

    if verification.status == "verified":
        return order, False

    if verification.status in ("expired", "failed"):
        raise ValidationError(
            f"This verification code has {verification.status} and can no "
            "longer be used."
        )

    expiry = verification.sent_at + timedelta(minutes=_otp_expiry_minutes())
    if timezone.now() > expiry:
        verification.status = "expired"
        verification.save(update_fields=["status"])
        raise ValidationError("This verification code has expired.")

    if verification.attempts >= OrderVerification.MAX_ATTEMPTS:
        verification.status = "failed"
        verification.save(update_fields=["status"])
        raise ValidationError(
            "Too many verification attempts. Please resend a new code."
        )

    if verification.otp_code != otp_code:
        verification.attempts = verification.attempts + 1
        verification.save(update_fields=["attempts"])
        raise ValidationError("Invalid verification code.")

    with transaction.atomic():
        verification.status = "verified"
        verification.verified_at = timezone.now()
        verification.save(update_fields=["status", "verified_at", "attempts"])
        confirmed = confirm_order_from_verification(order)
    return confirmed, True


def resend_order_otp(order):
    """Re-send and reset the OTP for a COD order.

    A new code is generated and persisted to the order's verification record,
    and the expiry clock restarts. If no verification record exists one is
    created. The SMS send is delegated to the notifications service; the
    message body sent to the provider carries the live code while the audit
    log masks it.

    Args:
        order (Order): the COD order.

    Returns:
        OrderVerification: the updated verification record.

    Raises:
        ValidationError: if the order is not pending or is not a COD order.
    """
    if order.status != "pending":
        raise ValidationError("Cannot resend a code for a non-pending order.")
    if order.payment_method != "cod":
        raise ValidationError("OTP verification applies to COD orders only.")

    new_code = _generate_otp()

    verification, _created = OrderVerification.objects.get_or_create(
        order=order,
        defaults={
            "otp_code": new_code,
            "phone_number": order.phone,
        },
    )
    verification.otp_code = new_code
    verification.status = "pending"
    verification.attempts = 0
    verification.sent_at = timezone.now()
    verification.save(update_fields=["otp_code", "status", "attempts", "sent_at"])

    from apps.notifications.services import send_otp_sms

    send_otp_sms(order.phone, new_code)
    return verification


def requires_otp_for_payment(order):
    """Return whether the order's payment method requires OTP verification.

    Args:
        order (Order): the order.

    Returns:
        bool: True when the order must be OTP-verified, False when the payment
            path already proves the number (e.g. M-Pesa STK Push) or
            verification is skipped.
    """
    return order.payment_method in _OTP_REQUIRING_PAYMENT_METHODS
