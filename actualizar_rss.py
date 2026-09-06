import os
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser


URL_BASE = "https://www.tubacex.com"
URL_NOTICIAS = "https://www.tubacex.com/es/noticias/"
ARCHIVO_RSS = Path("rss.xml")

ZONA_HORARIA = ZoneInfo("Europe/Madrid")
MAX_PAGINAS = 100
MAX_ARTICULOS = 3000

CABECERAS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.7",
    "Cache-Control": "no-cache",
    "Referer": URL_BASE + "/",
}


def dentro_del_horario():
    """
    Los lanzamientos manuales funcionan a cualquier hora.

    Las ejecuciones programadas funcionan:
    - De lunes a sábado.
    - Desde las 07:00 hasta las 22:59.
    - Según la hora peninsular española.
    """
    evento = os.environ.get("GITHUB_EVENT_NAME", "")

    if evento == "workflow_dispatch":
        print("Ejecución manual: se ignora el límite horario.")
        return True

    ahora = datetime.now(ZONA_HORARIA)
    print(f"Hora española: {ahora:%Y-%m-%d %H:%M:%S %Z}")

    if ahora.weekday() == 6:
        print("Domingo: no se actualiza el RSS.")
        return False

    if not 7 <= ahora.hour <= 22:
        print("Fuera del horario permitido: 07:00-22:59.")
        return False

    return True


def texto(valor):
    if valor is None:
        return ""

    return " ".join(str(valor).split()).strip()


def normalizar_url(url):
    url = texto(url)

    if not url:
        return ""

    return urljoin(URL_BASE, url).split("#")[0]


def pertenece_a_tubacex(url):
    try:
        dominio = urlparse(url).netloc.lower()
        return dominio in ("tubacex.com", "www.tubacex.com")
    except ValueError:
        return False


def descargar(sesion, url):
    ultimo_error = None

    for intento in range(1, 4):
        try:
            respuesta = sesion.get(
                url,
                headers=CABECERAS,
                timeout=45,
                allow_redirects=True,
            )
            respuesta.raise_for_status()

            if not respuesta.text.strip():
                raise RuntimeError("La página se descargó vacía.")

            return respuesta.text

        except Exception as error:
            ultimo_error = error
            print(
                f"Intento {intento}/3 fallido al descargar {url}: {error}"
            )

            if intento < 3:
                time.sleep(intento * 2)

    raise RuntimeError(
        f"No se pudo descargar {url}: {ultimo_error}"
    )


def obtener_fecha(contenedor):
    time_html = contenedor.find("time")

    if time_html:
        valor = (
            time_html.get("datetime")
            or time_html.get("content")
            or time_html.get_text(" ", strip=True)
        )

        fecha = convertir_fecha(valor)

        if fecha:
            return fecha

    meta_fecha = contenedor.select_one(
        'meta[property="article:published_time"], '
        'meta[itemprop="datePublished"]'
    )

    if meta_fecha and meta_fecha.get("content"):
        fecha = convertir_fecha(meta_fecha.get("content"))

        if fecha:
            return fecha

    elemento_fecha = contenedor.select_one(
        ".published, .entry-date, .post-date, "
        ".et_pb_post .post-meta, .post-meta"
    )

    if elemento_fecha:
        fecha = convertir_fecha(
            elemento_fecha.get_text(" ", strip=True)
        )

        if fecha:
            return fecha

    texto_contenedor = contenedor.get_text(" ", strip=True)

    patrones = [
        r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
        r"\s+\d{1,2},\s+\d{4}\b",
        r"\b\d{1,2}\s+de\s+[A-Za-zÁÉÍÓÚáéíóúñÑ]+\s+de\s+\d{4}\b",
        r"\b\d{1,2}/\d{1,2}/\d{4}\b",
    ]

    for patron in patrones:
        coincidencia = re.search(
            patron,
            texto_contenedor,
            flags=re.IGNORECASE,
        )

        if coincidencia:
            fecha = convertir_fecha(coincidencia.group(0))

            if fecha:
                return fecha

    return format_datetime(datetime.now(timezone.utc))


