"""Public, unauthenticated booking-portal reads (Doc 10.5 D3/D8, CU1–CU3).

`app.appointments.public_book` (POST /public/{slug}/appointments) is the *write*
half of the customer journey, but on its own it is unusable: a customer arriving
on a shared branch link cannot book without first being shown the shop, its
services, its barbers and the times that are actually free. Those reads live
here.

Everything in this package is deliberately unauthenticated. That is fine because
it only ever exposes data a customer standing in the street could read off a
price list: shop name, service names/prices, barber names/photos, and free
slots. No customer, staff, sales or billing data is reachable from these routes
(Doc 11.3 — public endpoints skip the tenant filter).

The slot endpoint reuses the exact same `_find_conflict()` used by the booking
write, so the times we advertise are the times booking will actually accept.
That is the whole point: advertising a slot that then 409s is the fastest way to
make a customer never come back.
"""
