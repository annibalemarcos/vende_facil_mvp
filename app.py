from __future__ import annotations

import hashlib
import html
import json
import mimetypes
import os
import re
import sqlite3
from datetime import datetime, date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
import secrets
from bs4 import BeautifulSoup

from flask import Flask, flash, g, jsonify, redirect, render_template, request, send_file, send_from_directory, session, url_for

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("VENDE_FACIL_DATA_DIR", str(BASE_DIR / "data"))).expanduser()
DB_PATH = DATA_DIR / "vende_facil.sqlite"
EXPORT_PATH = DATA_DIR / "vende_facil_export.json"
UPLOAD_DIR = DATA_DIR / "uploads"

app = Flask(__name__)
app.secret_key = os.environ.get("VENDE_FACIL_SECRET", "dev-secret-change-me")

LOGIN_EMAIL = os.environ.get("VENDE_FACIL_LOGIN_EMAIL", "admin@vendefacil.com")
LOGIN_PASSWORD = os.environ.get("VENDE_FACIL_LOGIN_PASSWORD", "Strongeta@1990")

STATUSES = ["rascunho", "anunciado", "reservado", "vendido", "parado"]
TEMPERATURES = ["🔥 quente", "😐 morno", "❄️ frio"]
PLATFORMS = ["OLX", "Mercado Livre", "Instagram", "WhatsApp", "Facebook", "Outro"]
PAYMENT_STATUSES = ["pendente", "recebido", "parcial"]
OLX_TIMEOUT_SECONDS = 12
MAX_IMPORT_IMAGE_BYTES = 8 * 1024 * 1024
OLX_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0 Safari/537.36 VendeFacilLocal/1.0"
)

THEMES = {
    "roxo": {"label": "Roxo elétrico", "primary": "#7c3aed", "secondary": "#ec4899", "soft": "#f1e9ff", "bg": "#faf7ff"},
    "verde": {"label": "Verde Pix", "primary": "#059669", "secondary": "#10b981", "soft": "#dcfce7", "bg": "#f7fff9"},
    "azul": {"label": "Azul vitrine", "primary": "#2563eb", "secondary": "#06b6d4", "soft": "#dbeafe", "bg": "#f7fbff"},
    "laranja": {"label": "Laranja feira", "primary": "#ea580c", "secondary": "#f59e0b", "soft": "#ffedd5", "bg": "#fffaf3"},
    "grafite": {"label": "Grafite limpo", "primary": "#111827", "secondary": "#475569", "soft": "#e5e7eb", "bg": "#f8fafc"},
}

DEFAULT_SETTINGS = {
    "app_name": "Vende Fácil",
    "brand_emoji": "🛒",
    "theme": "roxo",
    "default_city": "Campinas/SP",
    "match_threshold": "70",
    "show_match_alert": "1",
    "match_sound": "1",
    "fun_mode": "1",
}


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_: Exception | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def query(sql: str, args: tuple[Any, ...] = (), one: bool = False):
    cur = get_db().execute(sql, args)
    rows = cur.fetchall()
    cur.close()
    return (rows[0] if rows else None) if one else rows


def execute(sql: str, args: tuple[Any, ...] = ()) -> int:
    db = get_db()
    cur = db.execute(sql, args)
    db.commit()
    last_id = cur.lastrowid
    cur.close()
    return last_id


def parse_tags(value: str | None) -> list[str]:
    if not value:
        return []
    return [tag.strip().lower() for tag in value.replace(";", ",").split(",") if tag.strip()]


def money(value: Any) -> str:
    try:
        return f"R$ {float(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return "R$ 0,00"


app.jinja_env.filters["money"] = money


def html_to_text(value: str | None, keep_lines: bool = False) -> str:
    """Converte texto/HTML importado em texto limpo.

    A OLX às vezes coloca <br>, &nbsp; e até trechos HTML dentro dos metadados.
    O app deve guardar descrição como texto humano, não como sopa de tags.
    """
    if not value:
        return ""

    text = html.unescape(str(value))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", text)
    text = re.sub(r"(?i)</\s*(p|div|li|tr|h[1-6])\s*>", "\n", text)
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = text.replace("\xa0", " ")

    if keep_lines:
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
        cleaned: list[str] = []
        blank = False
        for line in lines:
            if line:
                cleaned.append(line)
                blank = False
            elif cleaned and not blank:
                cleaned.append("")
                blank = True
        while cleaned and cleaned[-1] == "":
            cleaned.pop()
        return "\n".join(cleaned).strip()

    return re.sub(r"\s+", " ", text).strip()


def compact_spaces(value: str | None) -> str:
    return html_to_text(value, keep_lines=False)


def clean_description(value: str | None) -> str:
    return html_to_text(value, keep_lines=True)


