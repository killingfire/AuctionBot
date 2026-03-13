"""
utils.py – shared utilities for name resolution, evo families, query building.

All MongoDB field names use the shortened schema:
  mid  = message_id          aid  = auction_id
  ts   = unix_timestamp      pn   = pokemon_name
  lv   = level               sh   = shiny
  gx   = gmax                nat  = nature
  gen  = gender              hi   = held_item
  iv   = total_iv_percent    hp/atk/def/spa/spd/spe = individual IVs
  mv   = moves               bid  = winning_bid
  bdr  = bidder_id           sn   = seller_name
  sid  = seller_id
"""
from __future__ import annotations

import csv
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
import config

from config import (
    POKEMON_NAMES_FILE, EVENT_NAMES_FILE, EVOLUTION_CSV_FILE, CDN_MAPPING_CSV_FILE,
    POKEMON_DATA_CSV_FILE,
    CDN_BASE_URL, CDN_SHINY_URL,
    IV_BAR_LENGTH,
    FILLED_START, FILLED_MID, FILLED_END,
    EMPTY_START, EMPTY_MID, EMPTY_END,
    get_gender_emoji,
)
from filters import (
    resolve_flag, get_flag_info, is_flag,
    resolve_category_shortcut, is_category_shortcut,
)

# Path to the forms CSV
POKEMON_FORMS_FILE = Path("data/pokemon_forms.csv")


# ─────────────────────────────────────────────────────────────────────────────
# TEXT NORMALIZATION
# ─────────────────────────────────────────────────────────────────────────────

def normalize(s: str) -> str:
    """Lowercase + strip accents (é→e, etc.). NFC first to handle precomposed chars."""
    s = unicodedata.normalize('NFC', s)
    return ''.join(
        c for c in unicodedata.normalize('NFD', s.lower())
        if unicodedata.category(c) != 'Mn'
    )


# ─────────────────────────────────────────────────────────────────────────────
# POKEMON NAME DATA  (loaded once)
# ─────────────────────────────────────────────────────────────────────────────

class PokemonNameDB:
    """Maps every name/alias (all languages, normalized) → canonical English name."""

    def __init__(self, json_path: Path, extra_paths: list[Path] | None = None):
        self._map: dict[str, str] = {}
        self._load(json_path)
        for p in (extra_paths or []):
            if p.exists():
                with open(p, encoding="utf-8") as f:
                    self._load_entries(json.load(f))
            else:
                print(f"[WARN] Extra names file not found at {p}")

    def _load(self, path: Path):
        if not path.exists():
            print(f"[WARN] pokemon_names.json not found at {path}")
            return
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self._load_entries(data)

    def _load_entries(self, data: list):
        """Load a list of name entries into the map."""
        for entry in data:
            canonical = entry.get("name", "")
            if not canonical:
                continue
            self._map[normalize(canonical)] = canonical
            for _lang, value in entry.get("other_names", {}).items():
                if isinstance(value, list):
                    for v in value:
                        self._map[normalize(str(v))] = canonical
                elif value:
                    self._map[normalize(str(value))] = canonical

    def resolve(self, user_input: str) -> str | None:
        """
        Resolve user_input to a canonical English name.
        Exact normalized match only — no partial matching.
        """
        return self._map.get(normalize(user_input))

    def all_names(self) -> list[str]:
        return list(set(self._map.values()))


_name_db: PokemonNameDB | None = None

def get_name_db() -> PokemonNameDB:
    global _name_db
    if _name_db is None:
        _name_db = PokemonNameDB(POKEMON_NAMES_FILE, extra_paths=[EVENT_NAMES_FILE])
    return _name_db


def resolve_pokemon_name(user_input: str) -> str | None:
    return get_name_db().resolve(user_input)


# ─────────────────────────────────────────────────────────────────────────────
# POKEMON FORMS DB  (loaded once from pokemon_forms.csv)
# ─────────────────────────────────────────────────────────────────────────────

