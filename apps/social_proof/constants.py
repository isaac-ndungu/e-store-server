"""Tunable constants for the social proof app.

Values that shape the live-viewer counter, the durable event log, and the
recent-sales feed but never vary per request live here so they stay reviewable
in one place rather than scattered across modules.
"""

# First-party cookie carrying the anonymous visitor identity the live-viewer
# counters key on. It holds no token and no personal data — just a random hex
# identifier distinguishing one browser from another.
VISITOR_COOKIE = "e_store_visitor"

# How long a minted visitor identity is trusted. Browsing sessions far shorter
# than this keep their identity across the live window; the Redis set itself
# drops idle viewers after LIVE_VIEWER_WINDOW_SECONDS regardless.
VISITOR_COOKIE_MAX_AGE_DAYS = 30

# A visitor identity may be at most this long, matching the event row's field.
VISITOR_KEY_MAX_LENGTH = 100

# Durable view events are collapsed to at most one row per product, visitor,
# and bucket. Matching the live-window length keeps the database history in
# step with the Redis counter's notion of a "current" viewer.
VIEW_EVENT_BUCKET_MINUTES = 5

# View-event rows older than this are purged by the daily maintenance task.
VIEW_EVENT_RETENTION_DAYS = 90

# Recent-sales feed only surfaces purchases placed within this window.
RECENT_SALES_WINDOW_HOURS = 24

# Order statuses that represent a completed, fulfilled purchase. Pending,
# cancelled, and refunded orders never appear in the social-proof feed.
RECENT_SALES_STATUSES = ("confirmed", "processing", "shipped", "delivered")

# Upper bound on the number of slugs the batch viewers endpoint accepts, so a
# single request cannot fan out to thousands of Redis reads.
BATCH_VIEWERS_MAX_SLUGS = 50

# Upper bound on recent-sales rows served per request.
RECENT_SALES_MAX_ITEMS = 50

# User agents matching this pattern are treated as bots/crawlers: their
# views are acknowledged but never recorded, so automated traffic cannot
# inflate live-viewer counts or bloat the event table.
BOT_USER_AGENT_PATTERN = (
    r"(?:"
    r"bot\b|crawler|spider|slurp|curl|wget|scrapy|httpx|python-requests|"
    r"headlesschrome|phantomjs|facebookexternalhit|facebot|whatsapp|"
    r"googlebot|bingbot|duckduckbot|petalbot|ahrefs|semrush"
    r")"
)
