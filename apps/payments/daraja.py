"""Low-level Safaricom Daraja API client.

Encapsulates OAuth token management, STK Push initiation, and B2C payout
requests.  All HTTP I/O uses the standard library (``urllib.request``) so
no additional dependency is introduced.

Token lifecycle:
    ``get_access_token`` caches the bearer token for its declared lifetime
    (typically 3599 seconds) in Django's cache backend so repeated calls
    within the same worker process (and across workers sharing a Redis)
    do not re-authenticate.

Security:
    Credentials are read from environment variables via ``python-decouple``
    and are never stored in code or committed to the repository.
"""

import base64
import json
import logging
import urllib.error
import urllib.request
from datetime import datetime
from decimal import Decimal

from decouple import config
from django.core.cache import cache

logger = logging.getLogger(__name__)

_DARAJA_BASE_URL = config(
    "DARAJA_BASE_URL",
    default="https://sandbox.safaricom.co.ke",
)
_MPESA_SHORTCODE = config("MPESA_SHORTCODE", default="")
_MPESA_PASSKEY = config("MPESA_PASSKEY", default="")
_MPESA_CALLBACK_URL = config("MPESA_CALLBACK_URL", default="")
_MPESA_B2C_CALLBACK_URL = config("MPESA_B2C_CALLBACK_URL", default="")
_MPESA_INITIATOR_NAME = config("MPESA_INITIATOR_NAME", default="")
_MPESA_SECURITY_CREDENTIAL = config("MPESA_SECURITY_CREDENTIAL", default="")
_MPESA_TIMEOUT_URL = config("MPESA_TIMEOUT_URL", default="")
_MPESA_RESULT_URL = config("MPESA_RESULT_URL", default="")
_MPESA_CALLBACK_SECRET = config("MPESA_CALLBACK_SECRET", default="")

_ACCESS_TOKEN_CACHE_KEY = "daraja:access_token"
_ACCESS_TOKEN_TTL = 3500  # slightly less than the 3600s Safaricom grants


class DarajaError(Exception):
    """Raised when a Daraja API call fails.

    Attributes:
        status_code: the HTTP status code, if available.
        response_body: the parsed JSON body of the error response.
    """

    def __init__(self, message, status_code=None, response_body=None):
        """Initialize the error.

        Args:
            message (str): a human-readable description of the failure.
            status_code (int | None): the HTTP status code from Daraja.
            response_body (dict | None): the parsed JSON error body.
        """
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


