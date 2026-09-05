"""Tests for the returns app.

Covers the full post-delivery return lifecycle per refund method (M-Pesa B2C
and card reversal), the pre-shipment cancellation path, and the money/stock
invariants the service layer upholds: server-side refund caps, restock
correctness (count and serialized), audit-trail completeness, idempotent
payout handling, and the object-level/role-level access control on every
endpoint.
"""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.db import IntegrityError
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.cart.services import add_item, get_or_create_cart
from apps.catalog.models import Category, Product, ProductVariant
from apps.inventory.models import Inventory, SerialUnit
from apps.inventory.services import receive_serial_units
from apps.orders.models import Order, OrderStatusHistory
from apps.orders.services import (
    confirm_order_from_verification,
    create_order_from_cart,
    transition_order,
)
from apps.payments.daraja import DarajaError
from apps.payments.models import MpesaB2CPayout, Payment
from apps.returns.models import ReturnRequest, ReturnRequestStatusHistory
from apps.returns.services import (
    approve_return_request,
    create_return_request,
    record_item_received,
    refund_return_request,
    reject_return_request,
)

_SEQ = [0]

_B2C_HTTP_RESPONSE = {
    "ConversationID": "conv-refund-001",
    "OriginatorConversationID": "org-refund-001",
    "ResponseCode": "0",
    "ResponseDescription": "Accept the service request successfully.",
}


def _mock_b2c(success=True):
    """Return a context manager stubbing the Daraja B2C endpoint."""
    return mock.patch(
        "apps.payments.daraja._http_post",
        return_value=dict(_B2C_HTTP_RESPONSE) if success else {"ResponseCode": "1"},
    )


def _mock_oauth_token():
    """Return a context manager stubbing the Daraja OAuth token endpoint."""
    return mock.patch("apps.payments.daraja.get_access_token", return_value="token")


def _mock_b2c_callback_allowlist():
    """Return a context manager disabling the callback source-IP allowlist."""
    return mock.patch("apps.payments.views._MPESA_CALLBACK_IPS", [])


def _build_b2c_callback(conversation_id, result_code="0"):
    """Build a realistic B2C callback body for a confirmed payout."""
    return {
        "Result": {
            "ConversationID": conversation_id,
            "OriginatorConversationID": "org-001",
            "ResultCode": result_code,
            "ResultDesc": "The service request has been accepted successfully.",
            "TransactionID": "BLM3A7B1C2",
        }
    }


def _make_user(email="buyer@example.com", username="buyer", **kwargs):
    """Create a plain customer user for tests."""
    return User.objects.create_user(
        email=email,
        username=username,
        password=kwargs.pop("password", "StrongPass123!"),
        phone_number=kwargs.pop("phone_number", "+254712345678"),
        **kwargs,
    )


def _make_staff(email="staff@example.com", role="manager"):
    """Create a staff user holding the given role."""
    return User.objects.create_user(
        email=email,
        username=email.split("@")[0],
        password="StrongPass123!",
        phone_number="+254700000001",
        is_staff=True,
        role=role,
    )


def _login(client, email="buyer@example.com", password="StrongPass123!"):
    """Log in and attach the access token to the test client."""
    url = reverse("api:accounts:login")
    response = client.post(url, {"email": email, "password": password}, format="json")
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")


def _make_product(price="5000.00", **kwargs):
    """Create a product with a default active variant, ensuring unique keys."""
    _SEQ[0] += 1
    n = _SEQ[0]
    category = Category.objects.get_or_create(name="Appliances", slug="appliances")[0]
    product = Product.objects.create(
        name=f"Kettle {n}",
        slug=f"kettle-{n}",
        sku=f"KTL-{n}",
        description="A test product.",
        category=category,
        is_active=True,
        **kwargs,
    )
    variant = ProductVariant.objects.create(
        product=product,
        sku=f"KTL-{n}-V",
        attributes={"color": "Silver"},
        price=price,
        package_weight="2.00",
        is_active=True,
    )
    return product, variant


def _stock_variant(variant, quantity=10):
    """Ensure a count-tracked variant has stock in a test warehouse."""
    from apps.inventory.models import Warehouse

    warehouse, _ = Warehouse.objects.get_or_create(name="Main")
    Inventory.objects.update_or_create(
        variant=variant,
        warehouse=warehouse,
        defaults={"quantity": quantity, "reserved": 0},
    )
    return warehouse


def _active_zone():
    """Return a reusable active delivery zone for physical orders."""
    from apps.shipping.models import DeliveryZone

    zone, _ = DeliveryZone.objects.get_or_create(
        county="Nairobi",
        area_name="Westlands",
        defaults={"base_fee": "200.00", "per_kg_rate": "50.00"},
    )
    return zone


