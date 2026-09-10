"""Tests for the reviews app.

Covers the anonymous storefront review and Q&A lifecycle: submissions start
hidden pending staff approval, rating aggregation on approval, one review per
submitter contact per product, verified-purchase proof by matching the
submitter's contact against the order's phone/email, text sanitisation, the
feature toggle, the access-control matrix (fully public reads and writes,
manager/support-only moderation), and the photo lifecycle: session-bound
uploads, claim-once, per-review and per-identity caps, file cleanup on every
deletion path, and the orphan sweep.
"""

from datetime import timedelta
from decimal import Decimal
from io import BytesIO
from unittest import mock

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.cart.services import add_item, get_or_create_cart
from apps.catalog.models import Category, Product, ProductVariant
from apps.core.models import SiteConfig
from apps.orders.services import (
    confirm_order_from_verification,
    create_order_from_cart,
)
from apps.reviews.cache import invalidate_feature_enabled
from apps.reviews.models import (
    ProductAnswer,
    ProductQuestion,
    Review,
    ReviewPhoto,
)
from apps.reviews.services import (
    create_product_question,
    create_review,
    set_review_approval,
)
from apps.reviews.tasks import cleanup_orphan_review_photos
from apps.shipping.models import DeliveryZone

_SEQ = [0]

SUBMITTER = {
    "submitter_name": "Jane Buyer",
    "submitter_contact": "+254712345678",
}


def _make_user(email="buyer@example.com", username="buyer", **kwargs):
    """Create a user for tests (staff login or legacy attribution)."""
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
    """Create an active product with a default active variant."""
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


def _delivery_zone():
    """Return a reusable active delivery zone for physical orders."""
    zone, _ = DeliveryZone.objects.get_or_create(
        county="Nairobi",
        area_name="Westlands",
        defaults={"base_fee": "200.00", "per_kg_rate": "50.00"},
    )
    return zone


def _place_cod_order(variant, phone="+254712345678", quantity=1):
    """Place a guest COD order for a stocked variant and confirm it."""
    _stock_variant(variant)
    _SEQ[0] += 1
    cart = get_or_create_cart(session_key=f"review-cart-{_SEQ[0]}")
    add_item(cart, variant_id=variant.pk, quantity=quantity)
    order = create_order_from_cart(
        cart=cart,
        phone=phone,
        payment_method="cod",
        delivery_zone_id=_delivery_zone().pk,
    )
    return confirm_order_from_verification(order)


def _stock_variant(variant, quantity=20):
    """Ensure a count-tracked variant has stock in a test warehouse."""
    from apps.inventory.models import Inventory, Warehouse

    warehouse, _ = Warehouse.objects.get_or_create(name="Main")
    Inventory.objects.update_or_create(
        variant=variant,
        warehouse=warehouse,
        defaults={"quantity": quantity, "reserved": 0},
    )
    return warehouse


def _tiny_png():
    """Return the bytes of a minimal valid 1x1 PNG image, rewound for reading."""
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGBA", (1, 1), (255, 0, 0, 255)).save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


def _make_unattached_photo(name, user=None, session_key="test-session"):
    """Create an uploaded-but-unclaimed photo row for the given identity."""
    return ReviewPhoto.objects.create(
        user=user,
        session_key="" if user is not None else session_key,
        storage_name=f"reviews/photos/{name}",
        display_url=f"/media/{name}",
    )


