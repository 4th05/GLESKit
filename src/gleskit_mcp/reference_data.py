from __future__ import annotations

import csv
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from gleskit_mcp.config import Settings
from gleskit_mcp.models import dump_json, utc_now_iso
from gleskit_mcp.providers.base import ProviderError, ProviderResponse
from gleskit_mcp.utils import clean_text, normalize_gene_token


HGNC_COMPLETE_SET_URL = "https://storage.googleapis.com/public-download-files/hgnc/tsv/tsv/hgnc_complete_set.txt"
_DATASET_ID = "hgnc_human_genes"
_DATASET_TITLE = "HGNC Human Gene Reference"


class ReferenceCatalog:
    def __init__(
        self,
        settings: Settings,
        *,
        fetch_text: Callable[[str], str] | None = None,
    ) -> None:
        self.settings = settings
        self.fetch_text = fetch_text or _fetch_text
        self.settings.reference_root.mkdir(parents=True, exist_ok=True)

    def status(self) -> dict[str, Any]:
        manifest = self._load_manifest()
        dataset = {
            "dataset_id": _DATASET_ID,
            "title": _DATASET_TITLE,
            "species": "human",
            "installed": False,
            "status": "not_installed",
            "updated_at": None,
            "snapshot_id": None,
            "record_count": 0,
            "alias_count": 0,
            "source_url": HGNC_COMPLETE_SET_URL,
        }
        if manifest:
            for item in manifest.get("datasets", []):
                if str(item.get("dataset_id") or "") == _DATASET_ID:
                    dataset.update(item)
                    break
        return {
            "available": bool(dataset.get("installed")),
            "reference_root": str(self.settings.reference_root),
            "current_snapshot_id": manifest.get("current_snapshot_id") if manifest else None,
            "updated_at": manifest.get("updated_at") if manifest else None,
            "datasets": [dataset],
        }

    def refresh_all(self) -> dict[str, Any]:
        refreshed_at = utc_now_iso()
        snapshot_id = f"snapshot_{refreshed_at.replace(':', '').replace('-', '').replace('+', '')}"
        snapshots_root = self.settings.reference_root / "snapshots"
        snapshots_root.mkdir(parents=True, exist_ok=True)
        snapshot_dir = snapshots_root / snapshot_id

        with tempfile.TemporaryDirectory(prefix="gleskit_ref_") as tmp:
            tmp_dir = Path(tmp)
            raw_tsv = self.fetch_text(HGNC_COMPLETE_SET_URL)
            raw_path = tmp_dir / "hgnc_complete_set.txt"
            raw_path.write_text(raw_tsv, encoding="utf-8")
            db_path = tmp_dir / "reference.db"
            record_count, alias_count = _build_hgnc_reference_db(raw_path, db_path)
            shutil.copytree(tmp_dir, snapshot_dir)

        manifest = {
            "current_snapshot_id": snapshot_id,
            "updated_at": refreshed_at,
            "datasets": [
                {
                    "dataset_id": _DATASET_ID,
                    "title": _DATASET_TITLE,
                    "species": "human",
                    "installed": True,
                    "status": "ready",
                    "updated_at": refreshed_at,
                    "snapshot_id": snapshot_id,
                    "record_count": record_count,
                    "alias_count": alias_count,
                    "source_url": HGNC_COMPLETE_SET_URL,
                    "snapshot_path": str(snapshot_dir),
                    "database_path": str(snapshot_dir / "reference.db"),
                }
            ],
        }
        self.settings.reference_manifest_path.write_text(dump_json(manifest), encoding="utf-8")
        return self.status()

    def resolve_gene(self, query: str, *, species: str = "human", size: int = 5) -> ProviderResponse | None:
        if _normalize_species_token(species) not in {"human", "homo_sapiens", "9606"}:
            return None
        db_path = self._current_database_path()
        if db_path is None or not db_path.exists():
            return None

        token = normalize_gene_token(query, species=species)
        if not token:
            raise ProviderError("Query must not be empty.")

        rows = self._lookup_matches(db_path, token, limit=max(1, min(int(size), 10)))
        if not rows:
            return None
        matches = [_match_payload_from_row(row, query) for row in rows]
        summary = (
            f"Resolved {len(matches)} candidate gene match(es) for '{clean_text(query)}' from the local HGNC reference. "
            f"Top match uses {matches[0].get('matched_on') or 'symbol'}."
        )
        return ProviderResponse(
            provider="local_reference",
            summary=summary,
            payload={
                "query": clean_text(query),
                "species": "human",
                "match_count": len(matches),
                "matches": matches,
                "reference_dataset": _DATASET_ID,
            },
        )

    def resolve_genes(self, queries: list[str], *, species: str = "human") -> dict[str, ProviderResponse]:
        """Resolve many human identifiers with one local SQLite lookup per chunk."""
        if _normalize_species_token(species) not in {"human", "homo_sapiens", "9606"}:
            return {}
        db_path = self._current_database_path()
        if db_path is None or not db_path.exists():
            return {}

        original_by_token: dict[str, str] = {}
        for query in queries:
            cleaned_query = clean_text(query)
            token = normalize_gene_token(cleaned_query, species=species)
            if cleaned_query and token and token not in original_by_token:
                original_by_token[token] = cleaned_query
        if not original_by_token:
            return {}

        best_rows: dict[str, sqlite3.Row] = {}
        tokens = list(original_by_token)
        conn = sqlite3.connect(db_path.as_posix())
        conn.row_factory = sqlite3.Row
        try:
            # SQLite has a bound-variable limit on some installations; chunks
            # keep this local fast path safe for large RNA-seq gene lists.
            for start in range(0, len(tokens), 900):
                chunk = tokens[start : start + 900]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"""
                    SELECT
                      a.match_type,
                      a.token,
                      a.priority,
                      g.hgnc_id,
                      g.symbol,
                      g.name,
                      g.status,
                      g.locus_group,
                      g.locus_type,
                      g.ensembl_gene_id,
                      g.entrez_gene_id,
                      g.uniprot_ids_json,
                      g.alias_symbols_json,
                      g.prev_symbols_json
                    FROM gene_aliases AS a
                    JOIN genes AS g ON g.hgnc_id = a.hgnc_id
                    WHERE a.species = ? AND a.token IN ({placeholders})
                    ORDER BY a.token, a.priority DESC,
                      CASE WHEN g.status = 'Approved' THEN 1 ELSE 0 END DESC,
                      g.symbol ASC
                    """,
                    ("human", *chunk),
                ).fetchall()
                for row in rows:
                    token = str(row["token"] or "")
                    best_rows.setdefault(token, row)
        finally:
            conn.close()

        resolved: dict[str, ProviderResponse] = {}
        for token, row in best_rows.items():
            query = original_by_token[token]
            match = _match_payload_from_row(row, query)
            resolved[query] = ProviderResponse(
                provider="local_reference",
                summary=(
                    f"Resolved 1 candidate gene match for '{query}' from the local HGNC reference. "
                    f"Top match uses {match.get('matched_on') or 'symbol'}."
                ),
                payload={
                    "query": query,
                    "species": "human",
                    "match_count": 1,
                    "matches": [match],
                    "reference_dataset": _DATASET_ID,
                },
            )
        return resolved

    def _current_database_path(self) -> Path | None:
        manifest = self._load_manifest()
        if not manifest:
            return None
        for item in manifest.get("datasets", []):
            if str(item.get("dataset_id") or "") == _DATASET_ID and item.get("installed"):
                path = Path(str(item.get("database_path") or "")).expanduser()
                return path.resolve() if str(path) else None
        return None

    def _load_manifest(self) -> dict[str, Any]:
        path = self.settings.reference_manifest_path
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ProviderError(f"Reference manifest is unreadable: {exc}") from exc

    def _lookup_matches(self, db_path: Path, token: str, *, limit: int) -> list[sqlite3.Row]:
        conn = sqlite3.connect(db_path.as_posix())
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT
                  a.match_type,
                  a.token,
                  a.priority,
                  g.hgnc_id,
                  g.symbol,
                  g.name,
                  g.status,
                  g.locus_group,
                  g.locus_type,
                  g.ensembl_gene_id,
                  g.entrez_gene_id,
                  g.uniprot_ids_json,
                  g.alias_symbols_json,
                  g.prev_symbols_json
                FROM gene_aliases AS a
                JOIN genes AS g ON g.hgnc_id = a.hgnc_id
                WHERE a.species = ? AND a.token = ?
                ORDER BY
                  a.priority DESC,
                  CASE WHEN g.status = 'Approved' THEN 1 ELSE 0 END DESC,
                  g.symbol ASC
                LIMIT ?
                """,
                ("human", token, limit),
            ).fetchall()
        finally:
            conn.close()
        return rows


def _fetch_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": "GLESKit/0.2"})
    try:
        with urlopen(request, timeout=60) as response:
            return response.read().decode("utf-8")
    except HTTPError as exc:
        raise ProviderError(f"Reference download HTTP error: {exc.code}") from exc
    except URLError as exc:
        raise ProviderError(f"Reference download network error: {exc.reason}") from exc
    except Exception as exc:
        raise ProviderError(f"Reference download failed: {type(exc).__name__}: {exc}") from exc


def _build_hgnc_reference_db(raw_path: Path, db_path: Path) -> tuple[int, int]:
    conn = sqlite3.connect(db_path.as_posix())
    try:
        conn.executescript(
            """
            CREATE TABLE genes (
              hgnc_id TEXT PRIMARY KEY,
              symbol TEXT NOT NULL,
              name TEXT,
              status TEXT,
              locus_group TEXT,
              locus_type TEXT,
              ensembl_gene_id TEXT,
              entrez_gene_id TEXT,
              uniprot_ids_json TEXT NOT NULL,
              alias_symbols_json TEXT NOT NULL,
              prev_symbols_json TEXT NOT NULL
            );
            CREATE TABLE gene_aliases (
              species TEXT NOT NULL,
              token TEXT NOT NULL,
              match_type TEXT NOT NULL,
              priority INTEGER NOT NULL,
              hgnc_id TEXT NOT NULL,
              canonical_symbol TEXT NOT NULL
            );
            CREATE INDEX idx_gene_aliases_lookup ON gene_aliases(species, token, priority DESC);
            """
        )

        reader = csv.DictReader(raw_path.read_text(encoding="utf-8").splitlines(), delimiter="\t")
        gene_rows: list[tuple[Any, ...]] = []
        alias_rows: list[tuple[Any, ...]] = []
        seen_aliases: set[tuple[str, str, str]] = set()
        gene_count = 0

        for row in reader:
            status = clean_text(row.get("status"))
            symbol = clean_text(row.get("symbol")).upper()
            hgnc_id = clean_text(row.get("hgnc_id"))
            if not symbol or not hgnc_id:
                continue
            alias_symbols = _split_multivalue(row.get("alias_symbol"))
            prev_symbols = _split_multivalue(row.get("prev_symbol"))
            uniprot_ids = _split_multivalue(row.get("uniprot_ids"))
            ensembl_gene_id = clean_text(row.get("ensembl_gene_id")) or None
            entrez_gene_id = clean_text(row.get("entrez_id")) or None

            gene_rows.append(
                (
                    hgnc_id,
                    symbol,
                    clean_text(row.get("name")) or None,
                    status or None,
                    clean_text(row.get("locus_group")) or None,
                    clean_text(row.get("locus_type")) or None,
                    ensembl_gene_id,
                    entrez_gene_id,
                    json.dumps(uniprot_ids),
                    json.dumps(alias_symbols),
                    json.dumps(prev_symbols),
                )
            )
            gene_count += 1

            for match_type, priority, values in (
                ("symbol", 100, [symbol]),
                ("previous_symbol", 90, prev_symbols),
                ("alias", 80, alias_symbols),
                ("ensembl_gene_id", 70, [ensembl_gene_id] if ensembl_gene_id else []),
                ("entrez_gene_id", 70, [entrez_gene_id] if entrez_gene_id else []),
                ("uniprot_id", 70, uniprot_ids),
            ):
                for value in values:
                    token = clean_text(value).upper()
                    if not token:
                        continue
                    alias_key = ("human", token, hgnc_id)
                    if alias_key in seen_aliases:
                        continue
                    seen_aliases.add(alias_key)
                    alias_rows.append(("human", token, match_type, priority, hgnc_id, symbol))

        conn.executemany(
            """
            INSERT INTO genes (
              hgnc_id, symbol, name, status, locus_group, locus_type,
              ensembl_gene_id, entrez_gene_id, uniprot_ids_json,
              alias_symbols_json, prev_symbols_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            gene_rows,
        )
        conn.executemany(
            """
            INSERT INTO gene_aliases (
              species, token, match_type, priority, hgnc_id, canonical_symbol
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            alias_rows,
        )
        conn.commit()
        return gene_count, len(alias_rows)
    finally:
        conn.close()


def _split_multivalue(value: object) -> list[str]:
    token = str(value or "").strip()
    if not token:
        return []
    return [part.strip() for part in token.split("|") if part.strip()]


def _normalize_species_token(species: str | None) -> str:
    return clean_text(species).lower().replace(" ", "_").replace("-", "_")


def _match_payload_from_row(row: sqlite3.Row, query: str) -> dict[str, Any]:
    return {
        "query": clean_text(query),
        "symbol": str(row["symbol"]),
        "name": str(row["name"] or "") or None,
        "summary": None,
        "ensembl_gene_id": str(row["ensembl_gene_id"] or "") or None,
        "entrez_gene_id": str(row["entrez_gene_id"] or "") or None,
        "aliases": json.loads(str(row["alias_symbols_json"] or "[]")),
        "uniprot_ids": json.loads(str(row["uniprot_ids_json"] or "[]")),
        "genomic_position": None,
        "biotype": str(row["locus_type"] or "") or None,
        "score": float(row["priority"]),
        "matched_on": str(row["match_type"] or "") or None,
        "matched_value": str(row["token"] or "") or None,
        "reference_status": str(row["status"] or "") or None,
        "hgnc_id": str(row["hgnc_id"] or "") or None,
    }
