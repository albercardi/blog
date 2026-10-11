#!/usr/bin/env python3
"""
actualizar_libros.py
────────────────────
Uso: python3 actualizar_libros.py goodreads_library_export.csv

Descargá tu CSV desde: https://www.goodreads.com/review/import
Guardalo en updates/goodreads_library_export.csv y ejecutá desde la raíz:
    python3 tools/actualizar_libros.py

El script:
  1. Lee el CSV de Goodreads
  2. Descarga y guarda portadas locales; reutiliza las disponibles
  3. Genera libros/libros.js para publicar con el workflow
"""

import csv
import json
import re
import sys
import time
import urllib.request
import urllib.error
import html as html_module
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Configuración ──────────────────────────────────────────────────────────────

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_FILE = REPO_ROOT / "libros" / "libros.js"
DEFAULT_CSV = REPO_ROOT / "updates" / "goodreads_library_export.csv"
MAX_WORKERS = 3          # peticiones paralelas a Goodreads
DELAY       = 0.12       # segundos entre peticiones

MONTHS = {
    '01': 'enero',    '02': 'febrero',  '03': 'marzo',
    '04': 'abril',    '05': 'mayo',     '06': 'junio',
    '07': 'julio',    '08': 'agosto',   '09': 'septiembre',
    '10': 'octubre',  '11': 'noviembre','12': 'diciembre',
}

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    )
}

# ── Helpers ────────────────────────────────────────────────────────────────────

def parse_date(date_str):
    """Devuelve (sort_key, display, year) desde una fecha YYYY/MM/DD."""
    if not date_str or not date_str.strip():
        return ('0000/00/00', '', 'Sin fecha')
    parts = date_str.strip().split('/')
    if len(parts) == 3:
        y, m, d = parts
        display = f"{MONTHS.get(m, m)} {y}"
        return (date_str.strip(), display, y)
    return ('0000/00/00', '', 'Sin fecha')


def clean_isbn(raw):
    return raw.replace('="', '').replace('"', '').strip()


COVER_DIR = REPO_ROOT / "libros" / "portadas"
MAX_IMAGE_BYTES = 12 * 1024 * 1024


class CoverParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.candidates = []
        self.blocks = []
        self.parts = []
        self.in_json = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            key = (attrs.get("property") or attrs.get("name") or "").lower()
            if key in ("og:image", "twitter:image", "twitter:image:src"):
                self.candidates.append(attrs.get("content", ""))
        if tag == "img" and (
            attrs.get("id") == "coverImage"
            or "ResponsiveImage" in attrs.get("class", "")
        ):
            self.candidates.append(attrs.get("src") or attrs.get("data-src") or "")
        if tag == "script" and attrs.get("type", "").lower() == "application/ld+json":
            self.in_json = True
            self.parts = []

    def handle_data(self, data):
        if self.in_json:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.in_json:
            self.blocks.append("".join(self.parts))
            self.in_json = False
            self.parts = []


