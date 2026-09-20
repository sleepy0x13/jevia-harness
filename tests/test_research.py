import os
import socket
import unittest

from jevharness.research import Hit, Page, _unwrap, sectionize, search
from jevharness.tools import _is_public

DDG = """
<div class="result">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fone&amp;rut=x">
    First <b>result</b></a>
  <a class="result__snippet" href="#">A snippet about the first thing.</a>
</div>
<div class="result">
  <a class="result__a" href="https://plain.example/two">Second result</a>
  <a class="result__snippet" href="#">Another snippet.</a>
</div>
"""


class TestUnwrap(unittest.TestCase):
    def test_duckduckgo_redirects_are_unwrapped(self):
        self.assertEqual(
            _unwrap("//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa%3Fb%3D1&rut=z"),
            "https://example.com/a?b=1",
        )

    def test_plain_urls_pass_through(self):
        self.assertEqual(_unwrap("https://plain.example/x"), "https://plain.example/x")

    def test_protocol_relative_urls_get_a_scheme(self):
        self.assertEqual(_unwrap("//plain.example/x"), "https://plain.example/x")


class TestSearchParsing(unittest.TestCase):
    def parse(self, body):
        """Exercise the parser without the network."""
        import jevharness.research as research

        captured = []

        class FakeResponse:
            def __init__(self, text):
                self.text = text.encode()

            def read(self, *a):
                return self.text

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        original = research.urllib.request.urlopen
        research.urllib.request.urlopen = lambda req, timeout=0: FakeResponse(body)
        try:
            return research.search("anything")
        finally:
            research.urllib.request.urlopen = original

    def test_titles_urls_and_snippets_are_extracted(self):
        hits = self.parse(DDG)
        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0].url, "https://example.com/one")
        self.assertEqual(hits[0].title, "First result")
        self.assertIn("first thing", hits[0].snippet)
        self.assertEqual(hits[1].url, "https://plain.example/two")
        self.assertEqual([h.rank for h in hits], [0, 1])

    def test_duplicate_urls_are_dropped(self):
        hits = self.parse(DDG + DDG)
        self.assertEqual(len(hits), 2)

    def test_a_failed_search_returns_nothing_rather_than_raising(self):
        import jevharness.research as research

        original = research.urllib.request.urlopen

        def boom(*a, **k):
            raise OSError("network down")

        research.urllib.request.urlopen = boom
        try:
            self.assertEqual(research.search("x"), [])
        finally:
            research.urllib.request.urlopen = original

    def test_an_empty_query_makes_no_request(self):
        self.assertEqual(search("   "), [])


class TestSectionize(unittest.TestCase):
    def page(self, text):
        return Page(url="https://e/x", title="T", text=text)

    def test_short_pages_make_one_section(self):
        sections = sectionize(self.page("word " * 80))
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0].index, 0)

    def test_long_pages_are_split(self):
        body = "\n\n".join("para " * 60 for _ in range(6))
        sections = sectionize(self.page(body))
        self.assertGreater(len(sections), 1)
        self.assertTrue(all(len(s.text) <= 2200 for s in sections))

    def test_scraps_below_the_floor_are_dropped(self):
        self.assertEqual(sectionize(self.page("tiny")), [])

    def test_the_limit_is_respected(self):
        body = "\n\n".join("para " * 60 for _ in range(200))
        self.assertLessEqual(len(sectionize(self.page(body), limit=5)), 5)


class TestHostGuard(unittest.TestCase):
    """The guard must behave the same on any machine, so DNS is stubbed."""

    def setUp(self):
        import jevharness.tools as tools

        self.tools = tools
        self._resolver = tools.socket.getaddrinfo
        self._env = os.environ.get("JEVIA_ALLOW_PRIVATE_HOSTS")
        os.environ.pop("JEVIA_ALLOW_PRIVATE_HOSTS", None)

    def tearDown(self):
        self.tools.socket.getaddrinfo = self._resolver
        if self._env is None:
            os.environ.pop("JEVIA_ALLOW_PRIVATE_HOSTS", None)
        else:
            os.environ["JEVIA_ALLOW_PRIVATE_HOSTS"] = self._env

    def resolve_to(self, address):
        self.tools.socket.getaddrinfo = lambda host, port: [(2, 1, 6, "", (address, 0))]

    def test_names_that_must_never_resolve(self):
        self.resolve_to("93.184.216.34")  # even a public answer must not save them
        for host in ("localhost", "foo.localhost", "thing.internal", "box.local",
                     "metadata.google.internal", "169.254.169.254", "127.0.0.1",
                     "10.0.0.5", "192.168.1.1", "::1", ""):
            self.assertFalse(_is_public(host), host)

    def test_a_public_address_is_allowed(self):
        self.resolve_to("93.184.216.34")
        self.assertTrue(_is_public("example.com"))

    def test_a_name_resolving_into_the_private_range_is_refused(self):
        self.resolve_to("10.1.2.3")
        self.assertFalse(_is_public("sneaky.example"))

    def test_an_unresolvable_name_is_refused(self):
        def boom(host, port):
            raise socket.gaierror("nope")

        self.tools.socket.getaddrinfo = boom
        self.assertFalse(_is_public("nowhere.example"))

    def test_the_escape_hatch_skips_only_the_resolution_check(self):
        self.resolve_to("10.1.2.3")
        os.environ["JEVIA_ALLOW_PRIVATE_HOSTS"] = "1"
        self.assertTrue(_is_public("proxied.example"))
        self.assertFalse(_is_public("localhost"), "name rules still apply")
        self.assertFalse(_is_public("127.0.0.1"), "literal addresses still apply")


if __name__ == "__main__":
    unittest.main()


class TestRedirectsAreCheckedBeforeTheyAreFollowed(unittest.TestCase):
    """Validating only the final URL still lets the first hop reach a private
    address, which is the whole trick behind server-side request forgery."""

    def handler(self):
        from jevharness.tools import _SafeRedirect

        return _SafeRedirect()

    def request_for(self, url):
        import urllib.request

        return urllib.request.Request(url)

    def test_a_hop_to_a_private_host_raises_instead_of_being_followed(self):
        import io
        import urllib.error

        for target in ("http://169.254.169.254/latest/meta-data", "http://127.0.0.1:8765/api/health",
                       "http://localhost/admin", "file:///etc/passwd"):
            with self.assertRaises(urllib.error.HTTPError, msg=target):
                self.handler().redirect_request(
                    self.request_for("https://example.test/a"), io.BytesIO(b""), 302, "Found",
                    {}, target)

    def test_a_public_hop_is_still_followed(self):
        import io
        from unittest import mock

        with mock.patch("jevharness.tools._is_public", return_value=True):
            made = self.handler().redirect_request(
                self.request_for("https://example.test/a"), io.BytesIO(b""), 302, "Found",
                {}, "https://example.test/b")
        self.assertEqual(made.full_url, "https://example.test/b")
