"""
Scraper diario de urgente.bo (sección coyuntura) -> Supabase.

Convertido desde urgente_scrap.ipynb para correr de forma desatendida
(sin Google Drive, sin Colab, sin celdas de visualización).

Variables de entorno requeridas (se configuran como GitHub Secrets):
    SUPABASE_URL
    SUPABASE_KEY
"""

import os
import re
import sys
import datetime
import pandas as pd
import requests
from bs4 import BeautifulSoup
from supabase import create_client, Client

# --------------------------------------------------------------------------
# Configuración
# --------------------------------------------------------------------------
BASE = "https://www.urgente.bo"
SECTIONS = ["coyuntura"]

# Para el job diario basta con revisar las primeras páginas del listado
# (las noticias más recientes). El backfill histórico completo (59 páginas)
# se hizo una sola vez manualmente en el notebook original.
PAGINAS_A_REVISAR = int(os.environ.get("PAGINAS_A_REVISAR", "3"))

MONTH_MAP = {
    "Enero": "January", "Febrero": "February", "Marzo": "March",
    "Abril": "April", "Mayo": "May", "Junio": "June",
    "Julio": "July", "Agosto": "August", "Septiembre": "September",
    "Octubre": "October", "Noviembre": "November", "Diciembre": "December",
}

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


# --------------------------------------------------------------------------
# Scraping del listado (título + url de cada artículo)
# --------------------------------------------------------------------------
def extract_articles(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("div.views-row")

    articles = []
    for row in rows:
        a_tag = row.select_one(".views-field-title a")
        if not a_tag:
            continue
        title = a_tag.get_text(strip=True)
        url = BASE + a_tag["href"]
        articles.append({"title": title, "url": url})

    return articles


def build_listing_urls() -> list[str]:
    urls = []
    for section in SECTIONS:
        for page in range(1, PAGINAS_A_REVISAR + 1):
            if page == 1:
                urls.append(f"{BASE}/{section}")
            else:
                urls.append(f"{BASE}/{section}?page={page - 1}")
    return urls


# --------------------------------------------------------------------------
# Scraping del contenido completo de cada artículo
# --------------------------------------------------------------------------
def clean_text(text: str) -> str:
    text = text.replace("\n", " ").replace("\r", " ").replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_article(url: str) -> dict | None:
    try:
        html = requests.get(url, headers=HEADERS, timeout=10).text
    except Exception as e:
        print(f"  ⚠️  Error obteniendo {url}: {e}")
        return None

    soup = BeautifulSoup(html, "html.parser")

    title_tag = soup.select_one("h2.noticias-h2")
    if title_tag:
        title = title_tag.get_text(strip=True)
    else:
        meta_title = soup.select_one('span[property="dc:title"]')
        title = meta_title.get("content", "").strip() if meta_title else None

    date_tag = soup.select_one("p.news-by")
    if date_tag:
        datetime_raw = date_tag.get_text(strip=True)
    else:
        body_container = soup.select_one('div[property="content:encoded"]')
        first_p = body_container.find("p") if body_container else None
        strong_tag = first_p.find("strong") if first_p else None
        datetime_raw = strong_tag.get_text(strip=True) if strong_tag else None

    body_container = soup.select_one('div[property="content:encoded"]')
    paragraphs = []
    if body_container:
        for p in body_container.find_all("p"):
            text = clean_text(p.get_text(strip=True))
            if text:
                paragraphs.append(text)
    body_text = " ".join(paragraphs)

    return {
        "url": url,
        "headline": title,
        "datetime_raw": datetime_raw,
        "body_text": body_text,
    }


# --------------------------------------------------------------------------
# Pipeline principal
# --------------------------------------------------------------------------
def main():
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_KEY")
    if not supabase_url or not supabase_key:
        print("❌ Faltan las variables de entorno SUPABASE_URL / SUPABASE_KEY.")
        sys.exit(1)

    supabase: Client = create_client(supabase_url, supabase_key)

    # 1. URLs ya existentes en la base (para no duplicar)
    print("Consultando artículos existentes en Supabase...")
    response = supabase.table("urgente_articles").select("url").range(0, 9999).execute()
    existing_urls = {row["url"] for row in response.data}
    print(f"  {len(existing_urls)} URLs ya existentes.")

    # 2. Scraping del listado
    print(f"Escaneando las primeras {PAGINAS_A_REVISAR} páginas de cada sección...")
    all_articles = []
    for url in build_listing_urls():
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
        except Exception as e:
            print(f"  ⚠️  Error obteniendo listado {url}: {e}")
            continue
        articles = extract_articles(resp.text)
        print(f"  {url} -> {len(articles)} artículos (status {resp.status_code})")
        all_articles.extend(articles)

    # Deduplicar por URL dentro del propio scraping
    seen = set()
    unique_articles = []
    for a in all_articles:
        if a["url"] not in seen:
            unique_articles.append(a)
            seen.add(a["url"])

    df = pd.DataFrame(unique_articles)

    # 3. Filtrar solo artículos nuevos
    if df.empty:
        print("No se encontraron artículos en el listado. Finalizando.")
        return

    df = df[~df["url"].isin(existing_urls)]
    print(f"Se hallaron {len(df)} artículos nuevos.")

    if df.empty:
        print("No hay artículos nuevos para insertar. Finalizando.")
        return

    # 4. Extraer contenido completo de cada artículo nuevo
    print("Extrayendo contenido completo de cada artículo nuevo...")
    full_data = [extract_article(u) for u in df["url"]]
    full_data = [d for d in full_data if d is not None]  # descarta fallos

    if not full_data:
        print("No se pudo extraer contenido de ningún artículo nuevo. Finalizando.")
        return

    df_diario = pd.DataFrame(full_data)
    df_news = pd.merge(df, df_diario, how="inner", on="url")

    # 5. Parseo de fecha
    df_news["snapshot_date"] = datetime.date.today()
    df_news["time"] = df_news["datetime_raw"].str.split(",").str[1].str.split().str.join(" ")
    df_news["weekday"] = df_news["datetime_raw"].str.split(" ").str[0]
    df_news["datetime_str"] = df_news["datetime_raw"].str.split(",").str[0]
    df_news["date"] = df_news["datetime_str"].str.split(" ", n=1).str[1].str.split().str.join(" ")
    df_news["date"] = df_news["date"].replace(MONTH_MAP, regex=True)
    df_news["date"] = df_news["date"].str.replace(" de ", " ", regex=False)
    df_news["date"] = pd.to_datetime(df_news["date"], errors="coerce")

    # 6. Limpieza para insertar en Supabase
    df_news["snapshot_date"] = df_news["snapshot_date"].astype(str)
    df_news["date"] = df_news["date"].astype(str)
    df_news = df_news.replace({"NaT": None, "nan": None, "None": None})

    records = df_news.to_dict(orient="records")

    # 7. Insertar
    try:
        supabase.table("urgente_articles").insert(records).execute()
        print(f"✅ Insertados {len(records)} artículos nuevos.")
    except Exception as e:
        print(f"❌ Error durante la inserción: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
