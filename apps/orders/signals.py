"""Order-domain signals.

``order_confirmed`` is emitted by the staff intake path
(``create_staff_order``) after an order's stock is deducted and its status
lands in ``confirmed``.  Downstream systems that must react to a completed
order — tax invoice generation, fulfilment dispatch, analytics — connect a
receiver to it rather than patching the intake internals, so the order logic
stays closed while the set of side effects stays open.
"""

from django.dispatch import Signal

# Sent with ``sender`` = the ``Order`` instance.  No explicit keyword arguments
# are defined; receivers read fields off the order itself.
order_confirmed = Signal()