def meta_content(soup: BeautifulSoup, *names: str, clean: bool = True) -> str:
    """Busca conteúdo em og:, twitter: e meta name comuns."""
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            content = html.unescape(str(tag.get("content") or "")).strip()
            return compact_spaces(content) if clean else content
    return ""


def first_jsonld_objects(soup: BeautifulSoup) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            objects.append(value)
            graph = value.get("@graph")
            if isinstance(graph, list):
                for item in graph:
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text(" ", strip=True)
        if not raw:
            continue
        try:
            collect(json.loads(raw))
        except Exception:
            continue
    return objects


def deep_values(value: Any, keys: set[str], limit: int = 40) -> list[Any]:
    found: list[Any] = []

    def walk(node: Any) -> None:
        if len(found) >= limit:
            return
        if isinstance(node, dict):
            for key, child in node.items():
                normalized = str(key).lower().replace("_", "-")
                if normalized in keys and child not in (None, "", [], {}):
                    found.append(child)
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return found


def page_json_objects(soup: BeautifulSoup) -> list[Any]:
    objects: list[Any] = []
    for script in soup.find_all("script"):
        raw = script.string or script.get_text(" ", strip=True)
        if not raw or len(raw) < 30:
            continue
        stripped = raw.strip()
        candidates = []
        if stripped.startswith("{") or stripped.startswith("["):
            candidates.append(stripped)
        # Fallback para scripts do tipo window.__STATE__ = {...};
        match = re.search(r"=\s*(\{.*\})\s*;?\s*$", stripped, flags=re.DOTALL)
        if match:
            candidates.append(match.group(1))
        for candidate in candidates[:2]:
            try:
                objects.append(json.loads(candidate))
                break
            except Exception:
                continue
    return objects


def first_text_value(values: list[Any], min_len: int = 2, keep_lines: bool = False) -> str:
    for value in values:
        if isinstance(value, str):
            text = html_to_text(value, keep_lines=keep_lines)
            if len(text) >= min_len and not text.startswith("http"):
                return text
    return ""


def first_image_value(values: list[Any]) -> str:
    for value in values:
        if isinstance(value, str) and value.startswith("http"):
            return value
        if isinstance(value, list):
            nested = first_image_value(value)
            if nested:
                return nested
        if isinstance(value, dict):
            nested = first_image_value(list(value.values()))
            if nested:
                return nested
    return ""


def image_extension(content_type: str, image_url: str) -> str:
    content_type = (content_type or "").split(";", 1)[0].strip().lower()
    ext = mimetypes.guess_extension(content_type) or Path(urlparse(image_url).path).suffix
    if ext == ".jpe":
        ext = ".jpg"
    if ext.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        ext = ".jpg"
    return ext.lower()


def download_import_image(image_url: str, source_url: str = "") -> tuple[str, str | None]:
    """Baixa a capa do anúncio para a pasta persistente e devolve URL local.

    Retorna (url_local_ou_original, aviso). Se não der para baixar, mantém a URL original
    para não quebrar o cadastro.
    """
    image_url = (image_url or "").strip()
    if not image_url:
        return "", None
    if not image_url.startswith(("http://", "https://")):
        return image_url, "A imagem encontrada não era uma URL pública válida."

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    headers = {
        "User-Agent": OLX_USER_AGENT,
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Referer": source_url or "https://www.olx.com.br/",
    }

    try:
        with requests.get(image_url, headers=headers, timeout=OLX_TIMEOUT_SECONDS, stream=True) as response:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "")
            if "image" not in content_type.lower():
                return image_url, "Encontrei URL de imagem, mas o servidor não respondeu como imagem."

            length = response.headers.get("Content-Length")
            if length and int(length) > MAX_IMPORT_IMAGE_BYTES:
                return image_url, "A imagem de capa era grande demais para importar; mantive a URL original."

            ext = image_extension(content_type, image_url)
            digest = hashlib.sha256(f"{source_url}|{image_url}".encode("utf-8")).hexdigest()[:18]
            filename = f"olx_capa_{digest}{ext}"
            path = UPLOAD_DIR / filename

            if not path.exists():
                total = 0
                with path.open("wb") as f:
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > MAX_IMPORT_IMAGE_BYTES:
                            f.close()
                            path.unlink(missing_ok=True)
                            return image_url, "A imagem de capa passou do limite de tamanho; mantive a URL original."
                        f.write(chunk)

            return f"/uploads/{filename}", None
    except Exception:
        return image_url, "Não consegui baixar a capa; mantive a URL original."


def clean_olx_title(value: str) -> str:
    value = compact_spaces(value)
    value = re.sub(r"\s*[|\-–—]\s*OLX.*$", "", value, flags=re.IGNORECASE).strip()
    return value[:180]


