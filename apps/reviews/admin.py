"""Django admin registration for the reviews app.

Moderation happens through the API's manager/support endpoints or through these
admin pages; both must land on the service layer so every visibility change
also refreshes the product's denormalised rating aggregate — a raw
``.update(is_approved=...)`` in the admin would leave the storefront number
stale.
"""

from django.contrib import admin

from apps.reviews.models import (
    ProductAnswer,
    ProductQuestion,
    Review,
    ReviewPhoto,
)
from apps.reviews.services import set_question_approval, set_review_approval


def _approve_selected(modeladmin, request, queryset, target):
    """Approve the selected rows through the service for every affected product.

    The helper exists because the three models map differently to the
    approval services: reviews and questions each have one, while answers are
    approved through their parent question.

    Args:
        modeladmin: the model admin issuing the action.
        request: the admin request.
        queryset (QuerySet): the selected rows.
        target (bool): True to approve, False to reject.

    Returns:
        int: how many rows were updated.
    """
    updated = 0
    for obj in queryset:
        if isinstance(obj, ProductAnswer):
            set_question_approval(question=obj.question, approved=target)
        elif isinstance(obj, ProductQuestion):
            set_question_approval(question=obj, approved=target)
        else:
            set_review_approval(review=obj, approved=target)
        updated += 1
    return updated


def approve_selected(modeladmin, request, queryset):
    """Mark the selected reviews or questions as approved."""
    updated = _approve_selected(modeladmin, request, queryset, True)
    modeladmin.message_user(request, f"{updated} item(s) approved.")


def reject_selected(modeladmin, request, queryset):
    """Mark the selected reviews or questions as not approved."""
    updated = _approve_selected(modeladmin, request, queryset, False)
    modeladmin.message_user(request, f"{updated} item(s) hidden.")


approve_selected.short_description = "Approve selected reviews/questions"
reject_selected.short_description = "Reject selected reviews/questions"


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    """Admin page for browsing and moderating customer reviews."""

    list_display = (
        "product",
        "user",
        "rating",
        "is_approved",
        "order_item",
        "created_at",
    )
    list_filter = ("is_approved", "rating", "created_at")
    search_fields = (
        "product__name",
        "product__slug",
        "user__username",
        "title",
        "body",
    )
    actions = [approve_selected, reject_selected]


@admin.register(ReviewPhoto)
class ReviewPhotoAdmin(admin.ModelAdmin):
    """Admin page for inspecting and removing customer review photos."""

    list_display = ("user", "review", "display_url", "created_at")
    list_filter = ("created_at",)
    search_fields = ("user__username", "storage_name")


@admin.register(ProductQuestion)
class ProductQuestionAdmin(admin.ModelAdmin):
    """Admin page for browsing and moderating product questions."""

    list_display = ("product", "user", "is_approved", "created_at")
    list_filter = ("is_approved", "created_at")
    search_fields = ("product__name", "product__slug", "user__username", "question")
    actions = [approve_selected, reject_selected]


@admin.register(ProductAnswer)
class ProductAnswerAdmin(admin.ModelAdmin):
    """Admin page for browsing staff answers on product questions."""

    list_display = ("question", "answered_by", "is_staff_answer", "created_at")
    list_filter = ("is_staff_answer", "created_at")
    search_fields = ("question__question", "answer", "answered_by__username")
    actions = [approve_selected, reject_selected]