class ReviewRatingServiceTests(APITestCase):
    """Exercises anonymous submission, approval gating, and verification."""

    def setUp(self):
        cache.clear()
        self.product, self.variant = _make_product()
        _stock_variant(self.variant)

    def test_new_review_starts_hidden(self):
        """A fresh submission is unapproved and moves no aggregate."""
        review = create_review(
            product=self.product, rating=5, body="Lovely.", **SUBMITTER
        )
        self.assertFalse(review.is_approved)
        self.product.refresh_from_db()
        self.assertEqual(self.product.average_rating, Decimal("0.00"))
        self.assertEqual(self.product.review_count, 0)

    def test_approval_recomputes_product_rating(self):
        """Approving reviews moves the product's average and count."""
        first = create_review(product=self.product, rating=5, **SUBMITTER)
        second = create_review(
            product=self.product,
            rating=3,
            submitter_name="John",
            submitter_contact="+254700000001",
        )
        set_review_approval(review=first, approved=True)
        set_review_approval(review=second, approved=True)
        self.product.refresh_from_db()
        self.assertEqual(self.product.average_rating, Decimal("4.00"))
        self.assertEqual(self.product.review_count, 2)

    def test_rejected_reviews_do_not_count_towards_rating(self):
        """A hidden review leaves the product aggregate untouched."""
        review = create_review(product=self.product, rating=1, **SUBMITTER)
        self.product.refresh_from_db()
        self.assertEqual(self.product.average_rating, Decimal("0.00"))
        self.assertEqual(self.product.review_count, 0)

        set_review_approval(review=review, approved=True)
        self.product.refresh_from_db()
        self.assertEqual(self.product.average_rating, Decimal("1.00"))
        self.assertEqual(self.product.review_count, 1)

    def test_duplicate_review_per_contact_is_rejected(self):
        """A contact cannot review the same product twice."""
        create_review(product=self.product, rating=4, **SUBMITTER)
        with self.assertRaisesMessage(ValidationError, "already reviewed"):
            create_review(product=self.product, rating=5, **SUBMITTER)

    def test_different_contact_may_review_same_product(self):
        """The one-per-contact rule does not block other submitters."""
        create_review(product=self.product, rating=4, **SUBMITTER)
        review = create_review(
            product=self.product,
            rating=5,
            submitter_name="John",
            submitter_contact="+254700000001",
        )
        self.assertIsNotNone(review.pk)

    def test_authenticated_duplicate_hits_unique_constraint(self):
        """The unique constraint is a second line of defence below the service."""
        user = _make_user()
        create_review(user=user, product=self.product, rating=4, **SUBMITTER)
        with self.assertRaises(IntegrityError):
            Review.objects.create(user=user, product=self.product, rating=5)

    def test_missing_name_or_contact_is_rejected(self):
        """Anonymous submissions require both a name and a contact."""
        with self.assertRaises(ValidationError):
            create_review(
                product=self.product, rating=4, submitter_contact="+254712345678"
            )
        with self.assertRaises(ValidationError):
            create_review(product=self.product, rating=4, submitter_name="Jane")
        self.assertEqual(Review.objects.count(), 0)

    def test_free_form_text_is_sanitised(self):
        """Markup and event handlers are stripped from review text before storage."""
        review = create_review(
            product=self.product,
            rating=5,
            title="<script>alert(1)</script>Great",
            body="Lovely <img src=x onerror=alert(1)> kettle.",
            **SUBMITTER,
        )
        self.assertEqual(review.title, "alert(1)Great")
        self.assertNotIn("<img", review.body)
        self.assertNotIn("onerror", review.body)

    def test_rating_out_of_range_is_rejected(self):
        """Ratings outside the configured scale raise a clean validation error."""
        with self.assertRaises(ValidationError):
            create_review(product=self.product, rating=0, **SUBMITTER)
        with self.assertRaises(ValidationError):
            create_review(product=self.product, rating=6, **SUBMITTER)

    def test_verified_purchase_accepts_matching_phone(self):
        """A line from a confirmed order with the same phone verifies."""
        order = _place_cod_order(self.variant, phone="+254712345678")
        order_item = order.items.first()
        review = create_review(
            product=self.product,
            rating=4,
            order_item_id=order_item.pk,
            **SUBMITTER,
        )
        self.assertEqual(review.order_item_id, order_item.pk)

    def test_verified_purchase_rejects_contact_mismatch(self):
        """An order line for another contact never verifies a review."""
        order = _place_cod_order(self.variant, phone="+254700000009")
        with self.assertRaisesMessage(ValidationError, "does not qualify"):
            create_review(
                product=self.product,
                rating=4,
                order_item_id=order.items.first().pk,
                **SUBMITTER,
            )
        self.assertEqual(Review.objects.count(), 0)

    def test_verified_purchase_rejects_pending_order_line(self):
        """An unconfirmed order line does not prove a purchase."""
        cart = get_or_create_cart(session_key="pending-cart")
        add_item(cart, variant_id=self.variant.pk, quantity=1)
        order = create_order_from_cart(
            cart=cart,
            phone="+254712345678",
            payment_method="cod",
            delivery_zone_id=_delivery_zone().pk,
        )
        with self.assertRaisesMessage(ValidationError, "does not qualify"):
            create_review(
                product=self.product,
                rating=4,
                order_item_id=order.items.first().pk,
                **SUBMITTER,
            )

    def test_verified_purchase_rejects_wrong_product_line(self):
        """A verified line must reference the product actually reviewed."""
        other_product, other_variant = _make_product(price="8000.00")
        order = _place_cod_order(other_variant, phone="+254712345678")
        with self.assertRaisesMessage(ValidationError, "does not qualify"):
            create_review(
                product=self.product,
                rating=4,
                order_item_id=order.items.first().pk,
                **SUBMITTER,
            )
        _ = other_product

    def test_verified_purchase_line_can_be_claimed_only_once(self):
        """One purchase verifies only one review, guarded inside the service."""
        order = _place_cod_order(self.variant, phone="+254712345678", quantity=2)
        order_item = order.items.first()
        create_review(
            product=self.product,
            rating=4,
            order_item_id=order_item.pk,
            **SUBMITTER,
        )
        from apps.reviews.services import _resolve_verified_order_item

        with self.assertRaisesMessage(ValidationError, "already been used"):
            _resolve_verified_order_item(
                SUBMITTER["submitter_contact"], self.product, order_item.pk
            )


class ReviewEndpointTests(APITestCase):
    """Exercises the anonymous storefront review endpoints over HTTP."""

    def setUp(self):
        cache.clear()
        self.product, self.variant = _make_product()
        _stock_variant(self.variant)
        self.review_url = reverse(
            "api:reviews:product-reviews", kwargs={"slug": self.product.slug}
        )

    def test_anonymous_can_read_reviews(self):
        """Reading a product's approved reviews is public."""
        review = create_review(product=self.product, rating=4, **SUBMITTER)
        set_review_approval(review=review, approved=True)
        response = self.client.get(self.review_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["submitter_name"], "Jane Buyer")

    def test_anonymous_can_create_review(self):
        """Posting a review needs no account; it starts hidden."""
        response = self.client.post(
            self.review_url,
            {"rating": 4, "body": "Nice.", **SUBMITTER},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        review = Review.objects.get()
        self.assertFalse(review.is_approved)
        self.assertIsNone(review.user)
        self.assertEqual(self.client.get(self.review_url).data["count"], 0)

    def test_create_requires_name_and_contact(self):
        """A submission without identity is rejected."""
        response = self.client.post(
            self.review_url, {"rating": 4, "body": "Nice."}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Review.objects.count(), 0)

    def test_posting_review_with_verified_order_item(self):
        """A completed purchase matching the contact surfaces the badge."""
        order = _place_cod_order(self.variant, phone="+254712345678")
        response = self.client.post(
            self.review_url,
            {"rating": 5, "order_item_id": order.items.first().pk, **SUBMITTER},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["verified_purchase"])

    def test_posting_review_with_mismatched_order_item_returns_400(self):
        """Another contact's order line collapses into a plain 400, no leak."""
        order = _place_cod_order(self.variant, phone="+254700000009")
        response = self.client.post(
            self.review_url,
            {"rating": 5, "order_item_id": order.items.first().pk, **SUBMITTER},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Review.objects.count(), 0)

    def test_duplicate_review_via_api_returns_400(self):
        """A second review from the same contact is rejected, not duplicated."""
        create_review(product=self.product, rating=4, **SUBMITTER)
        response = self.client.post(
            self.review_url,
            {"rating": 5, "body": "Still great.", **SUBMITTER},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_inactive_product_slug_is_404(self):
        """Hidden and discontinued products are not reviewable."""
        self.product.is_active = False
        self.product.save()
        response = self.client.post(
            reverse("api:reviews:product-reviews", kwargs={"slug": self.product.slug}),
            {"rating": 4, **SUBMITTER},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_rejected_review_is_invisible_on_storefront(self):
        """Hidden reviews leave the public listing but stay in the database."""
        review = create_review(product=self.product, rating=1, **SUBMITTER)
        set_review_approval(review=review, approved=False)
        response = self.client.get(self.review_url)
        self.assertEqual(response.data["count"], 0)


class QuestionEndpointTests(APITestCase):
    """Exercises the anonymous Q&A storefront endpoints over HTTP."""

    def setUp(self):
        cache.clear()
        self.staff = _make_staff()
        self.product, _ = _make_product()
        self.question_url = reverse(
            "api:reviews:product-questions", kwargs={"slug": self.product.slug}
        )

    def test_anonymous_read_and_ask_allowed(self):
        """Questions are public to read and ask; new ones start hidden."""
        response = self.client.get(self.question_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.post(
            self.question_url,
            {"question": "Does it boil fast?", **SUBMITTER},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        question = ProductQuestion.objects.get()
        self.assertFalse(question.is_approved)
        self.assertEqual(self.client.get(self.question_url).data["count"], 0)

    def test_ask_requires_name_and_contact(self):
        """A question without identity is rejected."""
        response = self.client.post(
            self.question_url, {"question": "Does it boil fast?"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_staff_answer_approves_and_surfaces_thread(self):
        """Answering approves the question so the thread becomes visible."""
        response = self.client.post(
            self.question_url,
            {"question": "Does it boil fast?", **SUBMITTER},
            format="json",
        )
        question = ProductQuestion.objects.get()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        _login(self.client, email="staff@example.com", password="StrongPass123!")
        answer_url = reverse(
            "api:reviews:question-answers", kwargs={"question_id": question.pk}
        )
        response = self.client.post(
            answer_url, {"answer": "Yes, under three minutes."}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["answers"])
        self.assertTrue(ProductAnswer.objects.get().is_staff_answer)

        self.client.credentials()
        listing = self.client.get(self.question_url)
        self.assertEqual(listing.data["count"], 1)
        self.assertEqual(
            listing.data["results"][0]["answers"][0]["answer"],
            "Yes, under three minutes.",
        )

    def test_question_text_is_sanitised(self):
        """Raw question markup never reaches storage."""
        response = self.client.post(
            self.question_url,
            {
                "question": "<script>alert(1)</script>Does it boil fast?",
                **SUBMITTER,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        question = ProductQuestion.objects.get()
        self.assertEqual(question.question, "alert(1)Does it boil fast?")


class ModerationEndpointTests(APITestCase):
    """Exercises role-gated moderation over HTTP."""

    def setUp(self):
        cache.clear()
        self.customer = _make_user()
        self.manager = _make_staff(email="manager@example.com", role="manager")
        self.support = _make_staff(email="support@example.com", role="support")
        self.analyst = _make_staff(email="analyst@example.com", role="analyst")
        self.product, self.variant = _make_product()
        self.review = create_review(product=self.product, rating=2, **SUBMITTER)
        self.question = create_product_question(
            product=self.product, question="Is it in stock?", **SUBMITTER
        )

    def _login_as(self, email, password="StrongPass123!"):
        self.client.credentials()
        _login(self.client, email=email, password=password)

    def test_moderation_list_requires_staff_role(self):
        """Customers and analysts cannot reach the moderation inbox."""
        for email in ("buyer@example.com", "analyst@example.com"):
            self._login_as(email)
            response = self.client.get(reverse("api:reviews:review-moderation-list"))
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
            response = self.client.get(reverse("api:reviews:question-moderation-list"))
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_anonymous_cannot_reach_moderation(self):
        """An unauthenticated caller is rejected outright, not filtered."""
        response = self.client.get(reverse("api:reviews:review-moderation-list"))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        response = self.client.get(reverse("api:reviews:question-moderation-list"))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_garbage_approved_param_does_not_filter(self):
        """An unrecognised ``approved`` value leaves the inbox unfiltered."""
        create_review(
            product=self.product,
            rating=5,
            submitter_name="John",
            submitter_contact="+254700000001",
        )
        self._login_as("manager@example.com")
        response = self.client.get(
            reverse("api:reviews:review-moderation-list"),
            {"approved": "banana"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 2)

    def test_manager_and_support_can_filter_and_approve(self):
        """Manager/support can list hidden reviews and approve one back."""
        for email in ("manager@example.com", "support@example.com"):
            self._login_as(email)
            response = self.client.get(
                reverse("api:reviews:review-moderation-list"),
                {"approved": "false"},
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response.data["count"], 1)

        self._login_as("manager@example.com")
        response = self.client.post(
            reverse("api:reviews:review-approve", kwargs={"review_id": self.review.pk})
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_approved"])
        self.assertEqual(response.data["submitter_contact"], "+254712345678")
        self.product.refresh_from_db()
        self.assertEqual(self.product.review_count, 1)

    def test_customer_cannot_answer_questions(self):
        """Only manager/support tokens may post staff answers."""
        self._login_as("buyer@example.com")
        response = self.client.post(
            reverse(
                "api:reviews:question-answers",
                kwargs={"question_id": self.question.pk},
            ),
            {"answer": "Yes."},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class FeatureFlagTests(APITestCase):
    """Exercises the storefront write gate from the site configuration."""

    def setUp(self):
        cache.clear()
        self.product, _ = _make_product()
        self.review_url = reverse(
            "api:reviews:product-reviews", kwargs={"slug": self.product.slug}
        )

    def test_disabled_feature_blocks_new_writes_but_not_reads(self):
        """Turning the toggle off stops creation while published content stays."""
        response = self.client.post(
            self.review_url, {"rating": 4, "body": "Nice.", **SUBMITTER}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        review = Review.objects.get()
        set_review_approval(review=review, approved=True)

        config = SiteConfig.load()
        config.settings["enable_reviews"] = False
        config.save()
        invalidate_feature_enabled()

        response = self.client.post(
            self.review_url,
            {
                "rating": 4,
                "body": "Nice.",
                "submitter_name": "John",
                "submitter_contact": "+254700000001",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

        response = self.client.get(self.review_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)


class ReviewPhotoUploadTests(APITestCase):
    """Exercises anonymous review photo upload validation and processing."""

    def setUp(self):
        cache.clear()
        self.url = reverse("api:reviews:review-photo-upload")

    def test_anonymous_upload_binds_session(self):
        """A guest upload creates a session-bound, unclaimed photo row."""
        with (
            mock.patch(
                "apps.reviews.views.default_storage.save",
                return_value="reviews/photos/abc.png",
            ) as save_mock,
            mock.patch(
                "apps.reviews.views.generate_variants",
                return_value=[{"width": 640, "format": "webp", "url": "/media/v.webp"}],
            ),
            mock.patch(
                "apps.reviews.views.preferred_image_url",
                return_value="/media/v.webp",
            ),
        ):
            response = self.client.post(
                self.url,
                {"image": _tiny_png()},
                format="multipart",
                HTTP_IDEMPOTENCY_KEY="photo-valid-1",
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        save_mock.assert_called_once()
        photo = ReviewPhoto.objects.get()
        self.assertIsNone(photo.user)
        self.assertTrue(photo.session_key)
        self.assertIsNone(photo.review)
        self.assertEqual(response.data["id"], photo.pk)
        self.assertEqual(response.data["url"], "/media/v.webp")
        self.assertEqual(response.data["variants"][0]["format"], "webp")

    def test_authenticated_upload_binds_user(self):
        """A logged-in upload keeps the account attribution."""
        user = _make_user()
        _login(self.client)
        with (
            mock.patch(
                "apps.reviews.views.default_storage.save",
                return_value="reviews/photos/abc.png",
            ),
            mock.patch(
                "apps.reviews.views.generate_variants",
                return_value=[{"width": 640, "format": "webp", "url": "/media/v.webp"}],
            ),
            mock.patch(
                "apps.reviews.views.preferred_image_url",
                return_value="/media/v.webp",
            ),
        ):
            response = self.client.post(
                self.url,
                {"image": _tiny_png()},
                format="multipart",
                HTTP_IDEMPOTENCY_KEY="photo-valid-2",
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        photo = ReviewPhoto.objects.get()
        self.assertEqual(photo.user, user)
        self.assertEqual(photo.session_key, "")

    def test_non_image_upload_rejected(self):
        """A text file is rejected before any storage happens."""
        response = self.client.post(
            self.url,
            {"image": BytesIO(b"not an image")},
            format="multipart",
            HTTP_IDEMPOTENCY_KEY="photo-nonimage-1",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unattached_upload_bound_blocks_extra_uploads(self):
        """A session holding the cap of unclaimed photos cannot upload more."""
        upload_kwargs = {"format": "multipart", "HTTP_IDEMPOTENCY_KEY": "photo-b1"}
        with (
            mock.patch(
                "apps.reviews.views.default_storage.save",
                return_value="reviews/photos/held.png",
            ),
            mock.patch("apps.reviews.views.generate_variants", return_value=[]),
            mock.patch(
                "apps.reviews.views.preferred_image_url",
                return_value="/media/held.webp",
            ),
        ):
            first = self.client.post(self.url, {"image": _tiny_png()}, **upload_kwargs)
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        with mock.patch("apps.reviews.views.MAX_UNATTACHED_PHOTOS_PER_SESSION", 1):
            response = self.client.post(
                self.url,
                {"image": _tiny_png()},
                format="multipart",
                HTTP_IDEMPOTENCY_KEY="photo-bound-1",
            )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_disabled_feature_blocks_upload(self):
        """A disabled reviews toggle stops photo uploads with a 503."""
        config = SiteConfig.load()
        config.settings["enable_reviews"] = False
        config.save()
        invalidate_feature_enabled()
        response = self.client.post(
            self.url,
            {"image": _tiny_png()},
            format="multipart",
            HTTP_IDEMPOTENCY_KEY="photo-disabled-1",
        )
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    def test_oversized_image_rejected(self):
        """Files over the configured ceiling are refused."""
        with mock.patch("apps.reviews.views.REVIEW_PHOTO_MAX_SIZE_MB", 0):
            response = self.client.post(
                self.url,
                {"image": _tiny_png()},
                format="multipart",
                HTTP_IDEMPOTENCY_KEY="photo-oversize-1",
            )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ReviewPhotoDeleteTests(APITestCase):
    """Exercises identity-scoped deletion of unattached uploaded photos."""

    def setUp(self):
        cache.clear()
        self.owner = _make_user()
        self.upload_url = reverse("api:reviews:review-photo-upload")

    def _url(self, photo_id):
        """Return the delete endpoint for the given photo id."""
        return reverse("api:reviews:review-photo-delete", kwargs={"photo_id": photo_id})

    def _upload(self, key):
        """Upload a photo, establishing this client's guest session."""
        with (
            mock.patch(
                "apps.reviews.views.default_storage.save",
                return_value="reviews/photos/mine.png",
            ),
            mock.patch("apps.reviews.views.generate_variants", return_value=[]),
            mock.patch(
                "apps.reviews.views.preferred_image_url",
                return_value="/media/mine.webp",
            ),
        ):
            return self.client.post(
                self.upload_url,
                {"image": _tiny_png()},
                format="multipart",
                HTTP_IDEMPOTENCY_KEY=key,
            )

    def test_owner_session_can_delete_unattached_photo(self):
        """Deleting one's own session photo removes the row and its files."""
        response = self._upload("photo-del-1")
        photo_id = response.data["id"]
        with mock.patch("apps.reviews.signals.delete_image_files") as deleter:
            response = self.client.delete(self._url(photo_id))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(ReviewPhoto.objects.filter(pk=photo_id).exists())
        deleter.assert_called_once_with("reviews/photos/mine.png")

    def test_foreign_session_photo_delete_returns_404(self):
        """Another session's photo is indistinguishable from a missing one."""
        self._upload("photo-del-2")
        foreign = _make_unattached_photo("theirs.png", session_key="other-session")
        response = self.client.delete(self._url(foreign.pk))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(ReviewPhoto.objects.filter(pk=foreign.pk).exists())

    def test_missing_photo_delete_returns_404(self):
        """A nonexistent photo id is a clean 404."""
        self._upload("photo-del-3")
        response = self.client.delete(self._url(999999))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_authenticated_owner_can_delete_unattached_photo(self):
        """A logged-in owner deletes their own upload."""
        owned = _make_unattached_photo("owned.png", user=self.owner)
        _login(self.client)
        with mock.patch("apps.reviews.signals.delete_image_files") as deleter:
            response = self.client.delete(self._url(owned.pk))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(ReviewPhoto.objects.filter(pk=owned.pk).exists())
        deleter.assert_called_once_with("reviews/photos/owned.png")

    def test_attached_photo_delete_rejected(self):
        """A photo already on a review is published content and stays put."""
        upload = self._upload("photo-del-4")
        session_key = ReviewPhoto.objects.get(pk=upload.data["id"]).session_key
        product, _ = _make_product()
        review = create_review(product=product, rating=4, **SUBMITTER)
        attached = ReviewPhoto.objects.create(
            review=review,
            session_key=session_key,
            storage_name="reviews/photos/attached.png",
            display_url="/media/attached.png",
        )
        response = self.client.delete(self._url(attached.pk))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(ReviewPhoto.objects.filter(pk=attached.pk).exists())


class RatingIntegrityTests(APITestCase):
    """Exercises aggregate correctness across every deletion path."""

    def setUp(self):
        cache.clear()
        self.product, _ = _make_product()

    def test_deleting_review_recomputes_product_rating(self):
        """Deleting a review with the ORM keeps the aggregate right."""
        first = create_review(product=self.product, rating=5, **SUBMITTER)
        set_review_approval(review=first, approved=True)
        second = create_review(
            product=self.product,
            rating=3,
            submitter_name="John",
            submitter_contact="+254700000001",
        )
        set_review_approval(review=second, approved=True)
        first.delete()
        self.product.refresh_from_db()
        self.assertEqual(self.product.review_count, 1)
        self.assertEqual(self.product.average_rating, Decimal("3.00"))

    def test_user_delete_keeps_review_and_aggregate(self):
        """Losing a linked account nulls the link; the review survives."""
        user = _make_user()
        review = create_review(user=user, product=self.product, rating=5, **SUBMITTER)
        set_review_approval(review=review, approved=True)
        user.delete()
        review.refresh_from_db()
        self.assertIsNone(review.user_id)
        self.product.refresh_from_db()
        self.assertEqual(self.product.review_count, 1)
        self.assertEqual(self.product.average_rating, Decimal("5.00"))

    def test_photo_delete_removes_stored_files(self):
        """Deleting a photo row deletes its stored files."""
        photo = _make_unattached_photo("x.png")
        with mock.patch("apps.reviews.signals.delete_image_files") as deleter:
            photo.delete()
        deleter.assert_called_once_with("reviews/photos/x.png")

    def test_review_delete_cascades_photo_file_cleanup(self):
        """Deleting a review removes its photos and their files."""
        review = create_review(product=self.product, rating=4, **SUBMITTER)
        photo = ReviewPhoto.objects.create(
            review=review,
            storage_name="reviews/photos/x.png",
            display_url="/media/x.png",
        )
        with mock.patch("apps.reviews.signals.delete_image_files") as deleter:
            review.delete()
        deleter.assert_called_once_with("reviews/photos/x.png")
        self.assertFalse(ReviewPhoto.objects.filter(pk=photo.pk).exists())


class ReviewPhotoClaimTests(APITestCase):
    """Exercises session-bound photo claims over the API."""

    def setUp(self):
        cache.clear()
        self.product, self.variant = _make_product()
        _stock_variant(self.variant)
        self.other_product, _ = _make_product(price="9000.00")
        self.review_url = reverse(
            "api:reviews:product-reviews", kwargs={"slug": self.product.slug}
        )
        self.other_review_url = reverse(
            "api:reviews:product-reviews", kwargs={"slug": self.other_product.slug}
        )

    def _upload(self, key, name="claim.png"):
        """Upload a photo through the endpoint, binding it to this session."""
        url = reverse("api:reviews:review-photo-upload")
        with (
            mock.patch(
                "apps.reviews.views.default_storage.save",
                return_value=f"reviews/photos/{name}",
            ),
            mock.patch("apps.reviews.views.generate_variants", return_value=[]),
            mock.patch(
                "apps.reviews.views.preferred_image_url",
                return_value=f"/media/{name}",
            ),
        ):
            response = self.client.post(
                url,
                {"image": _tiny_png()},
                format="multipart",
                HTTP_IDEMPOTENCY_KEY=key,
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        return response.data["id"]

    def test_claiming_photos_attaches_them_to_the_review(self):
        """The session's own unclaimed upload lands on the created review."""
        photo_id = self._upload("claim-1", "one.png")
        response = self.client.post(
            self.review_url,
            {"rating": 5, "photo_ids": [photo_id], **SUBMITTER},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(len(response.data["photos"]), 1)
        self.assertEqual(response.data["photos"][0]["id"], photo_id)
        photo = ReviewPhoto.objects.get(pk=photo_id)
        self.assertEqual(photo.review_id, response.data["id"])

    def test_foreign_session_photo_cannot_be_attached(self):
        """Another session's upload is never attachable, and stays unclaimed."""
        foreign = _make_unattached_photo("theirs.png", session_key="other-session")
        response = self.client.post(
            self.review_url,
            {"rating": 5, "photo_ids": [foreign.pk], **SUBMITTER},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Review.objects.count(), 0)
        foreign.refresh_from_db()
        self.assertIsNone(foreign.review)

    def test_duplicate_photo_ids_rejected(self):
        """The same photo cannot be cited twice in one review."""
        photo_id = self._upload("claim-2", "dup.png")
        response = self.client.post(
            self.review_url,
            {"rating": 5, "photo_ids": [photo_id, photo_id], **SUBMITTER},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Review.objects.count(), 0)

    def test_too_many_photos_rejected(self):
        """The per-review photo cap is enforced at the serializer."""
        photo_ids = [self._upload(f"claim-cap-{i}", f"p{i}.png") for i in range(6)]
        response = self.client.post(
            self.review_url,
            {"rating": 5, "photo_ids": photo_ids, **SUBMITTER},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_claimed_photo_cannot_be_attached_again(self):
        """A photo used on one review cannot back a second review."""
        photo_id = self._upload("claim-3", "used.png")
        response = self.client.post(
            self.review_url,
            {"rating": 5, "photo_ids": [photo_id], **SUBMITTER},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        response = self.client.post(
            self.other_review_url,
            {
                "rating": 4,
                "photo_ids": [photo_id],
                "submitter_name": "John",
                "submitter_contact": "+254700000001",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class OrphanPhotoSweepTests(APITestCase):
    """Exercises the scheduled sweep of never-attached uploads."""

    def setUp(self):
        cache.clear()
        self.product, _ = _make_product()
        self.orphan = _make_unattached_photo("orphan.png")

    def test_sweep_removes_only_expired_orphans(self):
        """Old unattached photos are purged; attached and fresh ones survive."""
        review = create_review(product=self.product, rating=4, **SUBMITTER)
        attached = ReviewPhoto.objects.create(
            review=review,
            storage_name="reviews/photos/attached.png",
            display_url="/media/attached.png",
        )
        fresh = _make_unattached_photo("fresh.png")
        self.orphan.created_at = timezone.now() - timedelta(hours=48)
        self.orphan.save(update_fields=["created_at"])

        with mock.patch("apps.reviews.signals.delete_image_files") as deleter:
            deleted = cleanup_orphan_review_photos()
        self.assertEqual(deleted, 1)
        deleter.assert_called_once_with("reviews/photos/orphan.png")
        self.assertFalse(ReviewPhoto.objects.filter(pk=self.orphan.pk).exists())
        self.assertTrue(ReviewPhoto.objects.filter(pk=attached.pk).exists())
        self.assertTrue(ReviewPhoto.objects.filter(pk=fresh.pk).exists())

        with mock.patch("apps.reviews.signals.delete_image_files") as second_run:
            self.assertEqual(cleanup_orphan_review_photos(), 0)
        second_run.assert_not_called()
