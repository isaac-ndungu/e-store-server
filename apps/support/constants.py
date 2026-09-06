"""Tunable constants for the support app.

Field-length ceilings and attachment limits live here so they are reviewable
in one place rather than scattered across models, serializers, and services.
"""

TICKET_SUBJECT_MAX_LENGTH = 255
TICKET_MESSAGE_MAX_LENGTH = 10000
CHAT_MESSAGE_MAX_LENGTH = 4000

# A ticket attachment is a receipt, screenshot, or fault photo — an image or a
# PDF. Ten megabytes accommodates a phone photo without letting the endpoint
# be used to park arbitrarily large files.
ATTACHMENT_MAX_SIZE_MB = 10
