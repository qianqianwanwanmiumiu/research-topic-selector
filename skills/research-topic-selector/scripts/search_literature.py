#!/usr/bin/env python3
"""Search PubMed and OpenAlex using only the Python standard library."""

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


_last_ncbi_request = 0.0


def request(base, params, headers=None, ncbi=False):
    """Fetch bytes, with bounded retries and errors that cannot expose API keys."""
    global _last_ncbi_request
    url = base + "?" + urllib.parse.urlencode(params)
    headers = {"User-Agent": "research-topic-selector/0.2", **(headers or {})}
    for attempt in range(3):
        if ncbi:
            time.sleep(max(0, 0.35 - (time.monotonic() - _last_ncbi_request)))
            _last_ncbi_request = time.monotonic()
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers=headers), timeout=30
            ) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            message = "HTTP {} from literature service.".format(exc.code)
            retry = exc.code == 429 or 500 <= exc.code < 600
            exc.close()
            if not retry or attempt == 2:
                raise RuntimeError(message) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == 2:
                raise RuntimeError("Network request failed or timed out.") from None
        time.sleep(2 ** attempt)


def text(element):
    return " ".join("".join(element.itertext()).split()) if element is not None else ""


def normalize_doi(value):
    return re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", (value or "").strip(), flags=re.I).strip().lower() or None


def record(source):
    return dict(title="", authors=[], year=None, publication_date=None, journal="",
                doi=None, pmid=None, openalex_id=None, abstract="", url=None,
                is_retracted=None, sources=[source])


def publication_date(element):
    if element is None:
        return None, None
    year = element.findtext("Year")
    if not year:
        match = re.search(r"\b\d{4}\b", element.findtext("MedlineDate", ""))
        year = match.group() if match else None
    if not year or not year.isdigit():
        return None, None
    month = element.findtext("Month", "")
    if month and not month.isdigit():
        months = "jan feb mar apr may jun jul aug sep oct nov dec".split()
        month = str(months.index(month[:3].lower()) + 1) if month[:3].lower() in months else ""
    value = year
    if month and 1 <= int(month) <= 12:
        value += "-{:02d}".format(int(month))
        day = element.findtext("Day", "")
        if day.isdigit():
            try:
                value = dt.date(int(year), int(month), int(day)).isoformat()
            except ValueError:
                pass
    return int(year), value


def pubmed_record(element):
    citation = element.find("MedlineCitation")
    article = citation.find("Article") if citation is not None else None
    if article is None:
        citation = element.find("BookDocument")
        article = citation
    if citation is None or article is None:
        raise RuntimeError("PubMed returned an unsupported record.")
    item = record("pubmed")
    item["pmid"] = citation.findtext("PMID")
    item["title"] = text(article.find("ArticleTitle")) or text(article.find("Book/BookTitle"))
    for author in article.findall("AuthorList/Author") or article.findall("Book/AuthorList/Author"):
        name = text(author.find("CollectiveName"))
        if not name:
            name = " ".join(filter(None, [author.findtext("ForeName") or author.findtext("Initials"), author.findtext("LastName")]))
        if name:
            item["authors"].append(name)
    item["journal"] = article.findtext("Journal/Title") or article.findtext("Book/BookTitle") or ""
    date = article.find("ArticleDate")
    if date is None:
        date = article.find("Journal/JournalIssue/PubDate")
    if date is None:
        date = article.find("Book/PubDate")
    item["year"], item["publication_date"] = publication_date(date)
    abstracts = article.findall("Abstract/AbstractText") or citation.findall("OtherAbstract/AbstractText")
    item["abstract"] = "\n".join(
        ((section.get("Label") + ": ") if section.get("Label") else "") + text(section)
        for section in abstracts
    )
    identifiers = [identifier for path in ("PubmedData/ArticleIdList/ArticleId", "BookDocument/ArticleIdList/ArticleId", "PubmedBookData/ArticleIdList/ArticleId")
                   for identifier in element.findall(path)]
    for identifier in identifiers:
        if identifier.get("IdType") == "doi":
            item["doi"] = normalize_doi(text(identifier))
            break
    if not item["doi"]:
        for identifier in article.findall("ELocationID"):
            if identifier.get("EIdType") == "doi":
                item["doi"] = normalize_doi(text(identifier))
                break
    types = [text(value).lower() for value in article.findall("PublicationTypeList/PublicationType") + article.findall("PublicationType")]
    item["is_retracted"] = "retracted publication" in types or any(
        value.get("RefType") == "RetractionIn" for value in citation.findall("CommentsCorrectionsList/CommentsCorrections")
    )
    item["url"] = "https://pubmed.ncbi.nlm.nih.gov/{}/".format(item["pmid"]) if item["pmid"] else None
    return item


