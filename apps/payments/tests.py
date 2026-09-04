"""Tests for the payments app.

Covers M-Pesa Daraja integration: STK Push callback idempotency (success,
duplicate no-ops, failure/timeout/cancel mapping) and callback-amount
verification, B2C callback idempotency, STK Push initiation with mocked
Daraja and a per-phone rate limit, B2C refund destination binding and
amount capping, transaction-status endpoint ownership and anonymous
rejection, phone normalization, Daraja gateway availability, callback
IP validation, and the shared-secret callback token.
"""

from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.cart.services import add_item, get_or_create_cart
from apps.catalog.models import Brand, Category, Product, ProductVariant
from apps.inventory.models import Inventory, Warehouse
from apps.orders.services import create_order_from_cart
from apps.payments.daraja import DarajaError, _normalize_mpesa_phone
from apps.payments.models import MpesaB2CPayout, MpesaTransaction, Payment
from apps.payments.services import (
    StkPushRateLimited,
    handle_b2c_callback,
    handle_stk_callback,
    initiate_b2c_refund,
    initiate_stk_push,
)
from apps.shipping.models import DeliveryZone

_SEQ = [0]


def _make_user(email="payer@example.com", username="payer", **kwargs):
    """Create a test user."""
    return User.objects.create_user(
        email=email,
        username=username,
        password=kwargs.pop("password", "StrongPass123!"),
        phone_number=kwargs.pop("phone_number", "+254712345678"),
        **kwargs,
    )


def _make_product(name="Blender", slug="blender", sku="BND", price="8000.00"):
    """Create a product with a default active variant."""
    _SEQ[0] += 1
    n = _SEQ[0]
    category, _ = Category.objects.get_or_create(name="Kitchen", slug="kitchen")
    brand, _ = Brand.objects.get_or_create(name="Ramtons", slug="ramtons")
    product = Product.objects.create(
        name=f"{name} {n}",
        slug=f"{slug}-{n}",
        sku=f"{sku}-{n}",
        description="A test product.",
        category=category,
        brand=brand,
        is_active=True,
    )
    variant = ProductVariant.objects.create(
        product=product,
        sku=f"{sku}-{n}-V",
        attributes={"color": "White"},
        price=price,
        package_weight=Decimal("3.00"),
        is_active=True,
    )
    return product, variant


def _stock_variant(variant, quantity=50, warehouse_name="Main"):
    """Ensure a variant has stock in a warehouse."""
    warehouse, _ = Warehouse.objects.get_or_create(name=warehouse_name)
    Inventory.objects.update_or_create(
        variant=variant,
        warehouse=warehouse,
        defaults={"quantity": quantity, "reserved": 0},
    )
    return warehouse


def _active_zone():
    """Return a reusable active delivery zone."""
    zone, _ = DeliveryZone.objects.get_or_create(
        county="Nairobi",
        area_name="CBD",
        defaults={"base_fee": "150.00", "per_kg_rate": "40.00"},
    )
    return zone


def _mock_sms():
    """Return a context manager stubbing the SMS provider to report success."""
    return mock.patch(
        "apps.notifications.services._send_via_provider",
        return_value={
            "success": True,
            "message_id": "test-sms-id",
            "response": {},
            "segments": 1,
            "error": "",
        },
    )


def _mock_stk_push():
    """Return a context manager stubbing Daraja STK Push to succeed."""
    return mock.patch(
        "apps.payments.daraja._http_post",
        return_value={
            "MerchantRequestID": "mr-001",
            "CheckoutRequestID": "ws_CO_123456",
            "ResponseCode": "0",
            "ResponseDescription": "Success. Request accepted for processing",
            "CustomerMessage": "Success. Request accepted for processing",
        },
    )


def _mock_b2c():
    """Return a context manager stubbing the Daraja B2C endpoint to succeed."""
    return mock.patch(
        "apps.payments.daraja._http_post",
        return_value={
            "ConversationID": "conv-refund-001",
            "OriginatorConversationID": "org-refund-001",
            "ResponseCode": "0",
            "ResponseDescription": "Accept the service request successfully.",
        },
    )


def _mock_oauth_token():
    """Return a context manager stubbing the Daraja OAuth endpoint."""
    return mock.patch(
        "apps.payments.daraja.get_access_token",
        return_value="test-access-token",
    )


