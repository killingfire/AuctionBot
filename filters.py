"""
filters.py – centralised filter/flag definitions for auction search.

To add a new filter:
  1. Add an entry to FLAG_DEFINITIONS below.
  2. (Optional) Add aliases to its "aliases" list.
  3. Add handling logic in build_query() inside utils.py if needed.

Operator support for numeric fields:
  Bare number  → exact (or $gte for IV/price/level by convention)
  >n  >=n  <n  <=n  n-m  → range queries

mongo_field uses the SHORT schema field names:
  pn   = pokemon_name        lv   = level
  iv   = total_iv_percent    hp   = iv_hp
  atk  = iv_attack           def  = iv_defense
  spa  = iv_sp_atk           spd  = iv_sp_def
  spe  = iv_speed            bid  = winning_bid
  sh   = shiny               gx   = gmax
  nat  = nature              mv   = moves
  sn   = seller_name         sid  = seller_id
  bdr  = bidder_id
"""

from __future__ import annotations

# ─────────────────────────────────────────────────────────────────────────────
# FLAG DEFINITIONS
# Each entry:
#   "aliases"     – all names the user can type (primary name is the key)
#   "takes_arg"   – True if the flag consumes the next token(s), False if boolean
#   "multi"       – True if the flag may appear multiple times (e.g. --move)
#   "help"        – short description shown in the help command
#   "mongo_field" – the SHORT MongoDB document field name
#   "iv_count"    – (int) for multi-IV filters: how many IVs must match the value
# ─────────────────────────────────────────────────────────────────────────────