def image_values(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from image_values(item)
    elif isinstance(value, dict):
        for key in ("url", "contentUrl"):
            if isinstance(value.get(key), str):
                yield value[key]


def book_images(data):
    if isinstance(data, list):
        for item in data:
            yield from book_images(item)
    elif isinstance(data, dict):
        kinds = data.get("@type", [])
        if isinstance(kinds, str):
            kinds = [kinds]
        if "Book" in kinds:
            yield from image_values(data.get("image"))
        for value in data.values():
            if isinstance(value, (list, dict)):
                yield from book_images(value)


def image_extension(data):
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if len(data) >= 16 and data[4:8] == b"ftyp" and data[8:12] in (b"avif", b"avis"):
        return ".avif"
    return ""


def previous_covers():
    if not OUTPUT_FILE.is_file():
        return {}
    try:
        source = OUTPUT_FILE.read_text(encoding="utf-8")
        match = re.search(r"\b(?:const|let|var)\s+BOOKS\s*=\s*", source)
        if not match:
            return {}
        data, _ = json.JSONDecoder().raw_decode(source[match.end():].lstrip())
        return {str(b["id"]): b.get("cover", "") for b in data if isinstance(b, dict) and "id" in b}
    except (OSError, ValueError, TypeError) as error:
        print(f"Aviso: no se pudo leer la lista anterior: {error}")
        return {}


def local_cover(book_id):
    for ext in (".jpg", ".png", ".webp", ".gif", ".avif"):
        path = COVER_DIR / (book_id + ext)
        if path.is_file():
            with path.open("rb") as handle:
                head = handle.read(32)
            if image_extension(head) == ext:
                return "portadas/" + path.name
    return ""


def save_cover(book_id, url):
    url = html_module.unescape(url or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith(("https://", "http://")):
        raise ValueError("La portada anterior no es una URL descargable.")
    if any(word in url.lower() for word in ("nophoto", "transparent", "noimage")):
        raise ValueError("La URL corresponde a una imagen de reemplazo.")
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=15) as response:
        data = response.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Imagen demasiado grande.")
    extension = image_extension(data)
    if not extension:
        raise ValueError("La respuesta no tiene una firma de imagen reconocida.")
    COVER_DIR.mkdir(parents=True, exist_ok=True)
    target = COVER_DIR / (book_id + extension)
    temporary = target.with_suffix(extension + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(target)
    return "portadas/" + target.name


def fetch_cover(book_id, previous=""):
    cached = local_cover(book_id)
    if cached:
        return cached, "local"
    errors = []
    if previous.startswith(("https://", "http://", "//")):
        try:
            return save_cover(book_id, previous), "portada anterior descargada"
        except Exception as error:
            errors.append(f"portada anterior: {error}")

    for attempt in range(2):
        try:
            request = urllib.request.Request(
                f"https://www.goodreads.com/book/show/{book_id}", headers=HEADERS
            )
            with urllib.request.urlopen(request, timeout=15) as response:
                source = response.read(5 * 1024 * 1024).decode("utf-8", errors="replace")
            parser = CoverParser()
            parser.feed(source)
            candidates = []
            for block in parser.blocks:
                try:
                    candidates.extend(book_images(json.loads(block)))
                except (ValueError, TypeError):
                    pass
            candidates.extend(parser.candidates)
            tried = set()
            for candidate in candidates:
                if not candidate or candidate in tried:
                    continue
                tried.add(candidate)
                try:
                    return save_cover(book_id, candidate), "Goodreads"
                except Exception as error:
                    errors.append(f"descarga de imagen: {error}")
            if not candidates:
                errors.append("La página recibida no contiene una portada reconocible.")
            break
        except urllib.error.HTTPError as error:
            errors.append(f"Goodreads: HTTP {error.code}")
            if error.code in (403, 404, 429):
                break
        except Exception as error:
            errors.append(f"Goodreads: {type(error).__name__}: {error}")
        if attempt == 0:
            time.sleep(2)
    return "", "; ".join(errors[-3:]) or "No se obtuvo una imagen."


# ── Lectura del CSV ─────────────────────────────────────────────────────────────

def load_books(csv_path):
    books = []
    seen_ids = set()

    with open(csv_path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    def process(row):
        bid        = row['Book Id'].strip()
        if not re.fullmatch(r'[0-9]+', bid):
            raise ValueError(f'Identificador Goodreads inválido: {bid!r}')
        shelf      = row['Exclusive Shelf'].strip()
        rating_raw = row['My Rating'].strip()
        rc         = row['Read Count'].strip()
        date_read  = row['Date Read'].strip()
        date_added = row['Date Added'].strip()

        # Incluir si está en 'read', o si tiene lecturas/rating aunque esté en otra estantería
        is_read = (
            shelf == 'read'
            or (float(rc or 0) > 0)
            or (float(rating_raw or 0) > 0)
        )
        if not is_read:
            return None

        # Fecha: Date Read > Date Added > sin fecha
        if date_read:
            sort_key, display, year = parse_date(date_read)
        elif date_added:
            sort_key, display, year = parse_date(date_added)
        else:
            sort_key, display, year = '0000/00/00', '', 'Sin fecha'

        return {
            'id':        bid,
            'title':     row['Title'].strip(),
            'author':    row['Author'].strip(),
            'rating':    float(rating_raw) if rating_raw else 0,
            'pages':     row['Number of Pages'].strip(),
            'date_read': display,
            'year':      year,
            '_sort':     sort_key,
            'cover':     '',   # se llena después
            'review':    row['My Review'].strip(),
            'goodreads': f"https://www.goodreads.com/book/show/{bid}",
            'pub_year':  row['Original Publication Year'].strip() or row['Year Published'].strip(),
        }

    for row in rows:
        bid = row['Book Id'].strip()
        if bid in seen_ids:
            continue
        b = process(row)
        if b:
            seen_ids.add(bid)
            books.append(b)

    return books


# ── Descarga de portadas ────────────────────────────────────────────────────────

def fetch_all_covers(books):
    previous = previous_covers()
    print(f"Buscando o recuperando portadas para {len(books)} libros...")
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(fetch_cover, b['id'], previous.get(b['id'], '')): b
            for b in books
        }
        found = 0
        for done, future in enumerate(as_completed(futures), 1):
            book = futures[future]
            try:
                cover, detail = future.result()
            except Exception as error:
                cover, detail = '', f'{type(error).__name__}: {error}'
            book['cover'] = cover
            if cover:
                found += 1
            else:
                print(f"SIN PORTADA: {book['id']} — {book['title']}: {detail}")
            if done % 20 == 0 or done == len(books):
                print(f"{done}/{len(books)} procesados; {found} portadas disponibles")
    return books


# ── Generación del JS ──────────────────────────────────────────────────────────

JS_TEMPLATE = """\
// libros.js — generado automáticamente por actualizar_libros.py
const BOOKS = {books_json};

function stars(n) {{
  var s = '';
  for (var i = 1; i <= 5; i++) {{
    s += '<span class="star' + (i <= n ? ' filled' : '') + '">&#9733;</span>';
  }}
  return s;
}}

function esc(str) {{
  return (str || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}

var COVER_ERR = "this.style.display='none';this.nextElementSibling.style.display='flex'";

function cardCoverHTML(b) {{
  var t = esc(b.title);
  if (b.cover) {{
    return '<div class="book-cover-wrap">'
      + '<img class="book-cover" src="' + esc(b.cover) + '" alt="' + t + '" loading="lazy" onerror="' + COVER_ERR + '">'
      + '<div class="book-cover-placeholder" style="display:none"><span>' + t + '</span></div>'
      + '</div>';
  }}
  return '<div class="book-cover-wrap"><div class="book-cover-placeholder"><span>' + t + '</span></div></div>';
}}

function modalCoverHTML(b) {{
  var t = esc(b.title);
  if (b.cover) {{
    return '<div class="modal-cover-wrap">'
      + '<img src="' + esc(b.cover) + '" alt="' + t + '" onerror="' + COVER_ERR + '">'
      + '<div class="modal-cover-placeholder" style="display:none"><span>' + t + '</span></div>'
      + '</div>';
  }}
  return '<div class="modal-cover-wrap"><div class="modal-cover-placeholder"><span>' + t + '</span></div></div>';
}}

var years = ['Todos'];
var seenYears = {{}};
BOOKS.forEach(function(b) {{
  if (!seenYears[b.year]) {{ seenYears[b.year] = true; years.push(b.year); }}
}});
years.sort(function(a, b) {{
  if (a === 'Todos') return -1;
  if (b === 'Todos') return 1;
  return parseInt(b) - parseInt(a);
}});

var activeYear = 'Todos';

var filterBtnsEl = document.getElementById('filterBtns');
years.forEach(function(y) {{
  var btn = document.createElement('button');
  btn.className = 'filter-btn' + (y === 'Todos' ? ' active' : '');
  btn.textContent = y;
  btn.addEventListener('click', function() {{
    activeYear = y;
    document.querySelectorAll('.filter-btn').forEach(function(b) {{ b.classList.remove('active'); }});
    btn.classList.add('active');
    renderBooks();
  }});
  filterBtnsEl.appendChild(btn);
}});

function renderStats(filtered) {{
  var total = filtered.length;
  var pages = filtered.reduce(function(s, b) {{ return s + (parseInt(b.pages) || 0); }}, 0);
  var rated = filtered.filter(function(b) {{ return b.rating > 0; }});
  var avg = rated.length ? (rated.reduce(function(s,b){{return s+b.rating;}},0)/rated.length).toFixed(1) : '&mdash;';
  document.getElementById('statsBar').innerHTML =
    '<div class="stat"><span class="stat-value">' + total + '</span><span class="stat-label">libros</span></div>'
    + '<div class="stat"><span class="stat-value">' + pages.toLocaleString('es') + '</span><span class="stat-label">p&aacute;ginas</span></div>'
    + '<div class="stat"><span class="stat-value">' + avg + '</span><span class="stat-label">puntuaci&oacute;n media</span></div>';
}}

function renderBooks() {{
  var filtered = activeYear === 'Todos'
    ? BOOKS
    : BOOKS.filter(function(b) {{ return b.year === activeYear; }});
  var container = document.getElementById('booksContainer');
  container.innerHTML = '';
  if (!filtered.length) {{
    container.innerHTML = '<div class="no-results">No hay libros para mostrar.</div>';
    renderStats([]);
    return;
  }}
  renderStats(filtered);
  var grid = document.createElement('div');
  grid.className = 'books-grid';
  filtered.forEach(function(book) {{
    var idx = BOOKS.indexOf(book);
    var card = document.createElement('div');
    card.className = 'book-card';
    card.setAttribute('role', 'button');
    card.setAttribute('tabindex', '0');
    card.setAttribute('aria-label', book.title);
    card.innerHTML =
      cardCoverHTML(book)
      + '<div class="book-stars">' + stars(book.rating) + '</div>'
      + '<div class="book-title">' + esc(book.title) + '</div>'
      + '<div class="book-author">' + esc(book.author) + '</div>'
      + (book.date_read ? '<div class="book-meta">' + esc(book.date_read) + '</div>' : '');
    card.addEventListener('click', (function(i){{return function(){{openModal(i);}};}})(idx));
    card.addEventListener('keydown', (function(i){{return function(e){{if(e.key==='Enter'||e.key===' ')openModal(i);}};}})(idx));
    grid.appendChild(card);
  }});
  container.appendChild(grid);
}}

function openModal(idx) {{
  var b = BOOKS[idx];
  document.getElementById('modalContent').innerHTML =
    '<div class="modal-inner">'
    + modalCoverHTML(b)
    + '<div class="modal-info">'
    + '<div class="modal-title">' + esc(b.title) + '</div>'
    + '<div class="modal-author">' + esc(b.author) + '</div>'
    + (b.rating > 0 ? '<div class="modal-stars">' + stars(b.rating) + '</div>' : '')
    + '<div class="modal-details">'
    + (b.date_read ? '<span>Le&iacute;do en ' + esc(b.date_read) + '</span>' : '')
    + (b.pages ? '<span>' + esc(b.pages) + ' p&aacute;ginas</span>' : '')
    + (b.pub_year ? '<span>Publicado en ' + esc(b.pub_year) + '</span>' : '')
    + '</div>'
    + (b.review ? '<div class="modal-review">' + esc(b.review) + '</div>' : '')
    + '<a class="modal-link" href="' + esc(b.goodreads) + '" target="_blank" rel="noopener">Ver en Goodreads &rarr;</a>'
    + '</div></div>';
  document.getElementById('modalOverlay').classList.add('open');
  document.body.style.overflow = 'hidden';
}}

function closeModal() {{
  document.getElementById('modalOverlay').classList.remove('open');
  document.body.style.overflow = '';
}}

document.getElementById('modalClose').addEventListener('click', closeModal);
document.getElementById('modalOverlay').addEventListener('click', function(e) {{
  if (e.target === e.currentTarget) closeModal();
}});
document.addEventListener('keydown', function(e) {{ if (e.key === 'Escape') closeModal(); }});

renderBooks();
"""

def write_js(books, output_path):
    for b in books:
        b.pop('_sort', None)
    books_json = json.dumps(books, ensure_ascii=False)
    js = JS_TEMPLATE.format(books_json=books_json)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(js)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    csv_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CSV
    print(f"\n📚 Leyendo {csv_path}...")
    books = load_books(csv_path)
    print(f"  {len(books)} libros encontrados")

    if not books:
        raise ValueError("No hay libros seleccionados; se evita vaciar el sitio.")

    books.sort(key=lambda b: b['_sort'], reverse=True)

    print("\n🖼  Descargando portadas desde Goodreads...")
    books = fetch_all_covers(books)

    print(f"\n✍  Generando {OUTPUT_FILE}...")
    write_js(books, OUTPUT_FILE)

    with_cover = sum(1 for b in books if b['cover'])
    print(f"\n✅ Listo.")
    print(f"   Libros: {len(books)}")
    print(f"   Con portada: {with_cover}")
    print(f"   Archivo generado: {OUTPUT_FILE}")
    print(f"   Sin portada: {len(books) - with_cover}")


if __name__ == '__main__':
    main()
