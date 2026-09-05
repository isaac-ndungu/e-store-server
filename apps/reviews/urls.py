"""URL routing for the reviews endpoints.

Mounted at ``/api/v1/`` from ``config/api_urls.py``. Product-scoped routes
live under ``products/<slug>`` to match the catalog's URL style; the
storefront reads a product's reviews and Q&A publicly while creating content
and uploading photos require the authenticated customer. Moderation routes
sitting under their own ``reviews/`` and ``questions/`` prefixes are reachable
only by manager/support tokens.
"""

from django.urls import path

from apps.reviews.views import (
    ProductQuestionsView,
    ProductReviewsView,
    QuestionAnswerCreateView,
    QuestionApproveView,
    QuestionModerationListView,
    QuestionRejectView,
    ReviewApproveView,
    ReviewModerationListView,
    ReviewPhotoDeleteView,
    ReviewPhotoUploadView,
    ReviewRejectView,
)

urlpatterns = [
    path(
        "products/<slug:slug>/reviews/",
        ProductReviewsView.as_view(),
        name="product-reviews",
    ),
    path(
        "products/<slug:slug>/questions/",
        ProductQuestionsView.as_view(),
        name="product-questions",
    ),
    path(
        "reviews/photo-upload/",
        ReviewPhotoUploadView.as_view(),
        name="review-photo-upload",
    ),
    path(
        "reviews/photos/<int:photo_id>/",
        ReviewPhotoDeleteView.as_view(),
        name="review-photo-delete",
    ),
    path(
        "reviews/moderate/",
        ReviewModerationListView.as_view(),
        name="review-moderation-list",
    ),
    path(
        "reviews/<int:review_id>/approve/",
        ReviewApproveView.as_view(),
        name="review-approve",
    ),
    path(
        "reviews/<int:review_id>/reject/",
        ReviewRejectView.as_view(),
        name="review-reject",
    ),
    path(
        "questions/moderate/",
        QuestionModerationListView.as_view(),
        name="question-moderation-list",
    ),
    path(
        "questions/<int:question_id>/answers/",
        QuestionAnswerCreateView.as_view(),
        name="question-answers",
    ),
    path(
        "questions/<int:question_id>/approve/",
        QuestionApproveView.as_view(),
        name="question-approve",
    ),
    path(
        "questions/<int:question_id>/reject/",
        QuestionRejectView.as_view(),
        name="question-reject",
    ),
]
