import os
import discord

# ─── Bot Settings ─────────────────────────────────────────────────────────────
DISCORD_TOKEN   = os.getenv("DISCORD_BOT_TOKEN")
MONGO_URI       = os.getenv("MONGO_URI")
MONGO_DB_NAME   = os.getenv("MONGO_DB_NAME", "poketwo")
MONGO_COLLECTION = os.getenv("MONGO_COLLECTION", "auctions")

COMMAND_PREFIX  = ["n!", "N!","a!", "A!"]

# ─── Pagination ───────────────────────────────────────────────────────────────
RESULTS_PER_PAGE = 10

# ─── Colors (must be discord.Colour for Components V2 accent_colour) ─────────
EMBED_COLOR = discord.Colour(0x1E1F22)  # default / normal
SHINY_EMBED_COLOR  = discord.Colour(0xfe9ac9)   # shiny pokemon
GMAX_EMBED_COLOR   = discord.Colour(0xe65100)   # gigantamax

# ─── Pokemon CDN ──────────────────────────────────────────────────────────────
# Normal sprite:  CDN_BASE_URL.format(cdn_number)
# Shiny sprite:   CDN_SHINY_URL.format(cdn_number)
CDN_BASE_URL  = "https://cdn.poketwo.net/images/{}.png"
CDN_SHINY_URL = "https://cdn.poketwo.net/shiny/{}.png"

# ─── Gender Emojis ────────────────────────────────────────────────────────────
# Set to None or empty string to use defaults (♂️ ♀️ ❔)
EMOJI_MALE    = "<:male:1475429567530405959>"   # e.g. "<:male:1234567890>"
EMOJI_FEMALE  = "<:female:1475429581816070145>"   # e.g. "<:female:1234567890>"
EMOJI_UNKNOWN = "<:unknown:1475429593853989006>"   # e.g. "<:unknown:1234567890>"
REPLY   = "<:reply:1475429605870534719>"

# Fallback defaults if custom emojis not configured above
_DEFAULT_MALE    = "♂️"
_DEFAULT_FEMALE  = "♀️"
_DEFAULT_UNKNOWN = "❔"

def get_gender_emoji(gender: str | None) -> str:
    """Return configured emoji for gender, falling back to defaults."""
    if gender is None:
        return ""  # pre-gender era record — show nothing
    g = gender.lower()
    if g == "male":
        return EMOJI_MALE or _DEFAULT_MALE
    if g == "female":
        return EMOJI_FEMALE or _DEFAULT_FEMALE
    return EMOJI_UNKNOWN or _DEFAULT_UNKNOWN  # "Unknown" / genderless

# ─── Data Paths ───────────────────────────────────────────────────────────────
import pathlib
DATA_DIR              = pathlib.Path(__file__).parent / "data"
POKEMON_NAMES_FILE    = DATA_DIR / "pokemon_names.json"
EVOLUTION_CSV_FILE    = DATA_DIR / "evolution.csv"
CDN_MAPPING_CSV_FILE  = DATA_DIR / "pokemon_cdn_mapping.csv"
EVENT_NAMES_FILE = DATA_DIR / "event_names.json"  # adjust path to match where you put it
POKEMON_DATA_CSV_FILE = DATA_DIR / "pokemon_data.csv"


# ─── IV Bar Settings ──────────────────────────────────────────────────────────
IV_BAR_FILLED  = "█"
IV_BAR_EMPTY   = "░"

# ─── IV Bar Emojis ────────────────────────────────────────────────────────────
FILLED_START = "<:1_:1477903265415823472>"
FILLED_MID   = "<:2_:1477903260621738016>"
FILLED_END   = "<:3_:1477903258130448554>"
EMPTY_START  = "<:4_:1477903263335710842>"
EMPTY_MID    = "<:5_:1477903253768507503>"
EMPTY_END    = "<:6_:1477903255865397401>"

IV_BAR_LENGTH = 13  # total segments (start + mids + end)
