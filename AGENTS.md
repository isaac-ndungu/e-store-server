# AGENTS.md — Kenyan E-Commerce Platform (Home Appliances)

This file is standing instructions for every OpenCode session in this repo. Read it before
writing or editing any code. If a request conflicts with a rule below, follow this file and
say so, rather than silently picking one.

**Default escalation behavior:** if something is destructive or hard to reverse (see the
"ask before doing anything destructive" list below), or the correct handling of money, tax,
or inventory is unclear even after reading this file and the plan, stop and ask before
proceeding. If a security or permissions question is ambiguous, don't stop — default to the
more restrictive, secure interpretation, state that assumption in your response, and
proceed (see the Security section's "if a security requirement is ambiguous" rule). For
everything else — a naming choice, a minor structural decision — state the assumption
you're making and continue; don't block on small things.

## Project context

- Django 5.x + Django REST Framework, PostgreSQL 16, Celery + Celery Beat, Redis.
- Single-tenant Kenyan appliance e-commerce backend. Full spec lives in `docs/plan.md`.
  Read it before touching a domain you're unfamiliar with — don't guess field names or
  model relationships from memory. If that path doesn't exist, say so rather than
  proceeding without it.
- Money, tax (eTIMS/KRA VAT), and inventory correctness matter more than speed. Get these
  right even if it takes longer.
- Target environment: mixed strong 4G / patchy throttled connections, mostly mid/low-end
  Android devices, data-cost-conscious users. Performance work should be judged against
  this, not against a fast office wifi connection.

## Non-negotiable domain rules

These come from real bugs found during planning review. Never reintroduce them.

1. **Inventory is per-warehouse.** `Inventory.variant` is a `ForeignKey`, never `OneToOneField`
   — a variant can have multiple `Inventory` rows (one per warehouse). Always filter/aggregate
   across warehouses explicitly; never assume a variant has exactly one stock row.
2. **Stock changes go through the reservation lifecycle**, not direct `Inventory.quantity`
   edits. Any code that decrements stock outside `StockReservation` fulfillment/release is a bug.
   Wrap reservation creation/release in `select_for_update()` transactions — this path runs
   under concurrent checkout load and must not race.
3. **Every `Order.status` change must produce an `OrderStatusHistory` row.** Never update
   `Order.status` directly in a view or task without going through the helper/signal that
   logs the transition. If you're writing new status-changing code, check that logging fires.
4. **Tax rate is per `OrderItem`, never a single flat rate per order.** A cart can mix
   standard/zero-rated/exempt products. Never compute VAT by multiplying an order total by
   one rate — always sum from each line's `tax_rate`/`tax`.
5. **Any return, refund, or cancellation of an already-transmitted eTIMS invoice requires an
   `ETIMSCreditNote`**, not just a status change on the order. Don't mark an order `refunded`
   or `cancelled` post-confirmation without checking whether a credit note needs to fire.
6. **M-Pesa/Daraja callbacks must be idempotent.** Always look up by `checkout_request_id` /
   `conversation_id` and no-op if the transaction is already in a terminal state before
   mutating anything. Assume Safaricom will retry callbacks.
7. **Money fields are `Decimal`, never `float`.** Never use `float` for prices, totals, VAT,
   or loyalty conversions — rounding errors compound across the checkout math.
8. **Bundle purchases create one `OrderItem` per component**, sharing a `bundle_group_id` —
   never a single opaque line for the whole bundle. Inventory and tax are per-component.
9. **Don't touch `Inventory.reserved` counts and `SerialUnit.status` in only one place.** For
   serialized products (`tracks_serial_numbers=True`), both must move together or stock counts
   will drift. If you're not sure how they should reconcile, ask rather than guess.
10. **Never trust a client-submitted price, total, or discount amount.** Cart/checkout/order
    endpoints must always recompute price server-side via `get_effective_price()` /
    `get_bundle_price()` from the current `Discount`/`Coupon`/`PricingTier` state — a
    client-sent amount is display data, never an input to what actually gets charged.

## General engineering rules

- **Never guess a model field, API shape, or config key.** Grep/read the actual model or
  serializer before writing code against it. If it doesn't exist yet, say so — don't invent
  a plausible-looking field name and move on.
- **Write migrations, don't hand-edit the database.** Every model change ships with a
  generated migration in the same change. Check `makemigrations` output before applying it —
  don't blindly run `migrate` on an unreviewed migration.
- **No bare `except:`.** Catch specific exceptions. Payment/webhook code especially must not
  swallow errors silently — log and re-raise or return an explicit failure state.
- **Business logic lives in a services/selectors layer, not in views or serializers.** Views
  stay thin: parse input, call a service function, return a response. This is what keeps the
  backend cloneable for future ventures per the plan.
- **Snapshot order-time data on `OrderItem`** (name, price, tax rate, variant attributes) —
  never join back to live `Product`/`ProductVariant` to render a historical order, since prices
  and specs change after the order is placed.
- **Every Celery task must be safe to run twice.** Assume retries happen. Design tasks to be
  idempotent (check state before acting) rather than relying on Celery's at-most-once delivery.
- **Write a test for every bug fix and every new endpoint that touches money, stock, order
  status, or auth/permissions**, at minimum. Don't ask permission to add tests — just add
  them alongside the code.
- **Never weaken a test to make it pass.** If a test fails, fix the underlying code or ask —
  don't loosen an assertion, comment it out, or mark it skipped just to get a green run.
- **Don't silently change an interface.** If fixing something requires changing a function
  signature, a URL, or a model field other code depends on, grep for all call sites and update
  them in the same change — don't leave it half-migrated.
- **Ask before doing anything destructive or hard to reverse**: dropping/altering a column
  on a table with data, force-pushing, deleting migrations, running raw SQL against a
  non-local database, rotating secrets.
- **Don't add a new dependency without saying so.** Flag it in your response and explain why
  the standard library / an existing dependency wasn't enough. Check it's actively maintained
  and has no known critical vulnerabilities before adding it.
- **No debug leftovers.** No stray `print()` statements, no commented-out blocks of old code,
  no `TODO` without a one-line reason — clean these up as part of the change, don't leave
  them for later.
- **Write human-readable code over compact "clever" shorthand.** Don't reach for dense
  one-liners, obscure language tricks, or non-idiomatic shortcuts just because they're
  shorter — if a construct isn't standard, well-known industry practice for this stack,
  don't use it purely to save lines. Optimize for a fast, correct read by someone unfamiliar
  with the codebase, not for fewer lines. Favor clear names, small functions with one job,
  and straightforward control flow over compressed or "smart" alternatives.
- **Follow consistent spacing and formatting — run it through the formatter, don't hand-space
  it.** Python code must pass `black` and `isort` before it's considered done; don't manually
  cram multiple statements onto one line, don't align `=` signs or trailing comments into
  columns, and don't add decorative spacing. Use a single blank line to separate logical
  steps within a function, two blank lines between top-level classes/functions per PEP 8,
  and no blank line at the very start or end of a block. If any frontend JS/TS/HTML code is
  written, run it through `prettier`/the project's configured formatter the same way — this
  rule isn't Python-only, it's whatever the file type's standard formatter is.
- **Every function, method, and class needs a docstring** — not just the tricky ones. This
  includes Django views, serializers, model methods, Celery tasks, service/selector
  functions, and management commands. The only exceptions are one-line trivial
  properties/dunder methods where the name already says everything (e.g. `__str__`).
  - Use Google-style docstrings: a one-line summary, then `Args:`, `Returns:`, and `Raises:`
    sections when applicable. Be consistent about this format across the whole codebase.
  - The docstring describes what the function *does* and its contract (inputs, outputs,
    side effects, exceptions) — not implementation history. Same rule as comments: no
    "FIXED:", no "previously," no reference to a prior version or a review pass.
  - For model classes, a short class-level docstring explaining what the model represents
    and any non-obvious constraint (e.g. why a field is a `ForeignKey` and not `OneToOneField`,
    if that distinction matters) is required, especially for models tied to money, tax,
    or inventory.
  - For Celery tasks, state whether the task is idempotent/safe to retry as part of the
    docstring — that's exactly the kind of contract a caller needs and shouldn't have to
    infer from reading the body.
- **Write comments like a developer documenting their own code, not like an agent narrating
  its work.** Never write meta-commentary about what changed, when, or why in agent terms —
  no "FIXED:", "previously this was...", "changed from X to Y", "added per review", "NOTE:
  updated on this pass," or anything referencing a prior version, a review, or the fact that
  an agent touched the file. Git history is where that information belongs, not the code
  itself. A comment should read as if a developer wrote it once, describing the code as it
  is now — not as a diff or a changelog entry.
  - Only comment where the *why* isn't obvious from the code — a non-obvious business rule,
    a gotcha, a reason a simpler approach won't work, a regulatory/domain constraint. Don't
    comment what the code already says plainly.
  - No decorative dividers, banners, or ASCII rules (e.g. `# --- Database ---...---`). A
    plain one- or two-line comment above the relevant code is enough; it doesn't need a
    header treatment.
  - Keep comments plain and factual, in active language, and self-contained — someone
    reading only the comment (no surrounding conversation or PR context) should understand
    it. E.g. write `# Kenyan tax law requires a credit note, not just a status change, when
    reversing a transmitted invoice.` — not `# [FIXED] Added credit note handling, this was
    missing before.`
- **When something in the plan is ambiguous or looks like it might be wrong, say so before
  building it**, rather than picking a silent interpretation and continuing.

## Performance

Speed is a functional requirement here, not a later optimization pass — see Project
Context above. Every change that touches a list endpoint, a product/checkout page, or a
background task should be checked against these before being considered done.

- **No N+1 queries.** Any serializer or view that iterates a relation must use
  `select_related` (for FK/O2O) or `prefetch_related` (for M2M/reverse-FK) — never let DRF
  lazily fire a query per row. For list endpoints returning more than a handful of objects,
  check the query count (e.g. via `django-debug-toolbar` or `assertNumQueries` in tests)
  before calling it done, and don't let it grow silently as fields get added later.
- **Every list endpoint is paginated.** Never return an unbounded queryset from an API view.
- **Use `only()`/`defer()` on wide tables when a serializer needs a small subset of fields**
  — `Product`/`ProductVariant` in particular carry a lot of columns most list views don't need.
- **Cache hot, read-heavy data in Redis with explicit invalidation, not a blind TTL.**
  Effective prices, stock availability, smart-collection membership, and category trees are
  named in the plan as cache candidates — when you cache one of these, wire invalidation to
  the relevant model's `save`/`delete` signal (or the specific event that changes it) rather
  than just setting a TTL and hoping it's short enough.
- **Add a database index for every field a view filters, sorts, or looks up by** — check the
  relevant `Meta.indexes` / `db_index=True` exists before shipping a new filter or ordering
  option, don't rely on Postgres coping without one.
- **Nothing that isn't needed for the HTTP response happens in the request/response cycle.**
  SMS/OTP sending, eTIMS transmission, email, image processing, and anything calling an
  external API (M-Pesa, SMS provider, KRA) goes to a Celery task — a view should never block
  on an external network call it doesn't need the result of immediately.
  - Where the immediate result *is* needed (e.g. an M-Pesa STK Push must actually be
    triggered before responding), keep that specific call as narrow as possible and don't
    bundle unrelated slow work into the same request.
- **Keep `select_for_update()` transactions short.** The stock-reservation path runs under
  concurrent checkout load — do the minimum work inside the locked transaction (the actual
  read-check-write on `Inventory`) and push anything else (logging, notifications, further
  computation) outside it.
- **Uploaded images must be processed, not served as-is.** Resize/compress on upload and
  serve responsive sizes (the plan specifies AVIF/WebP + `srcset`) — never point the
  storefront at a full-resolution original upload.
- **Serializers should be shaped for their endpoint, not maximal by default.** A list-view
  serializer shouldn't nest full related objects that only the detail view needs — measure
  payload size for a product-list or search-results response, not just correctness.
- **Respect the connection pool.** With PgBouncer in front of Postgres, avoid long-lived or
  unnecessarily wide transactions that hold a connection longer than needed.

## Security

Treat this as an API handling real money, KRA tax filings, and Kenyan customers' personal
data. These are non-negotiable invariants, not optional hardening — validate every line of
security-relevant code against them before considering it done.

**Whitelist-based input validation**
- Every request payload is validated by a DRF serializer with an explicit, named list of
  writable fields — never `fields = '__all__'` on a `ModelSerializer`, including for
  internal/admin endpoints, and never build a model instance from raw `request.data` with
  `**kwargs`. Reject any field not explicitly defined; don't rely on implicit model
  behavior (this is how a customer-facing serializer accidentally lets someone set
  `is_staff`, `price`, or `status`).
- When defining a new endpoint, explicitly decide and document its readable vs. writable
  fields rather than inheriting whatever the model happens to expose.

**Object-level authorization (authentication is not authorization)**
- Every view/endpoint must declare an explicit DRF permission class — never rely on the
  default. If an endpoint is genuinely public (e.g. product catalog browsing), that's a
  deliberate `AllowAny`, stated as such, not an omission.
- Proving who the caller is doesn't prove they're allowed to touch the resource. For
  retrieve/update/partial_update/delete on any resource (`Order`, `Address`,
  `UserProfile`, etc.), the view must verify `request.user` owns it or holds the specific
  role required — never grant access based solely on a URL parameter like `order_id`
  existing (IDOR). Implement this via an explicit `get_object()` override that checks
  ownership/role before returning the object, not just a permission class and a hope.
- Admin-dashboard and courier-role endpoints use the separate JWT scope described in the
  plan; check the caller's role matches what the action requires (e.g. only a courier-role
  token can update `CODCollection`, only manager/analyst roles see revenue data).