def _build_stk_callback(
    checkout_request_id, result_code="0", receipt="QJK3AB1", amount=8000
):
    """Build a realistic STK Push callback body.

    Args:
        checkout_request_id (str): the Daraja checkout request ID.
        result_code (str): the Daraja result code.
        receipt (str): the M-Pesa receipt number.
        amount (int | Decimal): the Amount echoed in the callback metadata.
    """
    body = {
        "Body": {
            "StkCallback": {
                "MerchantRequestID": "mr-001",
                "CheckoutRequestID": checkout_request_id,
                "ResultCode": result_code,
                "ResultDesc": "The service request is processed successfully.",
            }
        }
    }
    if result_code == "0":
        body["Body"]["StkCallback"]["CallbackMetadata"] = {
            "Item": [
                {"Name": "Amount", "Value": amount},
                {"Name": "MpesaReceiptNumber", "Value": receipt},
                {"Name": "Balance"},
                {"Name": "TransactionDate", "Value": 20260101120000},
                {"Name": "PhoneNumber", "Value": 254712345678},
            ]
        }
    return body


def _build_b2c_callback(conversation_id, result_code="0"):
    """Build a realistic B2C callback body."""
    return {
        "Result": {
            "ConversationID": conversation_id,
            "OriginatorConversationID": "org-001",
            "ResultCode": result_code,
            "ResultDesc": (
                "The service request has been accepted successfully."
                if result_code == "0"
                else "Insufficient balance in the M-Pesa account."
            ),
            "TransactionID": "BLM3A7B1C2",
        }
    }


class MpesaSTKCallbackTests(APITestCase):
    """Test the M-Pesa STK Push callback processing."""

    def setUp(self):
        cache.clear()
        _, self.variant = _make_product()
        _stock_variant(self.variant, quantity=20)
        self.zone = _active_zone()

    def _create_order(self):
        """Create a pending M-Pesa order with an MpesaTransaction."""
        user = _make_user()
        cart = get_or_create_cart(user=user)
        add_item(cart, variant_id=self.variant.pk, quantity=1)
        with _mock_sms():
            order = create_order_from_cart(
                cart=cart,
                user=user,
                phone="+254712345678",
                payment_method="cod",
                delivery_zone_id=self.zone.pk,
            )
        order.payment_method = "mpesa"
        order.save(update_fields=["payment_method"])
        return order

    def test_success_callback_confirms_order(self):
        """A successful STK callback confirms the order and creates Payment."""
        order = self._create_order()
        MpesaTransaction.objects.create(
            order=order,
            phone_number=order.phone,
            amount=order.grand_total,
            checkout_request_id="ws_CO_123456",
            status="pending",
        )
        body = _build_stk_callback("ws_CO_123456", amount=str(order.grand_total))

        result = handle_stk_callback(body)

        self.assertIsNotNone(result)
        result.refresh_from_db()
        self.assertEqual(result.status, "success")
        self.assertEqual(result.mpesa_receipt_number, "QJK3AB1")
        self.assertIsNotNone(result.confirmed_at)

        order.refresh_from_db()
        self.assertEqual(order.status, "confirmed")
        self.assertTrue(Payment.objects.filter(order=order).exists())

    def test_success_callback_idempotent(self):
        """A duplicate success callback does not re-mutate state."""
        order = self._create_order()
        MpesaTransaction.objects.create(
            order=order,
            phone_number=order.phone,
            amount=order.grand_total,
            checkout_request_id="ws_CO_IDEMP",
            status="success",
            mpesa_receipt_number="ORIG123",
        )
        body = _build_stk_callback("ws_CO_IDEMP", receipt="ORIG123")

        result = handle_stk_callback(body)

        self.assertIsNotNone(result)
        self.assertEqual(result.mpesa_receipt_number, "ORIG123")

    def test_user_cancel_callback(self):
        """Daraja code 1032 (user cancel) cancels the order."""
        order = self._create_order()
        MpesaTransaction.objects.create(
            order=order,
            phone_number=order.phone,
            amount=order.grand_total,
            checkout_request_id="ws_CO_CANCEL",
            status="pending",
        )
        body = _build_stk_callback("ws_CO_CANCEL", result_code="1032")

        result = handle_stk_callback(body)

        self.assertIsNotNone(result)
        result.refresh_from_db()
        self.assertEqual(result.status, "cancelled")
        order.refresh_from_db()
        self.assertEqual(order.status, "cancelled")

    def test_timeout_callback(self):
        """Daraja code 1037 (timeout) times out the transaction and cancels."""
        order = self._create_order()
        MpesaTransaction.objects.create(
            order=order,
            phone_number=order.phone,
            amount=order.grand_total,
            checkout_request_id="ws_CO_TIMEOUT",
            status="pending",
        )
        body = _build_stk_callback("ws_CO_TIMEOUT", result_code="1037")

        result = handle_stk_callback(body)

        self.assertIsNotNone(result)
        result.refresh_from_db()
        self.assertEqual(result.status, "timeout")
        order.refresh_from_db()
        self.assertEqual(order.status, "cancelled")

    def test_generic_failure_callback(self):
        """A non-zero, non-cancel/timeout code marks the transaction as failed."""
        order = self._create_order()
        MpesaTransaction.objects.create(
            order=order,
            phone_number=order.phone,
            amount=order.grand_total,
            checkout_request_id="ws_CO_FAIL",
            status="pending",
        )
        body = _build_stk_callback("ws_CO_FAIL", result_code="2001")

        result = handle_stk_callback(body)

        self.assertIsNotNone(result)
        result.refresh_from_db()
        self.assertEqual(result.status, "failed")
        order.refresh_from_db()
        self.assertEqual(order.status, "cancelled")

    def test_callback_for_unknown_checkout_id_returns_none(self):
        """A callback for a nonexistent checkout_request_id returns None."""
        body = _build_stk_callback("ws_CO_NONEXISTENT")
        result = handle_stk_callback(body)
        self.assertIsNone(result)

    def test_callback_missing_checkout_id_returns_none(self):
        """A callback body with no CheckoutRequestID returns None."""
        result = handle_stk_callback({"Body": {"StkCallback": {}}})
        self.assertIsNone(result)

    def test_already_terminal_transaction_is_not_mutated(self):
        """A callback on a failed transaction is a no-op."""
        order = self._create_order()
        MpesaTransaction.objects.create(
            order=order,
            phone_number=order.phone,
            amount=order.grand_total,
            checkout_request_id="ws_CO_ALREADY_FAILED",
            status="failed",
        )
        body = _build_stk_callback("ws_CO_ALREADY_FAILED")

        result = handle_stk_callback(body)

        self.assertIsNotNone(result)
        self.assertEqual(result.status, "failed")