def openalex_record(work):
    item = record("openalex")
    item.update(title=work.get("title") or "", year=work.get("publication_year"),
                publication_date=work.get("publication_date"), doi=normalize_doi(work.get("doi")),
                openalex_id=work.get("id"), is_retracted=work.get("is_retracted") if isinstance(work.get("is_retracted"), bool) else None)
    item["authors"] = [entry["author"]["display_name"] for entry in work.get("authorships") or []
                       if (entry.get("author") or {}).get("display_name")]
    location = work.get("primary_location") or {}
    item["journal"] = (location.get("source") or {}).get("display_name") or ""
    pmid = (work.get("ids") or {}).get("pmid")
    item["pmid"] = str(pmid).rstrip("/").rsplit("/", 1)[-1] if pmid else None
    index = work.get("abstract_inverted_index") or {}
    positions = [(position, word) for word, offsets in index.items() for position in offsets]
    item["abstract"] = " ".join(word for _, word in sorted(positions))
    item["url"] = ("https://doi.org/" + item["doi"]) if item["doi"] else location.get("landing_page_url") or item["openalex_id"]
    return item


def search_pubmed(query, limit, from_date, to_date, info, records):
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
    common = {"db": "pubmed", "tool": "research_topic_selector"}
    for parameter, variable in [("api_key", "NCBI_API_KEY"), ("email", "NCBI_EMAIL")]:
        if os.environ.get(variable):
            common[parameter] = os.environ[variable]
    params = {**common, "term": query, "retmode": "json", "retmax": limit, "sort": "relevance"}
    if from_date or to_date:
        params.update(datetype="pdat", mindate=(from_date or "0001-01-01").replace("-", "/"),
                      maxdate=(to_date or "3000-12-31").replace("-", "/"))
    response = json.loads(request(base + "esearch.fcgi", params, ncbi=True))
    result = response.get("esearchresult")
    if not isinstance(result, dict) or response.get("error") or result.get("ERROR"):
        raise RuntimeError("PubMed ESearch returned an error or invalid response.")
    info["querytranslation"] = result.get("querytranslation")
    for category in ("warninglist", "errorlist"):
        for name, values in (result.get(category) or {}).items():
            info["warnings"].append({"type": name, "messages": values})
    info["total_count"] = int(result["count"])
    ids = result["idlist"][:limit]
    if info["total_count"] and not ids:
        raise RuntimeError("PubMed reported matches but returned no identifiers.")
    for start in range(0, len(ids), 100):
        batch = ids[start:start + 100]
        xml = request(base + "efetch.fcgi", {**common, "id": ",".join(batch), "retmode": "xml"}, ncbi=True)
        root = ET.fromstring(xml)
        if root.tag != "PubmedArticleSet" or root.find(".//ERROR") is not None:
            raise RuntimeError("PubMed EFetch returned an error or invalid response.")
        batch_records = [pubmed_record(element) for element in root if element.tag in ("PubmedArticle", "PubmedBookArticle")]
        by_pmid = {item["pmid"]: item for item in batch_records}
        records.extend(by_pmid[pmid] for pmid in batch if pmid in by_pmid)
        if any(pmid not in by_pmid for pmid in batch):
            raise RuntimeError("PubMed EFetch did not return every requested record.")


def search_openalex(query, limit, from_date, to_date, info, records):
    params = {"search": query, "sort": "relevance_score:desc", "cursor": "*",
              "per_page": min(limit, 100),
              "select": "id,doi,title,authorships,publication_year,publication_date,primary_location,ids,abstract_inverted_index,is_retracted"}
    filters = []
    if from_date:
        filters.append("from_publication_date:" + from_date)
    if to_date:
        filters.append("to_publication_date:" + to_date)
    if filters:
        params["filter"] = ",".join(filters)
    key = os.environ.get("OPENALEX_API_KEY")
    headers = {"Authorization": "Bearer " + key} if key else {}
    while len(records) < limit:
        params["per_page"] = min(100, limit - len(records))
        response = json.loads(request("https://api.openalex.org/works", params, headers))
        if not isinstance(response.get("results"), list) or not isinstance(response.get("meta"), dict):
            raise RuntimeError("OpenAlex returned an invalid response.")
        info["total_count"] = int(response["meta"]["count"])
        records.extend(openalex_record(work) for work in response["results"][:limit - len(records)])
        cursor = response["meta"].get("next_cursor")
        if not response["results"] or not cursor:
            break
        if cursor == params["cursor"]:
            raise RuntimeError("OpenAlex repeated its pagination cursor.")
        params["cursor"] = cursor
    if len(records) < min(limit, info["total_count"]):
        raise RuntimeError("OpenAlex pagination ended before the expected records were returned.")


