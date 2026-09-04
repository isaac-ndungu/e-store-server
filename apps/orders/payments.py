"""Payment gateway adapter used by the order service.

The order service never talks to a payment provider directly — it calls into
this adapter so the eventual `payments` app (M-Pesa Daraja STK Push, an
optional card gateway) can be dropped in without changing the checkout math or
the status machine.

Today no provider is configured, so every gateway reports ``is_available()``
as False. That is deliberate: if an ``mpesa`` order were creatable but had no
gateway behind it, it would sit in ``pending`` holding reserved stock with no
callback to confirm or refund it. By rejecting the method before order
creation, the storefront gets a clear error instead of a stuck order. When a
gateway is implemented, registering it here (and in the site's enabled payment
methods) flips the method on with no further changes to the order path.

The completion handlers (`confirm_order_from_payment` /
`cancel_order_from_payment`) are the contract a future callback adapter must
drive. ``confirm_order_from_payment`` routes through the same
reservation-fulfilment + coupon-redemption path the OTP flow uses, so a paid
order can never skip the stock bookkeeping.
"""

from apps.orders.services import (
    cancel_pending_order,
    confirm_order_from_verification,
)


class PaymentUnavailable(Exception):
    """Raised when a payment method has no configured, operational gateway."""


class PaymentGateway:
    """Base contract every payment gateway must satisfy.

    A gateway determines whether the payment method may be offered, whether
    placing an order with it needs an OTP verification, and initiates the
    charge.
    """

    def is_available(self):
        """Return whether this gateway is configured and operational.

        Returns:
            bool: True when orders can be placed and completed through it.
        """
        raise NotImplementedError

    def requires_otp(self):
        """Return whether this gateway's orders must be OTP-verified.

        Returns:
            bool: True when an OTP must be sent on order placement.
        """
        raise NotImplementedError

    def initiate(self, order):
        """Kick off the charge for a pending order.

        Args:
            order (Order): the pending order to charge.

        Raises:
            PaymentUnavailable: if the gateway is not operational.
        """
        raise NotImplementedError


class UnavailablePaymentGateway(PaymentGateway):
    """Placeholder gateway used until a real provider is wired in.

    Correctness hinges on ``is_available()`` returning False here: an order is
    not created until a gateway can actually complete it, so no stock is ever
    held against an order that cannot be paid or refunded.
    """

    def is_available(self):
        """Report that no provider is configured yet.

        Returns:
            bool: always False.
        """
        return False

    def requires_otp(self):
        """Conservatively require OTP until a gateway proves the number.

        Returns:
            bool: False — unused while the gateway is unavailable.
        """
        return False

    def initiate(self, order):
        """Decline to charge because no gateway is configured.

        Args:
            order (Order): the order that would be charged.

        Raises:
            PaymentUnavailable: always.
        """
        raise PaymentUnavailable(
            f"No payment gateway is configured for method '{order.payment_method}'."
        )


# Registry of payment methods to their gateway. Only methods listed here with
# an operational gateway are available; a method absent or reporting
# unavailable is rejected at order placement.
PAYMENT_GATEWAYS = {
    "mpesa": UnavailablePaymentGateway(),
    "card": UnavailablePaymentGateway(),
}


def gateway_for(payment_method):
    """Return the gateway bound to a payment method.

    Args:
        payment_method (str): the payment method key.

    Returns:
        PaymentGateway: the matching gateway, or an unavailable placeholder
            when the method has no registered gateway.
    """
    return PAYMENT_GATEWAYS.get(payment_method, UnavailablePaymentGateway())


def is_payment_method_available(payment_method):
    """Return whether orders may be placed with a payment method.

    Args:
        payment_method (str): the payment method key.

    Returns:
        bool: True when the method is COD (completed via OTP) or backed by an
            operational gateway.
    """
    if payment_method == "cod":
        return True
    return gateway_for(payment_method).is_available()


def requires_otp_for_payment_method(payment_method):
    """Return whether a payment method needs OTP verification on placement.

    Args:
        payment_method (str): the payment method key.

    Returns:
        bool: True for COD (verifying the contact number), otherwise True
            until a gateway is available.
    """
    if payment_method == "cod":
        return True
    return gateway_for(payment_method).requires_otp()


def initiate_payment(order):
    """Initiate the charge for a pending order through its gateway.

    Called from the order-placement flow. For COD this is a no-op (the OTP
    path drives confirmation); a provider-backed method delegates to its
    gateway, which raises ``PaymentUnavailable`` if not operational.

    Args:
        order (Order): the pending order to charge.

    Raises:
        PaymentUnavailable: if the order's gateway is not operational.
    """
    if order.payment_method == "cod":
        return None
    return gateway_for(order.payment_method).initiate(order)


def confirm_order_from_payment(order, *, user=None):
    """Confirm an order after a payment succeeds.

    The completion hook a payment callback drives on success. Routes through
    the same fulfilment path as OTP verification so stock is really deducted
    and coupon redemptions recorded, not just the status relabelled.

    Args:
        order (Order): the order whose payment succeeded.
        user (User | None): the acting user, if any.

    Returns:
        Order: the confirmed order.
    """
    return confirm_order_from_verification(order, user=user)


def cancel_order_from_payment(order, *, user=None, note=""):
    """Cancel an order when its payment fails or is abandoned.

    The completion hook a payment callback drives on failure. Releases the
    held reservation stock so it is not left reserved indefinitely.

    Args:
        order (Order): the order whose payment did not complete.
        user (User | None): the acting user, if any.
        note (str): a note for the audit trail.

    Returns:
        Order: the cancelled order.
    """
    return cancel_pending_order(order, user=user, note=note)