FLAG_DEFINITIONS: dict[str, dict] = {
    # ── Name ──────────────────────────────────────────────────────────────────
    "--name": {
        "aliases":     ["--n", "-n", "--pokemon", "--poke"],
        "takes_arg":   True,
        "multi":       False,
        "help":        "Pokémon name (exact canonical match, any language). In auction search, expands to all forms with the same dex number.",
        "mongo_field": "pn",       # handled specially in build_query (name resolution)
    },

    # ── Level ─────────────────────────────────────────────────────────────────
    "--level": {
        "aliases":     ["--l", "--lv", "--lvl"],
        "takes_arg":   True,
        "multi":       False,
        "help":        "Level (e.g. 50, >50, >=50, <100, 30-100)",
        "mongo_field": "lv",
    },

    # ── Total IV ──────────────────────────────────────────────────────────────
    "--iv": {
        "aliases":     ["--totaliv", "--total_iv", "--iv%", "--ivpercent"],
        "takes_arg":   True,
        "multi":       False,
        "help":        "Total IV % (e.g. 90, >90, >=85.5)",
        "mongo_field": "iv",
    },

    # ── Individual IVs ────────────────────────────────────────────────────────
    "--hpiv": {
        "aliases":     ["--hp", "--iv_hp", "--ivhp"],
        "takes_arg":   True,
        "multi":       False,
        "help":        "HP IV (e.g. 31, >20, >=25)",
        "mongo_field": "hp",
    },
    "--atkiv": {
        "aliases":     ["--atk", "--iv_atk", "--ivatk", "--attackiv", "--attack"],
        "takes_arg":   True,
        "multi":       False,
        "help":        "Attack IV",
        "mongo_field": "atk",
    },
    "--defiv": {
        "aliases":     ["--def", "--iv_def", "--ivdef", "--defenseiv", "--defense"],
        "takes_arg":   True,
        "multi":       False,
        "help":        "Defense IV",
        "mongo_field": "def",
    },
    "--spatkiv": {
        "aliases":     ["--spatk", "--spa", "--iv_spa", "--ivspa", "--spattackiv", "--spattack", "--sp_atk"],
        "takes_arg":   True,
        "multi":       False,
        "help":        "Sp. Attack IV",
        "mongo_field": "spa",
    },
    "--spdefiv": {
        "aliases":     ["--spdef", "--spd", "--iv_spd", "--ivspd", "--spdefenseiv", "--sp_def"],
        "takes_arg":   True,
        "multi":       False,
        "help":        "Sp. Defense IV",
        "mongo_field": "spd",
    },
    "--spdiv": {
        "aliases":     ["--spe", "--speed", "--iv_spe", "--ivspe", "--speediv", "--iv_speed"],
        "takes_arg":   True,
        "multi":       False,
        "help":        "Speed IV",
        "mongo_field": "spe",
    },

    # ── Multi-IV count filters ─────────────────────────────────────────────────
    "--triple": {
        "aliases":     ["--three", "--trip", "--tri"],
        "takes_arg":   True,
        "multi":       False,
        "help":        "At least 3 IVs equal this value (e.g. --triple 31, --triple 0)",
        "mongo_field": None,
        "iv_count":    3,
    },
    "--quadruple": {
        "aliases":     ["--four", "--quadra", "--quad", "--tetra"],
        "takes_arg":   True,
        "multi":       False,
        "help":        "At least 4 IVs equal this value (e.g. --quad 31)",
        "mongo_field": None,
        "iv_count":    4,
    },
    "--pentuple": {
        "aliases":     ["--quintuple", "--penta", "--pent", "--five"],
        "takes_arg":   True,
        "multi":       False,
        "help":        "At least 5 IVs equal this value (e.g. --penta 31)",
        "mongo_field": None,
        "iv_count":    5,
    },
    "--hextuple": {
        "aliases":     ["--sextuple", "--hexa", "--hex", "--six"],
        "takes_arg":   True,
        "multi":       False,
        "help":        "All 6 IVs equal this value (e.g. --hex 31 for perfect, --hex 0 for all-zero)",
        "mongo_field": None,
        "iv_count":    6,
    },

    # ── Price ─────────────────────────────────────────────────────────────────
    "--price": {
        "aliases":     ["--p","--bid","--winningbid"],
        "takes_arg":   True,
        "multi":       False,
        "help":        "Price filter (e.g. 5000, >5000, <10000, 500-5000)",
        "mongo_field": "bid",
    },
    "--maxprice": {
        "aliases":     ["--max_price", "--maxbid", "--max_bid"],
        "takes_arg":   True,
        "multi":       False,
        "help":        "Max price shorthand — same as --price <=N",
        "mongo_field": "bid",
    },
    "--minprice": {
        "aliases":     ["--min_price", "--minbid", "--min_bid"],
        "takes_arg":   True,
        "multi":       False,
        "help":        "Min price shorthand — same as --price >=N",
        "mongo_field": "bid",
    },

    # ── Booleans ──────────────────────────────────────────────────────────────
    "--shiny": {
        "aliases":     ["--sh", "--shinys"],
        "takes_arg":   False,
        "multi":       False,
        "help":        "Shiny Pokémon only",
        "mongo_field": "sh",
    },
    "--gmax": {
        "aliases":     ["--gigantamax", "--gm", "--giga"],
        "takes_arg":   False,
        "multi":       False,
        "help":        "Gigantamax Pokémon only",
        "mongo_field": "gx",
    },

    # ── Nature ────────────────────────────────────────────────────────────────
    "--nature": {
        "aliases":     ["-nat", "--nat"],
        "takes_arg":   True,
        "multi":       False,
        "help":        "Nature (exact, case-insensitive)",
        "mongo_field": "nat",
    },

    # ── Move ──────────────────────────────────────────────────────────────────
    "--move": {
        "aliases":     ["-m", "--moves", "--m"],
        "takes_arg":   True,
        "multi":       True,
        "help":        "Has this move (stackable, supports multi-word)",
        "mongo_field": "mv",
    },

    # ── Seller ────────────────────────────────────────────────────────────────
    "--seller": {
        "aliases":     ["--se", "--sold_by", "--soldby"],
        "takes_arg":   True,
        "multi":       False,
        "help":        "Seller — @mention or ID matches exactly; text matches seller name",
        "mongo_field": "sid",
    },

    # ── Bidder ────────────────────────────────────────────────────────────────
    "--bidder": {
        "aliases":     ["--b", "--buyer", "--won_by", "--wonby"],
        "takes_arg":   True,
        "multi":       False,
        "help":        "Bidder (Discord @mention or user ID)",
        "mongo_field": "bdr",
    },

    # ── Gender ────────────────────────────────────────────────────────────────
    "--gender": {
        "aliases":     ["--sex","--g"],
        "takes_arg":   True,
        "multi":       False,
        "help":        "Gender: male, female, unknown",
        "mongo_field": "gen",
    },

    # ── Type ──────────────────────────────────────────────────────────────────
    "--type": {
        "aliases":     ["--t", "--types"],
        "takes_arg":   True,
        "multi":       True,   # stackable up to 2 times
        "help":        "Filter by type (stackable up to 2, e.g. --type fire --type flying)",
        "mongo_field": None,   # handled specially in build_query
    },

    # ── Region ────────────────────────────────────────────────────────────────
    "--region": {
        "aliases":     ["--r", "--reg"],
        "takes_arg":   True,
        "multi":       False,
        "help":        "Filter by region (e.g. --region kanto, --region galar)",
        "mongo_field": None,   # handled specially in build_query
    },

    # ── Sort ──────────────────────────────────────────────────────────────────
    "--sort": {
        "aliases":     ["--orderby", "--order", "--or"],
        "takes_arg":   True,
        "multi":       False,
        "help": "Sort: iv+/iv- | bid+/bid-/price+/price- | level+/level- | date+/date- | id+/id- (default: date-)",
        "mongo_field": None,
    },

    # ── Exclusion booleans ────────────────────────────────────────────────────
    "--noshiny": {
        "aliases":     ["--nonshiny", "--excludeshiny", "--nosh"],
        "takes_arg":   False,
        "multi":       False,
        "help":        "Exclude shiny Pokémon",
        "mongo_field": "sh",
    },
    "--nogmax": {
        "aliases":     ["--nongmax", "--excludegmax", "--nogm"],
        "takes_arg":   False,
        "multi":       False,
        "help":        "Exclude Gigantamax Pokémon",
        "mongo_field": "gx",
    },

    # ── Category ──────────────────────────────────────────────────────────────
    "--category": {
        "aliases":     ["--c", "--cat", "--group"],
        "takes_arg":   True,
        "multi":       False,
        "help":        "Filter by category (e.g. --category rares, --rares, --starters)",
        "mongo_field": None,
    },

      # ── Exclude ───────────────────────────────────────────────────────────────
    "--exclude": {
        "aliases":     ["--ex", "--not", "--no", "--except", "--without"],
        "takes_arg":   True,
        "multi":       True,
        "help":        "Exclude by subcommand: --ex name <name> | --ex evo <name> | --ex type <type> | --ex region <region> | --ex category <cat>. Stackable.",
        "mongo_field": None,
    },

    # ── Evolution family ──────────────────────────────────────────────────────
    "--evo": {
        "aliases":     ["--evolution", "--family", "--fam"],
        "takes_arg":   True,
        "multi":       False,
        "help":        "Show only Pokémon in the same evo family as the given name",
        "mongo_field": None,
    },

    # ── Spawn rate ────────────────────────────────────────────────────────────
    "--spawnrate": {
        "aliases":     ["--sr", "--spawn_rate", "--spawn"],
        "takes_arg":   True,
        "multi":       False,
        "help":        "Filter by Pokétwo spawn rate (e.g. --sr 1/225, --sr 225, --sr 1/337)",
        "mongo_field": None,   # handled specially in build_query / graph
    },

    # ── Limit ─────────────────────────────────────────────────────────────────
    "--limit": {
        "aliases":     ["--lim", "--max", "--top"],
        "takes_arg":   True,
        "multi":       False,
        "help":        "Limit results to the N most recent matching records (e.g. --limit 10)",
        "mongo_field": None,   # handled specially in build_query / callers
    },
}