class MpesaB2CCallbackTests(APITestCase):
    """Test the M-Pesa B2C callback processing."""

    def setUp(self):
        cache.clear()

    def _make_payout(self, conversation_id, status="pending"):
        """Create a pending B2C payout record."""
        user = _make_user()
        _, variant = _make_product()
        _stock_variant(variant)
        zone = _active_zone()
        cart = get_or_create_cart(user=user)
        add_item(cart, variant_id=variant.pk, quantity=1)
        with _mock_sms():
            order = create_order_from_cart(
                cart=cart,
                user=user,
                phone="+254712345678",
                payment_method="cod",
                delivery_zone_id=zone.pk,
            )
        return MpesaB2CPayout.objects.create(
            order=order,
            reason="return_refund",
            phone_number=order.phone,
            amount=Decimal("5000.00"),
            conversation_id=conversation_id,
            status=status,
        )

    def test_success_callback(self):
        """A successful B2C callback marks the payout as success."""
        self._make_payout("conv-001")
        body = _build_b2c_callback("conv-001")

        result = handle_b2c_callback(body)

        self.assertIsNotNone(result)
        result.refresh_from_db()
        self.assertEqual(result.status, "success")
        self.assertEqual(result.mpesa_receipt_number, "BLM3A7B1C2")

    def test_success_callback_idempotent(self):
        """A duplicate success callback is a no-op."""
        self._make_payout("conv-002", status="success")
        body = _build_b2c_callback("conv-002")

        result = handle_b2c_callback(body)

        self.assertIsNotNone(result)
        self.assertEqual(result.status, "success")

    def test_failure_callback(self):
        """A failed B2C callback marks the payout as failed."""
        self._make_payout("conv-003")
        body = _build_b2c_callback("conv-003", result_code="1")

        result = handle_b2c_callback(body)

        self.assertIsNotNone(result)
        result.refresh_from_db()
        self.assertEqual(result.status, "failed")

    def test_unknown_conversation_returns_none(self):
        """A callback for an unknown conversation_id returns None."""
        result = handle_b2c_callback(_build_b2c_callback("conv-nonexistent"))
        self.assertIsNone(result)

    def test_missing_conversation_returns_none(self):
        """A callback with no ConversationID returns None."""
        result = handle_b2c_callback({"Result": {}})
        self.assertIsNone(result)


