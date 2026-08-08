import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "yuque.py"
spec = importlib.util.spec_from_file_location("yuque_skill", MODULE_PATH)
yuque = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(yuque)


class FakeBooksClient:
    def __init__(self, books):
        self.books = books
    def request_json(self, method, path, **kwargs):
        return {"data": self.books}


class TestOriginValidation(unittest.TestCase):
    def test_yuque_subdomain_is_allowed(self):
        self.assertEqual(
            yuque._normalize_yuque_origin("https://dtstack.yuque.com"),
            "https://dtstack.yuque.com",
        )

    def test_public_alias_is_canonicalized(self):
        self.assertEqual(
            yuque._normalize_yuque_origin("https://yuque.com"),
            "https://www.yuque.com",
        )

    def test_non_yuque_domain_is_rejected(self):
        with self.assertRaises(yuque.YuqueError):
            yuque.clean_url("https://evil.example.com/team/book/doc")

    def test_custom_port_is_rejected(self):
        with self.assertRaises(yuque.YuqueError):
            yuque.clean_url("https://dtstack.yuque.com:8443/team/book/doc")

    def test_relative_url_is_rejected(self):
        with self.assertRaises(yuque.YuqueError):
            yuque.clean_url("team/book/doc")

    def test_client_requires_explicit_origin(self):
        with self.assertRaises(TypeError):
            yuque.YuqueClient()


class TestUnicodeShielding(unittest.TestCase):
    def test_restore_requires_exactly_one_token(self):
        protected, mapping = yuque.protect_non_bmp("A😀B")
        token = next(iter(mapping))
        self.assertEqual(yuque.restore_non_bmp(protected, mapping), "A😀B")
        with self.assertRaises(yuque.YuqueError):
            yuque.restore_non_bmp(protected.replace(token, "CHANGED"), mapping)
        with self.assertRaises(yuque.YuqueError):
            yuque.restore_non_bmp(protected.replace(token, token + token), mapping)


class TestConversionIntegrity(unittest.TestCase):
    class FakeConverter:
        def __init__(self):
            self.calls = []

        def convert(self, source, target, content):
            self.calls.append((source, target, content))
            if (source, target) == ("markdown", "lake"):
                return f"<lake>{content}</lake>"
            if (source, target) == ("lake", "html"):
                return f"<html>{content}</html>"
            if (source, target) == ("markdown", "html"):
                return f"<html>{content}</html>"
            raise AssertionError((source, target))

    def test_literal_nan_is_valid_content(self):
        fake = self.FakeConverter()
        result = yuque.YuqueClient.markdown_to_content(
            fake,
            "JavaScript NaN means Not-a-Number.",
        )
        self.assertIn("NaN", result["body_asl"])

    def test_lake_to_html_receives_protected_non_bmp_tokens(self):
        fake = self.FakeConverter()
        result = yuque.YuqueClient.markdown_to_content(
            fake,
            "Emoji 😀",
        )
        lake_html_call = next(
            call for call in fake.calls
            if call[0:2] == ("lake", "html")
        )
        self.assertNotIn("😀", lake_html_call[2])
        self.assertIn("YUQUEUNICODE", lake_html_call[2])
        self.assertIn("😀", result["body_asl"])
        self.assertIn("😀", result["body_html"])