def convertir_fecha(valor):
    valor = texto(valor)

    if not valor:
        return ""

    meses = {
        "enero": "January",
        "febrero": "February",
        "marzo": "March",
        "abril": "April",
        "mayo": "May",
        "junio": "June",
        "julio": "July",
        "agosto": "August",
        "septiembre": "September",
        "setiembre": "September",
        "octubre": "October",
        "noviembre": "November",
        "diciembre": "December",
    }

    valor_traducido = valor.lower()

    for espanol, ingles in meses.items():
        valor_traducido = re.sub(
            rf"\b{espanol}\b",
            ingles,
            valor_traducido,
            flags=re.IGNORECASE,
        )

    valor_traducido = re.sub(
        r"\bde\b",
        " ",
        valor_traducido,
        flags=re.IGNORECASE,
    )

    try:
        fecha = date_parser.parse(
            valor_traducido,
            fuzzy=True,
            dayfirst=True,
        )

        if fecha.tzinfo is None:
            fecha = fecha.replace(tzinfo=ZONA_HORARIA)

        return format_datetime(fecha.astimezone(timezone.utc))

    except (ValueError, TypeError, OverflowError):
        return ""


def obtener_imagen(contenedor):
    imagen = contenedor.find("img")

    if not imagen:
        return ""

    for atributo in (
        "data-lazy-src",
        "data-src",
        "data-original",
        "src",
    ):
        url = normalizar_url(imagen.get(atributo))

        if url and not url.startswith("data:"):
            return url

    srcset = texto(
        imagen.get("data-srcset")
        or imagen.get("srcset")
    )

    if srcset:
        primera = srcset.split(",")[0].strip().split(" ")[0]
        return normalizar_url(primera)

    return ""


def obtener_resumen(contenedor):
    selectores = [
        ".post-content p",
        ".entry-content p",
        ".post-excerpt",
        ".excerpt",
        ".entry-summary",
        "p",
    ]

    for selector in selectores:
        elementos = contenedor.select(selector)

        for elemento in elementos:
            contenido = texto(elemento.get_text(" ", strip=True))

            if not contenido:
                continue

            if contenido.lower() in ("leer más", "read more"):
                continue

            if len(contenido) >= 25:
                return contenido

    return ""


def obtener_categorias(contenedor):
    categorias = []

    selectores = [
        ".post-meta a",
        ".entry-meta a",
        'a[rel="category tag"]',
        'a[href*="/category/"]',
    ]

    for selector in selectores:
        for enlace in contenedor.select(selector):
            categoria = texto(enlace.get_text(" ", strip=True))

            if not categoria:
                continue

            if categoria.lower() in (
                "leer más",
                "read more",
                "tubacex",
            ):
                continue

            if categoria not in categorias:
                categorias.append(categoria)

    return categorias


def localizar_noticias(sopa):
    """
    Tubacex utiliza una estructura de publicaciones de WordPress/Divi.
    Se prueban varios selectores para soportar cambios de diseño.
    """
    selectores = [
        "article.et_pb_post",
        "article.post",
        ".et_pb_post",
        ".post",
    ]

    for selector in selectores:
        encontrados = sopa.select(selector)

        if encontrados:
            return encontrados

    return []


def convertir_publicacion(contenedor):
    enlace_titulo = contenedor.select_one(
        "h1.entry-title a, "
        "h2.entry-title a, "
        "h3.entry-title a, "
        ".entry-title a, "
        "h2 a, "
        "h3 a"
    )

    if enlace_titulo is None:
        return None

    titulo = texto(enlace_titulo.get_text(" ", strip=True))
    enlace = normalizar_url(enlace_titulo.get("href"))

    if not titulo or not enlace:
        return None

    if not pertenece_a_tubacex(enlace):
        return None

    if enlace.rstrip("/") == URL_NOTICIAS.rstrip("/"):
        return None

    resumen = obtener_resumen(contenedor)
    imagen = obtener_imagen(contenedor)
    categorias = obtener_categorias(contenedor)
    fecha = obtener_fecha(contenedor)

    descripcion = ""

    if resumen:
        descripcion += f"<p>{resumen}</p>"

    if categorias:
        descripcion += (
            "<p><strong>Categorías:</strong> "
            + ", ".join(categorias)
            + "</p>"
        )

    descripcion += (
        f'<p><a href="{enlace}">Leer la noticia completa en Tubacex</a></p>'
    )

    return {
        "title": titulo,
        "link": enlace,
        "guid": enlace,
        "pubDate": fecha,
        "description": descripcion,
        "author": "Tubacex",
        "categories": categorias,
        "image": imagen,
    }


