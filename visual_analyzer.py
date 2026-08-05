from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
VISION_MODEL = os.getenv("VISION_MODEL", "gpt-5.6").strip()
VISION_MAX_IMAGES = max(1, min(int(os.getenv("VISION_MAX_IMAGES", "3")), 5))
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"

VISION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "category": {"type": "string"},
        "brand": {"type": "string"},
        "model": {"type": "string"},
        "variant": {"type": "string"},
        "storage_or_size": {"type": "string"},
        "estimated_year": {"type": "string"},
        "visible_condition": {
            "type": "string",
            "enum": ["new", "like_new", "good", "fair", "damaged", "unknown"],
        },
        "visible_defects": {"type": "array", "items": {"type": "string"}},
        "visible_accessories": {"type": "array", "items": {"type": "string"}},
        "text_image_consistency": {
            "type": "string",
            "enum": ["consistent", "partially_consistent", "inconsistent", "unknown"],
        },
        "counterfeit_or_fraud_signals": {
            "type": "array",
            "items": {"type": "string"},
        },
        "recognition_confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        "condition_confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        "notes": {"type": "string"},
    },
    "required": [
        "category", "brand", "model", "variant", "storage_or_size",
        "estimated_year", "visible_condition", "visible_defects",
        "visible_accessories", "text_image_consistency",
        "counterfeit_or_fraud_signals", "recognition_confidence",
        "condition_confidence", "notes",
    ],
    "additionalProperties": False,
}


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _normalize_listing_url(raw_url: str) -> str:
    """Pulisce l'URL senza eliminare l'estensione .htm."""
    cleaned = _clean(raw_url).strip(" <>\"'")

    parsed = urlsplit(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("URL annuncio non valido.")

    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/")

    if "subito.it" in host:
        if re.search(r"-\d{5,}$", path):
            path += ".htm"

        return urlunsplit((
            parsed.scheme,
            parsed.netloc,
            path,
            "",
            "",
        ))

    return urlunsplit((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        parsed.query,
        "",
    ))


def _image_candidates(soup: BeautifulSoup, page_url: str) -> List[str]:
    candidates: List[str] = []

    for selector, attribute in (
        ('meta[property="og:image"]', "content"),
        ('meta[name="twitter:image"]', "content"),
    ):
        node = soup.select_one(selector)
        if node and node.get(attribute):
            candidates.append(urljoin(page_url, str(node.get(attribute))))

    for image in soup.find_all("img"):
        for attribute in ("src", "data-src", "data-lazy-src"):
            value = image.get(attribute)
            if isinstance(value, str) and value.strip():
                candidates.append(urljoin(page_url, value.strip()))

        srcset = image.get("srcset")
        if isinstance(srcset, str):
            for part in srcset.split(","):
                image_url = part.strip().split(" ")[0]
                if image_url:
                    candidates.append(urljoin(page_url, image_url))

    unique: List[str] = []
    seen = set()

    for image_url in candidates:
        lowered = image_url.lower()

        if not lowered.startswith(("http://", "https://")):
            continue

        if any(token in lowered for token in ("logo", "icon", "avatar", "sprite")):
            continue

        if image_url not in seen:
            seen.add(image_url)
            unique.append(image_url)

    return unique[:VISION_MAX_IMAGES]


async def fetch_listing_context(url: str) -> Dict[str, Any]:
    original_url = _clean(url)
    normalized_url = _normalize_listing_url(original_url)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.7",
        "Cache-Control": "no-cache",
        "Referer": "https://www.subito.it/",
    }

    async with httpx.AsyncClient(
        headers=headers,
        follow_redirects=True,
        timeout=30,
    ) as client:
        response = await client.get(normalized_url)

        if response.status_code == 404 and original_url != normalized_url:
            response = await client.get(original_url)

        if response.status_code == 404:
            raise RuntimeError(
                "Annuncio non trovato (HTTP 404). "
                "Il link può essere scaduto, rimosso oppure copiato incompleto."
            )

        response.raise_for_status()

    final_url = str(response.url)
    soup = BeautifulSoup(response.text, "html.parser")

    title = ""
    og_title = soup.select_one('meta[property="og:title"]')
    if og_title and og_title.get("content"):
        title = _clean(og_title.get("content"))

    if not title and soup.title:
        title = _clean(soup.title.get_text(" ", strip=True))

    description = ""
    for selector in (
        'meta[property="og:description"]',
        'meta[name="description"]',
    ):
        node = soup.select_one(selector)
        if node and node.get("content"):
            description = _clean(node.get("content"))
            if description:
                break

    image_urls = _image_candidates(soup, final_url)

    return {
        "url": final_url,
        "title": title[:500],
        "description": description[:3000],
        "image_urls": image_urls,
    }


def _extract_output_text(payload: Dict[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    for output in payload.get("output", []):
        if not isinstance(output, dict) or output.get("type") != "message":
            continue

        for content in output.get("content", []):
            if not isinstance(content, dict):
                continue

            if content.get("type") in {"output_text", "text"}:
                text = content.get("text")
                if isinstance(text, str) and text.strip():
                    return text.strip()

    raise RuntimeError(
        "La risposta OpenAI non contiene output testuale leggibile."
    )


async def analyze_listing(
    url: str,
    *,
    listing_context: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    if not OPENAI_API_KEY:
        raise RuntimeError(
            "OPENAI_API_KEY non configurata nelle variabili Railway."
        )

    listing = listing_context or await fetch_listing_context(url)
    image_urls = listing["image_urls"]

    if not image_urls:
        raise RuntimeError(
            "Nessuna immagine utile trovata nella pagina dell'annuncio."
        )

    prompt = (
        "Analizza questo annuncio di compravendita usato come perito prudente. "
        "Usa immagini, titolo e descrizione. Identifica marca, modello e variante "
        "solo quando visivamente o testualmente sostenibili. Non inventare dati "
        "interni non visibili, come salute reale della batteria o funzionamento. "
        "Segnala incongruenze tra testo e immagini, difetti visibili, accessori "
        "visibili e possibili segnali di contraffazione o truffa."
        f"\nTitolo: {listing['title']}"
        f"\nDescrizione: {listing['description']}"
        f"\nURL: {listing['url']}"
    )

    content: List[Dict[str, Any]] = [
        {"type": "input_text", "text": prompt}
    ]
    content.extend(
        {
            "type": "input_image",
            "image_url": image_url,
            "detail": "high",
        }
        for image_url in image_urls
    )

    body = {
        "model": VISION_MODEL,
        "input": [{"role": "user", "content": content}],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "radar_visual_analysis",
                "strict": True,
                "schema": VISION_SCHEMA,
            }
        },
        "max_output_tokens": 1200,
    }

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(
            OPENAI_RESPONSES_URL,
            headers=headers,
            json=body,
        )

    if response.status_code >= 400:
        raise RuntimeError(
            f"OpenAI API HTTP {response.status_code}: "
            f"{response.text[:800]}"
        )

    payload = response.json()
    result = json.loads(_extract_output_text(payload))

    result["listing_title"] = listing["title"]
    result["listing_description"] = listing["description"]
    result["listing_url"] = listing["url"]
    result["image_urls"] = list(image_urls)
    result["images_analyzed"] = len(image_urls)
    result["vision_model"] = VISION_MODEL

    return result