# ─── Fast alias → canonical flag map (built once at import time) ──────────────

def _build_alias_map() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for canonical, data in FLAG_DEFINITIONS.items():
        mapping[canonical.lower()] = canonical
        for alias in data.get("aliases", []):
            mapping[alias.lower()] = canonical
    return mapping

FLAG_ALIAS_MAP: dict[str, str] = _build_alias_map()


def resolve_flag(token: str) -> str | None:
    """Return the canonical flag name for a token, or None if unknown."""
    return FLAG_ALIAS_MAP.get(token.lower())


def is_flag(token: str) -> bool:
    """Return True if token maps to any known flag."""
    return token.lower() in FLAG_ALIAS_MAP


def get_flag_info(canonical: str) -> dict:
    """Return the definition dict for a canonical flag name."""
    return FLAG_DEFINITIONS.get(canonical, {})


def all_flags_help() -> list[dict]:
    """Return list of {flag, aliases, help} for help command rendering."""
    result = []
    for canonical, data in FLAG_DEFINITIONS.items():
        result.append({
            "flag":      canonical,
            "aliases":   data.get("aliases", []),
            "takes_arg": data.get("takes_arg", True),
            "help":      data.get("help", ""),
        })
    return result


# ─── Category shortcut support ───────────────────────────────────────────────
# Populated at runtime by categories.py via register_category_shortcuts().
# Maps "--<key>" and "--<alias>" → "--category" so the tokeniser treats them
# as flags that forward their key as the argument value.

_CATEGORY_SHORTCUT_MAP: dict[str, str] = {}   # token_lower → category key


def register_category_shortcuts(keys_and_aliases: list[tuple[str, list[str]]]) -> None:
    """
    Called once by categories.py after loading category data.
    keys_and_aliases: list of (category_key, [alias, ...])
    """
    global _CATEGORY_SHORTCUT_MAP
    for key, aliases in keys_and_aliases:
        _CATEGORY_SHORTCUT_MAP[f"--{key.lower()}"] = key
        for alias in aliases:
            _CATEGORY_SHORTCUT_MAP[f"--{alias.lower()}"] = key


def resolve_category_shortcut(token: str) -> str | None:
    """
    Return the category key if token is a registered category shortcut,
    otherwise None.
    """
    return _CATEGORY_SHORTCUT_MAP.get(token.lower())


def is_category_shortcut(token: str) -> bool:
    """Return True if token is a registered category shortcut flag."""
    return token.lower() in _CATEGORY_SHORTCUT_MAP
