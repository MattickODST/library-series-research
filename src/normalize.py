import argparse
import json
import re

SPACE_RE = re.compile(r"\s+")
ROLE_RE = re.compile(r"(?i)(?:,\s*)?\bauthor\.?\s*$")
YEAR_RE = re.compile(r"^\s*(\d{4})\s*-\s*$")
VOLUME_RE = re.compile(r"(?i)(?:\s*[/,:]?\s*)\bv\.?\s*(\d+(?:\.\d+)?)\b")
GENERIC_NOVEL_RE = re.compile(r"(?i)\s*:\s*a\s+novel\s*$")

def clean_space(value):
    return SPACE_RE.sub(" ", str(value or "").strip())

def normalize_author(raw):
    original = clean_space(raw)
    cleaned = ROLE_RE.sub("", original).strip(" ,.;")
    parts = [p.strip() for p in cleaned.split(",") if p.strip()]
    year = None
    name_parts = []
    for part in parts:
        m = YEAR_RE.match(part)
        if m:
            year = int(m.group(1))
        else:
            name_parts.append(part)
    if len(name_parts) >= 2:
        search_author = clean_space(name_parts[1] + " " + name_parts[0])
    else:
        search_author = clean_space(cleaned)
    return search_author, year

def normalize_title(raw):
    original = clean_space(raw)
    volumes = VOLUME_RE.findall(original)
    cleaned = VOLUME_RE.sub(" ", original)
    cleaned = clean_space(cleaned).strip(" /,:;-")
    no_generic_novel = GENERIC_NOVEL_RE.sub("", cleaned).strip(" /,:;-")
    variants = []
    for value in [no_generic_novel, cleaned, original]:
        value = clean_space(value)
        if value and value.casefold() not in [v.casefold() for v in variants]:
            variants.append(value)
    return variants, volumes[0] if volumes else None

def normalize(title, author, publication_year=None):
    title_variants, volume_hint = normalize_title(title)
    search_author, author_year = normalize_author(author)
    return {
        "raw_title": title,
        "raw_author": author,
        "publication_year": publication_year,
        "search_title": title_variants[0] if title_variants else clean_space(title),
        "title_variants": title_variants,
        "search_author": search_author,
        "author_year": author_year,
        "catalog_volume_hint": volume_hint
    }

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--title", required=True)
    p.add_argument("--author", required=True)
    p.add_argument("--year")
    a = p.parse_args()
    print(json.dumps(normalize(a.title, a.author, a.year), ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