def _place_cod_order(variant, user=None, quantity=1, session_key=None):
    """Place a COD order for a stocked variant and confirm it."""
    if session_key is None:
        _SEQ[0] += 1
        session_key = f"return-cart-{_SEQ[0]}"
    cart = get_or_create_cart(session_key=session_key)
    add_item(cart, variant_id=variant.pk, quantity=quantity)
    order = create_order_from_cart(
        cart=cart,
        user=user,
        phone=user.phone_number if user else "+254712345678",
        payment_method="cod",
        delivery_zone_id=_active_zone().pk,
    )
    return confirm_order_from_verification(order)


def _delivered_cod_order(variant, user=None, session_key=None):
    """Place, confirm, ship, and deliver a COD order."""
    order = _place_cod_order(variant, user=user, session_key=session_key)
    transition_order(order, "shipped")
    transition_order(order, "delivered")
    return order


class ReturnRequestModelTests(APITestCase):
    """Exercises the return request model and its constraints."""

    def setUp(self):
        cache.clear()
        _, self.variant = _make_product()
        _stock_variant(self.variant)
        self.order = _delivered_cod_order(self.variant)

    def test_money_constraint_rejects_negative_stored_amounts(self):
        """The DB constraint forbids negative fee or refund amounts."""
        with self.assertRaises(IntegrityError):
            ReturnRequest.objects.create(
                order=self.order,
                order_item=self.order.items.first(),
                reason="faulty",
                restocking_fee_applied=Decimal("-1.00"),
            )

    def test_constraint_rejects_second_live_request_for_same_line(self):
        """The partial unique constraint forbids two live-or-resolved requests."""
        ReturnRequest.objects.create(
            order=self.order,
            order_item=self.order.items.first(),
            reason="faulty",
        )
        with self.assertRaises(IntegrityError):
            ReturnRequest.objects.create(
                order=self.order,
                order_item=self.order.items.first(),
                reason="changed mind",
            )

    def test_constraint_allows_request_after_rejection(self):
        """A rejected request is outside the partial unique constraint."""
        order_item = self.order.items.first()
        rejected = ReturnRequest.objects.create(
            order=self.order,
            order_item=order_item,
            reason="faulty",
        )
        ReturnRequest.objects.filter(pk=rejected.pk).update(
            status="rejected", resolved_at=timezone.now()
        )
        ReturnRequest.objects.create(
            order=self.order,
            order_item=order_item,
            reason="changed mind",
        )

    def test_status_history_written_for_each_transition(self):
        """Every lifecycle transition is recorded in the audit trail."""
        return_request = create_return_request(
            order=self.order, reason="faulty", user=None
        )
        approve_return_request(
            return_request=return_request,
            refund_method="card_reversal",
            user=None,
        )
        record_item_received(return_request=return_request)
        transitions = list(return_request.status_history.all())
        self.assertEqual(
            [t.to_status for t in transitions],
            ["requested", "approved", "item_received"],
        )
        self.assertEqual(ReturnRequestStatusHistory.objects.count(), 3)


