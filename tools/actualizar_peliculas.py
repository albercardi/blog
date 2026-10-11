#!/usr/bin/env python3
"""Ejecutar desde la raiz: python tools/actualizar_peliculas.py
Lee updates/peliculas.csv y genera blog/peliculas/peliculas.js.
Created es la fecha de incorporacion a la lista, no de visualizacion.
No necesita paquetes externos ni consulta servicios de imagenes.
"""
import argparse
import csv
import json
import math
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIELDS = {
    "title": "Title", "original_title": "Original Title",
    "imdb_rating": "IMDb Rating", "year": "Year", "genres": "Genres",
    "runtime": "Runtime (mins)", "directors": "Directors",
    "created": "Created", "your_rating": "Your Rating",
    "title_type": "Title Type",
}

def load_movies(path):
    movies, seen = [], set()
    duplicates = 0
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = ({"Const"} | set(FIELDS.values())) - set(reader.fieldnames or [])
        if missing:
            raise ValueError("Faltan columnas: " + ", ".join(sorted(missing)))
        for line, row in enumerate(reader, start=2):
            if not any(row.values()):
                continue
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"Fila {line}: cantidad de columnas incorrecta.")
            movie = {key: row[column].strip() for key, column in FIELDS.items()}
            imdb_id = row["Const"].strip()
            if not re.fullmatch(r"tt[0-9]+", imdb_id):
                raise ValueError(f"Fila {line}: identificador IMDb invalido.")
            if not movie["title"]:
                raise ValueError(f"Fila {line}: titulo vacio.")
            if movie["created"]:
                date.fromisoformat(movie["created"])
            for key in ("imdb_rating", "your_rating"):
                if movie[key]:
                    value = float(movie[key])
                    if not math.isfinite(value) or not 0 <= value <= 10:
                        raise ValueError(f"Fila {line}: puntuacion invalida en {key}.")
            for key in ("year", "runtime"):
                if movie[key] and not movie[key].isdigit():
                    raise ValueError(f"Fila {line}: valor invalido en {key}.")
            movie["original_title"] = movie["original_title"] or movie["title"]
            movie["imdb_id"] = imdb_id
            movie["url"] = f"https://www.imdb.com/title/{imdb_id}/"
            if imdb_id in seen:
                duplicates += 1
                continue
            seen.add(imdb_id)
            movies.append(movie)
    if not movies:
        raise ValueError("CSV sin titulos: se conserva el archivo anterior.")
    movies.sort(key=lambda movie: movie["created"], reverse=True)
    return movies, duplicates

def write_movies(movies, output):
    payload = json.dumps(movies, ensure_ascii=False, indent=2)
    payload = payload.replace(chr(0x2028), "\\u2028").replace(chr(0x2029), "\\u2029")
    content = "// Generado por tools/actualizar_peliculas.py.\nconst MOVIES = " + payload + ";\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)

def main():
    parser = argparse.ArgumentParser(description="Actualizar peliculas desde una exportacion CSV de IMDb")
    parser.add_argument("--csv", type=Path, default=ROOT / "updates" / "peliculas.csv")
    parser.add_argument("--output", type=Path, default=ROOT / "blog" / "peliculas" / "peliculas.js")
    args = parser.parse_args()
    try:
        movies, duplicates = load_movies(args.csv)
        write_movies(movies, args.output)
    except (OSError, ValueError, csv.Error) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"Titulos exportados: {len(movies)}")
    print(f"Duplicados omitidos: {duplicates}")
    print(f"Archivo generado: {args.output}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