- JWTs: short-lived access tokens (15–30 minutes), refresh tokens rotated on every use and
  revoked on logout or password/credential change. Any cookie carrying a token: `Secure`,
  `HttpOnly`, and `SameSite='Lax'` or `'Strict'`.

**Sanitization & injection**
- Never trust the frontend to escape output. Any freeform text that gets rendered
  elsewhere (reviews, Q&A, ticket messages, chat, product descriptions) is sanitized
  before storage with a library like `bleach` — strip `<script>`, `<iframe>`, inline event
  handlers — rather than relying on downstream auto-escaping alone.
- Never build SQL via string concatenation or `%`/f-string formatting with user input.
  Prefer the ORM; if raw SQL is genuinely necessary, use `.raw()` with a params list or
  `cursor.execute(sql, params)`.

**Idempotency & replay protection**
- Every endpoint that creates an order, initiates a payment, or applies a credit note must
  accept and enforce an `Idempotency-Key` header: store the key with the first successful
  result and return that cached result on a repeated request with the same key, rather
  than reprocessing. This is in addition to, not instead of, the M-Pesa callback
  idempotency already required (domain rule 6) — that covers the inbound webhook side,
  this covers the outbound client-initiated side, so a retried "place order" tap can't
  create two orders or double-charge.

**Audit logging for money & PII**
- Every create/update/delete on financial data (orders, payments, invoices, credit notes,
  refunds) is logged with `user_id`, `action`, `resource_id`, `timestamp`, and a short
  summary of the change (e.g. "status changed from pending to confirmed").