class FormsDB:
    """
    Loads data/pokemon_forms.csv  (2 columns: base_name, forms).
    Column 2 is a comma-separated list of form names (may be quoted).

    Provides:
      resolve_name_to_forms(user_input) -> set[str]

    Rules:
      • If user_input matches a BASE name exactly
          → return {base} ∪ {all its forms}
      • If user_input matches a FORM name exactly
          → return {that form only}
      • Otherwise
          → return empty set  (caller falls back to substring search)

    Matching is normalised (case-insensitive, accent-stripped).
    """

    def __init__(self, csv_path: Path):
        # norm(base_name) → canonical base name
        self._base_map:   dict[str, str]       = {}
        # norm(base_name) → frozenset of canonical form names
        self._forms_map:  dict[str, frozenset[str]] = {}
        # norm(form_name) → canonical form name
        self._form_map:   dict[str, str]       = {}
        # norm(form_name) → norm(base_name) it belongs to
        self._form_to_base: dict[str, str]     = {}

        self._load(csv_path)

    def _load(self, path: Path):
        if not path.exists():
            print(f"[WARN] pokemon_forms.csv not found at {path}")
            return

        with open(path, encoding="utf-8") as f:
            reader = csv.reader(f)
            for row in reader:
                if not row:
                    continue

                base_raw = row[0].strip()
                if not base_raw:
                    continue

                base_norm = normalize(base_raw)
                self._base_map[base_norm] = base_raw

                # Parse forms from column 2 (comma-separated, already handled
                # by csv.reader which strips the outer quotes)
                forms: set[str] = set()
                if len(row) > 1 and row[1].strip():
                    for f in row[1].split(","):
                        f = f.strip()
                        if f:
                            forms.add(f)
                            fn = normalize(f)
                            self._form_map[fn] = f
                            self._form_to_base[fn] = base_norm

                self._forms_map[base_norm] = frozenset(forms)

    # ── Public API ─────────────────────────────────────────────────────────

    def resolve_name_to_forms(self, user_input: str) -> set[str]:
        """
        Given user input, return the set of canonical names to search for.

        Base name  → base + all its forms
        Form name  → only that exact form
        No match   → empty set
        """
        key = normalize(user_input)

        # 1. Exact base match
        if key in self._base_map:
            base = self._base_map[key]
            forms = self._forms_map.get(key, frozenset())
            return {base} | set(forms)

        # 2. Exact form match
        if key in self._form_map:
            return {self._form_map[key]}

        # 3. No match
        return set()

    def all_names(self) -> set[str]:
        """Return every canonical name (bases + forms) known to this DB."""
        result: set[str] = set(self._base_map.values())
        for forms in self._forms_map.values():
            result |= set(forms)
        return result


_forms_db: FormsDB | None = None

def get_forms_db() -> FormsDB:
    global _forms_db
    if _forms_db is None:
        _forms_db = FormsDB(POKEMON_FORMS_FILE)
    return _forms_db


# ─────────────────────────────────────────────────────────────────────────────
# POKEMON DATA  (dex number, types, region — loaded once from pokemon_data.csv)
# ─────────────────────────────────────────────────────────────────────────────

class PokemonDataDB:
    """
    Loads pokemon_data.csv with columns:
      dex_number, name, region, type1, type2

    Provides:
      get_dex_number(canonical_name) → int | None
      get_names_by_dex(dex_number)   → list[str]   all canonical names sharing that dex
      get_names_by_type(types)       → list[str]   names matching ALL given types
      get_names_by_region(region)    → list[str]   names in that region
    """

    def __init__(self, csv_path: Path, name_db: PokemonNameDB):
        # canonical_name (lower) → dex_number
        self._name_to_dex: dict[str, int] = {}
        # dex_number → set of canonical names
        self._dex_to_names: dict[int, set[str]] = {}
        # canonical_name (lower) → (type1_lower, type2_lower|"")
        self._name_to_types: dict[str, tuple[str, str]] = {}
        # canonical_name (lower) → region_lower
        self._name_to_region: dict[str, str] = {}

        self._load(csv_path, name_db)

    def _load(self, path: Path, name_db: PokemonNameDB):
        if not path.exists():
            print(f"[WARN] pokemon_data.csv not found at {path}")
            return

        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                raw_dex  = row.get("dex_number", "").strip()
                raw_name = row.get("name", "").strip()
                region   = row.get("region", "").strip()
                type1    = row.get("type1", "").strip()
                type2    = row.get("type2", "").strip()

                if not raw_dex.lstrip("-").isdigit() or not raw_name:
                    continue

                dex = int(raw_dex)

                # Try to resolve to canonical name via name_db; fall back to raw
                canonical = name_db.resolve(raw_name) or raw_name
                key = normalize(canonical)

                self._name_to_dex[key] = dex
                self._dex_to_names.setdefault(dex, set()).add(canonical)
                self._name_to_types[key] = (type1.lower(), type2.lower())
                self._name_to_region[key] = region.lower()

    # ── Public API ─────────────────────────────────────────────────────────

    def get_dex_number(self, canonical_name: str) -> int | None:
        return self._name_to_dex.get(normalize(canonical_name))

    def get_names_by_dex(self, dex_number: int) -> list[str]:
        """Return all canonical names that share this dex number."""
        return list(self._dex_to_names.get(dex_number, set()))

    def get_names_by_type(self, types: list[str]) -> list[str]:
        """
        Return canonical names that have ALL given types (1 or 2 types).
        Type matching is case-insensitive. A mon is included if every
        requested type appears in its (type1, type2) tuple.
        """
        wanted = [t.lower() for t in types]
        result = []
        for key, (t1, t2) in self._name_to_types.items():
            mon_types = {t1, t2} - {""}
            if all(w in mon_types for w in wanted):
                # Recover canonical name from dex map
                dex = self._name_to_dex.get(key)
                if dex is not None:
                    for name in self._dex_to_names.get(dex, set()):
                        if normalize(name) == key:
                            result.append(name)
                            break
        return result

    def get_names_by_region(self, region: str) -> list[str]:
        """Return all canonical names in the given region (case-insensitive)."""
        wanted = region.lower()
        result = []
        for key, reg in self._name_to_region.items():
            if reg == wanted:
                dex = self._name_to_dex.get(key)
                if dex is not None:
                    for name in self._dex_to_names.get(dex, set()):
                        if normalize(name) == key:
                            result.append(name)
                            break
        return result