class TestSearchScope(unittest.TestCase):
    def test_root_requires_related_or_organization(self):
        with self.assertRaises(yuque.YuqueError):
            yuque.parse_search_scope_url("https://dtstack.yuque.com/")

    def test_related_scope(self):
        result = yuque.parse_search_scope_url(
            "https://dtstack.yuque.com/",
            "related",
        )
        self.assertEqual(result["tab"], "related")
        self.assertEqual(result["scope"], "/")
        self.assertEqual(result["target_url"], "https://dtstack.yuque.com")

    def test_organization_scope(self):
        result = yuque.parse_search_scope_url(
            "https://dtstack.yuque.com/",
            "organization",
        )
        self.assertEqual(result["tab"], "organization")
        self.assertEqual(result["scope"], "/")

    def test_group_is_inferred_from_one_path_segment(self):
        result = yuque.parse_search_scope_url(
            "https://dtstack.yuque.com/rd-center",
        )
        self.assertEqual(result["tab"], "group")
        self.assertEqual(result["scope"], "rd-center")

    def test_book_is_inferred_from_two_path_segments(self):
        result = yuque.parse_search_scope_url(
            "https://dtstack.yuque.com/rd-center/tqk74v",
        )
        self.assertEqual(result["tab"], "book")
        self.assertEqual(result["scope"], "rd-center/tqk74v")

    def test_doc_url_searches_its_book(self):
        result = yuque.parse_search_scope_url(
            "https://dtstack.yuque.com/rd-center/tqk74v/doc-slug",
        )
        self.assertEqual(result["tab"], "book")
        self.assertEqual(result["scope"], "rd-center/tqk74v")
        self.assertEqual(
            result["target_url"],
            "https://dtstack.yuque.com/rd-center/tqk74v",
        )

    def test_scope_mismatch_is_rejected(self):
        with self.assertRaises(yuque.YuqueError):
            yuque.parse_search_scope_url(
                "https://dtstack.yuque.com/rd-center",
                "book",
            )

    def test_search_result_page_url_is_rejected(self):
        with self.assertRaises(yuque.YuqueError):
            yuque.parse_search_scope_url(
                "https://dtstack.yuque.com/search",
                "group",
            )


class TestSearch(unittest.TestCase):
    class FakeClient:
        def __init__(self, origin):
            self.origin = origin
            self.calls = []

        def request_json(self, method, path, **kwargs):
            self.calls.append((method, path, kwargs))
            return {
                "data": {
                    "type": "content",
                    "hits": [
                        {
                            "id": 280256843,
                            "title": "从 Skill 到可靠的 Bugfix Agent Workflow",
                            "slug": "bigv6d1gzz6z7qg3",
                            "type": "Doc",
                            "url": "/rd-center/tqk74v/bigv6d1gzz6z7qg3",
                            "abstract": "bugfix-<em>workflow</em> 说明",
                            "book_name": "数栈 UED 团队",
                            "group_name": "产品研发中心",
                            "_record": {
                                "book_id": 35616779,
                                "draft_version": 28,
                                "status": 1,
                                "updated_at": "2026-08-06T12:40:51.000Z",
                                "published_at": "2026-08-05T08:50:28.000Z",
                                "word_count": 3016,
                            },
                        }
                    ],
                    "totalHits": 5,
                    "numHits": 1,
                    "errorHits": 0,
                    "message": "OK",
                }
            }

    def test_book_search_maps_params_and_normalizes_hits(self):
        fake = self.FakeClient("https://dtstack.yuque.com")
        with patch.object(yuque, "YuqueClient", return_value=fake):
            result = yuque.search_yuque(
                "https://dtstack.yuque.com/rd-center/tqk74v",
                "bugfix-workflow",
            )

        self.assertEqual(result["search_scope"], "book")
        self.assertEqual(result["scope"], "rd-center/tqk74v")
        self.assertEqual(result["total_hits"], 5)
        self.assertEqual(result["results"][0]["abstract"], "bugfix-workflow 说明")
        self.assertEqual(
            result["results"][0]["url"],
            "https://dtstack.yuque.com/rd-center/tqk74v/bigv6d1gzz6z7qg3",
        )

        method, path, kwargs = fake.calls[0]
        self.assertEqual((method, path), ("GET", "/api/zsearch"))
        self.assertEqual(
            kwargs["params"],
            {
                "q": "bugfix-workflow",
                "type": "content",
                "scope": "rd-center/tqk74v",
                "tab": "book",
                "p": 1,
                "sence": "searchPage",
            },
        )
        self.assertTrue(kwargs["include_csrf"])

    def test_root_organization_search(self):
        fake = self.FakeClient("https://dtstack.yuque.com")
        with patch.object(yuque, "YuqueClient", return_value=fake):
            result = yuque.search_yuque(
                "https://dtstack.yuque.com/",
                "workflow",
                scope_mode="organization",
                page=2,
            )

        self.assertEqual(result["search_scope"], "organization")
        self.assertEqual(result["scope"], "/")
        self.assertEqual(result["page"], 2)
        self.assertEqual(fake.calls[0][2]["params"]["tab"], "organization")
        self.assertEqual(fake.calls[0][2]["params"]["p"], 2)

    def test_cross_origin_hit_url_is_not_exposed(self):
        hit = {
            "id": 1,
            "title": "bad",
            "url": "https://evil.example.com/doc",
            "abstract": "x",
        }
        result = yuque._normalize_search_hit(
            "https://dtstack.yuque.com",
            hit,
        )
        self.assertIsNone(result["url"])

    def test_empty_query_is_rejected(self):
        with self.assertRaises(yuque.YuqueError):
            yuque.search_yuque(
                "https://dtstack.yuque.com/rd-center",
                "   ",
            )