- Never log a full credit card number, full OTP code, an unmasked phone number/email, a
  raw M-Pesa/eTIMS payload, or a KRA PIN. Mask before logging —
  e.g. `+2547*****31`, `u***r@domain.com`.

**Secrets & deployment**
- No credential, API key, secret key, or environment-specific URL committed to code, ever
  — M-Pesa/Daraja keys, eTIMS credentials, SMS provider keys, JWT signing secrets all come
  from environment variables/settings, and `.env` is gitignored.
- `DEBUG = False` outside local dev, and the DRF `EXCEPTION_HANDLER` in non-local settings
  must not expose tracebacks or internal error strings to the client — return a generic
  message unless it's a validation error the client actually needs to act on.

**Rate limiting & abuse prevention**
- Apply DRF throttling with concrete limits, not just "some throttling": auth login and
  OTP send/resend around 3/min or 10/hour, STK Push initiation around 5/min, general
  public endpoints around 100/min. Tune the exact numbers with the team, but ship with
  real limits, not the framework default. Coupon-code entry and password/credential reset
  need throttling too — these are exactly the endpoints that cost real money (SMS charges)
  or enable fraud (OTP brute-forcing, coupon guessing) if left open.
- OTP codes: short-lived (per the configured expiry), limited verification attempts before
  lockout (`OrderVerification.attempts`), never guessable/sequential.

