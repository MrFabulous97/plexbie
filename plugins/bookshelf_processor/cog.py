# path: plugins/bookshelf_processor/cog.py
"""
Bookshelf Processor Plugin - Watches SABnzbd download directories for new ebooks
and audiobooks, parses usenet release names, fetches metadata and cover art, then
moves them into Audiobookshelf-compatible directory structures.

Ported from the standalone bookshelf-processor Docker container.
"""

import json
import os
import re
import shutil
import hashlib
import zipfile
from html import escape as html_escape
from pathlib import Path
from datetime import datetime

import aiohttp
import discord
from discord.ext import commands, tasks

from core.logging import get_logger
from core.admin_mirror import send_user_dm

logger = get_logger(__name__)

# ─── Constants ───────────────────────────────────────────────────────────────

JUNK_EXTENSIONS = {
    ".nfo", ".diz", ".txt", ".url", ".html", ".htm", ".cue", ".m3u",
    ".log", ".sfv", ".nzb", ".par2", ".jpg_original", ".png_original",
}

EBOOK_EXTENSIONS = {".epub", ".azw3", ".mobi", ".pdf", ".cbz", ".cbr"}
AUDIOBOOK_EXTENSIONS = {".mp3", ".m4a", ".m4b", ".ogg", ".flac", ".wma", ".aac"}

NOISE_TOKENS = {
    "retail", "epub", "ebook", "mobi", "azw3", "pdf", "audiobook",
    "mp3", "m4b", "m4a", "unabridged", "abridged", "unabr", "abr",
    "a novel", "a memoir", "novel", "fiction", "nonfiction", "non fiction",
}

# Audio bitrate patterns like "64k", "128k", "320kbps", "1064k"
BITRATE_RE = re.compile(r"\b\d{2,4}k(?:bps)?\b", re.IGNORECASE)

# Track numbering in embedded titles like "Title 01-10", "Title 1/10", "Title 01 of 10"
TRACK_NUM_RE = re.compile(r"\s*\d{1,3}\s*[-/]\s*\d{1,3}\s*$")
TRACK_OF_RE = re.compile(r"\s*\d{1,3}\s+of\s+\d{1,3}\s*$", re.IGNORECASE)

# Filesize patterns like "387.97 MB", "1.2 GB", "500 KB"
FILESIZE_RE = re.compile(r"\s*-?\s*\d+\.?\d*\s*(?:MB|GB|KB|bytes)\b", re.IGNORECASE)

RELEASE_GROUP_RE = re.compile(r"[_\-][A-Za-z][A-Za-z0-9_]{1,20}$")
YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")
YEAR_BRACKET_RE = re.compile(r"\[?\(?((?:19|20)\d{2})\)?\]?")
BOOK_NUM_RE = re.compile(
    r"[,\s]*\b(?:Book|Vol(?:ume)?\.?|Part|Bk\.?|#)\s*(\d+)\b", re.IGNORECASE
)
SERIES_IN_BRACKETS_RE = re.compile(r"\[([^\]]+)\s+(\d+)\]")

# Pattern for "Author-Series.Index-Title" format common in usenet audiobooks
# e.g., "Pierce.Brown-Red.Rising.01-Red.Rising"
DASH_DELIMITED_RE = re.compile(
    r"^([^-]+)-([^-]+?)\.?(\d{1,2})-(.+)$"
)

# Common title words that should NOT be treated as author names
TITLE_WORDS = {
    "the", "a", "an", "of", "and", "in", "on", "at", "to", "for", "is",
    "by", "with", "from", "or", "not", "but", "all", "no", "my", "his",
    "her", "our", "its", "new", "old", "big", "how", "why", "what",
}

API_TIMEOUT = 10


# ─── Name Parsing ────────────────────────────────────────────────────────────


