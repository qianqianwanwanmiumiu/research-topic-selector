"""Offline regression checks; run with python -m unittest discover -s tests."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.error
import xml.etree.ElementTree as ET


SCRIPT = Path(__file__).resolve().parents[1] / "skills/research-topic-selector/scripts/search_literature.py"
SPEC = importlib.util.spec_from_file_location("search_literature", SCRIPT)
search = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(search)


def encoded(value):
    return json.dumps(value).encode()


class LiteratureSearchTests(unittest.TestCase):
    def run_main(self, request, arguments, environment=None):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "search.json"
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, environment or {}, clear=True), \
                    mock.patch.object(search, "request", side_effect=request), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = search.main([*arguments, "--out", str(output)])
            return code, json.loads(output.read_text(encoding="utf-8")), stdout.getvalue() + stderr.getvalue()

    def test_pubmed_mixed_xml_and_reference_doi(self):
        article = ET.fromstring("""<PubmedArticle><MedlineCitation><PMID>42</PMID><Article>
          <ArticleTitle>Heat <i>and</i> health: H<sub>2</sub>O</ArticleTitle>
          <Abstract><AbstractText Label="AIM">Read <b>all</b> text.</AbstractText>
          <AbstractText>Second <i>section</i>.</AbstractText></Abstract>
          </Article></MedlineCitation><PubmedData><ReferenceList><Reference>
          <ArticleIdList><ArticleId IdType="doi">10.1000/reference</ArticleId></ArticleIdList>
          </Reference></ReferenceList></PubmedData></PubmedArticle>""")
        item = search.pubmed_record(article)
        self.assertEqual(item["title"], "Heat and health: H2O")
        self.assertEqual(item["abstract"], "AIM: Read all text.\nSecond section.")
        self.assertEqual(item["pmid"], "42")
        self.assertIsNone(item["doi"])
        self.assertIsNone(item["year"])

    def test_merge_doi_and_pmid_preserves_sources_and_missing_fields(self):
        items = [
            dict(doi=" https://doi.org/10.1000/ABC ", pmid=None, title="First", abstract="", sources=["pubmed"]),
            dict(doi=None, pmid="42", title="", abstract="Available abstract", sources=["openalex"]),
            dict(doi="10.1000/abc", pmid="42", title="First", journal="Journal", sources=["openalex"]),
            dict(doi=None, pmid=None, title="First", sources=["pubmed"]),
        ]
        merged = search.deduplicate(items)
        self.assertEqual(len(merged), 2)  # The same title alone is not an identifier.
        item = merged[0]
        self.assertEqual(item["doi"], "10.1000/abc")
        self.assertEqual(item["pmid"], "42")
        self.assertEqual(item["abstract"], "Available abstract")
        self.assertEqual(item["journal"], "Journal")
        self.assertEqual(set(item["sources"]), {"pubmed", "openalex"})

    def test_both_databases_respect_limit_and_work_without_keys(self):
        calls = []

        def request(base, params, headers=None, ncbi=False):
            calls.append((base, dict(params), headers))
            if base.endswith("esearch.fcgi"):
                return encoded({"esearchresult": {"count": "250", "idlist": [str(i) for i in range(1, 102)]}})
            if base.endswith("efetch.fcgi"):
                articles = "".join("<PubmedArticle><MedlineCitation><PMID>{}</PMID><Article/>"
                                   "</MedlineCitation></PubmedArticle>".format(pmid) for pmid in params["id"].split(","))
                return ("<PubmedArticleSet>" + articles + "</PubmedArticleSet>").encode()
            start = 0 if params["cursor"] == "*" else 100
            return encoded({"meta": {"count": 250, "next_cursor": "next" if start == 0 else "end"},
                            "results": [{"id": "https://openalex.org/W{}".format(i)}
                                        for i in range(start, start + params["per_page"])]})

        code, output, _ = self.run_main(request, ["--pubmed-query", "heat", "--openalex-query", "heat", "--limit", "101"])
        self.assertEqual(code, 0)
        self.assertEqual(len(output["records"]), 202)
        for info in output["searches"]:
            self.assertEqual((info["retrieved_count"], info["total_count"], info["status"]), (101, 250, "ok"))
            self.assertTrue(info["truncated"])
            self.assertFalse(any(info["auth_configured"].values()))
        self.assertEqual([len(params["id"].split(",")) for base, params, _ in calls if base.endswith("efetch.fcgi")], [100, 1])
        self.assertEqual([params["per_page"] for base, params, _ in calls if "openalex" in base], [100, 1])
        self.assertEqual(calls[0][1]["retmax"], 101)
        for _, params, headers in calls:
            self.assertNotIn("api_key", params)
            self.assertNotIn("email", params)
            self.assertFalse(headers)

    def test_failed_database_preserves_other_results_and_redacts_credentials(self):
        environment = {"NCBI_API_KEY": "fake-ncbi-secret", "NCBI_EMAIL": "private@example.invalid", "OPENALEX_API_KEY": "fake-openalex-secret"}

        def request(base, params, headers=None, ncbi=False):
            if ncbi:
                raise RuntimeError("Service error: " + " ".join(environment.values()))
            return encoded({"meta": {"count": 1}, "results": [{"id": "https://openalex.org/W1", "title": "Kept"}]})

        code, output, logs = self.run_main(request, ["--pubmed-query", "heat", "--openalex-query", "heat"], environment)
        self.assertEqual(code, 1)
        self.assertEqual([info["status"] for info in output["searches"]], ["error", "ok"])
        self.assertEqual(output["records"][0]["title"], "Kept")
        self.assertEqual(output["searches"][1]["retrieved_count"], 1)
        for secret in environment.values():
            self.assertNotIn(secret, json.dumps(output) + logs)

    def test_partial_pagination_preserves_records(self):
        responses = [encoded({"meta": {"count": 3, "next_cursor": "page2"},
                              "results": [{"id": "https://openalex.org/W1"}]}), RuntimeError("Temporary failure")]
        code, output, _ = self.run_main(responses, ["--openalex-query", "heat", "--limit", "3"])
        info = output["searches"][0]
        self.assertEqual(code, 1)
        self.assertEqual((info["status"], info["retrieved_count"], info["total_count"]), ("partial", 1, 3))
        self.assertTrue(info["truncated"])
        self.assertEqual(len(output["records"]), 1)

    def test_zero_matches_is_success(self):
        code, output, _ = self.run_main([encoded({"meta": {"count": 0}, "results": []})], ["--openalex-query", "heat"])
        info = output["searches"][0]
        self.assertEqual(code, 0)
        self.assertEqual((info["status"], info["total_count"], info["retrieved_count"]), ("ok", 0, 0))
        self.assertFalse(info["truncated"])
        self.assertEqual(output["records"], [])

    def test_transport_error_does_not_leak_request_url(self):
        with mock.patch.object(search.urllib.request, "urlopen", side_effect=urllib.error.URLError("https://example.invalid/?api_key=secret")) as urlopen, \
                mock.patch.object(search.time, "sleep"):
            with self.assertRaises(RuntimeError) as error:
                search.request("https://example.invalid", {"api_key": "secret"})
        self.assertEqual(urlopen.call_count, 3)
        self.assertNotIn("secret", str(error.exception))


if __name__ == "__main__":
    unittest.main()