class TestSearchCsrfHeader(unittest.TestCase):
    class FakeResponse:
        ok = True
        status_code = 200
        headers = {"content-type": "application/json"}
        text = "{}"
        url = "https://dtstack.yuque.com/api/zsearch"

        def json(self):
            return {}

    def test_get_can_send_bootstrapped_csrf_header(self):
        session = MagicMock()
        session.trust_env = False
        session.headers = MagicMock()
        session.cookies = yuque.requests.cookies.RequestsCookieJar()
        session.request.return_value = self.FakeResponse()

        with patch.dict(os.environ, {"YUQUE_SESSION": "secret"}), \
             patch.object(yuque.requests, "Session", return_value=session):
            client = yuque.YuqueClient("https://dtstack.yuque.com")
            client._ctoken = "ctoken"
            client._bootstrapped = True
            client.request_json(
                "GET",
                "/api/zsearch",
                include_csrf=True,
            )

        headers = session.request.call_args.kwargs["headers"]
        self.assertEqual(headers["x-csrf-token"], "ctoken")


class TestBookResolution(unittest.TestCase):
    def info(self):
        return {"type": "book", "user": "right", "book": "same"}

    def test_namespace_selects_correct_duplicate_slug(self):
        books = [
            {"id": 111, "slug": "same", "namespace": "wrong/same"},
            {"id": 222, "slug": "same", "namespace": "right/same"},
        ]
        self.assertEqual(yuque.get_book_id(FakeBooksClient(books), self.info()), 222)

    def test_ambiguous_duplicate_slug_is_rejected(self):
        books = [
            {"id": 111, "slug": "same"},
            {"id": 222, "slug": "same"},
        ]
        with self.assertRaises(yuque.YuqueError):
            yuque.get_book_id(FakeBooksClient(books), self.info())

    def test_single_unknown_identity_is_allowed(self):
        books = [{"id": 222, "slug": "same"}]
        self.assertEqual(yuque.get_book_id(FakeBooksClient(books), self.info()), 222)

    def test_single_known_namespace_mismatch_is_rejected(self):
        books = [{"id": 222, "slug": "same", "namespace": "wrong/same"}]
        with self.assertRaises(yuque.YuqueError):
            yuque.get_book_id(FakeBooksClient(books), self.info())


    def test_write_rejects_unverified_unique_slug(self):
        books = [{"id": 222, "slug": "same"}]
        with self.assertRaises(yuque.YuqueError):
            yuque.get_book_id(
                FakeBooksClient(books),
                self.info(),
                require_verified_namespace=True,
            )

    def test_concrete_namespace_has_priority_over_owner(self):
        books = [{
            "id": 222,
            "slug": "same",
            "namespace": "wrong/same",
            "owner": {"login": "right"},
        }]
        with self.assertRaises(yuque.YuqueError):
            yuque.get_book_id(
                FakeBooksClient(books),
                self.info(),
                require_verified_namespace=True,
            )

    def test_write_can_verify_namespace_from_book_detail(self):
        class DetailClient:
            def request_json(self, method, path, **kwargs):
                if path == "/api/mine/books":
                    return {"data": [{"id": 222, "slug": "same"}]}
                if path == "/api/books/222":
                    return {
                        "data": {
                            "id": 222,
                            "slug": "same",
                            "namespace": "right/same",
                        }
                    }
                raise AssertionError(path)

        self.assertEqual(
            yuque.get_book_id(
                DetailClient(),
                self.info(),
                require_verified_namespace=True,
            ),
            222,
        )