class ReturnRequestCreationTests(APITestCase):
    """Exercises the customer-facing return-request creation rules."""

    def setUp(self):
        cache.clear()
        _, self.variant = _make_product()
        _stock_variant(self.variant)
        self.user = _make_user()
        self.order = _delivered_cod_order(self.variant, user=self.user)
        _login(self.client)
        self.url = reverse("api:returns:order-return-requests", args=[self.order.pk])

    def _create(self, **overrides):
        payload = {
            "order_item_id": self.order.items.first().pk,
            "reason": "faulty",
            "requested_resolution": "refund",
        }
        payload.update(overrides)
        return self.client.post(self.url, payload, format="json")

    def test_customer_opens_return_for_delivered_order(self):
        """A delivered order's owner can open a return request."""
        response = self._create()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.data
        self.assertEqual(data["status"], "requested")
        self.assertEqual(data["requested_resolution"], "refund")
        self.assertEqual(ReturnRequest.objects.count(), 1)

    def test_not_delivered_rejected(self):
        """A return cannot be opened against a non-delivered order."""
        order = _place_cod_order(self.variant, user=self.user)
        url = reverse("api:returns:order-return-requests", args=[order.pk])
        response = self.client.post(url, {"reason": "faulty"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_outside_cooling_off_window_rejected(self):
        """A return past the configured cooling-off window is rejected."""
        old_order = Order.objects.get(pk=self.order.pk)
        Order.objects.filter(pk=old_order.pk).update(
            placed_at=timezone.now() - timedelta(days=30)
        )
        response = self._create()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_non_returnable_product_rejected(self):
        """A product flagged non-returnable cannot be returned."""
        _, non_returnable = _make_product(is_returnable=False)
        _stock_variant(non_returnable)
        order = _delivered_cod_order(non_returnable, user=self.user)
        url = reverse("api:returns:order-return-requests", args=[order.pk])
        response = self.client.post(
            url,
            {"order_item_id": order.items.first().pk, "reason": "changed mind"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_duplicate_open_request_rejected(self):
        """A second open request for the same line is rejected."""
        self.assertEqual(self._create().status_code, status.HTTP_201_CREATED)
        response = self._create()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_second_request_after_completed_refund_rejected(self):
        """A refunded line on a still-delivered order cannot be returned again."""
        _, variant_two = _make_product(price="3000.00")
        _stock_variant(variant_two)
        cart = get_or_create_cart(session_key="multi-line-return")
        add_item(cart, variant_id=self.variant.pk, quantity=1)
        add_item(cart, variant_id=variant_two.pk, quantity=1)
        order = create_order_from_cart(
            cart=cart,
            user=self.user,
            phone=self.user.phone_number,
            payment_method="cod",
            delivery_zone_id=_active_zone().pk,
        )
        confirm_order_from_verification(order)
        transition_order(order, "shipped")
        transition_order(order, "delivered")

        line_a = order.items.get(variant_sku=self.variant.sku)
        first = create_return_request(
            order=order,
            order_item_id=line_a.pk,
            reason="faulty",
            user=self.user,
        )
        approve_return_request(
            return_request=first,
            refund_method="card_reversal",
            user=None,
        )
        record_item_received(return_request=first, user=None)
        refund_return_request(return_request=first, user=None)
        first.refresh_from_db()
        self.assertEqual(first.status, "refunded")
        # The second line is untouched, so the order is still returnable.
        order.refresh_from_db()
        self.assertEqual(order.status, "delivered")

        url = reverse("api:returns:order-return-requests", args=[order.pk])
        response = self.client.post(
            url,
            {"order_item_id": line_a.pk, "reason": "faulty"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_rejected_request_leaves_return_right_intact(self):
        """A rejected request does not burn the right to return the line."""
        order_item = self.order.items.first()
        rejected = create_return_request(
            order=self.order, reason="faulty", user=self.user
        )
        reject_return_request(return_request=rejected, user=None)
        response = self._create()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(ReturnRequest.objects.filter(order_item=order_item).count(), 2)

    def test_whole_order_return_requires_single_line_order(self):
        """A whole-order return on a multi-line order is rejected."""
        _, variant_two = _make_product(price="3000.00")
        _stock_variant(variant_two)
        cart = get_or_create_cart(session_key="multi-line-cart")
        add_item(cart, variant_id=self.variant.pk, quantity=1)
        add_item(cart, variant_id=variant_two.pk, quantity=1)
        order = create_order_from_cart(
            cart=cart,
            user=self.user,
            phone=self.user.phone_number,
            payment_method="cod",
            delivery_zone_id=_active_zone().pk,
        )
        confirm_order_from_verification(order)
        transition_order(order, "shipped")
        transition_order(order, "delivered")
        url = reverse("api:returns:order-return-requests", args=[order.pk])
        response = self.client.post(url, {"reason": "faulty"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_foreign_line_rejected(self):
        """A line that does not belong to the order is rejected."""
        _, other_variant = _make_product(price="1000.00")
        _stock_variant(other_variant)
        other_order = _delivered_cod_order(other_variant, user=self.user)
        foreign_item = other_order.items.first()
        response = self._create(order_item_id=foreign_item.pk)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_guest_via_token_can_open_return(self):
        """A guest holding the order token can open a return."""
        self.client.credentials()
        guest_order = _delivered_cod_order(self.variant, session_key="guest-return")
        url = reverse(
            "api:returns:order-return-requests", args=[str(guest_order.lookup_token)]
        )
        line = guest_order.items.first()
        response = self.client.post(
            url,
            {"order_item_id": line.pk, "reason": "faulty"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_customer_list_is_paginated(self):
        """The customer-facing list returns a paginated envelope."""
        self.assertEqual(self._create().status_code, status.HTTP_201_CREATED)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("results", response.data)
        self.assertEqual(len(response.data["results"]), 1)

    def test_customer_reason_is_sanitized(self):
        """Free-form reason text has its HTML stripped before storage."""
        response = self._create(reason="<script>alert(1)</script>faulty")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertNotIn("<script>", response.data["reason"])
        self.assertIn("faulty", response.data["reason"])


class ReturnAccessTests(APITestCase):
    """Exercises ownership and role access control on return endpoints."""

    def setUp(self):
        cache.clear()
        _, self.variant = _make_product()
        _stock_variant(self.variant)
        self.owner = _make_user()
        self.order = _delivered_cod_order(self.variant, user=self.owner)
        self.return_request = create_return_request(
            order=self.order, reason="faulty", user=self.owner
        )
        self.staff = _make_staff()
        _login(self.client)

    def test_other_user_create_rejected_with_404(self):
        """A different logged-in user cannot open a return on the order."""
        _make_user(email="other@example.com", username="other")
        _login(self.client, email="other@example.com")
        url = reverse("api:returns:order-return-requests", args=[self.order.pk])
        response = self.client.post(url, {"reason": "faulty"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_other_user_detail_rejected_with_404(self):
        """A different logged-in user cannot read a return on the order."""
        _make_user(email="other@example.com", username="other")
        _login(self.client, email="other@example.com")
        url = reverse(
            "api:returns:order-return-request-detail",
            args=[self.order.pk, self.return_request.pk],
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_owner_can_read_detail(self):
        """The order owner can read the return request detail."""
        url = reverse(
            "api:returns:order-return-request-detail",
            args=[self.order.pk, self.return_request.pk],
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_customer_detail_hides_staff_fields(self):
        """The order owner never sees staff identity or notes in the history."""
        _login(self.client, email="staff@example.com")
        approve_url = reverse(
            "api:returns:return-request-approve", args=[self.return_request.pk]
        )
        self.client.post(approve_url, {"refund_method": "card_reversal"}, format="json")
        _login(self.client)
        url = reverse(
            "api:returns:order-return-request-detail",
            args=[self.order.pk, self.return_request.pk],
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["status_history"])
        for entry in response.data["status_history"]:
            self.assertNotIn("changed_by", entry)
            self.assertNotIn("note", entry)

    def test_anonymous_staff_listing_rejected(self):
        """An anonymous caller cannot list return requests as staff."""
        self.client.credentials()
        response = self.client.get(reverse("api:returns:return-request-staff-list"))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_customer_rejected_from_staff_actions(self):
        """A plain customer cannot reach staff return actions."""
        response = self.client.post(
            reverse(
                "api:returns:return-request-approve",
                args=[self.return_request.pk],
            ),
            {"refund_method": "card_reversal"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_without_role_rejected_from_staff_actions(self):
        """An is_staff user without the manager/support role is denied."""
        _make_staff(email="analyst@example.com", role="analyst")
        _login(self.client, email="analyst@example.com")
        response = self.client.post(
            reverse(
                "api:returns:return-request-approve", args=[self.return_request.pk]
            ),
            {"refund_method": "card_reversal"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_manager_can_approve(self):
        """A manager can approve a return request."""
        _login(self.client, email="staff@example.com")
        response = self.client.post(
            reverse(
                "api:returns:return-request-approve", args=[self.return_request.pk]
            ),
            {"refund_method": "card_reversal"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_missing_return_request_is_404(self):
        """A staff action on a missing request is a 404."""
        _login(self.client, email="staff@example.com")
        response = self.client.post(
            reverse("api:returns:return-request-approve", args=[99999]),
            {"refund_method": "card_reversal"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class ReturnRefundFlowTests(APITestCase):
    """Exercises the full refund lifecycle via B2C and card reversal."""

    def setUp(self):
        cache.clear()
        _, self.variant = _make_product()
        self.warehouse = _stock_variant(self.variant, quantity=10)
        self.customer = _make_user()
        self.order = _delivered_cod_order(self.variant, user=self.customer)
        self.return_request = create_return_request(
            order=self.order, reason="faulty", user=self.customer
        )
        self.staff = _make_staff()
        _login(self.client, email="staff@example.com")
        self.approve_url = reverse(
            "api:returns:return-request-approve", args=[self.return_request.pk]
        )
        self.staff_urls = {
            "receive": reverse(
                "api:returns:return-request-receive-item",
                args=[self.return_request.pk],
            ),
            "refund": reverse(
                "api:returns:return-request-refund", args=[self.return_request.pk]
            ),
        }

    def test_full_b2c_return_flow_to_refunded(self):
        """Approve, receive, initiate B2C; callback confirms and refunds."""
        approve = self.client.post(
            self.approve_url, {"refund_method": "mpesa_b2c"}, format="json"
        )
        self.assertEqual(approve.status_code, status.HTTP_200_OK)
        self.assertEqual(approve.data["status"], "approved")
        refund_amount = Decimal(approve.data["refund_amount"])

        inventory = Inventory.objects.get(
            variant=self.variant, warehouse=self.warehouse
        )
        before = inventory.quantity

        receive = self.client.post(self.staff_urls["receive"], format="json")
        self.assertEqual(receive.status_code, status.HTTP_200_OK)
        self.assertEqual(receive.data["status"], "item_received")

        inventory.refresh_from_db()
        self.assertEqual(inventory.quantity, before + 1)

        with _mock_b2c(), _mock_oauth_token():
            refund = self.client.post(
                self.staff_urls["refund"],
                {},
                format="json",
                HTTP_IDEMPOTENCY_KEY="refund-b2c-1",
            )
        self.assertEqual(refund.status_code, status.HTTP_200_OK)
        self.assertEqual(refund.data["status"], "item_received")

        payout = MpesaB2CPayout.objects.get(return_request=self.return_request)
        self.assertEqual(payout.status, "pending")
        self.assertEqual(payout.amount, refund_amount)
        self.assertEqual(payout.reason, "return_refund")

        with _mock_b2c_callback_allowlist():
            callback_url = reverse("api:payments:mpesa-b2c-callback")
            callback = self.client.post(
                callback_url,
                _build_b2c_callback(payout.conversation_id),
                format="json",
                REMOTE_ADDR="127.0.0.1",
            )
        self.assertEqual(callback.status_code, status.HTTP_200_OK)

        payout.refresh_from_db()
        self.assertEqual(payout.status, "success")
        self.return_request.refresh_from_db()
        self.assertEqual(self.return_request.status, "refunded")
        self.assertIsNotNone(self.return_request.resolved_at)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "refunded")

    def test_callback_is_idempotent(self):
        """A delivered callback does not double-mutate the return."""
        approve = self.client.post(
            self.approve_url, {"refund_method": "mpesa_b2c"}, format="json"
        )
        self.assertEqual(approve.status_code, status.HTTP_200_OK)
        self.client.post(self.staff_urls["receive"], format="json")
        with _mock_b2c(), _mock_oauth_token():
            self.client.post(
                self.staff_urls["refund"],
                {},
                format="json",
                HTTP_IDEMPOTENCY_KEY="refund-b2c-2",
            )
        payout = MpesaB2CPayout.objects.get(return_request=self.return_request)

        with _mock_b2c_callback_allowlist():
            callback_url = reverse("api:payments:mpesa-b2c-callback")
            body = _build_b2c_callback(payout.conversation_id)
            self.client.post(callback_url, body, format="json", REMOTE_ADDR="127.0.0.1")
            self.client.post(callback_url, body, format="json", REMOTE_ADDR="127.0.0.1")

        self.return_request.refresh_from_db()
        self.assertEqual(self.return_request.status, "refunded")
        self.assertEqual(
            ReturnRequestStatusHistory.objects.filter(to_status="refunded").count(),
            1,
        )

    def test_card_reversal_refund_completes_immediately(self):
        """A card-reversal refund records a Payment row and completes."""
        approve = self.client.post(
            self.approve_url, {"refund_method": "card_reversal"}, format="json"
        )
        self.assertEqual(approve.status_code, status.HTTP_200_OK)
        refund_amount = Decimal(approve.data["refund_amount"])

        self.client.post(self.staff_urls["receive"], format="json")
        refund = self.client.post(
            self.staff_urls["refund"],
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="refund-card-1",
        )
        self.assertEqual(refund.status_code, status.HTTP_200_OK)
        self.assertEqual(refund.data["status"], "refunded")

        self.return_request.refresh_from_db()
        self.assertEqual(self.return_request.status, "refunded")
        self.assertIsNotNone(self.return_request.resolved_at)

        reversal = Payment.objects.get(
            order=self.order, provider="card", status="refunded"
        )
        self.assertEqual(reversal.amount, refund_amount)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "refunded")

    def test_refund_requires_idempotency_key(self):
        """The refund endpoint rejects a request without an Idempotency-Key."""
        approve = self.client.post(
            self.approve_url, {"refund_method": "card_reversal"}, format="json"
        )
        self.assertEqual(approve.status_code, status.HTTP_200_OK)
        self.client.post(self.staff_urls["receive"], format="json")
        response = self.client.post(self.staff_urls["refund"], {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_refund_replays_stored_response_for_duplicate_key(self):
        """Reusing an Idempotency-Key replays the stored response."""
        approve = self.client.post(
            self.approve_url, {"refund_method": "card_reversal"}, format="json"
        )
        refund_amount = Decimal(approve.data["refund_amount"])
        self.client.post(self.staff_urls["receive"], format="json")
        first = self.client.post(
            self.staff_urls["refund"],
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="refund-card-dupe",
        )
        second = self.client.post(
            self.staff_urls["refund"],
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="refund-card-dupe",
        )
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertEqual(second.data["status"], "refunded")
        refunded_rows = Payment.objects.filter(
            order=self.order, provider="card", status="refunded"
        )
        self.assertEqual(refunded_rows.count(), 1)
        self.assertEqual(refunded_rows.first().amount, refund_amount)

    def test_refund_returns_503_when_provider_unavailable(self):
        """A Daraja outage surfaces as 503 and moves no money."""
        approve = self.client.post(
            self.approve_url, {"refund_method": "mpesa_b2c"}, format="json"
        )
        self.assertEqual(approve.status_code, status.HTTP_200_OK)
        self.client.post(self.staff_urls["receive"], format="json")

        with mock.patch(
            "apps.payments.services.initiate_b2c_refund",
            side_effect=DarajaError("provider down"),
        ):
            response = self.client.post(
                self.staff_urls["refund"],
                {},
                format="json",
                HTTP_IDEMPOTENCY_KEY="refund-503",
            )
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.return_request.refresh_from_db()
        self.assertEqual(self.return_request.status, "item_received")
        self.assertFalse(
            MpesaB2CPayout.objects.filter(return_request=self.return_request).exists()
        )

    def test_refund_before_receipt_rejected(self):
        """Refund without receiving the goods is rejected."""
        approve = self.client.post(
            self.approve_url, {"refund_method": "card_reversal"}, format="json"
        )
        self.assertEqual(approve.status_code, status.HTTP_200_OK)
        response = self.client.post(
            self.staff_urls["refund"],
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="refund-early",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_store_credit_approval_rejected(self):
        """Store-credit resolution is rejected until a loyalty ledger exists."""
        response = self.client.post(
            self.approve_url, {"refund_method": "store_credit"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ReturnApprovalMoneyTests(APITestCase):
    """Exercises the server-side refund economics at approval time."""

    def setUp(self):
        cache.clear()
        _, self.variant = _make_product(price="5000.00")
        _stock_variant(self.variant)
        self.customer = _make_user()
        self.order = _delivered_cod_order(self.variant, user=self.customer)
        self.return_request = create_return_request(
            order=self.order, reason="faulty", user=self.customer
        )
        self.staff = _make_staff()
        _login(self.client, email="staff@example.com")
        self.url = reverse(
            "api:returns:return-request-approve", args=[self.return_request.pk]
        )

    def test_refund_amount_computed_from_line_and_capped_at_collected(self):
        """The refund is the line total minus fee, capped at what was collected."""
        line = self.order.items.first()
        collected = self.order.grand_total
        eligible = min(
            line.total_price + line.tax, collected
        )  # no restocking fee by default
        response = self.client.post(
            self.url, {"refund_method": "card_reversal"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Decimal(response.data["refund_amount"]), Decimal(eligible))

    def test_explicit_refund_amount_capped(self):
        """A staff-named amount larger than eligible is capped."""
        line = self.order.items.first()
        eligible = line.total_price + line.tax
        response = self.client.post(
            self.url,
            {
                "refund_method": "card_reversal",
                "refund_amount": "999999.00",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Decimal(response.data["refund_amount"]), Decimal(eligible))

    def test_restocking_fee_reduces_refund(self):
        """A configured restocking fee reduces the refunded amount."""
        product = Product.objects.get(pk=self.order.items.first().product_id)
        product.restocking_fee_percent = Decimal("10.00")
        product.save()
        line = self.order.items.first()
        base = Decimal(line.total_price + line.tax)
        expected_fee = (base * Decimal("0.10")).quantize(Decimal("0.01"))
        response = self.client.post(
            self.url, {"refund_method": "card_reversal"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Decimal(response.data["refund_amount"]), base - expected_fee)
        self.assertEqual(Decimal(response.data["restocking_fee_applied"]), expected_fee)

    def test_negative_staff_amount_rejected(self):
        """A negative explicit amount is rejected at approval."""
        response = self.client.post(
            self.url,
            {"refund_method": "card_reversal", "refund_amount": "-5.00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_replacement_resolution_rejected_at_approval(self):
        """A replacement resolution is rejected until fulfillment accounting exists."""
        _, variant_two = _make_product(price="3000.00")
        _stock_variant(variant_two)
        replacement_order = _delivered_cod_order(variant_two, user=self.customer)
        replacement = create_return_request(
            order=replacement_order,
            requested_resolution="replacement",
            reason="prefer exchange",
            user=self.customer,
        )
        url = reverse("api:returns:return-request-approve", args=[replacement.pk])
        response = self.client.post(url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        replacement.refresh_from_db()
        self.assertEqual(replacement.status, "requested")

    def test_restocking_fee_exceeding_line_total_rejected(self):
        """A staff fee larger than the line total is refused, not stored."""
        line = self.order.items.first()
        base = line.total_price + line.tax
        response = self.client.post(
            self.url,
            {
                "refund_method": "card_reversal",
                "restocking_fee": str(Decimal(base) + Decimal("100.00")),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.return_request.refresh_from_db()
        self.assertEqual(self.return_request.status, "requested")
        self.assertIsNone(self.return_request.refund_amount)

    def test_zero_refund_after_full_fee_rejected(self):
        """A fee consuming the whole line total leaves nothing to refund."""
        line = self.order.items.first()
        base = line.total_price + line.tax
        response = self.client.post(
            self.url,
            {
                "refund_method": "card_reversal",
                "restocking_fee": str(base),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.return_request.refresh_from_db()
        self.assertEqual(self.return_request.status, "requested")


class ReturnRejectAndCloseTests(APITestCase):
    """Exercises rejection and closure of return requests."""

    def setUp(self):
        cache.clear()
        _, self.variant = _make_product()
        _stock_variant(self.variant)
        self.customer = _make_user()
        self.order = _delivered_cod_order(self.variant, user=self.customer)
        self.return_request = create_return_request(
            order=self.order, reason="changed mind", user=self.customer
        )
        self.staff = _make_staff()
        _login(self.client, email="staff@example.com")

    def test_requested_return_can_be_rejected(self):
        """A requested return can be rejected with a note."""
        url = reverse(
            "api:returns:return-request-reject", args=[self.return_request.pk]
        )
        response = self.client.post(url, {"note": "outside policy"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "rejected")
        self.assertIsNotNone(response.data["resolved_at"])

    def test_received_return_cannot_be_rejected(self):
        """A return whose goods are in hand cannot be rejected."""
        approve_url = reverse(
            "api:returns:return-request-approve", args=[self.return_request.pk]
        )
        self.client.post(approve_url, {"refund_method": "card_reversal"}, format="json")
        receive_url = reverse(
            "api:returns:return-request-receive-item", args=[self.return_request.pk]
        )
        self.client.post(receive_url, format="json")
        reject_url = reverse(
            "api:returns:return-request-reject", args=[self.return_request.pk]
        )
        response = self.client.post(reject_url, {"note": "policy"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_refunded_return_can_be_closed(self):
        """A refunded return can be closed for housekeeping."""
        approve_url = reverse(
            "api:returns:return-request-approve", args=[self.return_request.pk]
        )
        self.client.post(approve_url, {"refund_method": "card_reversal"}, format="json")
        receive_url = reverse(
            "api:returns:return-request-receive-item", args=[self.return_request.pk]
        )
        self.client.post(receive_url, format="json")
        refund_url = reverse(
            "api:returns:return-request-refund", args=[self.return_request.pk]
        )
        self.client.post(
            refund_url, {}, format="json", HTTP_IDEMPOTENCY_KEY="close-key"
        )
        close_url = reverse(
            "api:returns:return-request-close", args=[self.return_request.pk]
        )
        response = self.client.post(close_url, {"note": "file closed"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "closed")


class PreShipmentCancellationTests(APITestCase):
    """Exercises the confirmed-but-undelivered cancellation path."""

    def setUp(self):
        cache.clear()
        _, self.variant = _make_product()
        self.warehouse = _stock_variant(self.variant, quantity=10)
        self.customer = _make_user()
        self.order = _place_cod_order(self.variant, user=self.customer)
        self.staff = _make_staff()
        _login(self.client, email="staff@example.com")
        self.url = reverse(
            "api:returns:order-pre-shipment-cancel", args=[self.order.pk]
        )

        Payment.objects.create(
            order=self.order,
            provider="mpesa",
            transaction_id="stk-ref-1",
            amount=self.order.grand_total,
            status="completed",
        )

    def test_mpesa_cancellation_refunds_collected_and_cancels(self):
        """A confirmed M-Pesa order is restocked, refunded, and cancelled."""
        Order.objects.filter(pk=self.order.pk).update(payment_method="mpesa")
        inventory = Inventory.objects.get(
            variant=self.variant, warehouse=self.warehouse
        )
        before = inventory.quantity

        with _mock_b2c(), _mock_oauth_token():
            response = self.client.post(
                self.url, {}, format="json", HTTP_IDEMPOTENCY_KEY="cancel-m1"
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "cancelled")

        inventory.refresh_from_db()
        self.assertEqual(inventory.quantity, before + 1)

        payout = MpesaB2CPayout.objects.get(
            order=self.order, reason="order_cancellation"
        )
        self.assertEqual(payout.status, "pending")
        self.assertEqual(payout.amount, self.order.grand_total)

        self.assertTrue(
            OrderStatusHistory.objects.filter(
                order=self.order, to_status="cancelled"
            ).exists()
        )

    def test_cod_cancellation_collects_nothing(self):
        """A COD order cancelled pre-shipment restocks without any refund."""
        inventory = Inventory.objects.get(
            variant=self.variant, warehouse=self.warehouse
        )
        before = inventory.quantity

        response = self.client.post(
            self.url, {}, format="json", HTTP_IDEMPOTENCY_KEY="cancel-c1"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "cancelled")
        inventory.refresh_from_db()
        self.assertEqual(inventory.quantity, before + 1)
        self.assertFalse(MpesaB2CPayout.objects.filter(order=self.order).exists())
        self.assertFalse(
            Payment.objects.filter(order=self.order, status="refunded").exists()
        )

    def test_shipped_order_rejected(self):
        """Orders already shipped cannot take the pre-shipment cancel path."""
        rejected = _delivered_cod_order(self.variant, user=self.customer)
        url = reverse("api:returns:order-pre-shipment-cancel", args=[rejected.pk])
        response = self.client.post(
            url, {}, format="json", HTTP_IDEMPOTENCY_KEY="cancel-s1"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cancellation_requires_idempotency_key(self):
        """The cancellation endpoint rejects a request without a key."""
        Order.objects.filter(pk=self.order.pk).update(payment_method="mpesa")
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_non_staff_rejected(self):
        """A customer cannot cancel an order pre-shipment."""
        self.client.credentials()
        _make_user(email="other@example.com", username="other")
        _login(self.client, email="other@example.com")
        response = self.client.post(
            self.url, {}, format="json", HTTP_IDEMPOTENCY_KEY="cancel-x1"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_cancel_returns_503_when_provider_unavailable(self):
        """A Daraja outage leaves the order confirmed and fully restocked."""
        Order.objects.filter(pk=self.order.pk).update(payment_method="mpesa")
        inventory = Inventory.objects.get(
            variant=self.variant, warehouse=self.warehouse
        )
        before = inventory.quantity
        with mock.patch(
            "apps.payments.services.initiate_b2c_refund",
            side_effect=DarajaError("provider down"),
        ):
            response = self.client.post(
                self.url, {}, format="json", HTTP_IDEMPOTENCY_KEY="cancel-503"
            )
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "confirmed")
        inventory.refresh_from_db()
        self.assertEqual(inventory.quantity, before)
        self.assertFalse(MpesaB2CPayout.objects.filter(order=self.order).exists())


class SerializedReturnRestockTests(APITestCase):
    """Exercises serialized-unit restocking through the return lifecycle."""

    def setUp(self):
        cache.clear()
        _, self.variant = _make_product(tracks_serial_numbers=True)
        from apps.inventory.models import Warehouse

        self.warehouse, _ = Warehouse.objects.get_or_create(name="Main")
        receive_serial_units(
            variant=self.variant,
            warehouse=self.warehouse,
            serial_numbers=["SER-1", "SER-2", "SER-3"],
        )
        self.customer = _make_user()
        self.order = _delivered_cod_order(self.variant, user=self.customer)

    def test_receive_moves_sold_units_to_returned_and_keeps_ledger_in_step(self):
        """Serialized units restock to ``returned`` without count drift."""
        sold_units = SerialUnit.objects.filter(variant=self.variant, status="sold")
        self.assertEqual(sold_units.count(), 1)
        self.assertTrue(all(u.order_item_id is not None for u in sold_units))

        return_request = create_return_request(
            order=self.order, reason="faulty", user=self.customer
        )
        approve_return_request(
            return_request=return_request,
            refund_method="card_reversal",
            user=None,
        )
        record_item_received(return_request=return_request, user=None)

        self.assertEqual(
            SerialUnit.objects.filter(variant=self.variant, status="returned").count(),
            1,
        )
        inventory = Inventory.objects.get(
            variant=self.variant, warehouse=self.warehouse
        )
        self.assertEqual(SerialUnit.objects.filter(status="sold").count(), 0)
        # The sold unit already left the sellable count on fulfilment; moving it
        # to ``returned`` leaves the count untouched (restored only when the
        # unit is inspected and set back to ``in_stock``).
        self.assertEqual(inventory.quantity, 2)