def merge_record(target, extra):
    target["sources"] = list(dict.fromkeys(target["sources"] + extra["sources"]))
    for name, value in extra.items():
        if name != "sources" and (target.get(name) is None or target.get(name) == "" or target.get(name) == []):
            target[name] = value
    if extra.get("is_retracted") is True:
        target["is_retracted"] = True


def deduplicate(records):
    merged, lookup = [], {}
    for source in records:
        item = dict(source, doi=normalize_doi(source.get("doi")))
        keys = [(name, item[name]) for name in ("doi", "pmid") if item.get(name)]
        matches = []
        for key in keys:
            if key in lookup and all(lookup[key] is not match for match in matches):
                matches.append(lookup[key])
        target = matches[0] if matches else item
        if not matches:
            merged.append(target)
        for duplicate in matches[1:]:
            merge_record(target, duplicate)
            merged = [entry for entry in merged if entry is not duplicate]
            # ponytail: at most 2,000 records; rewrite aliases only when two groups join.
            for key, value in lookup.items():
                if value is duplicate:
                    lookup[key] = target
        merge_record(target, item)
        for key in keys:
            lookup[key] = target
    return merged


def iso_date(value):
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        return dt.date.fromisoformat(value).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError("Use a valid date in YYYY-MM-DD format.") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pubmed-query")
    parser.add_argument("--openalex-query")
    parser.add_argument("--limit", type=int, default=50, help="Maximum records per database (1-1000).")
    parser.add_argument("--from-date", type=iso_date)
    parser.add_argument("--to-date", type=iso_date)
    parser.add_argument("--out", type=Path, required=True, help="New JSON output file; existing files are never overwritten.")
    args = parser.parse_args(argv)
    queries = [("pubmed", args.pubmed_query, search_pubmed), ("openalex", args.openalex_query, search_openalex)]
    if not any(query for _, query, _ in queries):
        parser.error("Provide --pubmed-query and/or --openalex-query.")
    if any(query is not None and not query.strip() for _, query, _ in queries):
        parser.error("Search queries must not be empty.")
    if not 1 <= args.limit <= 1000:
        parser.error("--limit must be between 1 and 1000.")
    if args.from_date and args.to_date and args.from_date > args.to_date:
        parser.error("--from-date must not be after --to-date.")
    if args.out.exists():
        parser.error("Output already exists; choose a new --out path.")
    try:
        args.out.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        parser.error("Cannot create the output directory.")
    output = {"schema_version": "1.0", "searched_at_utc": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
              "searches": [], "records": []}
    failed = False
    for source, query, search in queries:
        if query is None:
            continue
        info = {"source": source, "query": query, "dates": {"from": args.from_date, "to": args.to_date}, "requested_limit": args.limit,
                "sort": "relevance" if source == "pubmed" else "relevance_score:desc",
                "total_count": None, "retrieved_count": 0, "truncated": False, "status": "ok",
                "warnings": [], "error": None,
                "auth_configured": {"api_key": bool(os.environ.get("NCBI_API_KEY" if source == "pubmed" else "OPENALEX_API_KEY"))}}
        if source == "pubmed":
            info["auth_configured"]["email"] = bool(os.environ.get("NCBI_EMAIL"))
            info["querytranslation"] = None
        records = []
        try:
            search(query, args.limit, args.from_date, args.to_date, info, records)
        except (RuntimeError, ValueError, KeyError, TypeError, AttributeError, ET.ParseError, urllib.error.URLError, OSError) as exc:
            failed = True
            info["status"] = "partial" if records else "error"
            info["error"] = str(exc) if isinstance(exc, RuntimeError) else "Invalid response from literature service."
            for variable in ("OPENALEX_API_KEY", "NCBI_API_KEY", "NCBI_EMAIL"):
                if os.environ.get(variable):
                    info["error"] = info["error"].replace(os.environ[variable], "[REDACTED]")
            print(source + ": " + info["error"], file=sys.stderr)
        info["retrieved_count"] = len(records)
        info["truncated"] = info["status"] != "ok" or (info["total_count"] is not None and len(records) < info["total_count"])
        output["searches"].append(info)
        output["records"].extend(records)
    output["records"] = deduplicate(output["records"])
    try:
        with args.out.open("x", encoding="utf-8") as handle:
            json.dump(output, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except OSError:
        print("Cannot write output; existing files are never overwritten.", file=sys.stderr)
        return 2
    print("Saved {} unique records to {}".format(len(output["records"]), args.out))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
