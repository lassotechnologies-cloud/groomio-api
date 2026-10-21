"""Business onboarding — derived state plus the status endpoint that exposes it.

Business creation itself lives in `app.business.router` (API group B, B1). It is
here rather than duplicated because a second write path for the same tenant is
how two shops end up with one owner.
"""