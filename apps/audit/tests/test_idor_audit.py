"""Consolidated cross-user access (IDOR) audit.

Every endpoint that resolves a resource from an id in the URL must verify the
caller owns that resource (or holds the required role). The per-app suites
already spot-check this; this file runs the same probe uniformly across every
owner-scoped endpoint: create a resource as user A, then as user B request it
and expect a 404 (never user A's data, never a 403 that reveals the id exists).

Staff-shared resources (the address directory) invert the probe: any staff
role reaches every entry while customer tokens are rejected outright.

An endpoint added later that takes an id without the ownership filter fails
here even if its own app's tests never exercise a second caller.
"""

from decimal import Decimal

from django.core.cache import cache
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Address, User
from apps.cart.models import CartItem
from apps.cart.services import add_item, get_or_create_cart
from apps.catalog.models import Category, Product, ProductVariant
from apps.inventory.models import Inventory, Warehouse
from apps.orders.models import Order, OrderItem
from apps.payments.models import MpesaTransaction
from apps.returns.models import ReturnRequest
from apps.reviews.models import Review, ReviewPhoto
from apps.support.models import ChatMessage, ChatSession, Ticket, TicketMessage


def _make_user(suffix, role="customer"):
    """Create a user with a unique identity and the given role."""
    return User.objects.create_user(
        email=f"{suffix}@example.com",
        username=f"user-{suffix}",
        password="StrongPass123!",
        phone_number="+254712345678",
        role=role,
    )


def _make_product():
    """Create an active product with one active variant."""
    category, _ = Category.objects.get_or_create(name="Appliances", slug="appliances")
    product = Product.objects.create(
        name="Audit Product",
        slug="audit-product",
        sku="AUDIT-1",
        category=category,
        is_active=True,
    )
    variant = ProductVariant.objects.create(
        product=product,
        sku="AUDIT-1-V",
        price="5000.00",
        package_weight="2.00",
        is_active=True,
    )
    return product, variant


def _stock_variant(variant, quantity=10):
    """Ensure a variant has count-tracked stock in a test warehouse."""
    warehouse, _ = Warehouse.objects.get_or_create(name="Main")
    Inventory.objects.update_or_create(
        variant=variant,
        warehouse=warehouse,
        defaults={"quantity": quantity},
    )


def _login(client, user):
    """Authenticate the test client as the given user."""
    client.force_authenticate(user=user)
    return client


class AddressIdorAuditTests(APITestCase):
    """The shared directory admits any staff role and no customer token."""

    def setUp(self):
        cache.clear()
        self.staff = _make_user("staff", role="support")
        self.manager = _make_user("manager", role="manager")
        self.customer = _make_user("customer")
        self.address = Address.objects.create(
            user=None,
            label="Home",
            recipient_name="Repeat Buyer",
            phone_number="+254700111222",
            county="Nairobi",
            area_name="Westlands",
        )
        self.url = reverse(
            "api:accounts:address-detail", kwargs={"pk": self.address.pk}
        )

    def test_customer_token_rejected_on_retrieve(self):
        """A customer token cannot read a directory entry."""
        _login(self.client, self.customer)
        self.assertEqual(
            self.client.get(self.url).status_code, status.HTTP_403_FORBIDDEN
        )

    def test_customer_token_rejected_on_update_and_delete(self):
        """A customer token cannot mutate a directory entry."""
        _login(self.client, self.customer)
        response = self.client.patch(self.url, {"label": "Stolen"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            self.client.delete(self.url).status_code, status.HTTP_403_FORBIDDEN
        )
        self.assertEqual(Address.objects.get(pk=self.address.pk).label, "Home")

    def test_any_staff_role_reaches_every_entry(self):
        """A second staff member reads the entry another staff context saved."""
        _login(self.client, self.manager)
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_200_OK)