class TestTitleDedup(unittest.TestCase):
    def test_plain_unique_first_h1_can_be_inferred(self):
        body, title, meta = yuque.prepare_markdown_for_create("# 周报\n\n正文\n", None)
        self.assertEqual(title, "周报")
        self.assertTrue(meta["h1_deduplicated"])
        self.assertEqual(body, "正文\n")

    def test_formatted_h1_requires_explicit_title(self):
        for markdown in (
            "# **周报**\n\n正文\n",
            "# _周报_\n\n正文\n",
            "# <span>周报</span>\n\n正文\n",
        ):
            with self.subTest(markdown=markdown):
                with self.assertRaises(yuque.YuqueError):
                    yuque.prepare_markdown_for_create(markdown, None)

    def test_multiple_h1_never_deduplicates(self):
        md = "# A\n\n# B\n"
        body, _, meta = yuque.prepare_markdown_for_create(md, "A")
        self.assertEqual(body, md)
        self.assertFalse(meta["h1_deduplicated"])


class DummyClient:
    pass


class TestSaveVerification(unittest.TestCase):
    def setUp(self):
        self.info = {"type": "doc", "doc": "slug"}

    def test_exact_readback_version_is_accepted(self):
        current = {
            "id": 7,
            "body_draft_asl": "NEW_DRAFT",
            "draft_version": 5,
            "published_at": "2026-08-01T00:00:00Z",
        }
        response = {"data": {
            "id": 7,
            "body_draft_asl": "NEW_DRAFT",
            "draft_version": 5,
        }}
        with patch.object(yuque, "get_doc_data", return_value=current):
            result = yuque.verify_saved_content(
                DummyClient(), self.info, 1, 4,
                save_response=response,
                expected_doc_id=7,
            )
        self.assertEqual(
            result["verification_source"],
            "readback_exact_version",
        )
        self.assertEqual(result["saved_draft_version"], 5)

    def test_hidden_draft_uses_exact_save_response(self):
        current = {
            "id": 7,
            "body_draft_asl": None,
            "body_asl": "OLD_PUBLISHED",
            "draft_version": 5,
            "published_at": "2026-08-01T00:00:00Z",
        }
        response = {"data": {
            "id": 7,
            "body_draft_asl": "NEW_DRAFT",
            "draft_version": 5,
        }}
        with patch.object(yuque, "get_doc_data", return_value=current):
            result = yuque.verify_saved_content(
                DummyClient(), self.info, 1, 4,
                save_response=response,
                expected_doc_id=7,
            )
        self.assertEqual(result["verification_source"], "save_response")
        self.assertEqual(result["body_field"], "body_draft_asl")

    def test_concurrent_newer_readback_is_rejected(self):
        current = {
            "id": 7,
            "body_draft_asl": "OTHER_WRITER",
            "draft_version": 6,
            "published_at": "2026-08-01T00:00:00Z",
        }
        response = {"data": {
            "id": 7,
            "body_draft_asl": "MY_WRITE",
            "draft_version": 5,
        }}
        with patch.object(yuque, "get_doc_data", return_value=current):
            with self.assertRaises(yuque.YuqueError):
                yuque.verify_saved_content(
                    DummyClient(), self.info, 1, 4,
                    save_response=response,
                    expected_doc_id=7,
                )

    def test_save_response_must_advance_version(self):
        current = {"id": 7, "draft_version": 4}
        response = {"data": {
            "id": 7,
            "body_draft_asl": "NEW_DRAFT",
            "draft_version": 4,
        }}
        with patch.object(yuque, "get_doc_data", return_value=current):
            with self.assertRaises(yuque.YuqueError):
                yuque.verify_saved_content(
                    DummyClient(), self.info, 1, 4,
                    save_response=response,
                    expected_doc_id=7,
                )


