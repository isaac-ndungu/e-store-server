"""Tests for the returns app.

Covers the post-delivery return lifecycle with manual refunds, the
pre-shipment cancellation path, and the money invariants the service layer
upholds: server-side refund caps, audit-trail completeness, idempotent
refund recording, and the staff-only access control on every endpoint.
Refunds are arranged by staff outside the system — these tests assert the
record, never a payout integration.
"""

from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.db import IntegrityError
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.catalog.models import Category, Product, ProductVariant
from apps.orders.models import Order, OrderStatusHistory
from apps.orders.services import create_staff_order, transition_order
from apps.returns.models import ReturnRequest, ReturnRequestStatusHistory
from apps.returns.services import (
    approve_return_request,
    create_return_request,
    record_item_received,
    refund_return_request,
    reject_return_request,
)

_SEQ = [0]

REFUND_METHOD = "M-Pesa - sent manually"


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


def _place_order(variant, payment_method="cod", phone="+254712345678", quantity=1):
    """Create a confirmed intake order for a variant."""
    _SEQ[0] += 1
    staff = _make_staff(email=f"intake{_SEQ[0]}@example.com")
    return create_staff_order(
        staff_user=staff,
        phone=phone,
        lines=[{"variant_id": variant.pk, "quantity": quantity}],
        order_source="whatsapp",
        payment_method=payment_method,
    )


def _delivered_order(variant, payment_method="cod", phone="+254712345678"):
    """Create, ship, and deliver an intake order."""
    order = _place_order(variant, payment_method=payment_method, phone=phone)
    transition_order(order, "shipped")
    transition_order(order, "delivered")
    return order