def _http_post(url, data, headers=None, timeout=30):
    """Send a POST request and return the parsed JSON response.

    Args:
        url (str): the target URL.
        data (dict): the JSON-serializable request body.
        headers (dict | None): additional HTTP headers.
        timeout (int): request timeout in seconds.

    Returns:
        dict: the parsed JSON response body.

    Raises:
        DarajaError: if the HTTP request fails or returns a non-2xx status.
    """
    payload = json.dumps(data, default=str).encode("utf-8")
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)

    req = urllib.request.Request(url, data=payload, headers=req_headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        try:
            body_json = json.loads(body_text)
        except (json.JSONDecodeError, ValueError) as _parse_err:
            body_json = {"raw": body_text}
        logger.error(
            "Daraja API error %s at %s: %s",
            exc.code,
            url,
            body_json,
        )
        raise DarajaError(
            f"Daraja API returned HTTP {exc.code}",
            status_code=exc.code,
            response_body=body_json,
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        logger.exception("Daraja API network error at %s", url)
        raise DarajaError(f"Network error calling Daraja: {exc}") from exc


def get_access_token():
    """Return a valid Daraja OAuth access token, using cache when possible.

    Safaricom tokens expire after 3600 seconds.  The token is cached for
    3500 seconds so a stale token is never served.

    Returns:
        str: a valid bearer token.

    Raises:
        DarajaError: if the token endpoint is unreachable or returns an error.
    """
    cached = cache.get(_ACCESS_TOKEN_CACHE_KEY)
    if cached:
        return cached

    consumer_key = config("MPESA_CONSUMER_KEY", default="")
    consumer_secret = config("MPESA_CONSUMER_SECRET", default="")
    url = f"{_DARAJA_BASE_URL}/oauth/v1/generate"

    credentials = base64.b64encode(
        f"{consumer_key}:{consumer_secret}".encode()
    ).decode()
    token = _http_post(
        url,
        data={},
        headers={"Authorization": f"Basic {credentials}"},
    ).get("access_token", "")

    if not token:
        raise DarajaError("Daraja OAuth returned an empty access token")

    cache.set(_ACCESS_TOKEN_CACHE_KEY, token, _ACCESS_TOKEN_TTL)
    return token


def _generate_password(timestamp):
    """Generate the Daraja API password from the shortcode, passkey, and timestamp.

    Args:
        timestamp (str): the YYYYMMDDHHmmss timestamp string.

    Returns:
        str: the base64-encoded password.
    """
    raw = f"{_MPESA_SHORTCODE}{_MPESA_PASSKEY}{timestamp}"
    return base64.b64encode(raw.encode()).decode()


def _format_timestamp(dt=None):
    """Return a Daraja-compatible timestamp string.

    Args:
        dt (datetime | None): the datetime to format; defaults to now.

    Returns:
        str: ``YYYYMMDDHHmmss`` format.
    """
    if dt is None:
        dt = datetime.now()
    return dt.strftime("%Y%m%d%H%M%S")


def _callback_url_with_token(base_url):
    """Return the base URL with the shared callback secret as a query param.

    The token is appended so the callback that Safaricom POSTs back carries
    the secret; the receiving view rejects callbacks that lack it.  When no
    secret is configured the URL is returned unchanged.

    Args:
        base_url (str): the configured callback URL.

    Returns:
        str: the callback URL with ``?token=<secret>`` appended, or the base
            URL unchanged when no secret is set.
    """
    if not _MPESA_CALLBACK_SECRET or not base_url:
        return base_url
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}token={_MPESA_CALLBACK_SECRET}"


def _normalize_mpesa_phone(phone):
    """Normalize a phone number for Daraja (strip leading +, ensure 254 prefix).

    Args:
        phone (str): an E.164 phone number.

    Returns:
        str: the Daraja-formatted number (254XXXXXXXXX).
    """
    digits = "".join(ch for ch in phone if ch.isdigit())
    if digits.startswith("254"):
        return digits
    if digits.startswith("0"):
        return "254" + digits[1:]
    return digits


def initiate_stk_push(phone_number, amount, order_id, account_reference):
    """Initiate an M-Pesa STK Push (Lipa Na M-Pesa Online) request.

    Args:
        phone_number (str): E.164 phone number to charge.
        amount (Decimal): the amount to charge.
        order_id (int): the order primary key, used as the account reference.
        account_reference (str): a short transaction description.

    Returns:
        dict: the Daraja response with ``MerchantRequestID`` and
            ``CheckoutRequestID``.

    Raises:
        DarajaError: if the Daraja API call fails.
    """
    access_token = get_access_token()
    timestamp = _format_timestamp()
    password = _generate_password(timestamp)
    phone = _normalize_mpesa_phone(phone_number)
    amount_int = int(Decimal(str(amount)).quantize(Decimal("1")))

    url = f"{_DARAJA_BASE_URL}/mpesa/stkpush/v1/processrequest"
    payload = {
        "BusinessShortCode": _MPESA_SHORTCODE,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerBuyGoodsOnline",
        "Amount": amount_int,
        "PartyA": phone,
        "PartyB": _MPESA_SHORTCODE,
        "PhoneNumber": phone,
        "CallBackURL": _callback_url_with_token(_MPESA_CALLBACK_URL),
        "AccountReference": str(account_reference)[:12],
        "TransactionDesc": account_reference[:13],
    }

    logger.info(
        "Initiating STK Push for order %s, amount %s, phone %s",
        order_id,
        amount,
        f"+{phone[-2:]}",
    )

    return _http_post(
        url,
        data=payload,
        headers={"Authorization": f"Bearer {access_token}"},
    )


def query_stk_push_status(checkout_request_id):
    """Query the status of an STK Push transaction.

    Used as a fallback when the callback is not received within the expected
    window.  The response ``ResponseCode`` is ``0`` for success.

    Args:
        checkout_request_id (str): the Daraja checkout request ID.

    Returns:
        dict: the Daraja query response body.
    """
    access_token = get_access_token()
    timestamp = _format_timestamp()
    password = _generate_password(timestamp)

    url = f"{_DARAJA_BASE_URL}/mpesa/transactionstatus/v1/query"
    payload = {
        "BusinessShortCode": _MPESA_SHORTCODE,
        "Password": password,
        "Timestamp": timestamp,
        "CheckoutRequestID": checkout_request_id,
        "Initiator": _MPESA_INITIATOR_NAME,
        "SecurityCredential": _MPESA_SECURITY_CREDENTIAL,
        "CommandID": "TransactionStatusQuery",
        "QueueTimeOutURL": _MPESA_TIMEOUT_URL,
        "ResultURL": _MPESA_RESULT_URL,
    }

    return _http_post(
        url,
        data=payload,
        headers={"Authorization": f"Bearer {access_token}"},
    )


def initiate_b2c_payment(phone_number, amount, occasion, remarks):
    """Initiate an M-Pesa B2C (Business to Customer) payment.

    Used for refunds and payouts.  The ``occasion`` and ``remarks`` are
    included in the customer's M-Pesa notification.

    Args:
        phone_number (str): E.164 phone number of the recipient.
        amount (Decimal): the amount to pay.
        occasion (str): a short description shown in the customer's M-Pesa
            message (e.g. "Order #123 refund").
        remarks (str): internal remarks for the transaction.

    Returns:
        dict: the Daraja response with ``ConversationID`` and
            ``OriginatorConversationID``.

    Raises:
        DarajaError: if the Daraja API call fails.
    """
    access_token = get_access_token()
    phone = _normalize_mpesa_phone(phone_number)
    amount_int = int(Decimal(str(amount)).quantize(Decimal("1")))

    url = f"{_DARAJA_BASE_URL}/mpesa/b2c/v1/paymentrequest"
    payload = {
        "InitiatorName": _MPESA_INITIATOR_NAME,
        "SecurityCredential": _MPESA_SECURITY_CREDENTIAL,
        "CommandID": "BusinessPayment",
        "Amount": amount_int,
        "PartyA": _MPESA_SHORTCODE,
        "PartyB": phone,
        "Remarks": remarks[:255],
        "QueueTimeOutURL": _callback_url_with_token(_MPESA_B2C_CALLBACK_URL),
        "ResultURL": _callback_url_with_token(_MPESA_B2C_CALLBACK_URL),
        "Occasion": occasion[:255],
    }

    logger.info(
        "Initiating B2C payment to phone %s, amount %s",
        f"+{phone[-2:]}",
        amount,
    )

    return _http_post(
        url,
        data=payload,
        headers={"Authorization": f"Bearer {access_token}"},
    )