def parse_release_name(name: str) -> dict:
    """
    Parse a usenet release name into author, title, year, series, series_index.

    Handles formats like:
      - "Andy.Weir.Project.Hail.Mary.A.Novel.2021.Retail.EPUB.eBook-BitBook"
      - "Stephen King - The Shining (Unabridged)"
      - "Martha Wells-[The Murderbot Diaries 01]-All Systems Red"
      - "Project.Hail.Mary.by.Andy.Weir"
    """
    original = name
    result = {
        "author": "",
        "title": "",
        "year": None,
        "series": None,
        "series_index": None,
    }

    # ── Try dash-delimited format FIRST (before any substitution) ──
    # e.g., "Pierce.Brown-Red.Rising.01-Red.Rising.Unabr-64k.[2014]"
    dash_delim = DASH_DELIMITED_RE.match(name)
    if dash_delim:
        raw_author = dash_delim.group(1).replace(".", " ").replace("_", " ").strip()
        raw_series = dash_delim.group(2).replace(".", " ").replace("_", " ").strip()
        series_idx = int(dash_delim.group(3))
        raw_title = dash_delim.group(4).replace(".", " ").replace("_", " ").strip()

        # Extract year from title portion
        year_match = YEAR_BRACKET_RE.search(raw_title)
        if year_match:
            result["year"] = year_match.group(1)
            raw_title = raw_title[: year_match.start()] + raw_title[year_match.end() :]

        # Clean noise from title
        raw_title = BITRATE_RE.sub("", raw_title)
        for token in NOISE_TOKENS:
            raw_title = re.sub(r"\b" + re.escape(token) + r"\b", "", raw_title, flags=re.IGNORECASE)
        raw_title = re.sub(r"[\[\](){}]", "", raw_title)
        raw_title = re.sub(r"\s+", " ", raw_title).strip()

        result["author"] = _clean_name(raw_author)
        result["series"] = _clean_name(raw_series)
        result["series_index"] = series_idx
        # Use the series name as title if cleaned title is same or empty
        cleaned_title = _clean_name(raw_title)
        if cleaned_title and cleaned_title.lower() != raw_series.lower():
            result["title"] = cleaned_title
        else:
            result["title"] = _clean_name(raw_series)

        if not result["author"]:
            result["author"] = "Unknown"

        logger.debug(f"Parsed (dash-delimited) '{original}' -> {result}")
        return result

    # ── Standard parsing path ──

    # Strip release group tag at end
    name = RELEASE_GROUP_RE.sub("", name)

    # Remove content in parentheses that's noise (Unabridged, mp3, etc.)
    name = re.sub(
        r"\((?:unabridged|abridged|unabr|mp3|m4b|epub|retail|audiobook)[^)]*\)",
        "",
        name,
        flags=re.IGNORECASE,
    )

    # Check for series info in brackets like [Series Name 01]
    bracket_match = SERIES_IN_BRACKETS_RE.search(name)
    if bracket_match:
        result["series"] = bracket_match.group(1).strip()
        result["series_index"] = int(bracket_match.group(2))
        name = SERIES_IN_BRACKETS_RE.sub("", name)

    # Remove square bracket content that looks like format info or year
    name = re.sub(r"\[(?:epub|mobi|mp3|m4b|pdf|audiobook)[^\]]*\]", "", name, flags=re.IGNORECASE)

    # Extract year (handles [2014], (2014), and bare 2014)
    year_match = YEAR_BRACKET_RE.search(name)
    if year_match:
        result["year"] = year_match.group(1)
        name = name[: year_match.start()] + name[year_match.end() :]

    # Remove remaining square/round brackets
    name = re.sub(r"[\[\]()]", " ", name)

    # Replace dots and underscores with spaces
    name = name.replace(".", " ").replace("_", " ")

    # Remove bitrate patterns (64k, 128kbps, 1064k, etc.)
    name = BITRATE_RE.sub("", name)

    # Remove filesize patterns (387.97 MB, 1.2 GB, etc.)
    name = FILESIZE_RE.sub("", name)

    # Remove noise tokens
    for token in NOISE_TOKENS:
        name = re.sub(r"\b" + re.escape(token) + r"\b", "", name, flags=re.IGNORECASE)

    # Clean up extra whitespace
    name = re.sub(r"\s+", " ", name).strip()

    # ── Try dash separator (most reliable) ──
    # "Author - Title" or "Author-Title" with spaces around dash
    dash_match = re.match(r"^(.+?)\s*[-\u2013\u2014]\s+(.+)$", name)
    if dash_match:
        result["author"] = _clean_name(dash_match.group(1))
        title_part = dash_match.group(2)
    else:
        # ── Try "by Author" pattern ──
        by_match = re.search(r"\bby\s+(.+)$", name, re.IGNORECASE)
        if by_match:
            result["author"] = _clean_name(by_match.group(1))
            title_part = name[: by_match.start()].strip()
        else:
            # ── Heuristic: first 2-3 words are author if they look like names ──
            words = name.split()
            author_words, title_words_list = _split_author_title(words)
            result["author"] = _clean_name(" ".join(author_words))
            title_part = " ".join(title_words_list)

    # Extract book number from title if not already found
    if result["series_index"] is None:
        book_match = BOOK_NUM_RE.search(title_part)
        if book_match:
            result["series_index"] = int(book_match.group(1))
            title_part = BOOK_NUM_RE.sub("", title_part)

    result["title"] = _clean_name(title_part)

    # If we still don't have an author, use "Unknown"
    if not result["author"]:
        result["author"] = "Unknown"
    if not result["title"]:
        result["title"] = original

    logger.debug(f"Parsed '{original}' -> {result}")
    return result


def _split_author_title(words: list[str]) -> tuple[list[str], list[str]]:
    """
    Heuristic split: first N capitalized words that look like a person name
    become the author, rest becomes the title.
    """
    if len(words) <= 2:
        return words[:1], words[1:] if len(words) > 1 else words

    # Try 2-word and 3-word author candidates
    for n in [2, 3]:
        if n > len(words):
            continue
        candidate = words[:n]
        rest = words[n:]

        # Check if candidate looks like a person name:
        # - All words capitalized (or single letter like "J" "K")
        # - No common title words
        all_name_like = all(
            (w[0].isupper() or len(w) <= 2) and w.lower() not in TITLE_WORDS
            for w in candidate
        )
        # The remaining words should start with something that looks like a title
        has_rest = len(rest) > 0

        if all_name_like and has_rest:
            return candidate, rest

    # Fallback: first 2 words are author
    return words[:2], words[2:]


def _clean_name(name: str) -> str:
    """Clean up a parsed name string."""
    # Remove trailing/leading punctuation and whitespace
    name = re.sub(r"^[\s\-\u2013\u2014,.:;]+|[\s\-\u2013\u2014,.:;]+$", "", name)
    # Collapse whitespace
    name = re.sub(r"\s+", " ", name).strip()
    # Title case if all lower or all upper
    if name == name.lower() or name == name.upper():
        name = name.title()
    return name


# ─── Embedded Metadata Extraction ────────────────────────────────────────────


def extract_embedded_metadata(file_path: Path) -> dict | None:
    """
    Extract metadata embedded in the file itself.
    - M4B/M4A: MP4 tags (title, artist, album/series, year, cover, description)
    - EPUB: OPF metadata (title, creator, identifier/ISBN, date, series)
    - MP3: ID3 tags
    Returns a dict with keys matching our metadata format, or None.
    """
    suffix = file_path.suffix.lower()

    if suffix in {".m4b", ".m4a"}:
        return _extract_m4b_metadata(file_path)
    elif suffix == ".epub":
        return _extract_epub_metadata(file_path)
    elif suffix == ".mp3":
        return _extract_mp3_metadata(file_path)

    return None


