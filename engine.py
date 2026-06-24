"""
engine.py  —  Content Calendar Engine
Matches the HTML app exactly: topic parsing (label | hint), word count
targets, multiple anchor variants per pillar page, sitemap fetching,
single-tag resolution from allowed pool, and Yoast SEO meta fields.
"""

import json
import os
import re
import base64
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime

# ── Constants ──────────────────────────────────────────────────────────────────
ANTHROPIC_URL   = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-sonnet-4-6"
UNSPLASH_URL    = "https://api.unsplash.com/search/photos"


# ── Env helpers ────────────────────────────────────────────────────────────────
def _key(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise EnvironmentError(f"Missing environment variable: {name}")
    return val


# ── WordPress helpers ──────────────────────────────────────────────────────────
def _wp_headers(site: dict, extra: dict | None = None) -> dict:
    creds = f"{site['wp_username']}:{site['wp_app_password']}"
    token = base64.b64encode(creds.encode()).decode()
    h = {"Authorization": f"Basic {token}", "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h


def _wp_url(site: dict, path: str = "") -> str:
    return site["wp_url"].rstrip("/") + "/wp-json/wp/v2" + path


def _wp_get(site: dict, path: str, params: dict | None = None) -> dict | list:
    url = _wp_url(site, path)
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=_wp_headers(site))
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _wp_post(site: dict, path: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        _wp_url(site, path),
        data=data,
        headers=_wp_headers(site),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


# ── Topic parsing ──────────────────────────────────────────────────────────────
def parse_topic(raw: str) -> tuple[str, str]:
    """
    Split "Label | hint about angles" into (label, hint).
    Plain topics return (topic, "").
    """
    parts = raw.split("|", 1)
    label = parts[0].strip()
    hint  = parts[1].strip() if len(parts) > 1 else ""
    return label, hint


# ── Sitemap fetcher ────────────────────────────────────────────────────────────
def fetch_sitemap_urls(sitemap_url: str) -> list[str]:
    """
    Fetch and parse sitemap XML. Returns up to 150 <loc> URLs.
    Tries direct fetch first, then two CORS-free proxy fallbacks.
    Non-fatal — returns [] on any failure.
    """
    if not sitemap_url:
        return []

    attempts = [
        sitemap_url,
        f"https://api.allorigins.win/raw?url={urllib.parse.quote(sitemap_url)}",
        f"https://corsproxy.io/?{urllib.parse.quote(sitemap_url)}",
    ]

    for url in attempts:
        try:
            req = urllib.request.Request(
                url,
                headers={"Accept": "application/xml,text/xml,*/*", "User-Agent": "ContentCalendar/1.0"},
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                xml = r.read().decode("utf-8", errors="replace")
            if "<loc>" not in xml:
                continue
            locs = re.findall(r"<loc>(https?://[^<]+)</loc>", xml)
            urls = [u.strip() for u in locs][:150]
            if urls:
                print(f"  Sitemap: {len(urls)} URLs loaded from {url[:60]}")
                return urls
        except Exception:
            continue

    print("  Sitemap: could not fetch (will continue without sitemap links)")
    return []


# ── Internal linking block ─────────────────────────────────────────────────────
def build_linking_block(site: dict, sitemap_urls: list[str]) -> str:
    """
    Build the INTERNAL LINKING RULES section for the article prompt.
    Supports multiple anchor variants per pillar page.
    """
    pillar_pages = site.get("pillar_pages", [])
    if not pillar_pages and not sitemap_urls:
        return ""

    block = "\nINTERNAL LINKING RULES — CRITICAL:\n"
    block += "You may ONLY link to URLs from the lists below. Do NOT invent, guess, or construct any URLs. If no URL fits naturally, do not add a link.\n\n"

    if pillar_pages:
        block += "PILLAR PAGES (highest priority):\n"
        block += "For each page, choose ONE anchor variant that reads most naturally in context. Vary choices — do not always use the first option.\n\n"
        for p in pillar_pages:
            url = p.get("url", "")
            # Support both {anchors:[...]} (new) and {keyphrase:"..."} (legacy)
            anchors = p.get("anchors") or ([p["keyphrase"]] if p.get("keyphrase") else [])
            if not url or not anchors:
                continue
            options = " | ".join(f'"{a}"' for a in anchors)
            block += f"  URL: {url}\n"
            block += f"  Anchor options (pick ONE): {options}\n\n"

    if sitemap_urls:
        block += "SITEMAP PAGES (use as supporting links where genuinely relevant):\n"
        for u in sitemap_urls:
            block += f"  {u}\n"
        block += "\n"

    block += "LINK FORMAT: <a href=\"EXACT_URL_FROM_LISTS_ABOVE\">anchor text</a>\n"
    block += "Aim for 2-4 internal links per article. Only link when it genuinely helps the reader.\n"
    return block


# ── Tag resolution ─────────────────────────────────────────────────────────────
def resolve_tag(site: dict, chosen_name: str) -> int | None:
    """
    Match Claude's chosen tag name against the site's allowed tag pool.
    Returns a single WP tag ID or None.
    """
    pool    = site.get("wp_tag_pool", site.get("wpTagPool", []))
    allowed = site.get("allowed_tag_ids", site.get("allowedTagIds", []))
    if not pool or not allowed:
        return None
    chosen_lower = chosen_name.lower().strip()
    for tag in pool:
        if tag["id"] in allowed and tag["name"].lower().strip() == chosen_lower:
            return tag["id"]
    return None


def get_or_create_tag(site: dict, name: str) -> int | None:
    """Fallback: get-or-create a tag in WordPress when no allowed pool is configured."""
    try:
        result = _wp_post(site, "/tags", {"name": name})
        return result.get("id")
    except urllib.error.HTTPError as e:
        if e.code == 400:  # term already exists
            results = _wp_get(site, "/tags", {"search": name, "per_page": 5})
            if isinstance(results, list) and results:
                return results[0]["id"]
    except Exception:
        pass
    return None


# ── Article generation ─────────────────────────────────────────────────────────
def write_article(topic_raw: str, site: dict, sitemap_urls: list[str]) -> dict:
    """
    Call Claude to write a full SEO article.
    Returns dict: title, content, excerpt, yoast_keyphrase, yoast_meta, tags, search_term.
    """
    today       = datetime.utcnow().strftime("%B %d, %Y")
    label, hint = parse_topic(topic_raw)

    word_count  = int(site.get("word_count", site.get("wordCount", 800)))
    wc_low      = round(word_count * 0.9)
    wc_high     = round(word_count * 1.1)

    linking_block = build_linking_block(site, sitemap_urls)

    # Tag instruction — single tag from allowed pool or free suggestion
    pool    = site.get("wp_tag_pool", site.get("wpTagPool", []))
    allowed = site.get("allowed_tag_ids", site.get("allowedTagIds", []))
    allowed_names = [t["name"] for t in pool if t["id"] in allowed]
    if allowed_names:
        tag_instruction = (
            f"TAG: Choose exactly ONE tag from this list that most closely matches the topic "
            f"— do not invent tags, do not choose more than one:\n{', '.join(allowed_names)}"
        )
    else:
        tag_instruction = "TAG: Suggest exactly one short, relevant tag."

    prompt = f"""You are a content writer for {site.get('name', '')}, a {site.get('niche', 'general')} website.

TODAY: {today}
TOPIC AREA: {label}{f"{chr(10)}CONTENT FOCUS: {hint}" if hint else ""}

SITE INFO:
{site.get('description') or f"{site.get('name', '')} serves {site.get('audience', 'general readers')}."}
AUDIENCE: {site.get('audience', 'general readers')}
TONE: {site.get('tone', 'helpful and informative')}
{f"USP (reference naturally): {site['usp']}" if site.get('usp') else ""}
{linking_block}
TITLE RULE: Create an original, compelling SEO headline. Do NOT restate the topic word-for-word. Make it specific and search-friendly. Example: if topic is "Tax Planning" with focus "deductions, entity selection", a strong title is "7 Tax Planning Moves Every Small Business Should Make Before Year-End".

ARTICLE REQUIREMENTS:
- Target length: {wc_low}–{wc_high} words (target: {word_count})
- SEO-optimised, primary keyword used naturally throughout
- Strong hook opening paragraph
- 2-3 subheadings using <h2> tags
- Actionable insights drawn from the content focus
- HTML only: <p>, <strong>, <h2>, <a href="..."> — no markdown
- Internal links MUST use only URLs from the lists provided — no exceptions
- No disclaimers or "this is not advice" language
- End with a clear call to action

YOAST SEO:
- yoast_keyphrase: 2-5 word focus keyphrase (primary SEO term this article targets)
- yoast_meta: meta description exactly 120-158 characters, must include the focus keyphrase, written to earn a click

{tag_instruction}

Respond ONLY with valid JSON (no markdown fences, no preamble):
{{"title":"<compelling headline>","content":"<full article HTML>","excerpt":"<1-2 sentence WP excerpt>","yoast_keyphrase":"<2-5 word focus keyphrase>","yoast_meta":"<meta description 120-158 chars>","tags":["single_tag"],"search_term":"<3-5 word Unsplash photo search>"}}"""

    max_tokens = max(2000, round(word_count * 2.2))

    r_data = json.dumps({
        "model":      ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "messages":   [{"role": "user", "content": prompt}],
    }).encode()

    req = urllib.request.Request(
        ANTHROPIC_URL,
        data=r_data,
        headers={
            "x-api-key":          _key("ANTHROPIC_API_KEY"),
            "anthropic-version":  "2023-06-01",
            "content-type":       "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        resp = json.loads(r.read())

    text = resp["content"][0]["text"].strip()
    text = re.sub(r"^```json\s*", "", text)
    text = re.sub(r"\s*```$",    "", text)
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError(f"Could not parse article JSON from Claude: {text[:300]}")

    article = json.loads(m.group())

    # Enforce single tag
    if isinstance(article.get("tags"), list):
        article["tags"] = article["tags"][:1]

    # Enforce Yoast meta character range
    meta = article.get("yoast_meta", "")
    if meta and len(meta) > 158:
        article["yoast_meta"] = meta[:155] + "..."

    return article


# ── Unsplash ───────────────────────────────────────────────────────────────────
def fetch_unsplash_photo(search_term: str) -> dict | None:
    params = urllib.parse.urlencode({
        "query":          search_term,
        "per_page":       5,
        "orientation":    "landscape",
        "content_filter": "high",
    })
    req = urllib.request.Request(
        f"{UNSPLASH_URL}?{params}",
        headers={"Authorization": f"Client-ID {_key('UNSPLASH_ACCESS_KEY')}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        results = data.get("results", [])
        if not results:
            return None
        p = results[0]
        return {
            "url":              p["urls"]["regular"],
            "download_url":     p["links"]["download_location"],
            "photographer":     p["user"]["name"],
            "photographer_url": p["user"]["links"]["html"],
            "alt":              p.get("alt_description", search_term),
        }
    except Exception as e:
        print(f"  Unsplash error: {e}")
        return None


def trigger_unsplash_download(download_url: str):
    """Required by Unsplash API terms of service."""
    try:
        req = urllib.request.Request(
            download_url,
            headers={"Authorization": f"Client-ID {_key('UNSPLASH_ACCESS_KEY')}"},
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


# ── WordPress publishing ───────────────────────────────────────────────────────
def upload_photo(site: dict, photo: dict) -> int | None:
    """Download photo from Unsplash and upload to WordPress. Returns media ID."""
    try:
        with urllib.request.urlopen(photo["url"], timeout=20) as r:
            img_bytes = r.read()
    except Exception as e:
        print(f"  Photo download error: {e}")
        return None

    filename = f"cc-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.jpg"
    headers = {
        **_wp_headers(site),
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type": "image/jpeg",
    }
    del headers["Content-Type"]  # will be reset below
    headers["Content-Type"] = "image/jpeg"

    req = urllib.request.Request(
        _wp_url(site, "/media"),
        data=img_bytes,
        headers={
            "Authorization":      _wp_headers(site)["Authorization"],
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type":        "image/jpeg",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            media = json.loads(r.read())
        media_id = media["id"]
        # Set caption and alt text
        credit = (
            f'Photo by <a href="{photo["photographer_url"]}" target="_blank">'
            f'{photo["photographer"]}</a> on '
            f'<a href="https://unsplash.com" target="_blank">Unsplash</a>'
        )
        _wp_post(site, f"/media/{media_id}", {"caption": credit, "alt_text": photo["alt"]})
        return media_id
    except Exception as e:
        print(f"  Media upload error: {e}")
        return None


def get_or_create_category(site: dict, name: str) -> int | None:
    try:
        results = _wp_get(site, "/categories", {"search": name, "per_page": 20})
        for cat in results:
            if cat["name"].lower() == name.lower():
                return cat["id"]
        result = _wp_post(site, "/categories", {"name": name})
        return result.get("id")
    except Exception as e:
        print(f"  Category error: {e}")
        return None


def publish_post(site: dict, article: dict, media_id: int | None, photo: dict | None) -> str:
    """
    Publish the article to WordPress.
    Handles: category, single tag (from allowed pool), featured image,
    Yoast focus keyphrase, Yoast meta description.
    Returns the published post URL.
    """
    # Category
    cat_id = get_or_create_category(site, site.get("category", "Blog"))
    print(f"  Category: {site.get('category')} (ID {cat_id})")

    # Single tag — from allowed pool, or get-or-create as fallback
    tag_ids = []
    chosen_tag = (article.get("tags") or [""])[0]
    if chosen_tag:
        resolved = resolve_tag(site, chosen_tag)
        if resolved:
            tag_ids = [resolved]
            print(f"  Tag: \"{chosen_tag}\" (ID {resolved})")
        else:
            fallback = get_or_create_tag(site, chosen_tag)
            if fallback:
                tag_ids = [fallback]
                print(f"  Tag (created): \"{chosen_tag}\" (ID {fallback})")
            else:
                print(f"  Tag: no match for \"{chosen_tag}\" — publishing without tag")

    # Photo credit appended to content
    credit = ""
    if photo:
        credit = (
            f'<p style="font-size:11px;color:#9a9a9a;margin-top:24px">'
            f'Photo: <a href="{photo["photographer_url"]}">{photo["photographer"]}</a>'
            f" / Unsplash</p>"
        )

    post_data = {
        "title":          article["title"],
        "content":        article["content"] + credit,
        "excerpt":        article.get("excerpt", ""),
        "status":         "publish",
        "categories":     [cat_id] if cat_id else [],
        "tags":           tag_ids,
        "comment_status": "open",
        "meta": {
            "_yoast_wpseo_focuskw":  article.get("yoast_keyphrase", ""),
            "_yoast_wpseo_metadesc": article.get("yoast_meta", article.get("excerpt", "")),
        },
    }
    if media_id:
        post_data["featured_media"] = media_id

    result = _wp_post(site, "/posts", post_data)
    post_id  = result["id"]
    post_url = result.get("link", "")

    # Second Yoast update — belt-and-suspenders for plugins that need a separate PATCH
    if article.get("yoast_keyphrase") or article.get("yoast_meta"):
        try:
            _wp_post(site, f"/posts/{post_id}", {
                "meta": {
                    "_yoast_wpseo_focuskw":  article.get("yoast_keyphrase", ""),
                    "_yoast_wpseo_metadesc": article.get("yoast_meta", ""),
                }
            })
            print(f"  Yoast: keyphrase=\"{article.get('yoast_keyphrase')}\" meta={len(article.get('yoast_meta',''))} chars")
        except Exception as e:
            print(f"  Yoast meta update failed (post still published): {e}")

    return post_url