**Webhooks & external callbacks**
- Never trust an inbound webhook (M-Pesa/Daraja or otherwise) just because it hit the
  right URL. Validate source IP against an allowlist and/or the provider's request
  signature/token, combined with idempotent processing (domain rule 6) to reject
  duplicates — verify authenticity *and* dedupe, not one or the other.

**File uploads**
- Validate file type by content (not just extension) and enforce a size limit on every
  upload path: product images/manuals, review photos, ticket attachments. Never let an
  uploaded file be served in a way that could execute as code, and store uploads outside
  any path that could be interpreted as an application code path.

**Transport & headers**
- HTTPS only outside local dev, with HSTS enabled. CSRF protection stays on for any
  cookie-based session flow. CORS `ALLOWED_ORIGINS` is an explicit domain list — never
  `CORS_ALLOW_ALL_ORIGINS = True` in production.

**Mandatory security test cases.** For every new endpoint that touches money, users, or
internal data, its test suite must include: a test for anonymous access (expect a
rejection), a test for access by a *different* logged-in user (expect a rejection, not
someone else's data), and a test for role-based restriction where relevant (e.g. a
customer token can't reach an admin endpoint). Don't consider the endpoint done without
these three.

**If a security requirement is ambiguous** — e.g. a task says "update user profile" with
no mention of permissions — don't silently build the unsafe version. State the assumption
and the security measure you're applying (e.g. "I'll enforce that only the profile's
owner can update it") as part of your response, then proceed.

**Before treating any endpoint as done, check it against:** explicit serializer field
list · ownership/role check in `get_object()` · throttling class applied · indexes/
`select_related`/`prefetch_related` used if it touches the DB · PII masked if it's logged
· idempotency handled if it touches money.

## Workflow

- **Never perform a git commit (or push).** Stage changes or leave them uncommitted and
  describe what would go into the commit and why — the human reviews and commits themselves.
  This applies even if asked to "just commit it" mid-task; flag it back instead of running
  `git commit`.
- Run the linter/formatter and the relevant test subset before considering a task done —
  don't just report success because the code "looks right."
- When a task touches payments, tax, inventory, or auth/permissions, summarize what you
  changed and why in plain language at the end, even if the diff is small — these are the
  areas mistakes are most expensive in.

## Explicitly out of scope (don't build unless asked)

BNPL, WhatsApp Business Catalog/Cart, AR product preview, AI product advisor, consumable
subscriptions, Swahili localization, USSD ordering, installation/demo scheduling, online
warranty/AMC self-service, trade-in/recycling.