def _extract_m4b_metadata(file_path: Path) -> dict | None:
    """Extract metadata from M4B/M4A (MP4 container) files."""
    try:
        from mutagen.mp4 import MP4

        tags = MP4(str(file_path))
        meta = {}

        # Title
        title = tags.get("\xa9nam")
        if title:
            meta["title"] = str(title[0])

        # Author/Artist
        artist = tags.get("\xa9ART")
        if artist:
            meta["author"] = str(artist[0])

        # Album (sometimes the series name for audiobooks, but often just the book title)
        album = tags.get("\xa9alb")
        if album:
            album_str = str(album[0])
            # Clean edition markers
            album_clean = re.sub(
                r"\s*\((?:Unabridged|Abridged|Unabr|Audio(?:book)?)\)\s*$",
                "", album_str, flags=re.IGNORECASE
            ).strip()
            # Only treat as series if album is meaningfully different from title
            # (not just the same name or the same name + edition info)
            title_str = meta.get("title", "").lower()
            if album_clean and title_str and album_clean.lower() != title_str:
                # Make sure it's not just a minor variation (e.g., "Title: Subtitle" vs "Title")
                if not (album_clean.lower().startswith(title_str) or title_str.startswith(album_clean.lower())):
                    meta["series"] = album_clean

        # Year
        year = tags.get("\xa9day")
        if year:
            year_str = str(year[0])[:4]
            if year_str.isdigit():
                meta["year"] = year_str

        # Track number - do NOT use as series index
        # Track numbers in audiobooks represent chapters/parts, not book number
        # Series index should only come from API lookups or explicit metadata

        # Genre
        genre = tags.get("\xa9gen")
        if genre:
            meta["genre"] = str(genre[0])

        # Description/Comment
        desc = tags.get("desc") or tags.get("\xa9cmt")
        if desc:
            meta["description"] = str(desc[0])[:500]

        # Embedded cover art
        covr = tags.get("covr")
        if covr and covr[0]:
            meta["embedded_cover"] = bytes(covr[0])

        if meta.get("title") or meta.get("author"):
            logger.info(f"Embedded M4B metadata: title={meta.get('title')}, "
                        f"author={meta.get('author')}, series={meta.get('series')}")
            return meta

    except Exception as e:
        logger.debug(f"Failed to read M4B metadata from {file_path.name}: {e}")

    return None


def _extract_epub_metadata(file_path: Path) -> dict | None:
    """Extract metadata from EPUB files (ZIP with OPF inside)."""
    try:
        with zipfile.ZipFile(str(file_path)) as z:
            # Find the OPF file
            opf_name = None
            for name in z.namelist():
                if name.endswith(".opf"):
                    opf_name = name
                    break

            if not opf_name:
                return None

            content = z.read(opf_name).decode("utf-8", errors="ignore")
            meta = {}

            # Title
            title_match = re.search(r"<dc:title[^>]*>([^<]+)</dc:title>", content)
            if title_match:
                meta["title"] = title_match.group(1).strip()

            # Author
            creator_match = re.search(r"<dc:creator[^>]*>([^<]+)</dc:creator>", content)
            if creator_match:
                meta["author"] = creator_match.group(1).strip()

            # ISBN - look in dc:identifier tags
            identifiers = re.findall(
                r"<dc:identifier[^>]*>([^<]+)</dc:identifier>", content
            )
            for ident in identifiers:
                ident = ident.strip()
                # Check for ISBN-13 or ISBN-10 pattern
                isbn_clean = re.sub(r"[^0-9X]", "", ident.upper())
                if len(isbn_clean) in {10, 13} and isbn_clean.isdigit():
                    meta["isbn"] = isbn_clean
                    break
                # Also check for "urn:isbn:..." format
                isbn_match = re.search(r"(?:isbn[:\s]*)(\d{10,13})", ident, re.I)
                if isbn_match:
                    meta["isbn"] = isbn_match.group(1)
                    break

            # Date/Year
            date_match = re.search(r"<dc:date[^>]*>([^<]+)</dc:date>", content)
            if date_match:
                year = date_match.group(1).strip()[:4]
                if year.isdigit():
                    meta["year"] = year

            # Publisher
            pub_match = re.search(r"<dc:publisher[^>]*>([^<]+)</dc:publisher>", content)
            if pub_match:
                meta["publisher"] = pub_match.group(1).strip()

            # Calibre series metadata
            series_match = re.search(
                r'name="calibre:series"\s*content="([^"]+)"', content
            )
            if series_match:
                meta["series"] = series_match.group(1).strip()

            series_idx_match = re.search(
                r'name="calibre:series_index"\s*content="([^"]+)"', content
            )
            if series_idx_match:
                try:
                    meta["series_index"] = int(float(series_idx_match.group(1)))
                except ValueError:
                    pass

            if meta.get("title") or meta.get("author") or meta.get("isbn"):
                logger.info(f"Embedded EPUB metadata: title={meta.get('title')}, "
                            f"author={meta.get('author')}, isbn={meta.get('isbn')}, "
                            f"series={meta.get('series')}")
                return meta

    except Exception as e:
        logger.debug(f"Failed to read EPUB metadata from {file_path.name}: {e}")

    return None


def _extract_mp3_metadata(file_path: Path) -> dict | None:
    """Extract metadata from MP3 files using mutagen."""
    try:
        from mutagen.mp3 import MP3
        from mutagen.id3 import ID3

        tags = ID3(str(file_path))
        meta = {}

        # Title
        if "TIT2" in tags:
            meta["title"] = str(tags["TIT2"])
        # Artist/Author
        if "TPE1" in tags:
            meta["author"] = str(tags["TPE1"])
        # Album (series)
        if "TALB" in tags:
            album = str(tags["TALB"])
            if meta.get("title") and album.lower() != meta["title"].lower():
                meta["series"] = album
        # Year
        if "TDRC" in tags:
            year = str(tags["TDRC"])[:4]
            if year.isdigit():
                meta["year"] = year
        # Track number
        if "TRCK" in tags:
            trk = str(tags["TRCK"]).split("/")[0]
            if trk.isdigit() and meta.get("series"):
                meta["series_index"] = int(trk)

        # Embedded cover
        if "APIC:" in tags:
            meta["embedded_cover"] = tags["APIC:"].data

        if meta.get("title") or meta.get("author"):
            logger.info(f"Embedded MP3 metadata: title={meta.get('title')}, "
                        f"author={meta.get('author')}")
            return meta

    except Exception as e:
        logger.debug(f"Failed to read MP3 metadata from {file_path.name}: {e}")

    return None