class MpesaSTKInitiationTests(APITestCase):
    """Test STK Push initiation from the payments service."""

    def setUp(self):
        cache.clear()
        _, self.variant = _make_product()
        _stock_variant(self.variant)
        self.zone = _active_zone()

    def _create_order(self):
        """Create a pending M-Pesa order."""
        user = _make_user()
        cart = get_or_create_cart(user=user)
        add_item(cart, variant_id=self.variant.pk, quantity=1)
        with _mock_sms():
            order = create_order_from_cart(
                cart=cart,
                user=user,
                phone="+254712345678",
                payment_method="cod",
                delivery_zone_id=self.zone.pk,
            )
        order.payment_method = "mpesa"
        order.save(update_fields=["payment_method"])
        return order

    def test_creates_transaction_and_calls_daraja(self):
        """Initiating STK Push creates a pending MpesaTransaction."""
        order = self._create_order()

        with _mock_stk_push(), _mock_oauth_token():
            txn = initiate_stk_push(order)

        self.assertEqual(txn.status, "pending")
        self.assertEqual(txn.checkout_request_id, "ws_CO_123456")
        self.assertEqual(txn.merchant_request_id, "mr-001")
        self.assertEqual(txn.order, order)
        self.assertEqual(txn.amount, order.grand_total)

    def test_daraja_failure_propagates(self):
        """A Daraja API error propagates to the caller."""
        order = self._create_order()

        with mock.patch(
            "apps.payments.services._daraja_stk_push",
            side_effect=DarajaError("Network error"),
        ):
            with self.assertRaises(DarajaError):
                initiate_stk_push(order)

    def test_empty_checkout_request_id_marks_failed(self):
        """When Daraja returns no CheckoutRequestID, the transaction is failed."""
        order = self._create_order()

        with (
            mock.patch(
                "apps.payments.services._daraja_stk_push",
                return_value={"ResponseCode": "0", "CheckoutRequestID": ""},
            ),
            mock.patch(
                "apps.payments.daraja.get_access_token",
                return_value="token",
            ),
        ):
            txn = initiate_stk_push(order)

        txn.refresh_from_db()
        self.assertEqual(txn.status, "failed")