def buscar_pagina_siguiente(sopa, url_actual):
    selectores = [
        "a.next",
        "a.nextpostslink",
        ".pagination a.next",
        ".wp-pagenavi a.nextpostslink",
        ".alignleft a",
    ]

    for selector in selectores:
        enlace = sopa.select_one(selector)

        if enlace and enlace.get("href"):
            siguiente = normalizar_url(enlace.get("href"))

            if siguiente != url_actual and pertenece_a_tubacex(siguiente):
                return siguiente

    for enlace in sopa.find_all("a", href=True):
        etiqueta = texto(enlace.get_text(" ", strip=True)).lower()

        if (
            "entradas más antiguas" in etiqueta
            or "entradas anteriores" in etiqueta
            or "older entries" in etiqueta
        ):
            siguiente = normalizar_url(enlace.get("href"))

            if siguiente != url_actual:
                return siguiente

    return ""


def extraer_noticias_web():
    sesion = requests.Session()
    sesion.headers.update(CABECERAS)

    articulos = []
    paginas_visitadas = set()
    url_actual = URL_NOTICIAS

    for numero in range(1, MAX_PAGINAS + 1):
        if not url_actual or url_actual in paginas_visitadas:
            break

        paginas_visitadas.add(url_actual)

        print(f"Descargando página {numero}: {url_actual}")
        contenido = descargar(sesion, url_actual)
        sopa = BeautifulSoup(contenido, "html.parser")

        publicaciones = localizar_noticias(sopa)
        encontrados = 0

        for publicacion in publicaciones:
            articulo = convertir_publicacion(publicacion)

            if articulo:
                articulos.append(articulo)
                encontrados += 1

        print(
            f"Página {numero}: {encontrados} noticias localizadas."
        )

        siguiente = buscar_pagina_siguiente(sopa, url_actual)

        if not siguiente:
            break

        url_actual = siguiente
        time.sleep(0.4)

    return eliminar_duplicados(articulos)


def leer_articulos_anteriores():
    if not ARCHIVO_RSS.exists():
        return []

    try:
        raiz = ET.parse(ARCHIVO_RSS).getroot()
    except ET.ParseError:
        print("El rss.xml anterior no es válido; se reconstruirá.")
        return []

    articulos = []

    for item in raiz.findall("./channel/item"):
        categorias = [
            texto(elemento.text)
            for elemento in item.findall("category")
            if texto(elemento.text)
        ]

        enclosure = item.find("enclosure")
        imagen = ""

        if enclosure is not None:
            imagen = texto(enclosure.get("url"))

        articulos.append(
            {
                "title": texto(item.findtext("title")),
                "link": texto(item.findtext("link")),
                "guid": texto(item.findtext("guid")),
                "pubDate": texto(item.findtext("pubDate")),
                "description": texto(item.findtext("description")),
                "author": texto(item.findtext("author")),
                "categories": categorias,
                "image": imagen,
            }
        )

    print(
        f"Noticias recuperadas del RSS anterior: {len(articulos)}"
    )
    return articulos


def clave_articulo(articulo):
    enlace = texto(articulo.get("link"))

    if enlace:
        return enlace.split("?")[0].rstrip("/").lower()

    return texto(
        articulo.get("guid") or articulo.get("title")
    ).lower()


def eliminar_duplicados(articulos):
    resultado = []
    encontrados = set()

    for articulo in articulos:
        clave = clave_articulo(articulo)

        if not clave or clave in encontrados:
            continue

        encontrados.add(clave)
        resultado.append(articulo)

    return resultado