_pokemon_data_db: PokemonDataDB | None = None

def get_pokemon_data_db() -> PokemonDataDB:
    global _pokemon_data_db
    if _pokemon_data_db is None:
        _pokemon_data_db = PokemonDataDB(POKEMON_DATA_CSV_FILE, get_name_db())
    return _pokemon_data_db


def get_dex_family_names(canonical_name: str) -> list[str]:
    """
    Return all canonical names that share the same dex number as
    canonical_name (i.e. all forms/variants of that species).
    """
    db  = get_pokemon_data_db()
    dex = db.get_dex_number(canonical_name)
    if dex is None:
        return [canonical_name]
    names = db.get_names_by_dex(dex)
    return names if names else [canonical_name]


# ─────────────────────────────────────────────────────────────────────────────
# EVOLUTION FAMILIES  (loaded once)
# ─────────────────────────────────────────────────────────────────────────────

class EvolutionDB:
    def __init__(self, csv_path: Path, name_db: PokemonNameDB):
        self._family: dict[str, frozenset[str]] = {}
        self._load(csv_path, name_db)

    def _load(self, path: Path, name_db: PokemonNameDB):
        if not path.exists():
            print(f"[WARN] evolution.csv not found at {path}")
            return

        families: list[frozenset[str]] = []

        with open(path, encoding="utf-8") as f:
            reader = csv.reader(f)
            next(reader, None)

            for row in reader:
                if len(row) < 2:
                    continue
                members: set[str] = set()
                for cell in row[1:]:
                    cell = cell.strip()
                    if not cell or cell.startswith("Pokemon"):
                        continue
                    canonical = name_db.resolve(cell) or cell
                    members.add(canonical)

                if members:
                    families.append(frozenset(members))

        for fam in families:
            for name in fam:
                existing = self._family.get(name)
                if existing is None or len(fam) < len(existing):
                    self._family[name] = fam

    def get_family(self, canonical_name: str) -> frozenset[str] | None:
        return self._family.get(canonical_name)


_evo_db: EvolutionDB | None = None

def get_evo_db() -> EvolutionDB:
    global _evo_db
    if _evo_db is None:
        _evo_db = EvolutionDB(EVOLUTION_CSV_FILE, get_name_db())
    return _evo_db


def get_evo_family(user_input: str) -> frozenset[str] | None:
    canonical = resolve_pokemon_name(user_input) or user_input
    return get_evo_db().get_family(canonical)


# ─────────────────────────────────────────────────────────────────────────────
# CDN MAPPING  (loaded once)
# ─────────────────────────────────────────────────────────────────────────────

