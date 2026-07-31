"""Transport-security settings (see the SESSION_COOKIE_SECURE block in settings.py).

These pin the *derivation*, not literal values: the cookie flags must track the
DEBUG value from the environment so production gets Secure cookies while plain
`runserver` (HTTP) can still log in. A hardcoded True or False in settings.py
breaks one of those two environments and fails here.

Note the flags are compared against the DEBUG value in os.environ, NOT
settings.DEBUG - Django's test runner forces settings.DEBUG to False after
settings.py has already been evaluated, so settings.DEBUG is not the input the
derivation actually saw.
"""

import os

from django.conf import settings
from django.core.checks import Warning as CheckWarning
from django.core.checks.security import base as baseChecks
from django.core.checks.security import csrf as csrfChecks
from django.core.checks.security import sessions as sessionChecks
from django.test import SimpleTestCase, override_settings

# environ.Env.read_env(dev-env.env) populates os.environ, so this is the same
# string settings.py resolved DEBUG from.
_ENV_DEBUG = os.environ.get("DEBUG", "False").strip().lower() in ("1", "true", "yes", "on")


class SecureCookieSettingsTests(SimpleTestCase):
    def test_session_cookie_secure_tracks_debug(self):
        # DEBUG on  -> HTTP dev server, a Secure cookie would never come back.
        # DEBUG off -> production behind TLS, cookie must never cross plaintext.
        self.assertEqual(settings.SESSION_COOKIE_SECURE, not _ENV_DEBUG)

    def test_csrf_cookie_secure_tracks_debug(self):
        self.assertEqual(settings.CSRF_COOKIE_SECURE, not _ENV_DEBUG)

    def test_session_cookie_is_httponly(self):
        # Django's default, asserted so a future edit to this block cannot
        # quietly drop it while adding the Secure flag.
        self.assertTrue(settings.SESSION_COOKIE_HTTPONLY)


class HstsSettingsTests(SimpleTestCase):
    def test_hsts_is_opt_in(self):
        """HSTS must default to off.

        Unlike the cookie flags, a wrong HSTS value cannot be undone from the
        server: browsers cache the HTTPS-only policy for the full max-age. It is
        enabled per deployment via SECURE_HSTS_SECONDS once TLS is known-good.
        """
        self.assertEqual(settings.SECURE_HSTS_SECONDS, 0)
        self.assertFalse(settings.SECURE_HSTS_INCLUDE_SUBDOMAINS)
        self.assertFalse(settings.SECURE_HSTS_PRELOAD)

    def test_hsts_flags_are_names_django_reads(self):
        """A deployment that sets SECURE_HSTS_SECONDS actually gets HSTS.

        W005/W021 fire when INCLUDE_SUBDOMAINS or PRELOAD is set without a
        non-zero max-age - a half-configured rollout emitting no usable header.
        Silence here means Django recognises all three names, so the opt-in
        documented in settings.py works rather than being inert.
        """
        with override_settings(
            DEBUG=False,
            SECURE_HSTS_SECONDS=31536000,
            SECURE_HSTS_INCLUDE_SUBDOMAINS=True,
            SECURE_HSTS_PRELOAD=True,
        ):
            issues = (
                baseChecks.check_sts_include_subdomains(None)
                + baseChecks.check_sts_preload(None)
            )
        self.assertEqual([i for i in issues if isinstance(i, CheckWarning)], [])


class DeployCheckTests(SimpleTestCase):
    def test_cookie_checks_pass_under_production_settings(self):
        """Django's own W012/W016 deploy warnings must be silent in production.

        These are the checks `manage.py check --deploy` runs, and they are the
        real acceptance test for this settings block: with DEBUG off, the values
        settings.py computes are exactly the ones asserted here.
        """
        with override_settings(
            DEBUG=False,
            SESSION_COOKIE_SECURE=True,
            CSRF_COOKIE_SECURE=True,
        ):
            issues = (
                sessionChecks.check_session_cookie_secure(None)
                + csrfChecks.check_csrf_cookie_secure(None)
            )
        self.assertEqual(issues, [])

    def test_cookie_checks_would_flag_the_pre_fix_configuration(self):
        """Guard against the fix being reverted to Django's non-Secure default.

        If someone drops SESSION_COOKIE_SECURE/CSRF_COOKIE_SECURE back to False
        for production, Django's checkers must be the thing that notices - this
        asserts they still do, so the checks above are load-bearing rather than
        vacuously passing.
        """
        with override_settings(
            DEBUG=False,
            SESSION_COOKIE_SECURE=False,
            CSRF_COOKIE_SECURE=False,
        ):
            issues = (
                sessionChecks.check_session_cookie_secure(None)
                + csrfChecks.check_csrf_cookie_secure(None)
            )
        self.assertEqual({i.id for i in issues}, {"security.W012", "security.W016"})