class TestSaveRetry(unittest.TestCase):
    def setUp(self):
        self.info = {"type": "doc", "doc": "slug"}
        self.content = {"body_asl": "<lake>x</lake>", "body_html": None}

    def test_non_409_http_error_is_not_retried_even_if_message_has_version(self):
        doc = {"id": 7, "draft_version": 4}
        error = yuque.YuqueHttpError(
            500,
            "HTTP 500 service version unavailable",
        )
        with patch.object(yuque, "get_doc_data", return_value=doc), \
             patch.object(yuque, "save_content", side_effect=error) as save:
            with self.assertRaises(yuque.YuqueHttpError):
                yuque.save_with_retry(
                    DummyClient(),
                    self.info,
                    1,
                    self.content,
                )
        self.assertEqual(save.call_count, 1)

    def test_http_409_is_retried_once(self):
        docs = [
            {"id": 7, "draft_version": 4},
            {"id": 7, "draft_version": 5},
        ]
        save_response = {
            "data": {
                "id": 7,
                "body_draft_asl": "<lake>x</lake>",
                "draft_version": 6,
            }
        }
        with patch.object(yuque, "get_doc_data", side_effect=docs), \
             patch.object(
                 yuque,
                 "save_content",
                 side_effect=[
                     yuque.YuqueHttpError(409, "conflict"),
                     save_response,
                 ],
             ) as save, \
             patch.object(
                 yuque,
                 "verify_saved_content",
                 return_value={"saved_draft_version": 6},
             ):
            result = yuque.save_with_retry(
                DummyClient(),
                self.info,
                1,
                self.content,
            )
        self.assertEqual(save.call_count, 2)
        self.assertEqual(result["verification"]["saved_draft_version"], 6)

    def test_existing_update_409_is_not_retried(self):
        with patch.object(
            yuque,
            "save_content",
            side_effect=yuque.YuqueHttpError(409, "conflict"),
        ) as save, patch.object(
            yuque,
            "get_doc_data",
        ) as get_doc:
            with self.assertRaises(yuque.YuqueError):
                yuque.save_with_retry(
                    DummyClient(),
                    self.info,
                    1,
                    self.content,
                    expected_doc_id=7,
                    expected_draft_version=4,
                    retry_on_409=False,
                )

        self.assertEqual(save.call_count, 1)
        get_doc.assert_not_called()


class TestUpdateCASWiring(unittest.TestCase):
    def test_update_uses_version_read_before_conversion(self):
        class FakeClient:
            def markdown_to_content(self, markdown):
                return {
                    "body_asl": "<lake>new</lake>",
                    "body_html": None,
                    "html_mode": None,
                }

        before = {
            "id": 7,
            "title": "Doc",
            "draft_version": 4,
        }
        saved = {
            "response": {"data": {"draft_version": 5}},
            "verification": {"saved_draft_version": 5},
        }

        with patch.object(
            yuque,
            "load_markdown_source",
            return_value=("正文", "stdin"),
        ), patch.object(
            yuque,
            "YuqueClient",
            return_value=FakeClient(),
        ), patch.object(
            yuque,
            "get_book_id",
            return_value=1,
        ) as get_book, patch.object(
            yuque,
            "get_doc_data",
            return_value=before,
        ), patch.object(
            yuque,
            "save_with_retry",
            return_value=saved,
        ) as save:
            yuque.update_from_markdown(
                "https://www.yuque.com/right/same/doc",
                None,
                True,
                False,
            )

        get_book.assert_called_once()
        self.assertTrue(
            get_book.call_args.kwargs["require_verified_namespace"]
        )
        self.assertEqual(
            save.call_args.kwargs["expected_doc_id"],
            7,
        )
        self.assertEqual(
            save.call_args.kwargs["expected_draft_version"],
            4,
        )
        self.assertFalse(save.call_args.kwargs["retry_on_409"])