class CdnDB:
    def __init__(self, csv_path: Path):
        self._map: dict[str, int] = {}
        self._load(csv_path)

    def _load(self, path: Path):
        if not path.exists():
            print(f"[WARN] pokemon_cdn_mapping.csv not found at {path}")
            return
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = row.get("name", "").strip()
                cdn  = row.get("cdn_number", "").strip()
                if name and cdn.isdigit():
                    self._map[normalize(name)] = int(cdn)

    def get_cdn(self, canonical_name: str) -> int | None:
        return self._map.get(normalize(canonical_name))


_cdn_db: CdnDB | None = None

def get_cdn_db() -> CdnDB:
    global _cdn_db
    if _cdn_db is None:
        _cdn_db = CdnDB(CDN_MAPPING_CSV_FILE)
    return _cdn_db


def get_pokemon_image_url(pokemon_name: str, shiny: bool = False) -> str | None:
    cdn_num = get_cdn_db().get_cdn(pokemon_name)
    if cdn_num is None:
        return None
    return (CDN_SHINY_URL if shiny else CDN_BASE_URL).format(cdn_num)


# ─────────────────────────────────────────────────────────────────────────────
# NUMERIC OPERATOR PARSER
# ─────────────────────────────────────────────────────────────────────────────

def parse_numeric_operator(val: str, field: str) -> dict | None:
    """
    Parse expressions like:
      31          → {field: {"$eq": 31}}
      >30         → {field: {"$gt": 30}}
      >=30        → {field: {"$gte": 30}}
      <100        → {field: {"$lt": 100}}
      <=100       → {field: {"$lte": 100}}
      =100        → {field: {"$eq": 100}}
      30-100      → {field: {"$gte": 30, "$lte": 100}}
    """
    val = val.strip().replace(",", "")

    try:
        if val.startswith(">="):
            return {field: {"$gte": float(val[2:])}}
        if val.startswith("<="):
            return {field: {"$lte": float(val[2:])}}
        if val.startswith(">"):
            return {field: {"$gt": float(val[1:])}}
        if val.startswith("<"):
            return {field: {"$lt": float(val[1:])}}
        if val.startswith("="):
            return {field: {"$eq": float(val[1:])}}
        if "-" in val and not val.startswith("-"):
            lo, hi = val.split("-", 1)
            return {field: {"$gte": float(lo), "$lte": float(hi)}}
        return {field: {"$eq": float(val)}}  # fallback: exact
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# MULTI-IV COUNT QUERY BUILDER
# ─────────────────────────────────────────────────────────────────────────────

_IV_FIELDS = ("hp", "atk", "def", "spa", "spd", "spe")


def build_iv_count_query(iv_value: str, min_count: int) -> dict | None:
    """
    Return a MongoDB $expr clause that asserts at least `min_count` of the
    six individual IV fields equal `iv_value`.
    """
    raw = iv_value.strip().replace(",", "")
    try:
        target = int(raw)
    except ValueError:
        return None

    if not (0 <= target <= 31):
        return None

    cond_exprs = [
        {"$cond": [{"$eq": [f"${field}", target]}, 1, 0]}
        for field in _IV_FIELDS
    ]

    return {
        "$expr": {
            "$gte": [
                {"$sum": cond_exprs},
                min_count,
            ]
        }
    }


# ─────────────────────────────────────────────────────────────────────────────
# TOKEN READER  (handles multi-word flag values)
# ─────────────────────────────────────────────────────────────────────────────

class TokenReader:
    def __init__(self, tokens: list[str]):
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def next(self) -> str | None:
        t = self.peek()
        if t is not None:
            self.pos += 1
        return t

    def read_value_greedy(self) -> str:
        """
        Consume tokens until the next flag (or category shortcut) or end,
        joining with spaces.
        """
        parts = []
        while self.pos < len(self.tokens):
            tok = self.tokens[self.pos]
            if is_flag(tok) or is_category_shortcut(tok):
                break
            parts.append(self.next())
        return " ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# EXCLUDE HELPER
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_exclude_names(val: str, expand_name_by_dex: bool) -> set[str]:
    """
    Given a value from --exclude / --ex, return the set of canonical Pokémon
    names to subtract from the final result.

    Resolution order (first match wins):
      1. Category  (e.g. "event", "rares", "legendary")
      2. Evo family (e.g. "pikachu" → entire evo line)
      3. Type      (e.g. "fire", "water")
      4. Region    (e.g. "kanto", "galar")
      5. Name      (exact canonical name, expanded by forms if expand_name_by_dex)
    """
    from categories import resolve_category as _resolve_cat

    val = val.strip()

    # 1. Category
    cat = _resolve_cat(val)
    if cat:
        return set(cat["pokemon"])

    # 2. Evo family
    family = get_evo_family(val)
    if family:
        return set(family)

    # 3. Type
    type_names = get_pokemon_data_db().get_names_by_type([val])
    if type_names:
        return set(type_names)

    # 4. Region
    region_names = get_pokemon_data_db().get_names_by_region(val)
    if region_names:
        return set(region_names)

    # 5. Name — use FormsDB when expand_name_by_dex, else single canonical
    if expand_name_by_dex:
        forms_result = get_forms_db().resolve_name_to_forms(val)
        if forms_result:
            return forms_result

    resolved = resolve_pokemon_name(val)
    if resolved:
        return {resolved}

    # No match found — return empty set
    return set()


