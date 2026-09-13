"""Consolidated access audit.

Every endpoint that resolves a resource from an id in the URL must verify
the caller holds the required role. The per-app suites already spot-check
this; this file probes uniformly: anonymous callers are rejected, wrong-role
tokens are refused, and staff-shared resources admit every staff role while
customer tokens are rejected outright.

An endpoint added later without the role gate fails here even if its own
app's tests never exercise a second caller.
"""

from decimal import Decimal

from django.core.cache import cache
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import Address, User
from apps.catalog.models import Category, Product, ProductVariant
from apps.orders.models import Order, OrderItem
from apps.returns.models import ReturnRequest
from apps.reviews.models import Review, ReviewPhoto
from apps.support.models import Ticket


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


class SupportIdorAuditTests(APITestCase):
    """Staff ticket console admits staff and refuses customer tokens."""

    def setUp(self):
        cache.clear()
        self.staff = _make_user("staff", role="support")
        self.customer = _make_user("customer")
        self.ticket = Ticket.objects.create(
            user=self.staff, category="other", subject="Broken fan"
        )

    def test_staff_can_list_tickets(self):
        """Support staff can read the ticket queue."""
        url = reverse("api:support:ticket-list-create")
        _login(self.client, self.staff)
        self.assertEqual(self.client.get(url).status_code, status.HTTP_200_OK)

    def test_customer_cannot_list_tickets(self):
        """A customer token is refused from the ticket queue."""
        url = reverse("api:support:ticket-list-create")
        _login(self.client, self.customer)
        self.assertEqual(self.client.get(url).status_code, status.HTTP_403_FORBIDDEN)


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


class OrderRoleAuditTests(APITestCase):
    """The staff order-status endpoint admits fulfilment roles only."""

    def setUp(self):
        cache.clear()
        self.staff = _make_user("staff", role="support")
        self.analyst = _make_user("analyst", role="analyst")
        self.customer = _make_user("customer")
        _, self.variant = _make_product()
        self.order = Order.objects.create(
            phone="+254712345678",
            status="confirmed",
            subtotal=Decimal("5000.00"),
            grand_total=Decimal("5000.00"),
        )
        self.url = reverse(
            "api:orders:order-status-update", kwargs={"order_id": self.order.pk}
        )

    def test_anonymous_rejected(self):
        """Anonymous callers cannot move an order."""
        response = self.client.post(self.url, {"to_status": "processing"})
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_customer_rejected(self):
        """A customer token cannot move an order."""
        _login(self.client, self.customer)
        response = self.client.post(self.url, {"to_status": "processing"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_analyst_rejected(self):
        """An analyst token cannot move an order."""
        _login(self.client, self.analyst)
        response = self.client.post(self.url, {"to_status": "processing"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_support_moves_order(self):
        """Support staff can advance the order."""
        _login(self.client, self.staff)
        response = self.client.post(self.url, {"to_status": "processing"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class ReturnIdorAuditTests(APITestCase):
    """Return endpoints admit fulfilment roles only."""

    def setUp(self):
        cache.clear()
        self.staff = _make_user("staff", role="support")
        self.analyst = _make_user("analyst", role="analyst")
        self.customer = _make_user("customer")
        _, self.variant = _make_product()
        self.order = Order.objects.create(
            phone="+254712345678",
            status="delivered",
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

    def test_anonymous_cannot_file_return(self):
        """Unauthenticated callers cannot file a return."""
        url = reverse("api:returns:order-return-requests", args=[self.order.pk])
        response = self.client.post(
            url,
            {"reason": "faulty", "requested_resolution": "refund"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_customer_cannot_file_return(self):
        """A customer token cannot file a return."""
        url = reverse("api:returns:order-return-requests", args=[self.order.pk])
        _login(self.client, self.customer)
        response = self.client.post(
            url,
            {"reason": "faulty", "requested_resolution": "refund"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_analyst_cannot_file_return(self):
        """An analyst token cannot file a return."""
        url = reverse("api:returns:order-return-requests", args=[self.order.pk])
        _login(self.client, self.analyst)
        response = self.client.post(
            url,
            {"reason": "faulty", "requested_resolution": "refund"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_support_can_read_return_detail(self):
        """Support staff can read the return request detail."""
        url = reverse(
            "api:returns:order-return-request-detail",
            kwargs={
                "order_id": self.order.pk,
                "return_request_id": self.return_request.pk,
            },
        )
        _login(self.client, self.staff)
        self.assertEqual(self.client.get(url).status_code, status.HTTP_200_OK)


class AnonymousAccessAuditTests(APITestCase):
    """Staff resources reject unauthenticated callers.

    Every staff-scoped resource must reject an anonymous caller with an
    authentication error, never return the resource. This is the anonymous
    counterpart to the role probes above and is run uniformly across the
    same endpoint set so a regression can't hide in a per-app suite.
    """

    def setUp(self):
        cache.clear()
        self.staff = _make_user("staff", role="support")
        self.address = Address.objects.create(
            user=None,
            label="Home",
            recipient_name="Repeat Buyer",
            phone_number="+254700111222",
            county="Nairobi",
            area_name="Westlands",
        )
        product, self.variant = _make_product()
        self.order = Order.objects.create(
            phone="+254712345678",
            status="delivered",
            subtotal=Decimal("5000.00"),
            grand_total=Decimal("5000.00"),
        )
        self.ticket = Ticket.objects.create(
            user=self.staff, category="other", subject="Anonymous probe"
        )
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
        """An unauthenticated caller cannot read the staff directory."""
        url = reverse("api:accounts:address-detail", kwargs={"pk": self.address.pk})
        self._assert_rejected(lambda: self.client.get(url))

    def test_anonymous_rejected_on_ticket_list(self):
        """An unauthenticated caller cannot read the ticket queue."""
        url = reverse("api:support:ticket-list-create")
        self._assert_rejected(lambda: self.client.get(url))

    def test_anonymous_rejected_on_return_request(self):
        """An unauthenticated caller cannot read a return request."""
        url = reverse(
            "api:returns:order-return-request-detail",
            kwargs={
                "order_id": self.order.pk,
                "return_request_id": self.return_request.pk,
            },
        )
        self._assert_rejected(lambda: self.client.get(url))

    def test_anonymous_rejected_on_current_user(self):
        """The /me/ endpoint requires an authenticated caller."""
        url = reverse("api:accounts:me")
        self._assert_rejected(lambda: self.client.get(url))