class MpesaTransactionStatusEndpointTests(APITestCase):
    """Test the GET /payments/mpesa/transactions/{id}/ endpoint."""

    def setUp(self):
        cache.clear()
        self.user = _make_user()
        _, self.variant = _make_product()
        _stock_variant(self.variant)
        self.zone = _active_zone()

    def _create_order_and_txn(self, user=None):
        """Create an order with a pending M-Pesa transaction."""
        target_user = user or self.user
        cart = get_or_create_cart(user=target_user)
        add_item(cart, variant_id=self.variant.pk, quantity=1)
        with _mock_sms():
            order = create_order_from_cart(
                cart=cart,
                user=target_user,
                phone="+254712345678",
                payment_method="cod",
                delivery_zone_id=self.zone.pk,
            )
        order.payment_method = "mpesa"
        order.save(update_fields=["payment_method"])
        txn = MpesaTransaction.objects.create(
            order=order,
            phone_number=order.phone,
            amount=order.grand_total,
            checkout_request_id="ws_CO_STATUS_TEST",
            status="pending",
        )
        return order, txn

    def _login(self, email="payer@example.com", password="StrongPass123!"):
        """Log in and attach the access token."""
        url = reverse("api:accounts:login")
        response = self.client.post(
            url, {"email": email, "password": password}, format="json"
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")

    def test_owner_can_view_transaction(self):
        """The order owner can view their M-Pesa transaction status."""
        _, txn = self._create_order_and_txn()
        self._login()
        url = reverse(
            "api:payments:mpesa-transaction-status",
            kwargs={"transaction_id": txn.pk},
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["checkout_request_id"], "ws_CO_STATUS_TEST")

    def test_anonymous_rejected(self):
        """An anonymous request is rejected."""
        _, txn = self._create_order_and_txn()
        url = reverse(
            "api:payments:mpesa-transaction-status",
            kwargs={"transaction_id": txn.pk},
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_other_user_rejected(self):
        """A different user cannot view another user's transaction."""
        _, txn = self._create_order_and_txn()
        other = _make_user(email="other@example.com", username="other")
        self.client.force_authenticate(user=other)
        url = reverse(
            "api:payments:mpesa-transaction-status",
            kwargs={"transaction_id": txn.pk},
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class DarajaPhoneNormalizationTests(APITestCase):
    """Test the Daraja phone number normalization helper."""

    def test_e164_plus254(self):
        """An E.164 number is returned as digits only."""
        self.assertEqual(_normalize_mpesa_phone("+254712345678"), "254712345678")

    def test_local_zero_prefix(self):
        """A local number starting with 0 gets 254 prefix."""
        self.assertEqual(_normalize_mpesa_phone("0712345678"), "254712345678")

    def test_bare_254_prefix(self):
        """A number already starting with 254 is returned as-is."""
        self.assertEqual(_normalize_mpesa_phone("254712345678"), "254712345678")

    def test_with_whitespace_and_dashes(self):
        """Whitespace and dashes are stripped."""
        self.assertEqual(_normalize_mpesa_phone("+254 712-345-678"), "254712345678")


class MpesaGatewayAvailabilityTests(APITestCase):
    """Test that MpesaGateway availability depends on credentials."""

    def test_unavailable_without_credentials(self):
        """M-Pesa is unavailable when credentials are not configured."""
        from apps.orders.payments import MpesaGateway

        gw = MpesaGateway()
        self.assertFalse(gw.is_available())

    def test_available_with_credentials(self):
        """M-Pesa is available when all required credentials are set."""
        from apps.orders.payments import MpesaGateway

        gw = MpesaGateway()
        fake_creds = {
            "MPESA_CONSUMER_KEY": "key",
            "MPESA_CONSUMER_SECRET": "secret",
            "MPESA_SHORTCODE": "12345",
            "MPESA_PASSKEY": "passkey",
        }

        def fake_config(key, default=""):
            return fake_creds.get(key, default)

        with mock.patch("apps.orders.payments._env_config", side_effect=fake_config):
            self.assertTrue(gw.is_available())

    def test_mpesa_does_not_require_otp(self):
        """M-Pesa gateway never requires OTP — STK Push proves the number."""
        from apps.orders.payments import MpesaGateway

        gw = MpesaGateway()
        self.assertFalse(gw.requires_otp())


class CallbackIPValidationTests(APITestCase):
    """Test that callback endpoints reject unauthorized source IPs."""

    def test_stk_callback_rejects_unauthorized_ip(self):
        """STK callback from an IP not in the allowlist is rejected."""

        with mock.patch(
            "apps.payments.views._MPESA_CALLBACK_IPS",
            ["1.2.3.4"],
        ):
            url = reverse("api:payments:mpesa-stk-callback")
            response = self.client.post(
                url, {"Body": {}}, format="json", REMOTE_ADDR="5.6.7.8"
            )
            self.assertEqual(response.status_code, 403)

    def test_stk_callback_accepts_allowed_ip(self):
        """STK callback from an allowed IP is processed."""
        with mock.patch(
            "apps.payments.views._MPESA_CALLBACK_IPS",
            ["5.6.7.8"],
        ):
            url = reverse("api:payments:mpesa-stk-callback")
            response = self.client.post(
                url, {"Body": {}}, format="json", REMOTE_ADDR="5.6.7.8"
            )
            # 400 because the body is malformed, but not 403
            self.assertIn(response.status_code, [200, 400])

    def test_empty_allowlist_allows_all(self):
        """An empty allowlist disables IP checking (for local dev)."""
        with mock.patch("apps.payments.views._MPESA_CALLBACK_IPS", []):
            url = reverse("api:payments:mpesa-stk-callback")
            response = self.client.post(
                url, {"Body": {}}, format="json", REMOTE_ADDR="127.0.0.1"
            )
            self.assertIn(response.status_code, [200, 400])


class STKCallbackAmountVerificationTests(APITestCase):
    """Cross-check the callback amount against the requested amount."""

    def setUp(self):
        cache.clear()
        _, self.variant = _make_product()
        _stock_variant(self.variant, quantity=20)
        self.zone = _active_zone()

    def _create_pending_txn(self, checkout_id="ws_CO_AMT"):
        """Return a pending M-Pesa order with a matching pending transaction."""
        user = _make_user()
        cart = get_or_create_cart(user=user)
        add_item(cart, variant_id=self.variant.pk, quantity=1)
        with _mock_sms():
            order = create_order_from_cart(
                cart=cart,
                user=user,
                phone="+254712345678",
                payment_method="cod",
                delivery_zone_id=self.zone.pk,
            )
        order.payment_method = "mpesa"
        order.save(update_fields=["payment_method"])
        txn = MpesaTransaction.objects.create(
            order=order,
            phone_number=order.phone,
            amount=order.grand_total,
            checkout_request_id=checkout_id,
            status="pending",
        )
        return order, txn

    def test_mismatched_amount_does_not_confirm(self):
        """A callback whose amount differs is failed, not silently confirmed."""
        order, txn = self._create_pending_txn()
        wrong_amount = int(order.grand_total) + 5000

        body = _build_stk_callback("ws_CO_AMT", amount=str(wrong_amount))
        result = handle_stk_callback(body)

        self.assertIsNotNone(result)
        result.refresh_from_db()
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.result_code, "AMOUNT_MISMATCH")

        order.refresh_from_db()
        self.assertEqual(order.status, "cancelled")
        self.assertFalse(Payment.objects.filter(order=order).exists())
        self.assertFalse(order.mpesa_transactions.filter(status="success").exists())

    def test_missing_amount_is_rejected(self):
        """A success callback with no amount is treated as a mismatch."""
        order, txn = self._create_pending_txn(checkout_id="ws_CO_NOAMT")

        body = _build_stk_callback("ws_CO_NOAMT", amount=None)
        result = handle_stk_callback(body)

        self.assertIsNotNone(result)
        result.refresh_from_db()
        self.assertEqual(result.status, "failed")
        order.refresh_from_db()
        self.assertEqual(order.status, "cancelled")

    def test_matching_decimal_amount_confirms(self):
        """A precision value like 1234.50 survives the callback boundary."""
        order, txn = self._create_pending_txn(checkout_id="ws_CO_DEC")
        txn.amount = Decimal("1234.50")
        txn.save(update_fields=["amount"])

        body = _build_stk_callback("ws_CO_DEC", amount="1234.50")
        result = handle_stk_callback(body)

        self.assertIsNotNone(result)
        result.refresh_from_db()
        self.assertEqual(result.status, "success")
        self.assertEqual(result.amount, Decimal("1234.50"))
        order.refresh_from_db()
        self.assertEqual(order.status, "confirmed")


class STKCallbackLateOrderTests(APITestCase):
    """A success callback arriving after the order moved past pending."""

    def setUp(self):
        cache.clear()
        _, self.variant = _make_product()
        _stock_variant(self.variant, quantity=20)
        self.zone = _active_zone()

    def _make_swept_order(self):
        """Create a pending order+txn, then cancel it like the sweep would."""
        user = _make_user()
        cart = get_or_create_cart(user=user)
        add_item(cart, variant_id=self.variant.pk, quantity=1)
        with _mock_sms():
            order = create_order_from_cart(
                cart=cart,
                user=user,
                phone="+254712345678",
                payment_method="cod",
                delivery_zone_id=self.zone.pk,
            )
        order.payment_method = "mpesa"
        order.save(update_fields=["payment_method"])
        txn = MpesaTransaction.objects.create(
            order=order,
            phone_number=order.phone,
            amount=order.grand_total,
            checkout_request_id="ws_CO_SWEPT",
            status="pending",
        )
        from apps.orders.services import cancel_pending_order

        cancel_pending_order(order, note="sweep")
        return order, txn

    def test_success_callback_after_sweep_flags_reconciliation(self):
        """A late success flags reconciliation instead of re-confirming."""
        order, txn = self._make_swept_order()
        self.assertEqual(order.status, "cancelled")

        body = _build_stk_callback("ws_CO_SWEPT", amount=str(order.grand_total))
        result = handle_stk_callback(body)

        self.assertIsNotNone(result)
        result.refresh_from_db()
        # Money was received so the transaction stays success, but the order
        # could not be confirmed and the discrepancy is flagged, not dropped.
        self.assertEqual(result.status, "success")
        self.assertTrue(result.needs_reconciliation)

        order.refresh_from_db()
        self.assertEqual(order.status, "cancelled")

        # The ledger still records the received payment for audit.
        self.assertTrue(Payment.objects.filter(order=order).exists())


class STKPhoneRateLimitTests(APITestCase):
    """Enforce a per-phone cap on STK Push initiation."""

    def setUp(self):
        cache.clear()
        _, self.variant = _make_product()
        _stock_variant(self.variant)
        self.zone = _active_zone()

    def _create_order(self, email):
        user = _make_user(email=email, username=email.split("@")[0])
        cart = get_or_create_cart(user=user)
        add_item(cart, variant_id=self.variant.pk, quantity=1)
        with _mock_sms():
            return create_order_from_cart(
                cart=cart,
                user=user,
                phone="+254712345678",
                payment_method="cod",
                delivery_zone_id=self.zone.pk,
            )

    def test_rate_limit_rejects_excess_pushes_to_same_phone(self):
        """More than the per-window maximum pushes to one phone is rejected."""
        max_pushes = 3

        def _fake_stk_push(**kwargs):
            return {"CheckoutRequestID": f"ws_CO_RR{kwargs['order_id']}"}

        with (
            mock.patch(
                "apps.payments.services._daraja_stk_push",
                side_effect=_fake_stk_push,
            ),
            _mock_oauth_token(),
        ):
            for i in range(max_pushes):
                order = self._create_order(f"rl{i}@example.com")
                order.payment_method = "mpesa"
                order.save(update_fields=["payment_method"])
                initiate_stk_push(order)

            order = self._create_order(f"rl{max_pushes}@example.com")
            order.payment_method = "mpesa"
            order.save(update_fields=["payment_method"])
            with self.assertRaises(StkPushRateLimited):
                initiate_stk_push(order)


class B2CRefundSerializationTests(APITestCase):
    """B2C refund destination binding and amount capping."""

    def setUp(self):
        cache.clear()
        _, self.variant = _make_product()
        _stock_variant(self.variant)
        self.zone = _active_zone()

    def _make_paid_order(self, paid_phone="+254700111222"):
        """Create a confirmed M-Pesa order with a recorded payment."""
        user = _make_user()
        cart = get_or_create_cart(user=user)
        add_item(cart, variant_id=self.variant.pk, quantity=1)
        with _mock_sms():
            order = create_order_from_cart(
                cart=cart,
                user=user,
                phone=paid_phone,
                payment_method="cod",
                delivery_zone_id=self.zone.pk,
            )
        order.payment_method = "mpesa"
        order.save(update_fields=["payment_method"])
        txn = MpesaTransaction.objects.create(
            order=order,
            phone_number=paid_phone,
            amount=order.grand_total,
            checkout_request_id=f"ws_CO_REFUND_{order.pk}",
            status="success",
            mpesa_receipt_number="RRR0001",
        )
        Payment.objects.create(
            order=order,
            provider="mpesa",
            transaction_id="RRR0001",
            amount=order.grand_total,
            status="completed",
            raw_response={},
        )
        return order, txn

    def test_refund_goes_to_payers_phone_not_caller_supplied(self):
        """The payout destination is bound to the payer's phone, never caller-supplied."""
        order, txn = self._make_paid_order()
        with (
            _mock_b2c(),
            mock.patch("apps.payments.daraja.get_access_token", return_value="token"),
        ):
            payout = initiate_b2c_refund(order, order.grand_total)

        self.assertEqual(payout.phone_number, "+254700111222")

    def test_refund_amount_capped_at_collected(self):
        """A refund larger than what was collected is capped, not authorized."""
        order, txn = self._make_paid_order()
        collected = order.grand_total
        oversized = collected * Decimal("3")

        with (
            _mock_b2c(),
            mock.patch("apps.payments.daraja.get_access_token", return_value="token"),
        ):
            payout = initiate_b2c_refund(order, oversized)

        self.assertEqual(payout.amount, collected)
        self.assertLessEqual(payout.amount, collected)


class CallbackTokenValidationTests(APITestCase):
    """Callback endpoints require the shared-secret token when configured."""

    def test_stk_callback_rejects_missing_token(self):
        """A callback without the token is rejected when a secret is set."""
        with (
            mock.patch("apps.payments.views._MPESA_CALLBACK_SECRET", "s3cret"),
            mock.patch("apps.payments.views._MPESA_CALLBACK_IPS", []),
        ):
            url = reverse("api:payments:mpesa-stk-callback")
            response = self.client.post(
                url, {"Body": {}}, format="json", REMOTE_ADDR="127.0.0.1"
            )
            self.assertEqual(response.status_code, 401)

    def test_stk_callback_rejects_wrong_token(self):
        """A callback with the wrong token is rejected."""
        with (
            mock.patch("apps.payments.views._MPESA_CALLBACK_SECRET", "s3cret"),
            mock.patch("apps.payments.views._MPESA_CALLBACK_IPS", []),
        ):
            url = reverse("api:payments:mpesa-stk-callback")
            response = self.client.post(
                f"{url}?token=wrong",
                {"Body": {}},
                format="json",
                REMOTE_ADDR="127.0.0.1",
            )
            self.assertEqual(response.status_code, 401)

    def test_stk_callback_accepts_correct_token(self):
        """A callback carrying the correct token passes the secret check."""
        with (
            mock.patch("apps.payments.views._MPESA_CALLBACK_SECRET", "s3cret"),
            mock.patch("apps.payments.views._MPESA_CALLBACK_IPS", []),
        ):
            url = reverse("api:payments:mpesa-stk-callback")
            response = self.client.post(
                f"{url}?token=s3cret",
                {"Body": {}},
                format="json",
                REMOTE_ADDR="127.0.0.1",
            )
            # Malformed body is a 400 (past the auth gate), never 401.
            self.assertIn(response.status_code, [200, 400])

    def test_no_secret_config_allows_all_tokens(self):
        """With no secret configured the token check is disabled."""
        with (
            mock.patch("apps.payments.views._MPESA_CALLBACK_SECRET", ""),
            mock.patch("apps.payments.views._MPESA_CALLBACK_IPS", []),
        ):
            url = reverse("api:payments:mpesa-stk-callback")
            response = self.client.post(
                url, {"Body": {}}, format="json", REMOTE_ADDR="127.0.0.1"
            )
            self.assertIn(response.status_code, [200, 400])


class B2CCallbackHTTPSecurityTests(APITestCase):
    """HTTP-level tests for the B2C callback endpoint security."""

    def test_b2c_callback_rejects_unauthorized_ip(self):
        """B2C callback from an IP not in the allowlist is rejected."""
        with mock.patch(
            "apps.payments.views._MPESA_CALLBACK_IPS",
            ["1.2.3.4"],
        ):
            url = reverse("api:payments:mpesa-b2c-callback")
            response = self.client.post(
                url, {"Result": {}}, format="json", REMOTE_ADDR="5.6.7.8"
            )
            self.assertEqual(response.status_code, 403)

    def test_b2c_callback_accepts_allowed_ip(self):
        """B2C callback from an allowed IP is processed."""
        with mock.patch(
            "apps.payments.views._MPESA_CALLBACK_IPS",
            ["5.6.7.8"],
        ):
            url = reverse("api:payments:mpesa-b2c-callback")
            response = self.client.post(
                url, {"Result": {}}, format="json", REMOTE_ADDR="5.6.7.8"
            )
            self.assertIn(response.status_code, [200, 400])

    def test_b2c_callback_empty_allowlist_allows_all(self):
        """An empty allowlist disables IP checking (for local dev)."""
        with mock.patch("apps.payments.views._MPESA_CALLBACK_IPS", []):
            url = reverse("api:payments:mpesa-b2c-callback")
            response = self.client.post(
                url, {"Result": {}}, format="json", REMOTE_ADDR="127.0.0.1"
            )
            self.assertIn(response.status_code, [200, 400])

    def test_b2c_callback_rejects_missing_token(self):
        """A B2C callback without the token is rejected when a secret is set."""
        with (
            mock.patch("apps.payments.views._MPESA_CALLBACK_SECRET", "s3cret"),
            mock.patch("apps.payments.views._MPESA_CALLBACK_IPS", []),
        ):
            url = reverse("api:payments:mpesa-b2c-callback")
            response = self.client.post(
                url, {"Result": {}}, format="json", REMOTE_ADDR="127.0.0.1"
            )
            self.assertEqual(response.status_code, 401)

    def test_b2c_callback_rejects_wrong_token(self):
        """A B2C callback with the wrong token is rejected."""
        with (
            mock.patch("apps.payments.views._MPESA_CALLBACK_SECRET", "s3cret"),
            mock.patch("apps.payments.views._MPESA_CALLBACK_IPS", []),
        ):
            url = reverse("api:payments:mpesa-b2c-callback")
            response = self.client.post(
                f"{url}?token=wrong",
                {"Result": {}},
                format="json",
                REMOTE_ADDR="127.0.0.1",
            )
            self.assertEqual(response.status_code, 401)

    def test_b2c_callback_accepts_correct_token(self):
        """A B2C callback carrying the correct token passes the secret check."""
        with (
            mock.patch("apps.payments.views._MPESA_CALLBACK_SECRET", "s3cret"),
            mock.patch("apps.payments.views._MPESA_CALLBACK_IPS", []),
        ):
            url = reverse("api:payments:mpesa-b2c-callback")
            response = self.client.post(
                f"{url}?token=s3cret",
                {"Result": {}},
                format="json",
                REMOTE_ADDR="127.0.0.1",
            )
            self.assertIn(response.status_code, [200, 400])
