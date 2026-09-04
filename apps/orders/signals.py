"""Order-domain signals.

``order_confirmed`` is emitted from the single confirmation path
(``confirm_order_from_verification``) after an order's stock is fulfilled and
its status moves to ``confirmed``.  Downstream systems that must react to a
completed order — tax invoice generation, fulfilment dispatch, analytics —
connect a receiver to it rather than patching the confirmation internals, so
the confirmation logic stays closed while the set of side effects stays open.
"""

from django.dispatch import Signal

# Sent with ``sender`` = the ``Order`` instance.  No explicit keyword arguments
# are defined; receivers read fields off the order itself.
order_confirmed = Signal()
