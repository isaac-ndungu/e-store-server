"""Business logic for the orders app.

The order service is the only layer that creates or mutates ``Order`` rows.
Staff create confirmed orders through ``create_staff_order`` after a
WhatsApp/email sale: lines are repriced server-side and the order lands in
``confirmed`` with its audit row. There is no stock tracking — staff keep
variant availability current by hand — and delivery cost is the amount
staff quoted the customer, typed in at intake.

Invariants upheld here:

- ``Order.status`` only ever changes through ``transition_order``, which writes
  a matching ``OrderStatusHistory`` row.
- All pricing is recomputed server-side from the current catalogue/effective
  price services — a client-supplied amount is display data, never charged.
  The one exception is ``delivery_fee``, which has no system source and is
  therefore staff-entered, validated non-negative, and stored as given.
"""

import logging
from decimal import ROUND_HALF_UP, Decimal

import bleach
from django.core.exceptions import ValidationError
from django.db import transaction

from apps.core.models import SiteConfig
from apps.orders.models import Order, OrderItem, OrderStatusHistory

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


def _validate_payment_method(payment_method):
    """Reject a payment method that is not enabled for the store.

    Payment is arranged between staff and the customer outside the system,
    so availability is purely the site's enabled-methods list — there is no
    gateway to probe.

    Args:
        payment_method (str): the payment method key.

    Raises:
        ValidationError: if the method is disabled.
    """
    enabled = SiteConfig.load().settings.get("payment_methods", [])
    if payment_method not in enabled:
        raise ValidationError(
            f"Payment method '{payment_method}' is not enabled on this store."
        )


def _money(value):
    """Return ``value`` as an exact ``Decimal``.

    Args:
        value: a money value in its backend-native form.

    Returns:
        Decimal: the exact decimal value.
    """
    return value if isinstance(value, Decimal) else Decimal(value)


def _sanitize_plain(value):
    """Strip markup from a free-text note before it is stored.

    Order notes are written by customers and staff and may later be rendered
    in an admin or notification context. ``bleach`` removes any tag or event
    handler so a stored note can never carry script content.

    Args:
        value (str): the raw note.

    Returns:
        str: the sanitized note.
    """
    if not value:
        return ""
    return bleach.clean(value, tags=set(), strip=True).strip()


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
            note=_sanitize_plain(note),
        )
        order.status = to_status
        order.save(update_fields=["status", "updated_at"])
    return order


def apply_staff_status(order, to_status, *, changed_by, note=""):
    """Apply a staff-initiated order status change.

    Every transition runs through ``transition_order`` so the audit trail is
    written alongside the status column. Intake orders are already confirmed
    at creation; this drives the fulfilment pipeline from there.

    ``cancelled``, ``refunded``, and ``returned`` are rejected here on
    purpose: those states carry a refund record, so they must go through the
    pre-shipment cancellation or the return flow, which capture the staff
    note and the manual-refund details. Allowing them through this generic
    endpoint would change the status without that record.

    Args:
        order (Order): the order to transition.
        to_status (str): the target status.
        changed_by (User): the acting staff user, recorded in the audit trail.
        note (str): an optional free-text note for the audit trail.

    Returns:
        Order: the updated order.

    Raises:
        ValidationError: if the transition is not allowed, or targets a
            terminal state that must go through the cancellation/return flow.
    """
    if to_status in ("cancelled", "refunded", "returned"):
        raise ValidationError(
            f"Status '{to_status}' must go through the cancellation or return "
            "flow, which records the staff note and refund details."
        )
    return transition_order(order, to_status, changed_by=changed_by, note=note)


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