def _clean_embedded_title(title: str) -> str:
    """Clean track numbering and noise from embedded metadata titles.
    E.g., 'Echopraxia 01-10' -> 'Echopraxia', 'Title 3 of 12' -> 'Title'
    """
    title = TRACK_NUM_RE.sub("", title)
    title = TRACK_OF_RE.sub("", title)
    # Also strip leading track numbers like "01 - Title"
    title = re.sub(r"^\d{1,3}\s*[-_.]\s*", "", title)
    return title.strip()


def extract_metadata_from_files(files: list[Path]) -> dict | None:
    """
    Extract embedded metadata from book/audio files.
    For multi-file audiobooks (many MP3s), checks multiple files and picks
    the best metadata (most complete). Cleans track numbering from titles.
    """
    best_meta = None
    best_score = 0

    for idx, f in enumerate(files):
        meta = extract_embedded_metadata(f)
        if not meta:
            continue

        # Clean track numbering from title
        if meta.get("title"):
            meta["title"] = _clean_embedded_title(meta["title"])

        # Score: how many useful fields does this have?
        score = sum(1 for k in ["title", "author", "series", "year", "isbn"]
                    if meta.get(k))
        # Bonus for having an ISBN (most reliable identifier)
        if meta.get("isbn"):
            score += 3
        # Bonus for embedded cover
        if meta.get("embedded_cover"):
            score += 1

        if score > best_score:
            best_meta = meta
            best_score = score

        # If we have title + author + ISBN, that's as good as it gets
        if meta.get("title") and meta.get("author") and meta.get("isbn"):
            break

        # For multi-file audio, only check first 3 files
        if idx >= 2:
            break

    return best_meta


# ─── Metadata Fetching (async) ───────────────────────────────────────────────


async def fetch_metadata(author: str, title: str, isbn: str = None) -> dict:
    """Fetch metadata from Open Library, falling back to Google Books.
    If an ISBN is provided, try an exact ISBN lookup first for best results."""

    # If we have an ISBN, try exact lookup first (most reliable)
    if isbn:
        meta = await _try_open_library_isbn(isbn)
        if meta and meta.get("title"):
            logger.info(f"ISBN lookup succeeded: {isbn} -> {meta.get('title')}")
            return meta
        meta = await _try_google_books_isbn(isbn)
        if meta and meta.get("title"):
            logger.info(f"ISBN Google lookup succeeded: {isbn} -> {meta.get('title')}")
            return meta

    meta = await _try_open_library(author, title)
    if not meta or not meta.get("title"):
        meta_gb = await _try_google_books(author, title)
        if meta_gb and meta_gb.get("title"):
            # Merge: prefer Google Books if Open Library had nothing
            if not meta:
                meta = meta_gb
            else:
                for k, v in meta_gb.items():
                    if v and not meta.get(k):
                        meta[k] = v

    if not meta:
        logger.warning(f"No metadata found for '{author}' - '{title}'")
        meta = {}

    return meta


async def _try_open_library_isbn(isbn: str) -> dict | None:
    """Look up a book by ISBN on Open Library (exact match)."""
    try:
        timeout = aiohttp.ClientTimeout(total=API_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"https://openlibrary.org/isbn/{isbn}.json") as resp:
                if resp.status != 200:
                    return None
                book = await resp.json()

            meta = {
                "title": book.get("title", ""),
                "year": str(book.get("publish_date", ""))[-4:]
                if book.get("publish_date") else None,
                "cover_url": None,
                "isbn": isbn,
            }

            # Get author from the works/authors endpoint
            authors = book.get("authors", [])
            if authors:
                author_key = authors[0].get("key", "")
                if author_key:
                    try:
                        async with session.get(f"https://openlibrary.org{author_key}.json") as aresp:
                            if aresp.status == 200:
                                adata = await aresp.json()
                                meta["author"] = adata.get("name", "")
                    except Exception:
                        pass

            # Cover from ISBN
            meta["cover_url"] = f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg"

            # Try to get series info from the work
            works = book.get("works", [])
            if works:
                work_key = works[0].get("key", "")
                if work_key:
                    try:
                        async with session.get(f"https://openlibrary.org{work_key}.json") as wresp:
                            if wresp.status == 200:
                                wdata = await wresp.json()
                                # Check subjects for series hints
                                for subj in wdata.get("subjects", []):
                                    series_match = re.search(
                                        r"(.+?)(?:\s*#\s*|\s+Book\s+)(\d+)", subj
                                    )
                                    if series_match:
                                        meta["series"] = series_match.group(1).strip()
                                        meta["series_index"] = int(series_match.group(2))
                                        break
                    except Exception:
                        pass

            if meta.get("year") and not meta["year"].isdigit():
                meta["year"] = None

            return meta if meta.get("title") else None

    except Exception as e:
        logger.debug(f"Open Library ISBN lookup failed: {e}")
        return None