# ─────────────────────────────────────────────────────────────────────────────
# QUERY BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def build_query(
    raw_args: list[str],
    expand_name_by_dex: bool = False,
) -> tuple[dict, list, int | None]:
    """
    Parse raw CLI tokens into a (mongo_query_dict, mongo_sort_list, limit).

    Name-pool logic
    ───────────────
    Three independent sets are built, then combined at the end:

    name_pool  – UNION set, grown by --name and --evo.
                 Each new --name / --evo adds its names to the pool.
                 If the pool is empty at the end, there is no pn filter.

    narrow_set – INTERSECTION set, built by --category / --type / --region.
                 Each narrowing filter intersects the running set.
                 If no narrowing filters were given, narrow_set stays None.

    exclude_set – SUBTRACTION set, grown by --exclude / --ex.
                  Resolved against categories → evo families → types →
                  regions → names (first match wins per value).
                  Subtracted from the final pn set at the very end.
                  If no pool/narrow filter exists, emits a $nin clause instead.

    Final pn filter:
      • Neither pool nor narrow, no exclude  → no pn filter (match everything)
      • Only name_pool                        → pn.$in = name_pool − exclude_set
      • Only narrow_set                       → pn.$in = narrow_set − exclude_set
      • Both                                  → pn.$in = (name_pool ∩ narrow_set) − exclude_set
      • Only exclude_set                      → pn.$nin = exclude_set

    --name expansion when expand_name_by_dex=True
    ──────────────────────────────────────────────
    Uses FormsDB (data/pokemon_forms.csv) instead of the dex-number approach:
      • Base name  (e.g. "blastoise")        → base + all its forms
      • Form name  (e.g. "mega blastoise")   → only that exact form
      • No match in FormsDB                  → falls back to substring search

    Parameters
    ----------
    raw_args : list[str]
        Tokenised input (result of filters.split()).
    expand_name_by_dex : bool
        When True (auction cog), --name uses FormsDB to expand base names to
        all their forms, or returns only the exact form if a form name is given.
        When False (other cogs), resolves to a single canonical name only.

    Returns
    -------
    (query, sort, limit)  — limit is None if --limit was not specified.
    """
    from categories import resolve_category

    query: dict        = {}
    sort: list         = [("ts", -1)]   # default: newest first
    and_clauses: list  = []
    limit: int | None  = None

    # ── Name resolution accumulators ──────────────────────────────────────────
    name_pool:   set[str] | None = None
    narrow_set:  set[str] | None = None
    exclude_set: set[str]        = set()
    type_filters: list[str]      = []

    def _pool_add(names: set[str]) -> None:
        nonlocal name_pool
        if name_pool is None:
            name_pool = set(names)
        else:
            name_pool |= names

    def _narrow_intersect(names: set[str]) -> None:
        nonlocal narrow_set
        if narrow_set is None:
            narrow_set = set(names)
        else:
            narrow_set &= names

    # ── Token loop ────────────────────────────────────────────────────────────
    reader = TokenReader(raw_args)

    while reader.peek() is not None:
        token = reader.next()

        # ── Category shortcut ─────────────────────────────────────────────────
        cat_key = resolve_category_shortcut(token)
        if cat_key is not None:
            cat = resolve_category(cat_key)
            if cat:
                _narrow_intersect(set(cat["pokemon"]))
            continue

        canonical = resolve_flag(token)
        if canonical is None:
            continue

        info = get_flag_info(canonical)

        # ── Boolean flags ─────────────────────────────────────────────────────
        if not info.get("takes_arg"):
            mongo_field = info.get("mongo_field")
            if mongo_field:
                if canonical in ("--noshiny", "--nogmax"):
                    query[mongo_field] = {"$ne": True}
                else:
                    query[mongo_field] = True
            continue

        val = reader.read_value_greedy()
        if not val:
            continue

        # ── Limit ─────────────────────────────────────────────────────────────
        if canonical == "--limit":
            try:
                limit = max(1, int(val.strip()))
            except ValueError:
                pass
            continue

        # ── Sort ──────────────────────────────────────────────────────────────
        if canonical == "--sort":
            sv = val.lower().strip()
            sort_field_map = {
                "iv":    "iv",
                "price": "bid",
                "level": "lv",
                "date":  "ts",
                "id":    "aid",
            }
            if sv.endswith("+"):
                direction, key = 1, sv[:-1]
            elif sv.endswith("-"):
                direction, key = -1, sv[:-1]
            else:
                direction, key = -1, sv
            field = sort_field_map.get(key)
            if field:
                sort = [(field, direction)]
            continue

        # ── Category → INTERSECT into narrow_set ─────────────────────────────
        if canonical == "--category":
            cat = resolve_category(val)
            if cat:
                _narrow_intersect(set(cat["pokemon"]))
            continue

        # ── Evo family → UNION into name_pool ─────────────────────────────────
        if canonical == "--evo":
            family = get_evo_family(val)
            if family:
                _pool_add(set(family))
            continue

        # ── Type → INTERSECT into narrow_set ─────────────────────────────────
        if canonical == "--type":
            if len(type_filters) < 2:
                type_filters.append(val.strip())
            type_names = set(get_pokemon_data_db().get_names_by_type(type_filters))
            if type_names:
                if narrow_set is not None:
                    narrow_set = narrow_set & type_names
                else:
                    narrow_set = type_names
            continue

        # ── Region → INTERSECT into narrow_set ───────────────────────────────
        if canonical == "--region":
            names = get_pokemon_data_db().get_names_by_region(val.strip())
            if names:
                _narrow_intersect(set(names))
            continue

        # ── Exclude ───────────────────────────────────────────────────────────
        if canonical == "--exclude":
            excluded = _resolve_exclude_names(val, expand_name_by_dex)
            if excluded:
                exclude_set.update(excluded)
            continue

        # ── Name → UNION into name_pool ───────────────────────────────────────
        if canonical == "--name":
            if expand_name_by_dex:
                # Strip trailing 'only' keyword (case-insensitive)
                # e.g. "blastoise only" → exact=True, val="blastoise"
                exact_only = False
                val_stripped = val.strip()
                if val_stripped.lower().endswith(" only"):
                    exact_only = True
                    val_stripped = val_stripped[:-5].strip()  # remove " only"

                if exact_only:
                    # Resolve to a single canonical name, no form expansion
                    resolved = resolve_pokemon_name(val_stripped)
                    if resolved:
                        _pool_add({resolved})
                else:
                    # ── FormsDB-based expansion (default) ────────────────────
                    # 1. Try FormsDB first (handles base names and exact form names)
                    forms_result = get_forms_db().resolve_name_to_forms(val_stripped)
                    if forms_result:
                        _pool_add(forms_result)
                    else:
                        # 2. Fallback: try exact name resolution, then expand through FormsDB
                        resolved = resolve_pokemon_name(val_stripped)
                        if resolved:
                            sub_forms = get_forms_db().resolve_name_to_forms(resolved)
                            _pool_add(sub_forms if sub_forms else {resolved})
            else:
                # ── Original single-name resolution (non-auction cogs) ────────
                resolved = resolve_pokemon_name(val)
                if resolved:
                    _pool_add({resolved})
            continue

        # ── Nature ────────────────────────────────────────────────────────────
        if canonical == "--nature":
            query["nat"] = {"$regex": f"^{re.escape(val)}$", "$options": "i"}
            continue

        # ── Move (stackable) ──────────────────────────────────────────────────
        if canonical == "--move":
            and_clauses.append({
                "mv": {"$elemMatch": {"$regex": f"^{re.escape(val)}$", "$options": "i"}}
            })
            continue

        # ── Gender ────────────────────────────────────────────────────────────
        if canonical == "--gender":
            gender_map = {
                "male":    "Male",
                "m":       "Male",
                "female":  "Female",
                "f":       "Female",
                "unknown": "Unknown",
                "unk":     "Unknown",
                "none":    "Unknown",
            }
            mapped = gender_map.get(val.strip().lower())
            if mapped:
                query["gen"] = {"$regex": f"^{re.escape(mapped)}$", "$options": "i"}
            else:
                query["gen"] = {"$regex": re.escape(val), "$options": "i"}
            continue

        # ── Bidder ────────────────────────────────────────────────────────────
        if canonical == "--bidder":
            clean = val.strip("<@!>")
            if clean.isdigit():
                query["bdr"] = int(clean)
            continue

        # ── Seller ────────────────────────────────────────────────────────────
        if canonical == "--seller":
            clean = val.strip("<@!>")
            if clean.isdigit():
                query["sid"] = int(clean)
            else:
                query["sn"] = {"$regex": re.escape(val), "$options": "i"}
            continue

        # ── Price ─────────────────────────────────────────────────────────────
        if canonical in ("--price", "--maxprice", "--minprice"):
            if canonical == "--maxprice" and not any(val.startswith(op) for op in (">", "<", "=")):
                val = "<=" + val
            elif canonical == "--minprice" and not any(val.startswith(op) for op in (">", "<", "=")):
                val = ">=" + val
            elif canonical == "--price" and not any(val.startswith(op) for op in (">", "<", "=")) and "-" not in val:
                val = "=" + val
            cond = parse_numeric_operator(val, "bid")
            if cond:
                existing = query.get("bid", {})
                existing.update(cond.get("bid", {}))
                query["bid"] = existing
            continue

        # ── Multi-IV count filters ─────────────────────────────────────────────
        if info.get("iv_count") is not None:
            clause = build_iv_count_query(val, info["iv_count"])
            if clause:
                and_clauses.append(clause)
            continue

        # ── Numeric IV / level fields ──────────────────────────────────────────
        mongo_field = info.get("mongo_field")
        if mongo_field:
            cond = parse_numeric_operator(val, mongo_field)
            if cond:
                existing = query.get(mongo_field, {})
                existing.update(cond.get(mongo_field, {}))
                query[mongo_field] = existing

    # ── Assemble final pn filter ──────────────────────────────────────────────
    if name_pool is not None or narrow_set is not None:
        if name_pool is not None and narrow_set is not None:
            final_names = name_pool & narrow_set
        elif name_pool is not None:
            final_names = name_pool
        else:
            final_names = narrow_set  # type: ignore[assignment]

        if exclude_set:
            final_names -= exclude_set

        query["pn"] = {"$in": list(final_names)}

    elif exclude_set:
        query["pn"] = {"$nin": list(exclude_set)}

    if and_clauses:
        query.setdefault("$and", []).extend(and_clauses)

    return query, sort, limit


