"""Tunable constants for the reviews app.

Values that shape review content, rating bounds, verified-purchase
eligibility, and photo uploads but never vary per request live here so they
stay reviewable in one place rather than scattered across modules.
"""

# The star-rating scale. The model also enforces the bound with a database
# check constraint, so the limit holds below the serializer.
MIN_REVIEW_RATING = 1
MAX_REVIEW_RATING = 5

# Longest engaging free-form text per surface. Raw input is capped here and
# the sanitised output is never longer than what was submitted.
REVIEW_TITLE_MAX_LENGTH = 255
REVIEW_BODY_MAX_LENGTH = 5000
QUESTION_MAX_LENGTH = 2000
ANSWER_MAX_LENGTH = 2000

# At most this many photos may be attached to one review.
REVIEW_MAX_PHOTOS = 5

# Order statuses that count as a completed purchase for the verified-purchase
# badge. Pending, cancelled, refunded, and returned orders do not, because a
# review should only verify a purchase that actually went through.
VERIFIED_PURCHASE_STATUSES = ("confirmed", "processing", "shipped", "delivered")

# Review photos are validated by content (Pillow decode) and capped at this
# size before any storage, so a single upload cannot exhaust disk.
REVIEW_PHOTO_MAX_SIZE_MB = 10