class ReturnRequestModelTests(APITestCase):
    """Exercises the return request model and its constraints."""

    def setUp(self):
        cache.clear()
        _, self.variant = _make_product()
        self.order = _delivered_order(self.variant)

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
            refund_method=REFUND_METHOD,
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
    """Exercises staff filing of return requests and its validation rules."""

    def setUp(self):
        cache.clear()
        _, self.variant = _make_product()
        self.staff = _make_staff()
        self.order = _delivered_order(self.variant)
        _login(self.client, email="staff@example.com")
        self.url = reverse("api:returns:order-return-requests", args=[self.order.pk])

    def _create(self, **overrides):
        payload = {
            "order_item_id": self.order.items.first().pk,
            "reason": "faulty",
            "requested_resolution": "refund",
        }
        payload.update(overrides)
        return self.client.post(self.url, payload, format="json")

    def test_staff_files_return_for_delivered_order(self):
        """Staff can file a return request against a delivered order."""
        response = self._create()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.data
        self.assertEqual(data["status"], "requested")
        self.assertEqual(data["requested_resolution"], "refund")
        self.assertEqual(ReturnRequest.objects.count(), 1)

    def test_anonymous_filing_rejected(self):
        """Unauthenticated callers cannot file returns."""
        self.client.credentials()
        response = self._create()
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_not_delivered_rejected(self):
        """A return cannot be opened against a non-delivered order."""
        order = _place_order(self.variant)
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
        order = _delivered_order(non_returnable)
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
        _SEQ[0] += 1
        order = create_staff_order(
            staff_user=_make_staff(email=f"multi{_SEQ[0]}@example.com"),
            phone="+254712345678",
            lines=[
                {"variant_id": self.variant.pk, "quantity": 1},
                {"variant_id": variant_two.pk, "quantity": 1},
            ],
            order_source="whatsapp",
            payment_method="cod",
        )
        transition_order(order, "shipped")
        transition_order(order, "delivered")

        line_a = order.items.get(variant_sku=self.variant.sku)
        first = create_return_request(
            order=order,
            order_item_id=line_a.pk,
            reason="faulty",
            user=self.staff,
        )
        approve_return_request(
            return_request=first,
            refund_method=REFUND_METHOD,
            user=None,
        )
        record_item_received(return_request=first, user=None)
        refund_return_request(
            return_request=first, refund_note="M-Pesa sent", user=None
        )
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
            order=self.order, reason="faulty", user=self.staff
        )
        reject_return_request(return_request=rejected, user=None)
        response = self._create()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(ReturnRequest.objects.filter(order_item=order_item).count(), 2)

    def test_whole_order_return_requires_single_line_order(self):
        """A whole-order return on a multi-line order is rejected."""
        _, variant_two = _make_product(price="3000.00")
        _SEQ[0] += 1
        order = create_staff_order(
            staff_user=_make_staff(email=f"whole{_SEQ[0]}@example.com"),
            phone="+254712345678",
            lines=[
                {"variant_id": self.variant.pk, "quantity": 1},
                {"variant_id": variant_two.pk, "quantity": 1},
            ],
            order_source="whatsapp",
            payment_method="cod",
        )
        transition_order(order, "shipped")
        transition_order(order, "delivered")
        url = reverse("api:returns:order-return-requests", args=[order.pk])
        response = self.client.post(url, {"reason": "faulty"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_foreign_line_rejected(self):
        """A line that does not belong to the order is rejected."""
        _, other_variant = _make_product(price="1000.00")
        other_order = _delivered_order(other_variant)
        foreign_item = other_order.items.first()
        response = self._create(order_item_id=foreign_item.pk)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_customer_list_is_paginated(self):
        """The staff list returns a paginated envelope."""
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
    """Exercises role access control on the staff-only return endpoints."""

    def setUp(self):
        cache.clear()
        _, self.variant = _make_product()
        self.staff = _make_staff()
        self.order = _delivered_order(self.variant)
        self.return_request = create_return_request(
            order=self.order, reason="faulty", user=self.staff
        )
        _login(self.client, email="staff@example.com")

    def test_anonymous_create_rejected(self):
        """An unauthenticated caller cannot file a return."""
        self.client.credentials()
        url = reverse("api:returns:order-return-requests", args=[self.order.pk])
        response = self.client.post(url, {"reason": "faulty"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_customer_credential_cannot_log_in(self):
        """A customer credential gets no token to reach returns with."""
        _make_user(email="other@example.com", username="other")
        login = self.client.post(
            reverse("api:accounts:login"),
            {"email": "other@example.com", "password": "StrongPass123!"},
            format="json",
        )
        self.assertEqual(login.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_can_read_detail(self):
        """Staff can read the return request detail with its audit trail."""
        url = reverse(
            "api:returns:order-return-request-detail",
            args=[self.order.pk, self.return_request.pk],
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_anonymous_staff_listing_rejected(self):
        """An anonymous caller cannot list return requests as staff."""
        self.client.credentials()
        response = self.client.get(reverse("api:returns:return-request-staff-list"))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_staff_without_role_rejected_from_staff_actions(self):
        """An analyst without the manager/support role is denied."""
        _make_staff(email="analyst@example.com", role="analyst")
        _login(self.client, email="analyst@example.com")
        response = self.client.post(
            reverse(
                "api:returns:return-request-approve", args=[self.return_request.pk]
            ),
            {"refund_method": REFUND_METHOD},
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
            {"refund_method": REFUND_METHOD},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_missing_return_request_is_404(self):
        """A staff action on a missing request is a 404."""
        _login(self.client, email="staff@example.com")
        response = self.client.post(
            reverse("api:returns:return-request-approve", args=[99999]),
            {"refund_method": REFUND_METHOD},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class ReturnRefundFlowTests(APITestCase):
    """Exercises the manual refund lifecycle: approve, receive, record."""

    def setUp(self):
        cache.clear()
        _, self.variant = _make_product()
        self.customer = _make_user()
        self.order = _delivered_order(self.variant)
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

    def _approve_and_receive(self):
        """Approve with a manual method and record receipt of the goods."""
        approve = self.client.post(
            self.approve_url, {"refund_method": REFUND_METHOD}, format="json"
        )
        self.assertEqual(approve.status_code, status.HTTP_200_OK)
        receive = self.client.post(self.staff_urls["receive"], format="json")
        self.assertEqual(receive.status_code, status.HTTP_200_OK)
        return Decimal(approve.data["refund_amount"])

    def test_full_manual_return_flow_to_refunded(self):
        """Approve, receive, record the hand-sent refund; order follows."""
        refund_amount = self._approve_and_receive()
        refund = self.client.post(
            self.staff_urls["refund"],
            {"refund_note": "M-Pesa sent, txn ABC123"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="refund-manual-1",
        )
        self.assertEqual(refund.status_code, status.HTTP_200_OK)
        self.assertEqual(refund.data["status"], "refunded")
        self.assertIsNotNone(refund.data["resolved_at"])
        self.return_request.refresh_from_db()
        self.assertEqual(self.return_request.status, "refunded")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "refunded")
        self.assertEqual(self.order.refund_amount, refund_amount)
        self.assertEqual(self.order.refund_note, "M-Pesa sent, txn ABC123")

    def test_refund_recording_is_idempotent(self):
        """Recording the same refund twice keeps one audit row, not two."""
        self._approve_and_receive()
        payload = {"refund_note": "M-Pesa sent, txn ABC123"}
        self.client.post(
            self.staff_urls["refund"],
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY="refund-manual-2",
        )
        refund_return_request(
            return_request=self.return_request,
            refund_note="M-Pesa sent, txn ABC123",
            user=None,
        )
        self.return_request.refresh_from_db()
        self.assertEqual(self.return_request.status, "refunded")
        self.assertEqual(
            ReturnRequestStatusHistory.objects.filter(to_status="refunded").count(),
            1,
        )

    def test_refund_requires_idempotency_key(self):
        """The refund endpoint rejects a request without an Idempotency-Key."""
        self._approve_and_receive()
        response = self.client.post(
            self.staff_urls["refund"],
            {"refund_note": "M-Pesa sent"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_refund_replays_stored_response_for_duplicate_key(self):
        """Reusing an Idempotency-Key replays the stored response."""
        self._approve_and_receive()
        payload = {"refund_note": "M-Pesa sent, txn ABC123"}
        first = self.client.post(
            self.staff_urls["refund"],
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY="refund-manual-dupe",
        )
        second = self.client.post(
            self.staff_urls["refund"],
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY="refund-manual-dupe",
        )
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertEqual(second.data["status"], "refunded")

    def test_refund_before_receipt_rejected(self):
        """Recording a refund without receiving the goods is rejected."""
        approve = self.client.post(
            self.approve_url, {"refund_method": REFUND_METHOD}, format="json"
        )
        self.assertEqual(approve.status_code, status.HTTP_200_OK)
        response = self.client.post(
            self.staff_urls["refund"],
            {"refund_note": "M-Pesa sent"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="refund-early",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_refund_without_note_rejected(self):
        """A refund record without a note says nothing and is rejected."""
        self._approve_and_receive()
        response = self.client.post(
            self.staff_urls["refund"],
            {"refund_note": "   "},
            format="json",
            HTTP_IDEMPOTENCY_KEY="refund-nonote",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.return_request.refresh_from_db()
        self.assertEqual(self.return_request.status, "item_received")

    def test_blank_refund_method_rejected_at_approval(self):
        """Approval without naming how the money goes back is rejected."""
        response = self.client.post(
            self.approve_url, {"refund_method": ""}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ReturnApprovalMoneyTests(APITestCase):
    """Exercises the server-side refund economics at approval time."""

    def setUp(self):
        cache.clear()
        _, self.variant = _make_product(price="5000.00")
        self.customer = _make_user()
        self.order = _delivered_order(self.variant)
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
            self.url, {"refund_method": REFUND_METHOD}, format="json"
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
                "refund_method": REFUND_METHOD,
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
            self.url, {"refund_method": REFUND_METHOD}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Decimal(response.data["refund_amount"]), base - expected_fee)
        self.assertEqual(Decimal(response.data["restocking_fee_applied"]), expected_fee)

    def test_negative_staff_amount_rejected(self):
        """A negative explicit amount is rejected at approval."""
        response = self.client.post(
            self.url,
            {"refund_method": REFUND_METHOD, "refund_amount": "-5.00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_replacement_resolution_rejected_at_approval(self):
        """A replacement resolution is rejected until fulfillment accounting exists."""
        _, variant_two = _make_product(price="3000.00")
        replacement_order = _delivered_order(variant_two)
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
                "refund_method": REFUND_METHOD,
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
                "refund_method": REFUND_METHOD,
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
        self.customer = _make_user()
        self.order = _delivered_order(self.variant)
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
        self.client.post(approve_url, {"refund_method": REFUND_METHOD}, format="json")
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
        self.client.post(approve_url, {"refund_method": REFUND_METHOD}, format="json")
        receive_url = reverse(
            "api:returns:return-request-receive-item", args=[self.return_request.pk]
        )
        self.client.post(receive_url, format="json")
        refund_url = reverse(
            "api:returns:return-request-refund", args=[self.return_request.pk]
        )
        self.client.post(
            refund_url,
            {"refund_note": "M-Pesa sent"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="close-key",
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
        self.customer = _make_user()
        self.order = _place_order(self.variant)
        self.staff = _make_staff()
        _login(self.client, email="staff@example.com")
        self.url = reverse(
            "api:returns:order-pre-shipment-cancel", args=[self.order.pk]
        )

    def _cancel(self, payload, key):
        """POST a cancellation with an idempotency key."""
        return self.client.post(
            self.url, payload, format="json", HTTP_IDEMPOTENCY_KEY=key
        )

    def test_cancel_with_note_cancels_and_records_history(self):
        """A confirmed order cancels with a required note and audit row."""
        response = self._cancel(
            {"note": "Customer asked to cancel; refunded via M-Pesa by hand."},
            key="cancel-m1",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "cancelled")
        self.assertTrue(
            OrderStatusHistory.objects.filter(
                order=self.order, to_status="cancelled"
            ).exists()
        )

    def test_cancel_records_manual_refund(self):
        """Refund details land on the order for reporting."""
        response = self._cancel(
            {
                "note": "Customer asked to cancel.",
                "refund_note": "M-Pesa sent, txn XYZ789",
                "refund_amount": "5800.00",
            },
            key="cancel-refund-1",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "cancelled")
        self.assertEqual(self.order.refund_amount, Decimal("5800.00"))
        self.assertEqual(self.order.refund_note, "M-Pesa sent, txn XYZ789")

    def test_cancel_requires_note(self):
        """Cancelling without saying what happened is rejected."""
        response = self._cancel({}, key="cancel-nonote")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "confirmed")

    def test_cancel_rejects_negative_refund_amount(self):
        """A negative refund amount is rejected and cancels nothing."""
        response = self._cancel(
            {"note": "Customer asked to cancel.", "refund_amount": "-5.00"},
            key="cancel-neg",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "confirmed")

    def test_shipped_order_rejected(self):
        """Orders already shipped cannot take the pre-shipment cancel path."""
        rejected = _delivered_order(self.variant)
        url = reverse("api:returns:order-pre-shipment-cancel", args=[rejected.pk])
        response = self.client.post(
            url,
            {"note": "Too late to cancel."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="cancel-s1",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cancellation_requires_idempotency_key(self):
        """The cancellation endpoint rejects a request without a key."""
        response = self.client.post(
            self.url, {"note": "Customer asked to cancel."}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_non_staff_rejected(self):
        """A customer credential gets no token to cancel with."""
        self.client.credentials()
        _make_user(email="other@example.com", username="other")
        login = self.client.post(
            reverse("api:accounts:login"),
            {"email": "other@example.com", "password": "StrongPass123!"},
            format="json",
        )
        self.assertEqual(login.status_code, status.HTTP_403_FORBIDDEN)