def create_staff_order(
    *,
    staff_user,
    phone,
    lines,
    order_source="whatsapp",
    payment_method="cod",
    payment_reference="",
    email="",
    notes="",
    delivery_area_id=None,
    delivery_fee=Decimal("0.00"),
    shipping_address_id=None,
    inquiry_id=None,
):
    """Create a confirmed order from a staff-assisted sale.

    Used after a WhatsApp/email conversation concludes: staff enter what the
    customer agreed to, and the order is created already ``confirmed`` — there
    is no pending-payment window because payment was arranged with the human
    in the loop. Every line price is recomputed server-side from the current
    catalogue/promotion state; client figures are never charged. The delivery
    fee is the amount staff quoted the customer in the conversation, typed in
    directly — no fee table stands behind it. No stock is touched: staff keep
    variant availability current by hand.

    Args:
        staff_user (User): the staff member creating the order.
        phone (str): the customer contact phone.
        lines (list): ``[{"variant_id": int, "quantity": int}]`` staff-entered
            lines. At least one is required.
        order_source (str): ``whatsapp``, ``email``, or ``admin_manual``.
        payment_method (str): must be enabled in site settings.
        payment_reference (str): staff-entered receipt/bank reference.
        email (str): optional customer email.
        notes (str): optional order memo.
        delivery_area_id (int | None): delivery area for the order, if known.
        delivery_fee (Decimal): staff-quoted delivery cost, non-negative.
        shipping_address_id (int | None): stored address id, if reused.
        inquiry_id (int | None): originating inquiry to mark converted.

    Returns:
        Order: the created confirmed order.

    Raises:
        ValidationError: on bad input, disabled payment method, unknown or
            inactive variant, a variant marked out of stock, a negative
            delivery fee, or an unknown delivery area.
    """
    from apps.catalog.models import ProductVariant
    from apps.promotions.services import get_effective_price

    if order_source not in dict(Order.ORDER_SOURCE_CHOICES):
        raise ValidationError(f"Unknown order source '{order_source}'.")
    _validate_payment_method(payment_method)
    phone = _normalize_phone(phone)
    notes = _sanitize_plain(notes)
    payment_reference = (payment_reference or "").strip()[:100]
    email = (email or "").strip()

    if not lines:
        raise ValidationError("At least one order line is required.")
    cleaned = []
    for entry in lines:
        try:
            variant_id = int(entry.get("variant_id"))
            quantity = int(entry.get("quantity"))
        except AttributeError, TypeError, ValueError:
            raise ValidationError(
                "Each line needs a variant_id and quantity."
            ) from None
        if quantity < 1 or quantity > 999:
            raise ValidationError("Line quantity must be between 1 and 999.")
        cleaned.append({"variant_id": variant_id, "quantity": quantity})

    variants = {
        variant.pk: variant
        for variant in ProductVariant.objects.select_related("product").filter(
            pk__in=[entry["variant_id"] for entry in cleaned],
            is_active=True,
            product__is_active=True,
        )
    }
    priced = []
    for entry in cleaned:
        variant = variants.get(entry["variant_id"])
        if variant is None:
            raise ValidationError(f"Variant {entry['variant_id']} is not available.")
        if variant.stock_status == "out_of_stock":
            raise ValidationError(f"Variant {variant.sku} is marked out of stock.")
        effective = get_effective_price(variant)
        unit_price = _money(effective["price"]).quantize(_PENNY, rounding=ROUND_HALF_UP)
        quantity = entry["quantity"]
        total_price = (unit_price * quantity).quantize(_PENNY, rounding=ROUND_HALF_UP)
        tax_rate = _component_tax_rate(variant)
        tax = (total_price * tax_rate / Decimal("100")).quantize(
            _PENNY, rounding=ROUND_HALF_UP
        )
        priced.append(
            {
                "variant": variant,
                "quantity": quantity,
                "unit_price": unit_price,
                "total_price": total_price,
                "tax_rate": tax_rate,
                "tax": tax,
            }
        )

    delivery_area = None
    if delivery_fee is None:
        delivery_fee = Decimal("0.00")
    try:
        delivery_fee = _money(delivery_fee).quantize(_PENNY, rounding=ROUND_HALF_UP)
    except ArithmeticError, ValueError, TypeError:
        raise ValidationError("Delivery fee must be a valid amount.") from None
    if delivery_fee < 0:
        raise ValidationError("Delivery fee must not be negative.")
    if delivery_area_id is not None:
        from apps.shipping.models import DeliveryArea

        delivery_area = DeliveryArea.objects.filter(pk=delivery_area_id).first()
        if delivery_area is None or not delivery_area.is_active:
            raise ValidationError("Invalid delivery area.")
    shipping_tax_rate = Decimal("0.00")
    shipping_tax_amount = Decimal("0.00")
    if delivery_fee and SiteConfig.load().settings.get("shipping_is_vatable", True):
        shipping_tax_rate = _money(SiteConfig.load().standard_vat_rate)
        shipping_tax_amount = (
            delivery_fee * shipping_tax_rate / Decimal("100")
        ).quantize(_PENNY, rounding=ROUND_HALF_UP)

    subtotal = sum((line["total_price"] for line in priced), Decimal("0.00"))
    tax_total = sum((line["tax"] for line in priced), Decimal("0.00"))
    grand_total = (subtotal + delivery_fee + shipping_tax_amount + tax_total).quantize(
        _PENNY, rounding=ROUND_HALF_UP
    )

    shipping_address = None
    if shipping_address_id is not None:
        from apps.accounts.models import Address

        shipping_address = Address.objects.filter(pk=shipping_address_id).first()
        if shipping_address is None:
            raise ValidationError("Invalid shipping address.")

    with transaction.atomic():
        order = Order.objects.create(
            user=None,
            phone=phone,
            email=email,
            notes=notes,
            status="confirmed",
            payment_method=payment_method,
            order_source=order_source,
            staff_created_by=staff_user,
            payment_reference=payment_reference,
            currency="KES",
            subtotal=subtotal,
            delivery_fee=delivery_fee,
            shipping_tax_rate=_money(shipping_tax_rate),
            shipping_tax_amount=shipping_tax_amount,
            tax_total=tax_total,
            discount_total=Decimal("0.00"),
            grand_total=grand_total,
            shipping_address=shipping_address,
            delivery_area=delivery_area,
        )
        OrderStatusHistory.objects.create(
            order=order,
            from_status="",
            to_status="confirmed",
            changed_by=staff_user,
            note="Created through staff intake.",
        )
        for line in priced:
            OrderItem.objects.create(
                order=order,
                product=line["variant"].product,
                variant_sku=line["variant"].sku,
                product_name=line["variant"].product.name,
                variant_attributes=line["variant"].attributes,
                unit_price=line["unit_price"],
                quantity=line["quantity"],
                total_price=line["total_price"],
                applied_discount=Decimal("0.00"),
                tax_rate=line["tax_rate"],
                tax=line["tax"],
            )
        if inquiry_id is not None:
            from apps.inquiries.models import Inquiry

            inquiry = Inquiry.objects.filter(pk=inquiry_id).first()
            if inquiry is None:
                raise ValidationError("Invalid inquiry.")
            inquiry.converted_order = order
            inquiry.status = "converted"
            inquiry.save(update_fields=["converted_order", "status", "updated_at"])

    from apps.orders.signals import order_confirmed

    order_confirmed.send(sender=order)
    return order