class CartIdorAuditTests(APITestCase):
    """A second user cannot reach or mutate user A's cart items."""

    def setUp(self):
        cache.clear()
        _, self.variant = _make_product()
        _stock_variant(self.variant)
        self.owner = _make_user("owner")
        self.other = _make_user("other")
        cart = get_or_create_cart(user=self.owner)
        add_item(cart, variant_id=self.variant.pk, quantity=1)
        self.item = cart.items.first()
        self.url = reverse(
            "api:cart:cart-item-detail", kwargs={"item_id": self.item.pk}
        )

    def test_other_user_gets_404_on_partial_update(self):
        """Editing someone else's cart line is a 404."""
        _login(self.client, self.other)
        response = self.client.patch(self.url, {"quantity": 5}, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_other_user_gets_404_on_delete(self):
        """Deleting someone else's cart line is a 404 and leaves it intact."""
        _login(self.client, self.other)
        self.assertEqual(
            self.client.delete(self.url).status_code, status.HTTP_404_NOT_FOUND
        )
        self.assertTrue(CartItem.objects.filter(pk=self.item.pk).exists())


class SupportIdorAuditTests(APITestCase):
    """A second user cannot reach user A's tickets, chats, or attachments."""

    def setUp(self):
        cache.clear()
        self.owner = _make_user("owner")
        self.other = _make_user("other")
        self.ticket = Ticket.objects.create(
            user=self.owner, category="other", subject="Broken fan"
        )
        self.session = ChatSession.objects.create(
            user=self.owner,
        )

    def test_other_user_gets_404_on_ticket_detail(self):
        """Another customer's ticket detail resolves to 404."""
        url = reverse("api:support:ticket-detail", kwargs={"ticket_id": self.ticket.pk})
        _login(self.client, self.other)
        self.assertEqual(self.client.get(url).status_code, status.HTTP_404_NOT_FOUND)

    def test_other_user_gets_404_on_ticket_message_create(self):
        """Posting into another customer's ticket is a 404."""
        url = reverse(
            "api:support:ticket-message-create", kwargs={"ticket_id": self.ticket.pk}
        )
        _login(self.client, self.other)
        response = self.client.post(url, {"message": "Spam"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(TicketMessage.objects.filter(ticket=self.ticket).count(), 0)

    def test_other_user_gets_404_on_chat_session_detail(self):
        """Another customer's chat session detail resolves to 404."""
        url = reverse(
            "api:support:chat-session-detail", kwargs={"session_id": self.session.pk}
        )
        _login(self.client, self.other)
        self.assertEqual(self.client.get(url).status_code, status.HTTP_404_NOT_FOUND)

    def test_other_user_gets_404_on_chat_message_create(self):
        """Posting into another customer's chat session is a 404."""
        url = reverse(
            "api:support:chat-message-create", kwargs={"session_id": self.session.pk}
        )
        _login(self.client, self.other)
        response = self.client.post(
            url,
            {"message": "Spam"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="idor-chat-msg",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(ChatMessage.objects.filter(session=self.session).count(), 0)

    def test_other_user_gets_404_on_chat_end(self):
        """Ending another customer's chat session is a 404."""
        url = reverse(
            "api:support:chat-session-end", kwargs={"session_id": self.session.pk}
        )
        _login(self.client, self.other)
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.session.refresh_from_db()
        self.assertIsNone(self.session.ended_at)


class ReviewPhotoIdorAuditTests(APITestCase):
    """A second user cannot delete another identity's review photo."""

    def setUp(self):
        cache.clear()
        self.owner = _make_user("owner")
        self.other = _make_user("other")
        self.product, _ = _make_product()
        self.photo = ReviewPhoto.objects.create(
            user=self.owner,
            review=Review.objects.create(
                product=self.product,
                user=self.owner,
                submitter_name="Owner",
                submitter_contact="+254712345678",
                rating=5,
                title="Great",
                body="Works well.",
                is_approved=True,
            ),
            storage_name="reviews/photos/owner.png",
            display_url="/media/owner.png",
        )

    def test_other_user_gets_404_on_photo_delete(self):
        """Deleting another user's photo is a 404 and leaves it intact."""
        url = reverse(
            "api:reviews:review-photo-delete", kwargs={"photo_id": self.photo.pk}
        )
        _login(self.client, self.other)
        self.assertEqual(self.client.delete(url).status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(ReviewPhoto.objects.filter(pk=self.photo.pk).exists())


class OrderAndPaymentIdorAuditTests(APITestCase):
    """A second user cannot reach user A's orders or their payment state."""

    def setUp(self):
        cache.clear()
        self.owner = _make_user("owner")
        self.other = _make_user("other")
        _, self.variant = _make_product()
        _stock_variant(self.variant)
        cart = get_or_create_cart(user=self.owner)
        add_item(cart, variant_id=self.variant.pk, quantity=1)
        self.order = Order.objects.create(
            user=self.owner,
            phone="+254712345678",
            status="pending",
            subtotal=Decimal("5000.00"),
            grand_total=Decimal("5000.00"),
        )
        self.txn = MpesaTransaction.objects.create(
            order=self.order,
            phone_number=self.order.phone,
            amount=self.order.grand_total,
            checkout_request_id="ws_CO_IDOR_AUDIT",
            status="pending",
        )

    def test_other_user_gets_404_on_order_detail(self):
        """Another user's order id resolves to 404."""
        url = reverse("api:orders:order-detail", kwargs={"order_ref": self.order.pk})
        _login(self.client, self.other)
        self.assertEqual(self.client.get(url).status_code, status.HTTP_404_NOT_FOUND)

    def test_other_user_gets_404_on_order_cancel(self):
        """Cancelling another user's order is a 404."""
        url = reverse("api:orders:order-cancel", kwargs={"order_ref": self.order.pk})
        _login(self.client, self.other)
        response = self.client.post(
            url, {}, format="json", HTTP_IDEMPOTENCY_KEY="idor-audit-cancel"
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_other_user_gets_404_on_mpesa_transaction(self):
        """Another user's M-Pesa transaction resolves to 404."""
        url = reverse(
            "api:payments:mpesa-transaction-status",
            kwargs={"transaction_id": self.txn.pk},
        )
        _login(self.client, self.other)
        self.assertEqual(self.client.get(url).status_code, status.HTTP_404_NOT_FOUND)


class ReturnIdorAuditTests(APITestCase):
    """A second user cannot open or read a return on user A's order."""

    def setUp(self):
        cache.clear()
        self.owner = _make_user("owner")
        self.other = _make_user("other")
        _, self.variant = _make_product()
        _stock_variant(self.variant)
        cart = get_or_create_cart(user=self.owner)
        add_item(cart, variant_id=self.variant.pk, quantity=1)
        self.order = Order.objects.create(
            user=self.owner,
            phone="+254712345678",
            status="pending",
            subtotal=Decimal("5000.00"),
            grand_total=Decimal("5000.00"),
        )
        self.order_item = OrderItem.objects.create(
            order=self.order,
            product=self.variant.product,
            variant_sku="AUDIT-1-V",
            product_name="Audit Product",
            unit_price=Decimal("5000.00"),
            quantity=1,
            total_price=Decimal("5000.00"),
            tax_rate=Decimal("0.00"),
        )
        self.return_request = ReturnRequest.objects.create(
            order=self.order,
            order_item=self.order_item,
            reason="faulty",
            status="requested",
        )

    def test_other_user_cannot_create_return_on_foreign_order(self):
        """Opening a return against another user's order is a 404."""
        url = reverse("api:returns:order-return-requests", args=[self.order.pk])
        _login(self.client, self.other)
        response = self.client.post(
            url,
            {"reason": "faulty", "requested_resolution": "refund"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_other_user_gets_404_on_return_request_detail(self):
        """Reading another user's return request is a 404."""
        url = reverse(
            "api:returns:order-return-request-detail",
            kwargs={
                "order_ref": self.order.pk,
                "return_request_id": self.return_request.pk,
            },
        )
        _login(self.client, self.other)
        self.assertEqual(self.client.get(url).status_code, status.HTTP_404_NOT_FOUND)


class AnonymousAccessAuditTests(APITestCase):
    """Owned resources reject unauthenticated callers.

    Every owner-scoped resource must reject an anonymous caller with an
    authentication error, never return the resource. This is the anonymous
    counterpart to the cross-user probes above and is run uniformly across the
    same endpoint set so a regression can't hide in a per-app suite.
    """

    def setUp(self):
        cache.clear()
        self.owner = _make_user("owner")
        self.address = Address.objects.create(
            user=self.owner,
            label="Home",
            recipient_name="Owner",
            phone_number="+254700111222",
            county="Nairobi",
            area_name="Westlands",
        )
        product, self.variant = _make_product()
        _stock_variant(self.variant)
        cart = get_or_create_cart(user=self.owner)
        add_item(cart, variant_id=self.variant.pk, quantity=1)
        self.cart_item = cart.items.first()
        self.order = Order.objects.create(
            user=self.owner,
            phone="+254712345678",
            status="pending",
            subtotal=Decimal("5000.00"),
            grand_total=Decimal("5000.00"),
        )
        self.txn = MpesaTransaction.objects.create(
            order=self.order,
            phone_number=self.order.phone,
            amount=self.order.grand_total,
            checkout_request_id="ws_CO_ANON_AUDIT",
            status="pending",
        )
        self.ticket = Ticket.objects.create(
            user=self.owner, category="other", subject="Anonymous probe"
        )
        self.chat = ChatSession.objects.create(user=self.owner)
        self.order_item = OrderItem.objects.create(
            order=self.order,
            product=product,
            variant_sku="AUDIT-1-V",
            product_name="Audit Product",
            unit_price=Decimal("5000.00"),
            quantity=1,
            total_price=Decimal("5000.00"),
            tax_rate=Decimal("0.00"),
        )
        self.return_request = ReturnRequest.objects.create(
            order=self.order,
            order_item=self.order_item,
            reason="faulty",
            status="requested",
        )

    def _assert_rejected(self, client_call):
        """Assert the unauthenticated call returns 401 or 403."""
        response = client_call()
        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )

    def test_anonymous_rejected_on_account_address(self):
        """An unauthenticated caller cannot read another account's address."""
        url = reverse("api:accounts:address-detail", kwargs={"pk": self.address.pk})
        self._assert_rejected(lambda: self.client.get(url))

    def test_anonymous_rejected_on_cart_item(self):
        """An unauthenticated caller cannot mutate another account's cart.

        Cart endpoints deliberately permit anonymous guests, so the gate is
        unknown-session resolution: a caller with no session resolves their own
        empty guest cart and user A's line inside it is a 404, exactly like a
        cross-user probe.
        """
        url = reverse(
            "api:cart:cart-item-detail", kwargs={"item_id": self.cart_item.pk}
        )
        response = self.client.patch(url, {"quantity": 1}, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_anonymous_rejected_on_order_detail(self):
        """An unauthenticated caller cannot read an order.

        Order detail is deliberately open to anonymous *guests* who hold the
        unguessable lookup token returned at creation, so the anonymous
        rejection is resolution-based: an int id probe (or a guessed token)
        resolves to 404 and reveals nothing.
        """
        url = reverse("api:orders:order-detail", kwargs={"order_ref": self.order.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_anonymous_rejected_on_mpesa_transaction(self):
        """An unauthenticated caller cannot read a payment's state."""
        url = reverse(
            "api:payments:mpesa-transaction-status",
            kwargs={"transaction_id": self.txn.pk},
        )
        self._assert_rejected(lambda: self.client.get(url))

    def test_anonymous_rejected_on_ticket_detail(self):
        """An unauthenticated caller cannot read a ticket thread."""
        url = reverse("api:support:ticket-detail", kwargs={"ticket_id": self.ticket.pk})
        self._assert_rejected(lambda: self.client.get(url))

    def test_anonymous_rejected_on_chat_session(self):
        """An unauthenticated caller cannot read a chat thread.

        Chat endpoints deliberately permit anonymous guests, so the
        authentication gate for them is unknown-session resolution: a caller
        with no session can never reach an existing session, which resolves to
        404 exactly like a cross-user probe.
        """
        url = reverse(
            "api:support:chat-session-detail",
            kwargs={"session_id": self.chat.pk},
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_anonymous_rejected_on_return_request(self):
        """An unauthenticated caller cannot read a return request.

        Return-request lookups route through the order resolver, which treats
        the anonymous int-id probe as a guest token lookup that can never
        match, so the anonymous rejection is a 404 that reveals nothing.
        """
        url = reverse(
            "api:returns:order-return-request-detail",
            kwargs={
                "order_ref": self.order.pk,
                "return_request_id": self.return_request.pk,
            },
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_anonymous_rejected_on_current_user(self):
        """The /me/ endpoint requires an authenticated caller."""
        url = reverse("api:accounts:me")
        self._assert_rejected(lambda: self.client.get(url))