def fecha_ordenacion(articulo):
    try:
        fecha = parsedate_to_datetime(articulo["pubDate"])

        if fecha.tzinfo is None:
            fecha = fecha.replace(tzinfo=timezone.utc)

        return fecha.timestamp()
    except (TypeError, ValueError, OverflowError, KeyError):
        return 0


def combinar_articulos(nuevos, anteriores):
    nuevos = eliminar_duplicados(nuevos)
    nuevos.sort(key=fecha_ordenacion, reverse=True)

    resultado = []
    encontrados = set()

    for articulo in nuevos + anteriores:
        clave = clave_articulo(articulo)

        if not clave or clave in encontrados:
            continue

        encontrados.add(clave)
        resultado.append(articulo)

        if len(resultado) >= MAX_ARTICULOS:
            break

    return resultado


def añadir_texto(padre, etiqueta, valor):
    elemento = ET.SubElement(padre, etiqueta)
    elemento.text = texto(valor)
    return elemento


def crear_rss(articulos):
    rss = ET.Element(
        "rss",
        {
            "version": "2.0",
            "xmlns:atom": "http://www.w3.org/2005/Atom",
        },
    )

    canal = ET.SubElement(rss, "channel")

    añadir_texto(canal, "title", "Tubacex — Noticias")
    añadir_texto(canal, "link", URL_NOTICIAS)
    añadir_texto(
        canal,
        "description",
        "Todas las noticias publicadas en la web oficial de Tubacex.",
    )
    añadir_texto(canal, "language", "es")
    añadir_texto(
        canal,
        "lastBuildDate",
        format_datetime(datetime.now(timezone.utc)),
    )
    añadir_texto(
        canal,
        "generator",
        "GitHub Actions RSS Generator",
    )

    atom = ET.SubElement(
        canal,
        "{http://www.w3.org/2005/Atom}link",
    )
    atom.set(
        "href",
        (
            "https://raw.githubusercontent.com/"
            "plis2100/tubacex-noticias-rss/main/rss.xml"
        ),
    )
    atom.set("rel", "self")
    atom.set("type", "application/rss+xml")

    for articulo in articulos:
        item = ET.SubElement(canal, "item")

        añadir_texto(item, "title", articulo["title"])
        añadir_texto(item, "link", articulo["link"])

        guid = añadir_texto(item, "guid", articulo["guid"])
        guid.set("isPermaLink", "true")

        añadir_texto(item, "pubDate", articulo["pubDate"])
        añadir_texto(
            item,
            "description",
            articulo["description"],
        )

        if articulo["author"]:
            añadir_texto(item, "author", articulo["author"])

        for categoria in articulo["categories"]:
            añadir_texto(item, "category", categoria)

        if articulo["image"]:
            enclosure = ET.SubElement(item, "enclosure")
            enclosure.set("url", articulo["image"])
            enclosure.set("type", "image/jpeg")

    arbol = ET.ElementTree(rss)
    ET.indent(arbol, space="  ")

    temporal = ARCHIVO_RSS.with_suffix(".xml.tmp")

    arbol.write(
        temporal,
        encoding="utf-8",
        xml_declaration=True,
    )

    temporal.replace(ARCHIVO_RSS)


def main():
    if not dentro_del_horario():
        return

    nuevos = extraer_noticias_web()
    anteriores = leer_articulos_anteriores()

    print(f"Noticias localizadas en Tubacex: {len(nuevos)}")

    if not nuevos and not anteriores:
        raise RuntimeError(
            "No se encontró ninguna noticia y tampoco existe "
            "un RSS anterior que pueda conservarse."
        )

    if not nuevos and anteriores:
        print(
            "AVISO: no se localizaron noticias nuevas. "
            "Se conservará el RSS anterior."
        )

    articulos = combinar_articulos(nuevos, anteriores)

    if not articulos:
        raise RuntimeError("No existen artículos para crear el RSS.")

    crear_rss(articulos)

    print(
        f"RSS creado correctamente con {len(articulos)} noticias."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise
