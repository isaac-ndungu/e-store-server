"""Tests for the reviews app.

Covers the storefront review and Q&A lifecycle: rating aggregation on create
and moderation, verified-purchase proof against the caller's own completed
orders, single-review-per-buyer enforcement, text sanitisation, the feature
toggle, the access-control matrix (anonymous read vs. write, customer vs.
manager/support/analyst on moderation paths, photo-upload validation), and the
photo lifecycle: ownership, claim-once, per-review and per-user caps, file
cleanup on every deletion path (including owner-only self-delete of unattached
uploads), and the orphan sweep.
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


def _place_cod_order(variant, user=None, quantity=1):
    """Place a COD order for a stocked variant and confirm it."""
    _stock_variant(variant)
    _SEQ[0] += 1
    cart = get_or_create_cart(session_key=f"review-cart-{_SEQ[0]}")
    add_item(cart, variant_id=variant.pk, quantity=quantity)
    order = create_order_from_cart(
        cart=cart,
        user=user,
        phone=user.phone_number if user else "+254712345678",
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


def _make_unattached_photo(user, name):
    """Create an uploaded-but-unclaimed photo row owned by the given user."""
    return ReviewPhoto.objects.create(
        user=user,
        storage_name=f"reviews/photos/{name}",
        display_url=f"/media/{name}",
    )


class ReviewRatingServiceTests(APITestCase):
    """Exercises rating aggregation and verified-purchase logic directly."""

    def setUp(self):
        cache.clear()
        self.buyer = _make_user()
        self.product, self.variant = _make_product()
        _stock_variant(self.variant)

    def test_create_review_recomputes_product_rating(self):
        """A new approved review moves the product's average and count."""
        self.product.refresh_from_db()
        self.assertEqual(self.product.average_rating, Decimal("0.00"))
        self.assertEqual(self.product.review_count, 0)

        create_review(user=self.buyer, product=self.product, rating=5, body="Lovely.")
        self.product.refresh_from_db()
        self.assertEqual(self.product.average_rating, Decimal("5.00"))
        self.assertEqual(self.product.review_count, 1)

        create_review(
            user=_make_user(email="second@example.com", username="second"),
            product=self.product,
            rating=3,
        )
        self.product.refresh_from_db()
        self.assertEqual(self.product.average_rating, Decimal("4.00"))
        self.assertEqual(self.product.review_count, 2)

    def test_rejected_reviews_do_not_count_towards_rating(self):
        """A hidden review leaves the product aggregate untouched."""
        review = create_review(user=self.buyer, product=self.product, rating=1)
        set_review_approval(review=review, approved=False)
        self.product.refresh_from_db()
        self.assertEqual(self.product.average_rating, Decimal("0.00"))
        self.assertEqual(self.product.review_count, 0)

        set_review_approval(review=review, approved=True)
        self.product.refresh_from_db()
        self.assertEqual(self.product.average_rating, Decimal("1.00"))
        self.assertEqual(self.product.review_count, 1)

    def test_duplicate_review_per_buyer_is_rejected(self):
        """A buyer cannot review the same product twice, even from a service call."""
        create_review(user=self.buyer, product=self.product, rating=4)
        with self.assertRaisesMessage(ValidationError, "already reviewed"):
            create_review(user=self.buyer, product=self.product, rating=5)

    def test_database_unique_constraint_blocks_duplicate_review(self):
        """The unique constraint is a second line of defence below the service."""
        create_review(user=self.buyer, product=self.product, rating=4)
        with self.assertRaises(IntegrityError):
            Review.objects.create(user=self.buyer, product=self.product, rating=5)

    def test_free_form_text_is_sanitised(self):
        """Markup and event handlers are stripped from review text before storage."""
        review = create_review(
            user=self.buyer,
            product=self.product,
            rating=5,
            title="<script>alert(1)</script>Great",
            body="Lovely <img src=x onerror=alert(1)> kettle.",
        )
        self.assertEqual(review.title, "alert(1)Great")
        self.assertNotIn("<img", review.body)
        self.assertNotIn("onerror", review.body)

    def test_rating_out_of_range_is_rejected(self):
        """Ratings outside the configured scale raise a clean validation error."""
        with self.assertRaises(ValidationError):
            create_review(user=self.buyer, product=self.product, rating=0)
        with self.assertRaises(ValidationError):
            create_review(user=self.buyer, product=self.product, rating=6)

    def test_verified_purchase_accepts_own_completed_order_line(self):
        """A line from the reviewer's own confirmed order verifies the review."""
        order = _place_cod_order(self.variant, user=self.buyer)
        order_item = order.items.first()
        review = create_review(
            user=self.buyer,
            product=self.product,
            rating=4,
            order_item_id=order_item.pk,
        )
        self.assertEqual(review.order_item_id, order_item.pk)

    def test_verified_purchase_rejects_foreign_order_line(self):
        """Another buyer's order line never verifies a review."""
        other = _make_user(email="other@example.com", username="other")
        order = _place_cod_order(self.variant, user=other)
        with self.assertRaisesMessage(ValidationError, "does not qualify"):
            create_review(
                user=self.buyer,
                product=self.product,
                rating=4,
                order_item_id=order.items.first().pk,
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
                user=self.buyer,
                product=self.product,
                rating=4,
                order_item_id=order.items.first().pk,
            )

    def test_verified_purchase_rejects_wrong_product_line(self):
        """A verified line must reference the product actually reviewed."""
        other_product, other_variant = _make_product(price="8000.00")
        order = _place_cod_order(other_variant, user=self.buyer)
        with self.assertRaisesMessage(ValidationError, "does not qualify"):
            create_review(
                user=self.buyer,
                product=self.product,
                rating=4,
                order_item_id=order.items.first().pk,
            )
        _ = other_product

    def test_verified_purchase_line_can_be_claimed_only_once(self):
        """One purchase verifies only one review, guarded inside the service."""
        order = _place_cod_order(self.variant, user=self.buyer, quantity=2)
        order_item = order.items.first()
        create_review(
            user=self.buyer,
            product=self.product,
            rating=4,
            order_item_id=order_item.pk,
        )
        # The public create path blocks a second review before the purchase is
        # resolved, so the already-claimed guard is exercised at the resolver —
        # it must never let the same line verify a review twice.
        from apps.reviews.services import _resolve_verified_order_item

        with self.assertRaisesMessage(ValidationError, "already been used"):
            _resolve_verified_order_item(self.buyer, self.product, order_item.pk)


class ReviewEndpointTests(APITestCase):
    """Exercises the storefront review endpoints over HTTP."""

    def setUp(self):
        cache.clear()
        self.buyer = _make_user()
        self.product, self.variant = _make_product()
        _stock_variant(self.variant)
        self.review_url = reverse(
            "api:reviews:product-reviews", kwargs={"slug": self.product.slug}
        )

    def test_anonymous_can_read_reviews(self):
        """Reading a product's reviews is deliberately public."""
        create_review(user=self.buyer, product=self.product, rating=4)
        response = self.client.get(self.review_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)

    def test_anonymous_cannot_create_review(self):
        """Creating a review requires an authenticated customer."""
        response = self.client.post(
            self.review_url, {"rating": 4, "body": "Nice."}, format="json"
        )
        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )

    def test_client_supplied_identity_is_ignored(self):
        """The reviewer identity always comes from the token, never the body."""
        other = _make_user(email="other@example.com", username="other")
        _login(self.client)
        response = self.client.post(
            self.review_url,
            {"rating": 4, "body": "Nice.", "user": other.pk},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        review = Review.objects.get()
        self.assertEqual(review.user, self.buyer)

    def test_posting_review_with_verified_order_item(self):
        """A completed purchase attached at creation surfaces the badge."""
        order = _place_cod_order(self.variant, user=self.buyer)
        _login(self.client)
        response = self.client.post(
            self.review_url,
            {"rating": 5, "order_item_id": order.items.first().pk},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["verified_purchase"])

    def test_posting_review_with_foreign_order_item_returns_400(self):
        """A foreign order line collapses into a plain 400, no leak."""
        other = _make_user(email="other@example.com", username="other")
        order = _place_cod_order(self.variant, user=other)
        _login(self.client)
        response = self.client.post(
            self.review_url,
            {"rating": 5, "order_item_id": order.items.first().pk},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Review.objects.count(), 0)

    def test_duplicate_review_via_api_returns_400(self):
        """A second review from the same buyer is rejected, not duplicated."""
        create_review(user=self.buyer, product=self.product, rating=4)
        _login(self.client)
        response = self.client.post(
            self.review_url, {"rating": 5, "body": "Still great."}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_inactive_product_slug_is_404(self):
        """Hidden and discontinued products are not reviewable."""
        _login(self.client)
        self.product.is_active = False
        self.product.save()
        response = self.client.post(
            reverse("api:reviews:product-reviews", kwargs={"slug": self.product.slug}),
            {"rating": 4},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_rejected_review_is_invisible_on_storefront(self):
        """Hidden reviews leave the public listing but stay in the database."""
        review = create_review(user=self.buyer, product=self.product, rating=1)
        set_review_approval(review=review, approved=False)
        response = self.client.get(self.review_url)
        self.assertEqual(response.data["count"], 0)


class QuestionEndpointTests(APITestCase):
    """Exercises the Q&A storefront endpoints over HTTP."""

    def setUp(self):
        cache.clear()
        self.buyer = _make_user()
        self.staff = _make_staff()
        self.product, _ = _make_product()
        self.question_url = reverse(
            "api:reviews:product-questions", kwargs={"slug": self.product.slug}
        )

    def test_anonymous_read_allowed_write_rejected(self):
        """Questions are public to read but only customers may ask."""
        response = self.client.get(self.question_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.post(
            self.question_url, {"question": "Does it boil fast?"}, format="json"
        )
        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )

    def test_customer_asks_and_staff_answers(self):
        """Answering approves the question so the thread becomes visible."""
        _login(self.client)
        response = self.client.post(
            self.question_url, {"question": "Does it boil fast?"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        question = ProductQuestion.objects.get()
        self.assertTrue(question.is_approved)

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

        listing = self.client.get(self.question_url)
        self.assertEqual(listing.data["count"], 1)
        self.assertEqual(
            listing.data["results"][0]["answers"][0]["answer"],
            "Yes, under three minutes.",
        )

    def test_question_text_is_sanitised(self):
        """Raw question markup never reaches storage."""
        _login(self.client)
        response = self.client.post(
            self.question_url,
            {"question": "<script>alert(1)</script>Does it boil fast?"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        question = ProductQuestion.objects.get()
        self.assertEqual(question.question, "alert(1)Does it boil fast?")


class ModerationEndpointTests(APITestCase):
    """Exercises role-gated moderation over HTTP."""

    def setUp(self):
        cache.clear()
        self.buyer = _make_user()
        self.manager = _make_staff(email="manager@example.com", role="manager")
        self.support = _make_staff(email="support@example.com", role="support")
        self.analyst = _make_staff(email="analyst@example.com", role="analyst")
        self.product, self.variant = _make_product()
        self.review = create_review(user=self.buyer, product=self.product, rating=2)
        set_review_approval(review=self.review, approved=False)
        self.question = create_product_question(
            user=self.buyer, product=self.product, question="Is it in stock?"
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
            user=_make_user(email="third@example.com", username="third"),
            product=self.product,
            rating=5,
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
        self.buyer = _make_user()
        self.product, _ = _make_product()
        self.review_url = reverse(
            "api:reviews:product-reviews", kwargs={"slug": self.product.slug}
        )

    def test_disabled_feature_blocks_new_writes_but_not_reads(self):
        """Turning the toggle off stops creation while published content stays."""
        _login(self.client)
        response = self.client.post(
            self.review_url, {"rating": 4, "body": "Nice."}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        config = SiteConfig.load()
        config.settings["enable_reviews"] = False
        config.save()
        invalidate_feature_enabled()

        response = self.client.post(
            self.review_url, {"rating": 4, "body": "Nice."}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

        response = self.client.get(self.review_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)


class ReviewPhotoUploadTests(APITestCase):
    """Exercises review photo upload validation and processing."""

    def setUp(self):
        cache.clear()
        self.buyer = _make_user()
        self.url = reverse("api:reviews:review-photo-upload")

    def test_anonymous_upload_rejected(self):
        """Photo uploads require an authenticated customer."""
        response = self.client.post(self.url, {}, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_non_image_upload_rejected(self):
        """A text file is rejected before any storage happens."""
        _login(self.client)
        response = self.client.post(
            self.url,
            {"image": BytesIO(b"not an image")},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_valid_image_upload_returns_processed_urls(self):
        """A valid image creates an owned, unclaimed photo row with variants."""
        _login(self.client)
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
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        save_mock.assert_called_once()
        photo = ReviewPhoto.objects.get()
        self.assertEqual(photo.user, self.buyer)
        self.assertIsNone(photo.review)
        self.assertEqual(response.data["id"], photo.pk)
        self.assertEqual(response.data["url"], "/media/v.webp")
        self.assertEqual(response.data["variants"][0]["format"], "webp")

    def test_unattached_upload_bound_blocks_extra_uploads(self):
        """A caller holding the cap of unclaimed photos cannot upload more."""
        _login(self.client)
        with mock.patch("apps.reviews.views.MAX_UNATTACHED_PHOTOS_PER_USER", 1):
            _make_unattached_photo(self.buyer, "held.png")
            response = self.client.post(
                self.url,
                {"image": _tiny_png()},
                format="multipart",
            )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_disabled_feature_blocks_upload(self):
        """A disabled reviews toggle stops photo uploads with a 503."""
        _login(self.client)
        config = SiteConfig.load()
        config.settings["enable_reviews"] = False
        config.save()
        invalidate_feature_enabled()
        response = self.client.post(
            self.url,
            {"image": _tiny_png()},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    def test_oversized_image_rejected(self):
        """Files over the configured ceiling are refused."""
        _login(self.client)
        with mock.patch("apps.reviews.views.REVIEW_PHOTO_MAX_SIZE_MB", 0):
            response = self.client.post(
                self.url,
                {"image": _tiny_png()},
                format="multipart",
            )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ReviewPhotoDeleteTests(APITestCase):
    """Exercises owner-only deletion of unattached uploaded photos."""

    def setUp(self):
        cache.clear()
        self.buyer = _make_user()
        self.other = _make_user(email="other@example.com", username="other")
        self.photo = _make_unattached_photo(self.buyer, "mine.png")
        self.foreign = _make_unattached_photo(self.other, "theirs.png")

    def _url(self, photo_id):
        """Return the delete endpoint for the given photo id."""
        return reverse("api:reviews:review-photo-delete", kwargs={"photo_id": photo_id})

    def test_anonymous_delete_rejected(self):
        """An unauthenticated caller cannot delete a photo."""
        response = self.client.delete(self._url(self.photo.pk))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertTrue(ReviewPhoto.objects.filter(pk=self.photo.pk).exists())

    def test_owner_can_delete_unattached_photo(self):
        """Deleting one's own unattached photo removes the row and its files."""
        _login(self.client)
        with mock.patch("apps.reviews.signals.delete_image_files") as deleter:
            response = self.client.delete(self._url(self.photo.pk))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(ReviewPhoto.objects.filter(pk=self.photo.pk).exists())
        deleter.assert_called_once_with("reviews/photos/mine.png")

    def test_foreign_photo_delete_returns_404(self):
        """Another user's photo is indistinguishable from a missing one."""
        _login(self.client)
        response = self.client.delete(self._url(self.foreign.pk))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(ReviewPhoto.objects.filter(pk=self.foreign.pk).exists())

    def test_missing_photo_delete_returns_404(self):
        """A nonexistent photo id is a clean 404."""
        _login(self.client)
        response = self.client.delete(self._url(999999))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_attached_photo_delete_rejected(self):
        """A photo already on a review is published content and stays put."""
        product, _ = _make_product()
        review = create_review(user=self.buyer, product=product, rating=4)
        attached = ReviewPhoto.objects.create(
            user=self.buyer,
            review=review,
            storage_name="reviews/photos/attached.png",
            display_url="/media/attached.png",
        )
        _login(self.client)
        response = self.client.delete(self._url(attached.pk))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(ReviewPhoto.objects.filter(pk=attached.pk).exists())


class RatingIntegrityTests(APITestCase):
    """Exercises aggregate correctness across every deletion path."""

    def setUp(self):
        cache.clear()
        self.buyer = _make_user()
        self.product, _ = _make_product()

    def test_deleting_review_recomputes_product_rating(self):
        """Deleting a review with the ORM keeps the aggregate right."""
        review = create_review(user=self.buyer, product=self.product, rating=5)
        create_review(
            user=_make_user(email="second@example.com", username="second"),
            product=self.product,
            rating=3,
        )
        review.delete()
        self.product.refresh_from_db()
        self.assertEqual(self.product.review_count, 1)
        self.assertEqual(self.product.average_rating, Decimal("3.00"))

    def test_user_cascade_deletes_recompute_product_rating(self):
        """Losing a reviewer's account never leaves a stale aggregate."""
        second = _make_user(email="second@example.com", username="second")
        create_review(user=self.buyer, product=self.product, rating=5)
        create_review(user=second, product=self.product, rating=3)
        second.delete()
        self.product.refresh_from_db()
        self.assertEqual(self.product.review_count, 1)
        self.assertEqual(self.product.average_rating, Decimal("5.00"))

    def test_photo_delete_removes_stored_files(self):
        """Deleting a photo row deletes its stored files."""
        photo = _make_unattached_photo(self.buyer, "x.png")
        with mock.patch("apps.reviews.signals.delete_image_files") as deleter:
            photo.delete()
        deleter.assert_called_once_with("reviews/photos/x.png")

    def test_review_delete_cascades_photo_file_cleanup(self):
        """Deleting a review removes its photos and their files."""
        review = create_review(user=self.buyer, product=self.product, rating=4)
        photo = ReviewPhoto.objects.create(
            user=self.buyer,
            review=review,
            storage_name="reviews/photos/x.png",
            display_url="/media/x.png",
        )
        with mock.patch("apps.reviews.signals.delete_image_files") as deleter:
            review.delete()
        deleter.assert_called_once_with("reviews/photos/x.png")
        self.assertFalse(ReviewPhoto.objects.filter(pk=photo.pk).exists())


class ReviewPhotoClaimTests(APITestCase):
    """Exercises photo ownership and claim-once rules over the API."""

    def setUp(self):
        cache.clear()
        self.buyer = _make_user()
        self.other = _make_user(email="other@example.com", username="other")
        self.product, self.variant = _make_product()
        _stock_variant(self.variant)
        self.other_product, _ = _make_product(price="9000.00")
        self.review_url = reverse(
            "api:reviews:product-reviews", kwargs={"slug": self.product.slug}
        )
        self.other_review_url = reverse(
            "api:reviews:product-reviews", kwargs={"slug": self.other_product.slug}
        )

    def test_claiming_photos_attaches_them_to_the_review(self):
        """A caller's own unclaimed upload lands on the created review."""
        _login(self.client)
        photo = _make_unattached_photo(self.buyer, "one.png")
        response = self.client.post(
            self.review_url,
            {"rating": 5, "photo_ids": [photo.pk]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(len(response.data["photos"]), 1)
        self.assertEqual(response.data["photos"][0]["id"], photo.pk)
        photo.refresh_from_db()
        self.assertEqual(photo.review_id, response.data["id"])

    def test_foreign_photo_cannot_be_attached(self):
        """Another user's upload is never attachable, and stays unclaimed."""
        foreign = _make_unattached_photo(self.other, "theirs.png")
        _login(self.client)
        response = self.client.post(
            self.review_url,
            {"rating": 5, "photo_ids": [foreign.pk]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Review.objects.count(), 0)
        foreign.refresh_from_db()
        self.assertIsNone(foreign.review)

    def test_duplicate_photo_ids_rejected(self):
        """The same photo cannot be cited twice in one review."""
        _login(self.client)
        photo = _make_unattached_photo(self.buyer, "dup.png")
        response = self.client.post(
            self.review_url,
            {"rating": 5, "photo_ids": [photo.pk, photo.pk]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Review.objects.count(), 0)

    def test_too_many_photos_rejected(self):
        """The per-review photo cap is enforced at the serializer."""
        _login(self.client)
        photos = [_make_unattached_photo(self.buyer, f"p{i}.png") for i in range(6)]
        response = self.client.post(
            self.review_url,
            {"rating": 5, "photo_ids": [photo.pk for photo in photos]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_claimed_photo_cannot_be_attached_again(self):
        """A photo used on one review cannot back a second review."""
        _login(self.client)
        photo = _make_unattached_photo(self.buyer, "used.png")
        response = self.client.post(
            self.review_url,
            {"rating": 5, "photo_ids": [photo.pk]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        response = self.client.post(
            self.other_review_url,
            {"rating": 4, "photo_ids": [photo.pk]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class OrphanPhotoSweepTests(APITestCase):
    """Exercises the scheduled sweep of never-attached uploads."""

    def setUp(self):
        cache.clear()
        self.buyer = _make_user()
        self.product, _ = _make_product()
        self.orphan = _make_unattached_photo(self.buyer, "orphan.png")

    def test_sweep_removes_only_expired_orphans(self):
        """Old unattached photos are purged; attached and fresh ones survive."""
        review = create_review(user=self.buyer, product=self.product, rating=4)
        attached = ReviewPhoto.objects.create(
            user=self.buyer,
            review=review,
            storage_name="reviews/photos/attached.png",
            display_url="/media/attached.png",
        )
        fresh = _make_unattached_photo(self.buyer, "fresh.png")
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