def parse_brl_price(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = compact_spaces(str(value))
    if not text:
        return 0.0
    # Exemplos: R$ 1.250,00 | 1250 | 1,250.00
    match = re.search(r"(\d[\d\.\,]*)", text.replace(" ", ""))
    if not match:
        return 0.0
    number = match.group(1)
    if "," in number and "." in number:
        number = number.replace(".", "").replace(",", ".")
    elif "," in number:
        number = number.replace(",", ".")
    else:
        # Se vier 1.250 sem centavos, trata ponto como milhar.
        parts = number.split(".")
        if len(parts) > 1 and len(parts[-1]) == 3:
            number = "".join(parts)
    try:
        return float(number)
    except ValueError:
        return 0.0


def is_olx_url(url: str) -> bool:
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False
    host = (parsed.netloc or "").lower()
    return parsed.scheme in {"http", "https"} and (host == "olx.com.br" or host.endswith(".olx.com.br"))


def category_from_olx_url(url: str) -> str:
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    # URLs comuns: /grande-campinas/eletro/geladeiras-e-freezers/slug-id
    if len(parts) >= 3:
        candidate = parts[-2]
    elif len(parts) >= 2:
        candidate = parts[-1]
    else:
        return ""
    candidate = re.sub(r"-\d+$", "", candidate)
    return candidate.replace("-", " ").strip().title()


def tags_from_import(title: str, category: str, url: str) -> str:
    base = f"{title} {category}"
    tokens = re.findall(r"[A-Za-zÀ-ÿ0-9]{3,}", base.lower())
    stop = {"para", "com", "sem", "uma", "uns", "das", "dos", "por", "que", "olx", "r$", "funcionando", "produto"}
    useful: list[str] = []
    for token in tokens:
        token = token.strip("-_")
        if token not in stop and token not in useful:
            useful.append(token)
        if len(useful) >= 8:
            break
    if "olx" not in useful:
        useful.append("olx")
    return ", ".join(useful)


def fetch_olx_listing(url: str) -> dict[str, Any]:
    url = url.strip()
    if not is_olx_url(url):
        raise ValueError("Cole um link válido da OLX, tipo https://sp.olx.com.br/...")

    headers = {
        "User-Agent": OLX_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.7",
    }
    response = requests.get(url, headers=headers, timeout=OLX_TIMEOUT_SECONDS, allow_redirects=True)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    jsonld = first_jsonld_objects(soup)

    title = clean_olx_title(meta_content(soup, "og:title", "twitter:title"))
    description = clean_description(meta_content(soup, "og:description", "description", "twitter:description", clean=False))
    image_url = meta_content(soup, "og:image", "twitter:image")
    price = parse_brl_price(meta_content(soup, "product:price:amount", "og:price:amount"))

    for obj in jsonld:
        obj_type = obj.get("@type")
        if isinstance(obj_type, list):
            obj_type = " ".join(str(x) for x in obj_type)
        obj_type = str(obj_type or "").lower()
        if not title and obj.get("name"):
            title = clean_olx_title(obj.get("name"))
        if not description and obj.get("description"):
            description = clean_description(obj.get("description"))
        if not image_url and obj.get("image"):
            image = obj.get("image")
            if isinstance(image, list):
                image_url = str(image[0]) if image else ""
            else:
                image_url = str(image)
        offers = obj.get("offers") if isinstance(obj, dict) else None
        if not price and isinstance(offers, dict):
            price = parse_brl_price(offers.get("price") or offers.get("lowPrice"))
        if title and price:
            break

    if not (title and price and description and image_url):
        for obj in page_json_objects(soup):
            if not title:
                title = clean_olx_title(first_text_value(deep_values(obj, {"subject", "title", "name"})))
            if not description:
                description = first_text_value(deep_values(obj, {"description", "body", "details"}), min_len=8, keep_lines=True)
            if not price:
                price = parse_brl_price(first_text_value(deep_values(obj, {"price", "value", "amount"})))
            if not image_url:
                image_url = first_image_value(deep_values(obj, {"image", "images", "picture", "pictures", "photo", "photos", "url"}))
            if title and price and description and image_url:
                break

    if not title and soup.title and soup.title.string:
        title = clean_olx_title(soup.title.string)

    final_url = response.url or url
    category = category_from_olx_url(final_url)
    tags = tags_from_import(title, category, final_url)

    local_image_url, image_warning = download_import_image(image_url, final_url)
    if local_image_url:
        image_url = local_image_url

    warnings: list[str] = []
    if image_warning:
        warnings.append(image_warning)
    if not title:
        warnings.append("Não consegui identificar o título automaticamente.")
    if not price:
        warnings.append("Não consegui identificar o preço automaticamente.")
    if not description:
        warnings.append("A descrição veio vazia ou protegida pela página.")
    if not image_url:
        warnings.append("Não encontrei imagem principal nos metadados.")

    return {
        "title": title,
        "description": description,
        "category": category,
        "price": price,
        "min_price": 0,
        "quantity": 1,
        "status": "anunciado",
        "temperature": "😐 morno",
        "tags": tags,
        "image_url": image_url,
        "source_url": final_url,
        "source_platform": "OLX",
        "warnings": warnings,
    }


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                category TEXT DEFAULT '',
                price REAL DEFAULT 0,
                min_price REAL DEFAULT 0,
                quantity INTEGER DEFAULT 1,
                status TEXT DEFAULT 'rascunho',
                temperature TEXT DEFAULT '😐 morno',
                tags TEXT DEFAULT '',
                image_url TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                whatsapp TEXT DEFAULT '',
                city TEXT DEFAULT '',
                budget REAL DEFAULT 0,
                tags TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                score INTEGER DEFAULT 50,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS listings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                platform TEXT NOT NULL,
                url TEXT DEFAULT '',
                status TEXT DEFAULT 'ativo',
                views INTEGER DEFAULT 0,
                messages INTEGER DEFAULT 0,
                clicks INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS sales (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                lead_id INTEGER,
                sale_price REAL DEFAULT 0,
                payment_status TEXT DEFAULT 'pendente',
                platform TEXT DEFAULT '',
                sold_at TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE,
                FOREIGN KEY(lead_id) REFERENCES leads(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        for key, value in DEFAULT_SETTINGS.items():
            db.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))
        db.commit()


def normalize_bool(value: str | None) -> str:
    return "1" if value in {"1", "true", "on", "yes", "sim"} else "0"


def get_settings() -> dict[str, Any]:
    rows = query("SELECT key, value FROM settings")
    settings = DEFAULT_SETTINGS.copy()
    settings.update({row["key"]: row["value"] for row in rows})

    if settings.get("theme") not in THEMES:
        settings["theme"] = DEFAULT_SETTINGS["theme"]

    try:
        threshold = int(settings.get("match_threshold", DEFAULT_SETTINGS["match_threshold"]))
    except ValueError:
        threshold = int(DEFAULT_SETTINGS["match_threshold"])
    settings["match_threshold"] = str(max(25, min(95, threshold)))

    settings["show_match_alert"] = normalize_bool(settings.get("show_match_alert"))
    settings["match_sound"] = normalize_bool(settings.get("match_sound"))
    settings["fun_mode"] = normalize_bool(settings.get("fun_mode"))
    settings["theme_data"] = THEMES[settings["theme"]]
    settings["themes"] = THEMES
    return settings


def save_settings(values: dict[str, str]) -> None:
    allowed = set(DEFAULT_SETTINGS)
    for key, value in values.items():
        if key not in allowed:
            continue
        execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def tag_usage(limit: int = 80) -> list[dict[str, Any]]:
    """Tags usadas nos produtos/leads, ordenadas por frequência.

    É o auto-preenchimento do jeito honesto: o app aprende com o uso real.
    Se você nunca usou uma tag, ela não aparece como sugestão ainda.
    """
    counts: dict[str, int] = {}
    for row in query("SELECT tags FROM products UNION ALL SELECT tags FROM leads"):
        for tag in parse_tags(row["tags"]):
            counts[tag] = counts.get(tag, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    return [{"tag": tag, "count": count} for tag, count in ranked]


def hot_match_summary() -> dict[str, Any]:
    settings = get_settings()
    if settings["show_match_alert"] != "1":
        return {"count": 0, "best": None, "threshold": int(settings["match_threshold"])}
    threshold = int(settings["match_threshold"])
    matches = [m for m in all_matches() if m["score"] >= threshold]
    best = matches[0] if matches else None
    return {"count": len(matches), "best": best, "threshold": threshold}


def match_product_lead(product: sqlite3.Row, lead: sqlite3.Row) -> dict[str, Any]:
    p_tags = set(parse_tags(product["tags"]) + parse_tags(product["category"]))
    l_tags = set(parse_tags(lead["tags"]) + parse_tags(lead["notes"]))
    common = sorted(p_tags.intersection(l_tags))
    score = 0
    reasons: list[str] = []

    if common:
        score += min(45, len(common) * 15)
        reasons.append(f"tags em comum: {', '.join(common[:5])}")

    budget = float(lead["budget"] or 0)
    price = float(product["price"] or 0)
    if budget and price:
        if price <= budget:
            score += 25
            reasons.append("cabe no orçamento")
        elif price <= budget * 1.15:
            score += 10
            reasons.append("passa um pouco do orçamento, mas dá negociação")
        else:
            score -= 15
            reasons.append("acima do orçamento")

    if (product["temperature"] or "").startswith("🔥"):
        score += 8
        reasons.append("produto marcado como quente")
    if (lead["score"] or 0) >= 80:
        score += 12
        reasons.append("lead com boa pontuação")
    if (product["status"] or "") in {"anunciado", "rascunho"}:
        score += 5
        reasons.append("produto disponível para abordagem")

    score = max(0, min(100, score))
    label = "fraco"
    emoji = "🧊"
    if score >= 85:
        label = "chama agora"
        emoji = "🚀"
    elif score >= 70:
        label = "bom lead"
        emoji = "🔥"
    elif score >= 40:
        label = "talvez valha mandar"
        emoji = "👀"

    return {
        "product": product,
        "lead": lead,
        "score": score,
        "label": label,
        "emoji": emoji,
        "reasons": reasons or ["poucos sinais ainda; precisa de mais tags/histórico"],
    }


def all_matches(product_id: int | None = None) -> list[dict[str, Any]]:
    if product_id:
        products = query("SELECT * FROM products WHERE id = ?", (product_id,))
    else:
        products = query("SELECT * FROM products WHERE status != 'vendido' ORDER BY created_at DESC")
    leads = query("SELECT * FROM leads ORDER BY score DESC, created_at DESC")
    matches: list[dict[str, Any]] = []
    for product in products:
        for lead in leads:
            match = match_product_lead(product, lead)
            if match["score"] >= 25:
                matches.append(match)
    return sorted(matches, key=lambda x: x["score"], reverse=True)


@app.context_processor
def inject_globals():
    return {
        "statuses": STATUSES,
        "temperatures": TEMPERATURES,
        "platforms": PLATFORMS,
        "payment_statuses": PAYMENT_STATUSES,
        "today": date.today().isoformat(),
        "tag_suggestions": tag_usage(),
        "hot_match_alert": hot_match_summary(),
        "app_settings": get_settings(),
        "themes": THEMES,
    }




def safe_next_url(value: str | None) -> str:
    """Evita redirecionamento externo depois do login."""
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return url_for("dashboard")


@app.before_request
def require_login():
    endpoint = request.endpoint or ""
    public_endpoints = {"login", "static", "health"}
    if endpoint in public_endpoints or endpoint.startswith("static"):
        return None
    if not session.get("logged_in"):
        next_url = request.full_path if request.query_string else request.path
        return redirect(url_for("login", next=next_url))
    return None


@app.route("/health")
def health():
    return {"status": "ok", "app": "vende_facil"}


@app.route("/uploads/<path:filename>")
def uploaded_file(filename: str):
    return send_from_directory(UPLOAD_DIR, filename)


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("logged_in") and request.method == "GET":
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        valid_email = secrets.compare_digest(email, LOGIN_EMAIL.lower())
        valid_password = secrets.compare_digest(password, LOGIN_PASSWORD)

        if valid_email and valid_password:
            session.clear()
            session["logged_in"] = True
            session["user_email"] = LOGIN_EMAIL
            flash("Login feito. Pode entrar no balcão. 🔐", "success")
            return redirect(safe_next_url(request.args.get("next")))

        flash("Login inválido. Eita: ou o e-mail ou a senha não bateram.", "danger")

    return render_template("login.html", title="Entrar")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("Você saiu do app. Porta fechada, balcão protegido. 🔒", "info")
    return redirect(url_for("login"))


@app.route("/")
def dashboard():
    stats = {
        "products": query("SELECT COUNT(*) AS c FROM products", one=True)["c"],
        "active": query("SELECT COUNT(*) AS c FROM products WHERE status = 'anunciado'", one=True)["c"],
        "sold": query("SELECT COUNT(*) AS c FROM products WHERE status = 'vendido'", one=True)["c"],
        "leads": query("SELECT COUNT(*) AS c FROM leads", one=True)["c"],
        "revenue": query("SELECT COALESCE(SUM(sale_price), 0) AS total FROM sales WHERE payment_status = 'recebido'", one=True)["total"],
        "receivable": query("SELECT COALESCE(SUM(sale_price), 0) AS total FROM sales WHERE payment_status != 'recebido'", one=True)["total"],
    }
    hot_products = query("SELECT * FROM products WHERE temperature LIKE '🔥%' ORDER BY created_at DESC LIMIT 5")
    cold_products = query("SELECT * FROM products WHERE temperature LIKE '❄️%' OR status = 'parado' ORDER BY created_at DESC LIMIT 5")
    recent_sales = query(
        """
        SELECT sales.*, products.title AS product_title, leads.name AS lead_name
        FROM sales
        JOIN products ON products.id = sales.product_id
        LEFT JOIN leads ON leads.id = sales.lead_id
        ORDER BY sales.created_at DESC LIMIT 5
        """
    )
    top_matches = all_matches()[:5]
    return render_template("dashboard.html", stats=stats, hot_products=hot_products, cold_products=cold_products, recent_sales=recent_sales, top_matches=top_matches)


@app.route("/products")
def products():
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    sql = "SELECT * FROM products WHERE 1=1"
    args: list[Any] = []
    if q:
        sql += " AND (title LIKE ? OR description LIKE ? OR tags LIKE ? OR category LIKE ?)"
        like = f"%{q}%"
        args.extend([like, like, like, like])
    if status:
        sql += " AND status = ?"
        args.append(status)
    sql += " ORDER BY created_at DESC"
    rows = query(sql, tuple(args))
    return render_template("products.html", products=rows, q=q, status=status)


@app.route("/products/new", methods=["GET", "POST"])
def product_new():
    imported = None
    import_url = ""

    if request.method == "POST" and request.form.get("_action") == "import_olx":
        import_url = request.form.get("import_olx_url", "").strip()
        try:
            imported = fetch_olx_listing(import_url)
            flash("Dados da OLX puxados para o cadastro. Agora revise e salve — piloto automático com freio de mão. 🧲", "success")
        except requests.Timeout:
            flash("A OLX demorou para responder. Tente novamente ou preencha manualmente.", "warning")
        except requests.HTTPError as exc:
            flash(f"Não consegui ler esse anúncio da OLX. HTTP {exc.response.status_code}. Pode ser bloqueio, anúncio expirado ou página protegida.", "warning")
        except requests.RequestException:
            flash("Não consegui acessar a OLX agora. Confira sua internet e tente de novo.", "warning")
        except ValueError as exc:
            flash(str(exc), "warning")
        except Exception:
            flash("Não consegui importar esse link. A OLX às vezes troca a fechadura da vitrine.", "warning")
        return render_template("product_form.html", product=None, imported=imported, import_url=import_url)

    if request.method == "POST":
        product_id = execute(
            """
            INSERT INTO products(title, description, category, price, min_price, quantity, status, temperature, tags, image_url, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request.form["title"].strip(),
                clean_description(request.form.get("description", "")),
                request.form.get("category", "").strip(),
                float(request.form.get("price") or 0),
                float(request.form.get("min_price") or 0),
                int(request.form.get("quantity") or 1),
                request.form.get("status", "rascunho"),
                request.form.get("temperature", "😐 morno"),
                request.form.get("tags", "").strip(),
                request.form.get("image_url", "").strip(),
                now_iso(),
            ),
        )
        source_url = request.form.get("source_url", "").strip()
        source_platform = request.form.get("source_platform", "").strip() or "OLX"
        if source_url:
            execute(
                """
                INSERT INTO listings(product_id, platform, url, status, views, messages, clicks, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (product_id, source_platform, source_url, "ativo", 0, 0, 0, now_iso()),
            )
            flash("Produto importado e link da OLX salvo. Meio caminho andado, sem copiar tudo na unha. 🧲", "success")
        else:
            flash("Produto cadastrado. Mais um item na prateleira digital. 🧺", "success")
        return redirect(url_for("products"))
    return render_template("product_form.html", product=None, imported=None, import_url="")


@app.route("/import/olx", methods=["GET", "POST"])
def olx_import():
    imported = None
    url = request.form.get("url", "").strip() if request.method == "POST" else request.args.get("url", "").strip()
    if request.method == "POST":
        try:
            imported = fetch_olx_listing(url)
            flash("Importação OLX feita. Revise antes de salvar — robô ajuda, mas não assina contrato. 🕵️", "success")
        except requests.Timeout:
            flash("A OLX demorou para responder. Tente novamente ou cadastre manualmente.", "warning")
        except requests.HTTPError as exc:
            flash(f"Não consegui ler esse anúncio da OLX. HTTP {exc.response.status_code}. Pode ser bloqueio, anúncio expirado ou página protegida.", "warning")
        except requests.RequestException:
            flash("Não consegui acessar a OLX agora. Confira sua internet e tente de novo.", "warning")
        except ValueError as exc:
            flash(str(exc), "warning")
        except Exception:
            flash("Não consegui importar esse link. A OLX às vezes muda a vitrine de lugar; vida de scraper é faroeste.", "warning")
    return render_template("olx_import.html", url=url, imported=imported)


@app.route("/products/<int:product_id>/edit", methods=["GET", "POST"])
def product_edit(product_id: int):
    product = query("SELECT * FROM products WHERE id = ?", (product_id,), one=True)
    if not product:
        flash("Produto não encontrado.", "warning")
        return redirect(url_for("products"))
    if request.method == "POST":
        execute(
            """
            UPDATE products SET title=?, description=?, category=?, price=?, min_price=?, quantity=?, status=?, temperature=?, tags=?, image_url=?
            WHERE id=?
            """,
            (
                request.form["title"].strip(),
                clean_description(request.form.get("description", "")),
                request.form.get("category", "").strip(),
                float(request.form.get("price") or 0),
                float(request.form.get("min_price") or 0),
                int(request.form.get("quantity") or 1),
                request.form.get("status", "rascunho"),
                request.form.get("temperature", "😐 morno"),
                request.form.get("tags", "").strip(),
                request.form.get("image_url", "").strip(),
                product_id,
            ),
        )
        flash("Produto atualizado. Tá ficando bonito esse balcão. ✨", "success")
        return redirect(url_for("products"))
    return render_template("product_form.html", product=product)


@app.route("/products/<int:product_id>/delete", methods=["POST"])
def product_delete(product_id: int):
    execute("DELETE FROM products WHERE id = ?", (product_id,))
    flash("Produto removido.", "info")
    return redirect(url_for("products"))


@app.route("/products/<int:product_id>/suggest")
def product_suggest(product_id: int):
    product = query("SELECT * FROM products WHERE id = ?", (product_id,), one=True)
    if not product:
        flash("Produto não encontrado.", "warning")
        return redirect(url_for("products"))
    title = product["title"].strip()
    category = product["category"].strip() or "produto"
    price = money(product["price"])
    desc = product["description"].strip()
    city = get_settings().get("default_city") or "sua região"
    suggested = f"{title} - funcionando / pronto para retirada\n\n{desc}\n\nPreço: {price}. Produto em {city}. Posso enviar mais fotos e vídeo pelo chat. Pode chamar sem novela: se estiver anunciado, ainda está disponível."
    return render_template("suggestion.html", product=product, suggested=suggested)


@app.route("/leads")
def leads():
    q = request.args.get("q", "").strip()
    sql = "SELECT * FROM leads WHERE 1=1"
    args: list[Any] = []
    if q:
        like = f"%{q}%"
        sql += " AND (name LIKE ? OR city LIKE ? OR tags LIKE ? OR notes LIKE ?)"
        args.extend([like, like, like, like])
    sql += " ORDER BY score DESC, created_at DESC"
    rows = query(sql, tuple(args))
    return render_template("leads.html", leads=rows, q=q)


@app.route("/leads/new", methods=["GET", "POST"])
def lead_new():
    if request.method == "POST":
        execute(
            """
            INSERT INTO leads(name, whatsapp, city, budget, tags, notes, score, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request.form["name"].strip(),
                request.form.get("whatsapp", "").strip(),
                request.form.get("city", "").strip(),
                float(request.form.get("budget") or 0),
                request.form.get("tags", "").strip(),
                clean_description(request.form.get("notes", "")),
                int(request.form.get("score") or 50),
                now_iso(),
            ),
        )
        flash("Lead salvo. Mais uma pessoa no radar. 📡", "success")
        return redirect(url_for("leads"))
    return render_template("lead_form.html", lead=None)


@app.route("/leads/<int:lead_id>/edit", methods=["GET", "POST"])
def lead_edit(lead_id: int):
    lead = query("SELECT * FROM leads WHERE id = ?", (lead_id,), one=True)
    if not lead:
        flash("Lead não encontrado.", "warning")
        return redirect(url_for("leads"))
    if request.method == "POST":
        execute(
            """
            UPDATE leads SET name=?, whatsapp=?, city=?, budget=?, tags=?, notes=?, score=? WHERE id=?
            """,
            (
                request.form["name"].strip(),
                request.form.get("whatsapp", "").strip(),
                request.form.get("city", "").strip(),
                float(request.form.get("budget") or 0),
                request.form.get("tags", "").strip(),
                clean_description(request.form.get("notes", "")),
                int(request.form.get("score") or 50),
                lead_id,
            ),
        )
        flash("Lead atualizado. CRM sem gravata, do jeito certo. 😎", "success")
        return redirect(url_for("leads"))
    return render_template("lead_form.html", lead=lead)


@app.route("/leads/<int:lead_id>/delete", methods=["POST"])
def lead_delete(lead_id: int):
    execute("DELETE FROM leads WHERE id = ?", (lead_id,))
    flash("Lead removido.", "info")
    return redirect(url_for("leads"))


@app.route("/listings", methods=["GET", "POST"])
def listings():
    if request.method == "POST":
        execute(
            """
            INSERT INTO listings(product_id, platform, url, status, views, messages, clicks, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(request.form["product_id"]),
                request.form["platform"],
                request.form.get("url", "").strip(),
                request.form.get("status", "ativo"),
                int(request.form.get("views") or 0),
                int(request.form.get("messages") or 0),
                int(request.form.get("clicks") or 0),
                now_iso(),
            ),
        )
        flash("Anúncio/link salvo. Agora ninguém se perde no matagal das plataformas. 🧭", "success")
        return redirect(url_for("listings"))
    products = query("SELECT id, title FROM products ORDER BY title")
    rows = query(
        """
        SELECT listings.*, products.title AS product_title
        FROM listings
        JOIN products ON products.id = listings.product_id
        ORDER BY listings.created_at DESC
        """
    )
    return render_template("listings.html", listings=rows, products=products)


@app.route("/listings/<int:listing_id>/delete", methods=["POST"])
def listing_delete(listing_id: int):
    execute("DELETE FROM listings WHERE id = ?", (listing_id,))
    flash("Anúncio removido.", "info")
    return redirect(url_for("listings"))


@app.route("/sales", methods=["GET", "POST"])
def sales():
    if request.method == "POST":
        product_id = int(request.form["product_id"])
        execute(
            """
            INSERT INTO sales(product_id, lead_id, sale_price, payment_status, platform, sold_at, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                product_id,
                int(request.form["lead_id"]) if request.form.get("lead_id") else None,
                float(request.form.get("sale_price") or 0),
                request.form.get("payment_status", "pendente"),
                request.form.get("platform", ""),
                request.form.get("sold_at") or date.today().isoformat(),
                clean_description(request.form.get("notes", "")),
                now_iso(),
            ),
        )
        execute("UPDATE products SET status='vendido', quantity=MAX(quantity - 1, 0) WHERE id=?", (product_id,))
        flash("Venda registrada. Pix mental recebido com sucesso. 💸", "success")
        return redirect(url_for("sales"))
    rows = query(
        """
        SELECT sales.*, products.title AS product_title, leads.name AS lead_name
        FROM sales
        JOIN products ON products.id = sales.product_id
        LEFT JOIN leads ON leads.id = sales.lead_id
        ORDER BY sold_at DESC, sales.created_at DESC
        """
    )
    products = query("SELECT id, title, price FROM products ORDER BY title")
    leads_rows = query("SELECT id, name FROM leads ORDER BY name")
    return render_template("sales.html", sales=rows, products=products, leads=leads_rows)


@app.route("/sales/<int:sale_id>/delete", methods=["POST"])
def sale_delete(sale_id: int):
    execute("DELETE FROM sales WHERE id = ?", (sale_id,))
    flash("Venda removida.", "info")
    return redirect(url_for("sales"))


@app.route("/matches")
def matches():
    product_id = request.args.get("product_id", type=int)
    products = query("SELECT id, title FROM products WHERE status != 'vendido' ORDER BY title")
    rows = all_matches(product_id)[:80]
    return render_template("matches.html", matches=rows, products=products, product_id=product_id)


@app.route("/calculator", methods=["GET", "POST"])
def calculator():
    result = None
    if request.method == "POST":
        cost = float(request.form.get("cost") or 0)
        desired_profit = float(request.form.get("desired_profit") or 0)
        fee_percent = float(request.form.get("fee_percent") or 0)
        shipping = float(request.form.get("shipping") or 0)
        discount = float(request.form.get("discount") or 0)
        recommended = (cost + desired_profit + shipping + discount) / max(0.01, (1 - fee_percent / 100))
        minimum = (cost + shipping) / max(0.01, (1 - fee_percent / 100))
        result = {
            "recommended": recommended,
            "minimum": minimum,
            "profit_at_recommended": recommended - (recommended * fee_percent / 100) - cost - shipping,
            "warning": "Se vender abaixo do mínimo, você está pagando para trabalhar. Aí é poesia triste.",
        }
    return render_template("calculator.html", result=result)


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        theme = request.form.get("theme", DEFAULT_SETTINGS["theme"])
        if theme not in THEMES:
            theme = DEFAULT_SETTINGS["theme"]

        try:
            threshold = int(request.form.get("match_threshold") or DEFAULT_SETTINGS["match_threshold"])
        except ValueError:
            threshold = int(DEFAULT_SETTINGS["match_threshold"])
        threshold = max(25, min(95, threshold))

        values = {
            "app_name": request.form.get("app_name", DEFAULT_SETTINGS["app_name"]).strip() or DEFAULT_SETTINGS["app_name"],
            "brand_emoji": request.form.get("brand_emoji", DEFAULT_SETTINGS["brand_emoji"]).strip() or DEFAULT_SETTINGS["brand_emoji"],
            "theme": theme,
            "default_city": request.form.get("default_city", DEFAULT_SETTINGS["default_city"]).strip() or DEFAULT_SETTINGS["default_city"],
            "match_threshold": str(threshold),
            "show_match_alert": "1" if request.form.get("show_match_alert") else "0",
            "match_sound": "1" if request.form.get("match_sound") else "0",
            "fun_mode": "1" if request.form.get("fun_mode") else "0",
        }
        save_settings(values)
        flash("Configurações salvas. O app vestiu a roupa nova sem reclamar. ⚙️", "success")
        return redirect(url_for("settings"))
    return render_template("settings.html")


@app.route("/export")
def export_data():
    data = {}
    for table in ["products", "leads", "listings", "sales"]:
        data[table] = [dict(row) for row in query(f"SELECT * FROM {table}")]
    EXPORT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return send_file(EXPORT_PATH, as_attachment=True, download_name="vende_facil_export.json")


@app.route("/api/tag-suggestions")
def api_tag_suggestions():
    return jsonify(tag_usage())


@app.route("/about")
def about():
    return render_template("about.html")


with app.app_context():
    init_db()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5433))
    app.run(host="127.0.0.1", port=port, debug=True)