async def _try_google_books_isbn(isbn: str) -> dict | None:
    """Look up a book by ISBN on Google Books."""
    try:
        timeout = aiohttp.ClientTimeout(total=API_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                "https://www.googleapis.com/books/v1/volumes",
                params={"q": f"isbn:{isbn}", "maxResults": "1"},
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

        items = data.get("items", [])
        if not items:
            return None

        vol = items[0].get("volumeInfo", {})
        meta = {
            "title": vol.get("title", ""),
            "author": (vol.get("authors") or [""])[0],
            "year": vol.get("publishedDate", "")[:4] or None,
            "cover_url": None,
            "isbn": isbn,
        }

        img_links = vol.get("imageLinks", {})
        for key in ["extraLarge", "large", "medium", "small", "thumbnail"]:
            if key in img_links:
                url = img_links[key].replace("http:", "https:")
                url = re.sub(r"zoom=\d", "zoom=1", url)
                meta["cover_url"] = url
                break

        return meta if meta.get("title") else None

    except Exception as e:
        logger.debug(f"Google Books ISBN lookup failed: {e}")
        return None


async def _try_open_library(author: str, title: str) -> dict | None:
    """Search Open Library for book metadata and cover."""
    try:
        params = {"title": title, "limit": "3"}
        if author and author != "Unknown":
            params["author"] = author

        timeout = aiohttp.ClientTimeout(total=API_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                "https://openlibrary.org/search.json",
                params=params,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

        if not data.get("docs"):
            return None

        # Find best match
        doc = _best_match(data["docs"], author, title)
        if not doc:
            doc = data["docs"][0]

        meta = {
            "title": doc.get("title", title),
            "author": (doc.get("author_name") or [author])[0],
            "year": str(doc["first_publish_year"]) if doc.get("first_publish_year") else None,
            "cover_url": None,
            "series": None,
            "series_index": None,
        }

        # Cover
        cover_id = doc.get("cover_i")
        if cover_id:
            meta["cover_url"] = f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg"

        # Series detection from subjects or title
        subjects = doc.get("subject", [])
        for subj in subjects:
            series_match = re.search(r"(.+?)(?:\s*#\s*|\s+Book\s+)(\d+)", subj)
            if series_match:
                meta["series"] = series_match.group(1).strip()
                meta["series_index"] = int(series_match.group(2))
                break

        logger.info(f"Open Library match: {meta['author']} - {meta['title']}")
        return meta

    except Exception as e:
        logger.debug(f"Open Library lookup failed: {e}")
        return None


async def _try_google_books(author: str, title: str) -> dict | None:
    """Search Google Books API for metadata and cover."""
    try:
        query = f"intitle:{title}"
        if author and author != "Unknown":
            query += f"+inauthor:{author}"

        timeout = aiohttp.ClientTimeout(total=API_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                "https://www.googleapis.com/books/v1/volumes",
                params={"q": query, "maxResults": "3"},
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

        items = data.get("items", [])
        if not items:
            return None

        vol = items[0].get("volumeInfo", {})

        meta = {
            "title": vol.get("title", title),
            "author": (vol.get("authors") or [author])[0],
            "year": vol.get("publishedDate", "")[:4] or None,
            "cover_url": None,
            "series": None,
            "series_index": None,
        }

        # Cover - get largest available
        img_links = vol.get("imageLinks", {})
        for key in ["extraLarge", "large", "medium", "small", "thumbnail"]:
            if key in img_links:
                url = img_links[key].replace("http:", "https:")
                # Request larger version
                url = re.sub(r"zoom=\d", "zoom=1", url)
                meta["cover_url"] = url
                break

        # Series info from Google Books
        series_info = vol.get("seriesInfo", {})
        if series_info:
            meta["series"] = series_info.get("shortSeriesBookTitle", "")
            pos = series_info.get("bookDisplayNumber", "")
            if pos and pos.isdigit():
                meta["series_index"] = int(pos)

        logger.info(f"Google Books match: {meta['author']} - {meta['title']}")
        return meta

    except Exception as e:
        logger.debug(f"Google Books lookup failed: {e}")
        return None


def _best_match(docs: list, author: str, title: str) -> dict | None:
    """Pick the best matching document from search results."""
    author_lower = author.lower()
    title_lower = title.lower()

    for doc in docs:
        doc_title = doc.get("title", "").lower()
        doc_authors = [a.lower() for a in doc.get("author_name", [])]

        title_match = title_lower in doc_title or doc_title in title_lower
        author_match = any(author_lower in a or a in author_lower for a in doc_authors)

        if title_match and author_match:
            return doc

    # Fallback: just title match
    for doc in docs:
        doc_title = doc.get("title", "").lower()
        if title_lower in doc_title or doc_title in title_lower:
            return doc

    return None


async def download_cover(url: str, dest: Path, cache_dir: Path | None = None) -> bool:
    """Download cover art, using cache to avoid redundant fetches."""
    if dest.exists():
        logger.debug(f"Cover already exists: {dest}")
        return True

    # Check cache
    if cache_dir:
        cache_key = hashlib.md5(url.encode()).hexdigest()
        cache_path = cache_dir / f"{cache_key}.jpg"

        if cache_path.exists():
            shutil.copy2(str(cache_path), str(dest))
            logger.debug(f"Cover from cache: {dest}")
            return True
    else:
        cache_path = None

    try:
        timeout = aiohttp.ClientTimeout(total=API_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                content = await resp.read()

        # Check that we got an actual image (not a placeholder)
        if len(content) < 1000:
            logger.warning(f"Cover too small ({len(content)} bytes), skipping: {url}")
            return False

        # Save to cache and destination
        if cache_path:
            cache_path.write_bytes(content)
        dest.write_bytes(content)
        logger.info(f"Cover downloaded: {dest.name} ({len(content)} bytes)")
        return True

    except Exception as e:
        logger.warning(f"Cover download failed: {e}")
        return False


def generate_opf(meta: dict, output_path: Path):
    """Generate a metadata.opf file in Dublin Core format for Audiobookshelf."""
    title = html_escape(meta.get("title", "Unknown"))
    author = html_escape(meta.get("author", "Unknown"))

    series_meta = ""
    if meta.get("series"):
        series_meta += f'    <meta name="calibre:series" content="{html_escape(meta["series"])}"/>\n'
    if meta.get("series_index") is not None:
        series_meta += f'    <meta name="calibre:series_index" content="{meta["series_index"]}"/>\n'

    year_meta = ""
    if meta.get("year"):
        year_meta = f"    <dc:date>{html_escape(str(meta['year']))}</dc:date>\n"

    opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"
            xmlns:opf="http://www.idpf.org/2007/opf">
    <dc:title>{title}</dc:title>
    <dc:creator opf:role="aut">{author}</dc:creator>
{year_meta}{series_meta}  </metadata>
</package>
"""
    output_path.write_text(opf, encoding="utf-8")
    logger.debug(f"Wrote metadata: {output_path}")


# ─── File Organization ───────────────────────────────────────────────────────


def sanitize_dirname(name: str) -> str:
    """Remove filesystem-unsafe characters from a directory name."""
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = re.sub(r"\s+", " ", name)
    name = name.strip(". ")
    return name or "Unknown"


def find_book_files(source: Path, media_type: str) -> list[Path]:
    """Recursively find all valid book/audio files."""
    extensions = AUDIOBOOK_EXTENSIONS if media_type == "audiobook" else EBOOK_EXTENSIONS
    files = []
    for f in source.rglob("*"):
        if f.is_file() and f.suffix.lower() in extensions:
            files.append(f)
    return sorted(files)


def find_existing_covers(source: Path) -> list[Path]:
    """Find any cover images already in the source directory."""
    covers = []
    for f in source.rglob("*"):
        if f.is_file() and f.suffix.lower() in {".jpg", ".jpeg", ".png"}:
            # Skip tiny files (likely thumbnails or junk)
            if f.stat().st_size > 5000:
                covers.append(f)
    return covers


def clean_junk_files(source: Path):
    """Remove junk files from the source directory."""
    removed = 0
    for f in source.rglob("*"):
        if f.is_file() and f.suffix.lower() in JUNK_EXTENSIONS:
            f.unlink()
            removed += 1
    if removed:
        logger.info(f"Cleaned {removed} junk file(s) from {source.name}")


def build_destination(library: Path, meta: dict) -> Path:
    """
    Build Audiobookshelf-compatible path:
      Author/Title/          (no series)
      Author/Series/Title/   (with series)
    """
    author = sanitize_dirname(meta.get("author", "Unknown"))
    title = sanitize_dirname(meta.get("title", "Unknown"))

    if meta.get("series"):
        series = sanitize_dirname(meta["series"])
        return library / author / series / title
    else:
        return library / author / title


def _similarity(a: str, b: str) -> float:
    """Simple word-overlap similarity between two strings."""
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    if not words_a or not words_b:
        return 0.0
    overlap = words_a & words_b
    return len(overlap) / max(len(words_a), len(words_b))


# ─── Main Processing Pipeline ────────────────────────────────────────────────


async def process_item(
    source_path: Path,
    media_type: str,
    audiobook_lib: Path,
    ebook_lib: Path,
    cache_dir: Path | None = None,
    bot=None,
):
    """
    Process a single download (folder or file) into Audiobookshelf format.
    media_type: 'audiobook' or 'ebook'

    Metadata priority (file contents first, folder name last):
      1. Hint file (.plexbie_meta.json) — if present, skip all parsing/extraction
      2. Embedded file metadata (M4B/EPUB/MP3 tags, ISBN)
      3. API lookup using embedded data (ISBN exact match, then title+author)
      4. Folder name parsing (only if files contain no metadata)
    """
    if source_path.name.startswith("."):
        return

    logger.info(f"{'=' * 60}")
    logger.info(f"Processing {media_type}: {source_path.name}")

    # ── Check for hint files from Plexbie media_requests ──
    hint_file = None
    hint_used = False
    hint = None

    # Check 1: .plexbie_meta.json inside the download folder
    candidate = source_path / ".plexbie_meta.json" if source_path.is_dir() else source_path.parent / ".plexbie_meta.json"
    if candidate.exists():
        hint_file = candidate

    # Check 2: .plexbie_hint_<name>.json in the watch directory (written by media_requests cog)
    if not hint_file:
        watch_dir = source_path.parent
        item_name = source_path.name
        for f in watch_dir.glob(".plexbie_hint_*.json"):
            try:
                with open(f) as fh:
                    h = json.load(fh)
                # Match by NZB title — SABnzbd creates folder with this name
                nzb_title = h.get("nzb_title", "")
                if nzb_title and (nzb_title in item_name or item_name in nzb_title):
                    hint_file = f
                    break
            except Exception:
                continue

    if hint_file:
        try:
            with open(hint_file) as f:
                hint = json.load(f)
            logger.info(f"Using hint file metadata: {hint.get('title')} by {hint.get('author')}")

            # Use hint data directly — skip all parsing and extraction
            final = {
                "author": hint.get("author", "Unknown"),
                "title": hint.get("title", source_path.name),
                "year": hint.get("year"),
                "series": hint.get("series"),
                "series_index": hint.get("series_index"),
                "cover_url": hint.get("cover_url"),
                "isbn": hint.get("isbn"),
            }
            hint_used = True
        except Exception as e:
            logger.warning(f"Failed to read hint file, falling back to extraction: {e}")

    # 1. Find book files first
    if source_path.is_file():
        book_files = [source_path]
        existing_covers = []
    else:
        clean_junk_files(source_path)
        book_files = find_book_files(source_path, media_type)
        existing_covers = find_existing_covers(source_path)

    if not book_files:
        logger.warning(f"No valid {media_type} files found in {source_path.name}, skipping")
        return

    logger.info(f"Found {len(book_files)} {media_type} file(s)")

    embedded_cover_data = None

    if not hint_used:
        # 2. READ THE FILES - extract embedded metadata (this is our primary source)
        embedded = extract_metadata_from_files(book_files)
        isbn = None

        if embedded:
            embedded_cover_data = embedded.pop("embedded_cover", None)
            isbn = embedded.get("isbn")
            embedded.pop("genre", None)
            embedded.pop("description", None)
            logger.info(f"FILE METADATA -> Title: {embedded.get('title')}, "
                        f"Author: {embedded.get('author')}, "
                        f"Series: {embedded.get('series')}, "
                        f"ISBN: {isbn}, Year: {embedded.get('year')}")
        else:
            logger.info("No embedded metadata found in files")

        # 3. Build metadata - file contents take absolute priority
        final = {
            "author": None,
            "title": None,
            "year": None,
            "series": None,
            "series_index": None,
            "cover_url": None,
        }

        # Apply embedded metadata first
        if embedded:
            for key in ["author", "title", "year", "series", "series_index"]:
                if embedded.get(key):
                    final[key] = embedded[key]

        # 4. Use API to enrich (fill gaps, get cover art)
        # Use embedded title+author for the search, NOT the folder name
        search_author = final.get("author") or ""
        search_title = final.get("title") or ""

        # Only fall back to folder name parsing if files gave us NOTHING
        if not search_author and not search_title and not isbn:
            logger.info("No file metadata available, falling back to folder name parsing")
            parsed = parse_release_name(source_path.name)
            search_author = parsed.get("author", "")
            search_title = parsed.get("title", "")
            # Apply parsed values as fallback for any gaps
            for key in ["author", "title", "year", "series", "series_index"]:
                if not final.get(key) and parsed.get(key):
                    final[key] = parsed[key]
            logger.info(f"FOLDER PARSE -> Author: {search_author}, Title: {search_title}")

        # API lookup - use ISBN if available (exact match), otherwise title+author
        if isbn or (search_author and search_title):
            api_meta = await fetch_metadata(search_author, search_title, isbn=isbn)
        elif search_title:
            api_meta = await fetch_metadata("", search_title, isbn=isbn)
        else:
            api_meta = None

        # Merge API results - only fill gaps, never override file metadata
        if api_meta:
            for key in ["author", "title", "year", "series", "series_index", "cover_url"]:
                api_val = api_meta.get(key)
                if api_val and not final.get(key):
                    final[key] = api_val

        # Last resort defaults
        if not final.get("author"):
            final["author"] = "Unknown"
        if not final.get("title"):
            # Try to get something from the folder name
            parsed = parse_release_name(source_path.name)
            final["title"] = parsed.get("title") or source_path.name

    logger.info(f"FINAL -> Author: {final['author']}, Title: {final['title']}, "
                f"Series: {final.get('series')}, Index: {final.get('series_index')}")

    # 4. Build destination path
    library = audiobook_lib if media_type == "audiobook" else ebook_lib
    dest = build_destination(library, final)

    # Handle duplicate destinations
    if dest.exists() and any(dest.iterdir()):
        base_dest = dest
        counter = 2
        while dest.exists() and any(dest.iterdir()):
            dest = base_dest.parent / f"{base_dest.name} ({counter})"
            counter += 1
        logger.warning(f"Destination exists, using: {dest}")

    dest.mkdir(parents=True, exist_ok=True)

    # 5. Move book files
    for f in book_files:
        target = dest / f.name
        try:
            shutil.move(str(f), str(target))
            logger.debug(f"Moved: {f.name}")
        except Exception as e:
            logger.error(f"Failed to move {f.name}: {e}")

    # 6. Handle cover art
    cover_done = False

    # First, try embedded cover from the file itself (only if not using hint)
    if embedded_cover_data and not cover_done:
        try:
            (dest / "cover.jpg").write_bytes(embedded_cover_data)
            cover_done = True
            logger.info(f"Used embedded cover art ({len(embedded_cover_data)} bytes)")
        except Exception as e:
            logger.debug(f"Failed to write embedded cover: {e}")

    # Second, check for existing cover in source directory
    if not cover_done and existing_covers:
        best_cover = max(existing_covers, key=lambda c: c.stat().st_size if c.exists() else 0)
        if best_cover.exists():
            try:
                shutil.move(str(best_cover), str(dest / "cover.jpg"))
                cover_done = True
                logger.info(f"Used existing cover: {best_cover.name}")
            except Exception:
                pass

    # If no existing cover, download one (works for both hint and normal paths)
    if not cover_done and final.get("cover_url"):
        cover_done = await download_cover(final["cover_url"], dest / "cover.jpg", cache_dir=cache_dir)

    if not cover_done:
        logger.warning(f"No cover art available for {final['title']}")

    # 7. Generate metadata.opf
    generate_opf(final, dest / "metadata.opf")

    # 8. Clean up source
    if source_path.is_dir() and source_path.exists():
        try:
            shutil.rmtree(source_path)
            logger.debug(f"Removed source directory: {source_path.name}")
        except Exception as e:
            logger.warning(f"Could not remove source directory: {e}")
    elif source_path.is_file() and source_path.exists():
        try:
            source_path.unlink()
        except Exception:
            pass

    # 9. Clean up hint file if used
    if hint_used and hint_file.exists():
        try:
            hint_file.unlink()
            logger.debug("Removed hint file after successful processing")
        except Exception:
            pass

    logger.info(f"Complete: {final['author']} / {final['title']} -> {dest}")

    # ── Send Discord notifications ──
    if bot:
        try:
            # 1. Send "New Book Added" embed to updates channel
            updates_channel_id = int(os.environ.get("UPDATES_CHANNEL_ID", "0"))
            if updates_channel_id:
                channel = bot.get_channel(updates_channel_id)
                if channel:
                    from datetime import timezone as _tz
                    format_emoji = "📖" if media_type == "ebook" else "🎧"
                    format_label = "Ebook" if media_type == "ebook" else "Audiobook"

                    embed = discord.Embed(
                        title=f"{format_emoji} New {format_label} Added to Library!",
                        description=f"**{final['title']}** by {final['author']}",
                        color=discord.Color.blue(),
                        timestamp=datetime.now(_tz.utc),
                    )
                    embed.add_field(name="Author", value=final['author'], inline=True)
                    embed.add_field(name="Format", value=f"{format_emoji} {format_label}", inline=True)
                    if final.get('year'):
                        embed.add_field(name="Year", value=str(final['year']), inline=True)
                    if final.get('series'):
                        series_text = final['series']
                        if final.get('series_index'):
                            series_text += f" #{final['series_index']}"
                        embed.add_field(name="Series", value=series_text, inline=True)
                    embed.set_footer(text="Added to Audiobookshelf")

                    # Attach cover image if available
                    cover_path = dest / "cover.jpg"
                    if cover_path.exists():
                        file = discord.File(str(cover_path), filename="cover.jpg")
                        embed.set_thumbnail(url="attachment://cover.jpg")
                        await channel.send(embed=embed, file=file)
                    else:
                        await channel.send(embed=embed)

                    logger.info(f"Sent notification to updates channel for: {final['title']}")

            # 2. DM the requester if hint file had a user ID
            if hint_used and hint is not None:
                requester_id = hint.get("requested_by")
                if requester_id:
                    try:
                        user = bot.get_user(int(requester_id))
                        if user:
                            from datetime import timezone as _tz
                            format_emoji = "📖" if media_type == "ebook" else "🎧"
                            format_label = "ebook" if media_type == "ebook" else "audiobook"

                            dm_embed = discord.Embed(
                                title=f"📗 Your {format_label.title()} is Ready!",
                                description=f"**{final['title']}** by {final['author']} is now available in Audiobookshelf!",
                                color=discord.Color.green(),
                                timestamp=datetime.now(_tz.utc),
                            )
                            dm_embed.add_field(name="Format", value=f"{format_emoji} {format_label.title()}", inline=True)
                            if final.get('series'):
                                series_text = final['series']
                                if final.get('series_index'):
                                    series_text += f" #{final['series_index']}"
                                dm_embed.add_field(name="Series", value=series_text, inline=True)
                            dm_embed.set_footer(text="Enjoy your reading! 📚")

                            await send_user_dm(bot, services, user, context=f"bookshelf item ready for {final['title']}", embed=dm_embed)
                            logger.info(f"Sent DM to user {requester_id} about {final['title']}")
                    except Exception as e:
                        logger.warning(f"Could not DM requester {requester_id}: {e}")

        except Exception as e:
            logger.error(f"Error sending notifications: {e}", exc_info=True)


# ─── Discord Cog ─────────────────────────────────────────────────────────────


class BookshelfProcessorCog(commands.Cog):
    """Watches SABnzbd download directories and organizes ebooks/audiobooks
    for Audiobookshelf. Replaces the standalone bookshelf-processor container."""

    def __init__(self, bot: commands.Bot, services):
        self.bot = bot
        self.services = services

        # Configuration from env vars
        self.audiobook_watch = Path(os.environ.get("BOOKSHELF_AUDIOBOOK_WATCH", "/watch/audiobooks"))
        self.ebook_watch = Path(os.environ.get("BOOKSHELF_EBOOK_WATCH", "/watch/ebooks"))
        self.audiobook_lib = Path(os.environ.get("BOOKSHELF_AUDIOBOOK_LIBRARY", "/library/audiobooks"))
        self.ebook_lib = Path(os.environ.get("BOOKSHELF_EBOOK_LIBRARY", "/library/ebooks"))
        self.settle_seconds = int(os.environ.get("BOOKSHELF_SETTLE_SECONDS", "120"))

        # Cache directory for cover art
        self.cache_dir = Path(os.environ.get("BOOKSHELF_CACHE_DIR", "/app/cache/bookshelf"))

        # Pending items: {path_str: datetime_first_seen}
        self.pending: dict[str, datetime] = {}

    async def cog_load(self):
        """Called when the cog is loaded. Start the watcher loop."""
        logger.info("Bookshelf Processor loading")
        logger.info(f"Audiobook watch:   {self.audiobook_watch}")
        logger.info(f"Ebook watch:       {self.ebook_watch}")
        logger.info(f"Audiobook library: {self.audiobook_lib}")
        logger.info(f"Ebook library:     {self.ebook_lib}")
        logger.info(f"Settle time:       {self.settle_seconds}s")

        # Ensure cache directory exists
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # Process any existing items in watch directories on startup
        await self._process_existing_items()

        # Start the scan loop
        self.scan_loop.start()

    async def cog_unload(self):
        """Called when the cog is unloaded. Stop the watcher loop."""
        self.scan_loop.cancel()
        logger.info("Bookshelf Processor stopped")

    async def _process_existing_items(self):
        """Process anything already sitting in watch dirs on startup."""
        logger.info("Checking for existing items in watch directories...")

        if self.audiobook_watch.exists():
            for path in sorted(self.audiobook_watch.iterdir()):
                if not path.name.startswith("."):
                    try:
                        await process_item(
                            path, "audiobook",
                            self.audiobook_lib, self.ebook_lib,
                            cache_dir=self.cache_dir,
                            bot=self.bot,
                        )
                    except Exception as e:
                        logger.error(f"Failed to process existing audiobook {path.name}: {e}", exc_info=True)

        if self.ebook_watch.exists():
            for path in sorted(self.ebook_watch.iterdir()):
                if not path.name.startswith("."):
                    try:
                        await process_item(
                            path, "ebook",
                            self.audiobook_lib, self.ebook_lib,
                            cache_dir=self.cache_dir,
                            bot=self.bot,
                        )
                    except Exception as e:
                        logger.error(f"Failed to process existing ebook {path.name}: {e}", exc_info=True)

    @tasks.loop(seconds=10)
    async def scan_loop(self):
        """Scan watch directories every 10 seconds for new items."""
        now = datetime.now()

        # Scan for new items in watch directories
        for watch_dir, media_type in [
            (self.audiobook_watch, "audiobook"),
            (self.ebook_watch, "ebook"),
        ]:
            if not watch_dir.exists():
                continue

            for path in watch_dir.iterdir():
                if path.name.startswith("."):
                    continue

                path_str = str(path)
                if path_str not in self.pending:
                    self.pending[path_str] = now
                    logger.info(f"New {media_type} detected: {path.name}")

        # Process settled items
        settled = []
        for path_str, first_seen in list(self.pending.items()):
            if (now - first_seen).total_seconds() >= self.settle_seconds:
                path = Path(path_str)
                if path.exists():
                    # Determine media type from which watch dir it's in
                    if str(path).startswith(str(self.audiobook_watch)):
                        media_type = "audiobook"
                    else:
                        media_type = "ebook"
                    settled.append((path, media_type))
                del self.pending[path_str]

        for path, media_type in settled:
            try:
                await process_item(
                    path, media_type,
                    self.audiobook_lib, self.ebook_lib,
                    cache_dir=self.cache_dir,
                    bot=self.bot,
                )
            except Exception as e:
                logger.error(f"Failed to process {media_type} {path.name}: {e}", exc_info=True)

    @scan_loop.before_loop
    async def before_scan_loop(self):
        """Wait for the bot to be ready before starting the scan loop."""
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(BookshelfProcessorCog(bot, bot.services))