# ─────────────────────────────────────────────────────────────────────────────
# DISPLAY HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def format_date(record: dict) -> str:
    ts = record.get("ts")
    if not ts:
        return "Unknown"
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%-m/%-d/%y")


def iv_bar(value: int | None, max_val: int = 31, length: int = 12) -> str:
    import math
    if value is None:
        value = 0

    inner_length = length - 1
    if value == 0:
        filled = 0
    else:
        filled = max(1, round((value / max_val) * inner_length))
    filled = min(filled, inner_length)

    last_filled = (value == max_val)
    total_filled = filled + (1 if last_filled else 0)

    inner = length - 2

    if total_filled == 0:
        bar = EMPTY_START + EMPTY_MID * inner + EMPTY_END
    elif total_filled == length:
        bar = FILLED_START + FILLED_MID * inner + FILLED_END
    else:
        filled_mids = max(0, total_filled - 1)
        empty_mids  = inner - filled_mids
        bar = (
            FILLED_START
            + FILLED_MID * filled_mids
            + EMPTY_MID  * empty_mids
            + EMPTY_END
        )
    return bar


def iv_line(label: str, value: int | None) -> str:
    val_s = str(value) if value is not None else "?"
    bar   = iv_bar(value)
    return f"`{label:>3}` {bar} `{val_s:>2}`"


def format_winning_bid(record: dict) -> str:
    bid = record.get("bid")
    return f"{bid:,} pc" if bid is not None else "??? pc"


def format_winning_bid_long(record: dict) -> str:
    bid = record.get("bid")
    return f"{bid:,} Pokécoins" if bid is not None else "???"


def shiny_prefix(record: dict) -> str:
    return "✨ " if record.get("sh") else ""
