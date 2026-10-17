import os

MUTATION_TRACE_FACTOR = 7


def traced(deadline):
    return deadline * (MUTATION_TRACE_FACTOR if os.environ.get("MUTANT_UNDER_TEST") else 1)


def trace_browser_deadlines():
    from playwright.sync_api import BrowserContext, Page

    for owner in (Page, BrowserContext):
        for name in ("set_default_timeout", "set_default_navigation_timeout"):
            setter = getattr(owner, name)
            setattr(owner, name, lambda self, timeout, setter=setter: setter(self, traced(timeout)))