class TestPublishVerification(unittest.TestCase):
    def setUp(self):
        self.info = {"type": "doc", "doc": "slug"}

    def test_publish_transition_is_accepted(self):
        before = {
            "id": 7,
            "draft_version": 5,
            "published_at": "2026-08-01T00:00:00Z",
            "updated_at": "2026-08-08T00:00:00Z",
            "status": 1,
        }
        current = {
            "id": 7,
            "draft_version": 5,
            "published_at": "2026-08-08T01:00:00Z",
            "updated_at": "2026-08-08T01:00:00Z",
            "status": 1,
        }
        with patch.object(yuque, "get_doc_data", return_value=current):
            result = yuque.verify_published_doc(
                DummyClient(),
                self.info,
                1,
                before_publish=before,
                expected_draft_version=5,
                publish_response={"data": {"id": 7}},
                expected_doc_id=7,
            )
        self.assertTrue(result["verified"])
        self.assertEqual(
            result["verification_source"],
            "readback_transition",
        )

    def test_old_published_marker_does_not_fake_new_publish(self):
        before = {
            "id": 7,
            "draft_version": 5,
            "published_at": "2026-08-01T00:00:00Z",
            "updated_at": "2026-08-08T00:00:00Z",
            "status": 1,
        }
        current = dict(before)
        with patch.object(yuque, "get_doc_data", return_value=current), \
             patch.object(yuque.time, "sleep", return_value=None):
            with self.assertRaises(yuque.YuqueError):
                yuque.verify_published_doc(
                    DummyClient(),
                    self.info,
                    1,
                    before_publish=before,
                    expected_draft_version=5,
                    publish_response={"data": dict(before)},
                    expected_doc_id=7,
                )

    def test_concurrent_newer_version_after_publish_is_rejected(self):
        before = {
            "id": 7,
            "draft_version": 5,
            "published_at": None,
            "updated_at": "2026-08-08T00:00:00Z",
            "status": 0,
        }
        current = {
            "id": 7,
            "draft_version": 6,
            "published_at": "2026-08-08T01:00:00Z",
            "updated_at": "2026-08-08T01:00:00Z",
            "status": 1,
        }
        with patch.object(yuque, "get_doc_data", return_value=current):
            with self.assertRaises(yuque.YuqueError):
                yuque.verify_published_doc(
                    DummyClient(),
                    self.info,
                    1,
                    before_publish=before,
                    expected_draft_version=5,
                    publish_response={"data": {"id": 7}},
                    expected_doc_id=7,
                )

    def test_publish_guard_rejects_newer_server_version(self):
        current = {"id": 7, "draft_version": 6}
        with patch.object(yuque, "get_doc_data", return_value=current):
            with self.assertRaises(yuque.YuqueError):
                yuque.assert_publish_version(
                    DummyClient(),
                    self.info,
                    1,
                    expected_doc_id=7,
                    expected_draft_version=5,
                )


class TestBootstrapRedirectSafety(unittest.TestCase):
    class FakeResponse:
        def __init__(self, status_code, url, location=None):
            self.status_code = status_code
            self.url = url
            self.headers = {}
            if location is not None:
                self.headers["location"] = location
            self.ok = 200 <= status_code < 300
            self.text = ""

    def _fake_session(self, responses):
        session = MagicMock()
        session.trust_env = False
        session.headers = MagicMock()
        session.cookies = yuque.requests.cookies.RequestsCookieJar()
        session.get.side_effect = responses
        return session

    def test_cross_origin_redirect_is_not_followed(self):
        first = self.FakeResponse(
            302,
            "https://dtstack.yuque.com/",
            "https://evil.example.com/login",
        )
        session = self._fake_session([first])
        with patch.dict(os.environ, {"YUQUE_SESSION": "secret"}), \
             patch.object(yuque.requests, "Session", return_value=session):
            client = yuque.YuqueClient("https://dtstack.yuque.com")
            with self.assertRaises(yuque.YuqueError):
                client.bootstrap(verify_auth=False)
        self.assertEqual(session.get.call_count, 1)
        session.get.assert_called_once_with(
            "https://dtstack.yuque.com/",
            timeout=yuque.DEFAULT_TIMEOUT,
            allow_redirects=False,
        )

    def test_same_origin_redirect_can_be_followed_manually(self):
        first = self.FakeResponse(
            302,
            "https://dtstack.yuque.com/",
            "/home",
        )
        second = self.FakeResponse(
            200,
            "https://dtstack.yuque.com/home",
        )
        session = self._fake_session([first, second])
        session.cookies.set(
            "yuque_ctoken",
            "ctoken",
            domain="dtstack.yuque.com",
            path="/",
        )
        with patch.dict(os.environ, {"YUQUE_SESSION": "secret"}), \
             patch.object(yuque.requests, "Session", return_value=session):
            client = yuque.YuqueClient("https://dtstack.yuque.com")
            token = client.bootstrap(verify_auth=False)
        self.assertEqual(token, "ctoken")
        self.assertEqual(session.get.call_count, 2)


class TestReadAndListCompatibility(unittest.TestCase):
    def test_read_does_not_claim_hidden_draft_is_empty(self):
        current = {
            "id": 7,
            "slug": "doc",
            "title": "Draft",
            "draft_version": 3,
            "published_at": None,
            "content": None,
            "body_html": None,
            "body": None,
            "body_draft_asl": None,
            "body_asl": None,
        }
        with patch.object(yuque, "YuqueClient", return_value=DummyClient()), \
             patch.object(yuque, "get_book_id", return_value=1), \
             patch.object(yuque, "get_doc_data", return_value=current):
            result = yuque.read_url("https://www.yuque.com/right/same/doc")
        self.assertEqual(result["content_state"], "not_exposed_or_empty")
        self.assertFalse(result["content_available"])

    def test_list_stops_if_offset_is_ignored(self):
        batch = [
            {"id": i, "slug": f"doc-{i}", "title": f"Doc {i}"}
            for i in range(100)
        ]
        class FakeListClient:
            def request_json(self, method, path, **kwargs):
                return {"data": batch}
        with patch.object(yuque, "YuqueClient", return_value=FakeListClient()), \
             patch.object(yuque, "get_book_id", return_value=1):
            result = yuque.list_book("https://www.yuque.com/right/same")
        self.assertEqual(result["count"], 100)
        self.assertTrue(result["truncated"])
        self.assertFalse(result["pagination_supported"])


class TestDoctor(unittest.TestCase):
    def test_doctor_uses_production_conversion_path(self):
        class FakeClient:
            origin = "https://www.yuque.com"
            class Session:
                trust_env = False
            session = Session()
            def bootstrap(self, verify_auth=True):
                return "ctoken"
            def markdown_to_content(self, markdown):
                self.seen = markdown
                return {"body_asl": "<lake>😀</lake>", "body_html": None, "html_mode": None}
        fake = FakeClient()
        with patch.object(yuque, "YuqueClient", return_value=fake):
            result = yuque.doctor("https://www.yuque.com")
        self.assertTrue(result["unicode_shielding"])
        self.assertTrue(result["write_ready"])
        self.assertIn("💡", fake.seen)


if __name__ == "__main__":
    unittest.main()
