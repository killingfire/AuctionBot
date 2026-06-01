import discord
from discord.ui import View, Button, Modal, TextInput
import json
import os
import re
import csv

# ── Config ───────────────────────────────────────────────────────────────────
TOKEN            = os.environ.get("DISCORD_TOKEN")
POKEMON_JSON     = "data/pokemon_data.json"
EVOLUTION_CSV    = "data/evolution.csv"
FORMS_CSV        = "data/pokemon_forms.csv"
DEX_CSV          = "data/dex_number.csv"
EGG_CSV          = "data/egg_groups.csv"
POKETWO_ID       = 716390085896962058
# ─────────────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


# ══════════════════════════════════════════════════════════════════════════════
#  pokemon_data.json helpers
# ══════════════════════════════════════════════════════════════════════════════

def load_pokemon_json() -> dict:
    os.makedirs(os.path.dirname(POKEMON_JSON), exist_ok=True)
    if not os.path.exists(POKEMON_JSON):
        return {}
    with open(POKEMON_JSON, "r", encoding="utf-8") as f:
        return json.load(f)


def save_pokemon_json(data: dict) -> None:
    with open(POKEMON_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def already_in_json(dex_number: str, name: str) -> bool:
    data = load_pokemon_json()
    key = f"{dex_number}_{name}"
    return key in data


def add_to_pokemon_json(entry: dict) -> str:
    data = load_pokemon_json()
    key = f"{entry['dex_number']}_{entry['name']}"
    if key in data:
        return f"⚠️ `{entry['name']}` already in pokemon_data.json."
    data[key] = entry
    save_pokemon_json(data)
    return f"✅ Saved `#{entry['dex_number']} {entry['name']}` to pokemon_data.json."


# ══════════════════════════════════════════════════════════════════════════════
#  dex_number.csv helpers
# ══════════════════════════════════════════════════════════════════════════════

def load_dex_csv() -> list[dict]:
    if not os.path.exists(DEX_CSV):
        return []
    with open(DEX_CSV, "r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def save_dex_csv(rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(DEX_CSV), exist_ok=True)
    with open(DEX_CSV, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["Number", "Name", "Form"])
        writer.writeheader()
        writer.writerows(rows)


def add_to_dex_csv(dex_number: str, base_name: str, form_name: str = "") -> str:
    """
    For a base Pokémon:   Number=dex, Name=base_name, Form=""
    For a variant/form:   Number=dex, Name=base_name, Form=full_form_name
    """
    rows = load_dex_csv()
    # Check duplicate
    for r in rows:
        if r["Number"] == dex_number and r["Name"] == base_name and r.get("Form", "") == form_name:
            label = form_name if form_name else base_name
            return f"⚠️ `{label}` already in dex_number.csv."

    new_row = {"Number": dex_number, "Name": base_name, "Form": form_name}

    # Insert after the last row with the same dex number, if any exist
    same_dex_indices = [i for i, r in enumerate(rows) if r["Number"] == dex_number]
    if same_dex_indices:
        insert_at = same_dex_indices[-1] + 1
        rows.insert(insert_at, new_row)
    else:
        # Insert in numeric order
        inserted = False
        for i, r in enumerate(rows):
            try:
                if int(r["Number"]) > int(dex_number):
                    rows.insert(i, new_row)
                    inserted = True
                    break
            except ValueError:
                pass
        if not inserted:
            rows.append(new_row)

    save_dex_csv(rows)
    label = form_name if form_name else base_name
    return f"✅ Added `#{dex_number} {label}` to dex_number.csv."


# ══════════════════════════════════════════════════════════════════════════════
#  egg_groups.csv helpers
# ══════════════════════════════════════════════════════════════════════════════

def load_egg_csv() -> list[dict]:
    if not os.path.exists(EGG_CSV):
        return []
    with open(EGG_CSV, "r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def save_egg_csv(rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(EGG_CSV), exist_ok=True)
    with open(EGG_CSV, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["Name", "Egg Groups"])
        writer.writeheader()
        writer.writerows(rows)


def add_to_egg_csv(name: str, egg_groups: str) -> str:
    rows = load_egg_csv()
    if any(r["Name"] == name for r in rows):
        return f"⚠️ `{name}` already in egg_groups.csv."
    rows.append({"Name": name, "Egg Groups": egg_groups})
    # Keep sorted alphabetically
    rows.sort(key=lambda r: r["Name"].lower())
    save_egg_csv(rows)
    return f"✅ Added `{name}` ({egg_groups}) to egg_groups.csv."


# ══════════════════════════════════════════════════════════════════════════════
#  evolution.csv + pokemon_forms.csv helpers
# ══════════════════════════════════════════════════════════════════════════════

def load_evolution_csv() -> tuple[list[str], list[list[str]]]:
    with open(EVOLUTION_CSV, "r", encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    return rows[0], rows[1:]


def save_evolution_csv(header: list[str], rows: list[list[str]]) -> None:
    with open(EVOLUTION_CSV, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


def load_forms_csv() -> list[dict]:
    with open(FORMS_CSV, "r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def save_forms_csv(rows: list[dict]) -> None:
    with open(FORMS_CSV, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["pokemon", "forms"])
        writer.writeheader()
        writer.writerows(rows)


def find_evolution_family(family_members: list[str]) -> tuple[int | None, list[str]]:
    _, rows = load_evolution_csv()
    for idx, row in enumerate(rows):
        cells = [c.strip() for c in row]
        if any(m.strip() in cells for m in family_members):
            members = [c for c in cells[1:] if c.strip()]
            return idx, members
    return None, []


def find_base_pokemon_forms(base_pokemon: str) -> list[str]:
    rows = load_forms_csv()
    for row in rows:
        if row["pokemon"].strip().lower() == base_pokemon.strip().lower():
            return [f.strip() for f in row["forms"].split(",") if f.strip()]
    return []


def commit_evolution_add(new_pokemon: str, row_index: int) -> str:
    header, rows = load_evolution_csv()
    row = rows[row_index]
    while len(row) < len(header):
        row.append("")
    if new_pokemon in [c.strip() for c in row]:
        return f"⚠️ `{new_pokemon}` is already in that evolution family."
    inserted = False
    for i in range(1, len(row)):
        if row[i].strip() == "":
            row[i] = new_pokemon
            inserted = True
            break
    if not inserted:
        row.append(new_pokemon)
        if len(row) > len(header):
            header.append(f"Pokemon {len(header)}")
    rows[row_index] = row
    save_evolution_csv(header, rows)
    return f"✅ Added `{new_pokemon}` to evolution family ID **{row[0]}**."


def commit_forms_add(new_pokemon: str, base_pokemon: str) -> str:
    rows = load_forms_csv()
    for row in rows:
        if row["pokemon"].strip().lower() == base_pokemon.strip().lower():
            existing = [f.strip() for f in row["forms"].split(",") if f.strip()]
            if new_pokemon in existing:
                return f"⚠️ `{new_pokemon}` is already a form of `{row['pokemon']}`."
            existing.append(new_pokemon)
            existing.sort()
            row["forms"] = ", ".join(existing)
            save_forms_csv(rows)
            return f"✅ Added `{new_pokemon}` as a form of `{row['pokemon']}`."
    return f"❌ `{base_pokemon}` not found in pokemon_forms.csv."


# ══════════════════════════════════════════════════════════════════════════════
#  Embed parsing helpers
# ══════════════════════════════════════════════════════════════════════════════

def parse_types_from_embed(embed: discord.Embed) -> tuple[str, str]:
    for field in embed.fields:
        if (field.name or "").strip().lower() == "types":
            text = re.sub(r"<:[^>]+>", "", field.value or "")
            text = re.sub(r"[\U0001F300-\U0001FFFF]", "", text)
            types = [w.strip() for w in text.split() if w.strip()]
            return (types[0] if types else ""), (types[1] if len(types) > 1 else "")
    return "", ""


def parse_region_from_embed(embed: discord.Embed) -> str:
    for field in embed.fields:
        if (field.name or "").strip().lower() == "region":
            return (field.value or "").strip()
    return ""


def parse_catchable_from_embed(embed: discord.Embed) -> str:
    for field in embed.fields:
        if (field.name or "").strip().lower() == "catchable":
            return (field.value or "").strip()
    return ""


def parse_egg_groups_from_embed(embed: discord.Embed) -> str:
    for field in embed.fields:
        if (field.name or "").strip().lower() == "egg groups":
            val = (field.value or "").strip()
            # Normalize newlines to comma-separated
            parts = [p.strip() for p in re.split(r"[\n,]+", val) if p.strip()]
            return ", ".join(parts)
    return ""


def parse_base_stats_from_embed(embed: discord.Embed) -> dict:
    stats = {"HP": None, "Attack": None, "Defense": None,
             "Sp. Atk": None, "Sp. Def": None, "Speed": None}
    for field in embed.fields:
        if (field.name or "").strip().lower() == "base stats":
            val = field.value or ""
            for line in val.splitlines():
                line = re.sub(r"\*\*", "", line).strip()
                for stat in stats:
                    if line.lower().startswith(stat.lower() + ":"):
                        num = re.search(r"\d+", line)
                        if num:
                            stats[stat] = int(num.group())
    return stats


def parse_names_field(names_raw: str) -> dict:
    text = re.sub(r"<:[^>]+>", "", names_raw).strip()
    parts = re.split(r"[\U0001F1E6-\U0001F1FF]{2}", text)
    names = [p.strip() for p in parts if p.strip()]
    if len(names) <= 1:
        return {}
    return {"en": [n for n in names[1:] if n]}


def parse_appearance_from_embed(embed: discord.Embed) -> dict:
    for field in embed.fields:
        if (field.name or "").strip().lower() == "appearance":
            val = field.value or ""
            h = re.search(r"Height:\s*([\d.]+)\s*m", val)
            w = re.search(r"Weight:\s*([\d.]+)\s*kg", val)
            return {
                "height_m": float(h.group(1)) if h else None,
                "weight_kg": float(w.group(1)) if w else None,
            }
    return {"height_m": None, "weight_kg": None}


def parse_gender_ratio_from_embed(embed: discord.Embed) -> str:
    for field in embed.fields:
        if (field.name or "").strip().lower() == "gender ratio":
            return (field.value or "").strip()
    return ""


def parse_hatch_time_from_embed(embed: discord.Embed) -> str:
    for field in embed.fields:
        if (field.name or "").strip().lower() == "hatch time":
            return (field.value or "").strip()
    return ""


def parse_poketwo_embed(embed: discord.Embed) -> dict | None:
    """Parse any Pokétwo info embed (event or non-event custom Pokémon)."""
    title = embed.title or ""
    m = re.match(r"#(\d+)\s+[—–-]\s+(.+)", title)
    if not m:
        return None

    dex_number   = m.group(1)
    primary_name = m.group(2).strip()
    rarity       = None
    names_raw    = None
    evolution_raw = None

    for field in embed.fields:
        fname = (field.name or "").strip().lower()
        fval  = (field.value or "").strip()
        if fname == "rarity":
            rarity = fval
        elif fname == "names":
            names_raw = fval
        elif fname == "evolution":
            evolution_raw = fval

    # Accept: event rarity OR no rarity field at all
    is_event    = rarity and rarity.lower() == "event"
    has_no_rarity = rarity is None
    if not is_event and not has_no_rarity:
        return None

    other_names = parse_names_field(names_raw) if names_raw else {}
    region      = parse_region_from_embed(embed)
    type1, type2 = parse_types_from_embed(embed)
    catchable   = parse_catchable_from_embed(embed)
    base_stats  = parse_base_stats_from_embed(embed)
    appearance  = parse_appearance_from_embed(embed)
    gender_ratio = parse_gender_ratio_from_embed(embed)
    egg_groups  = parse_egg_groups_from_embed(embed)
    hatch_time  = parse_hatch_time_from_embed(embed)

    # Build description from embed description
    description = embed.description or ""

    return {
        "dex_number":   dex_number,
        "name":         primary_name,
        "description":  description,
        "image_url":    embed.image.url if embed.image else "",
        "fields":       {"Evolution": evolution_raw or ""},
        "types":        [t for t in [type1, type2] if t],
        "region":       region,
        "catchable":    catchable,
        "base_stats":   base_stats,
        "names":        other_names,
        "appearance":   appearance,
        "gender_ratio": gender_ratio,
        "egg_groups":   egg_groups,
        "hatch_time":   hatch_time,
        # Internal use only — stripped before saving to JSON
        "_egg_groups":  egg_groups,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  Step 1 — Ask: is this a variant?
# ══════════════════════════════════════════════════════════════════════════════

class IsVariantView(View):
    """First question: is this Pokémon a variant/form of an existing one?"""

    def __init__(self, parsed: dict, channel: discord.TextChannel):
        super().__init__(timeout=300)
        self.parsed  = parsed
        self.channel = channel

    @discord.ui.button(label="✅ Yes, it's a variant/form", style=discord.ButtonStyle.success)
    async def yes_variant(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_modal(
            VariantInfoModal(self.parsed, self.channel)
        )
        self.stop()

    @discord.ui.button(label="❌ No, it's a base Pokémon", style=discord.ButtonStyle.danger)
    async def no_variant(self, interaction: discord.Interaction, button: Button):
        name       = self.parsed["name"]
        dex_number = self.parsed["dex_number"]
        egg_groups = self.parsed["_egg_groups"]

        lines = [f"**Saving `{name}` as a base Pokémon...**\n"]

        # Save to pokemon_data.json
        entry = {k: v for k, v in self.parsed.items() if not k.startswith("_")}
        lines.append(f"📄 **JSON:** {add_to_pokemon_json(entry)}")

        # dex_number.csv — base entry (no form)
        lines.append(f"📋 **Dex CSV:** {add_to_dex_csv(dex_number, name, '')}")

        # egg_groups.csv
        if egg_groups:
            lines.append(f"🥚 **Egg Groups CSV:** {add_to_egg_csv(name, egg_groups)}")

        await interaction.response.edit_message(
            content="\n".join(lines), embed=None, view=None
        )
        self.stop()

    @discord.ui.button(label="⏭️ Skip entirely", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: Button):
        await interaction.response.edit_message(
            content=f"Skipped `{self.parsed['name']}` entirely.", embed=None, view=None
        )
        self.stop()


# ══════════════════════════════════════════════════════════════════════════════
#  Step 2 — Modal: gather variant details
# ══════════════════════════════════════════════════════════════════════════════

class VariantInfoModal(Modal):
    def __init__(self, parsed: dict, channel: discord.TextChannel):
        super().__init__(title=f"Variant info for {parsed['name']}")
        self.parsed  = parsed
        self.channel = channel

        self.base_name_input = TextInput(
            label="Base Pokémon name (e.g. Tangrowth)",
            placeholder="The original / base form",
            style=discord.TextStyle.short,
            required=True,
            max_length=100,
        )
        self.family_input = TextInput(
            label="2 members of its evolution family",
            placeholder="e.g. Tangela, Tangrowth",
            style=discord.TextStyle.short,
            required=True,
            max_length=200,
        )
        self.add_item(self.base_name_input)
        self.add_item(self.family_input)

    async def on_submit(self, interaction: discord.Interaction):
        base_poke = self.base_name_input.value.strip()
        members   = [
            m.strip()
            for m in self.family_input.value.replace(";", ",").split(",")
            if m.strip()
        ]

        row_idx, family_members = find_evolution_family(members)
        existing_forms = find_base_pokemon_forms(base_poke)
        name       = self.parsed["name"]
        dex_number = self.parsed["dex_number"]
        egg_groups = self.parsed["_egg_groups"]

        preview = discord.Embed(
            title=f"Preview — adding `{name}` as variant of `{base_poke}`",
            color=discord.Color.orange(),
        )

        if row_idx is None:
            preview.add_field(
                name="❌ Evolution family",
                value=f"No family found for: **{', '.join(members)}**\nDouble-check names.",
                inline=False,
            )
            evo_ok = False
        else:
            preview.add_field(
                name="📋 Evolution family (current)",
                value=", ".join(f"`{m}`" for m in family_members) or "*(empty)*",
                inline=False,
            )
            preview.add_field(
                name="➕ Will add to family",
                value=f"`{name}`",
                inline=False,
            )
            evo_ok = True

        preview.add_field(
            name=f"🔀 Current forms of `{base_poke}`",
            value=(", ".join(f"`{f}`" for f in existing_forms) if existing_forms else "*(none yet)*"),
            inline=False,
        )
        preview.add_field(
            name="➕ Will add as form",
            value=f"`{name}` → form of `{base_poke}`",
            inline=False,
        )
        preview.add_field(
            name="📋 dex_number.csv",
            value=f"Row: `{dex_number}` | `{base_poke}` | `{name}`",
            inline=False,
        )
        if egg_groups:
            preview.add_field(
                name="🥚 egg_groups.csv",
                value=f"`{name}` → `{egg_groups}`",
                inline=False,
            )
        preview.set_footer(text="Confirm to write all changes, or Re-enter / Cancel.")

        view = VariantConfirmView(
            parsed=self.parsed,
            row_idx=row_idx,
            base_poke=base_poke,
            evo_ok=evo_ok,
            channel=self.channel,
        )
        await interaction.response.send_message(embed=preview, view=view)


# ══════════════════════════════════════════════════════════════════════════════
#  Step 3 — Confirm variant save
# ══════════════════════════════════════════════════════════════════════════════

class VariantConfirmView(View):
    def __init__(
        self,
        parsed: dict,
        row_idx: int | None,
        base_poke: str,
        evo_ok: bool,
        channel: discord.TextChannel,
    ):
        super().__init__(timeout=120)
        self.parsed    = parsed
        self.row_idx   = row_idx
        self.base_poke = base_poke
        self.evo_ok    = evo_ok
        self.channel   = channel

    @discord.ui.button(label="✅ Confirm & Save", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: Button):
        name       = self.parsed["name"]
        dex_number = self.parsed["dex_number"]
        egg_groups = self.parsed["_egg_groups"]

        lines = [f"**Saving `{name}` as variant...**\n"]

        # pokemon_data.json
        entry = {k: v for k, v in self.parsed.items() if not k.startswith("_")}
        lines.append(f"📄 **JSON:** {add_to_pokemon_json(entry)}")

        # evolution.csv
        if self.evo_ok:
            lines.append(f"🔗 **Evo CSV:** {commit_evolution_add(name, self.row_idx)}")
        else:
            lines.append("🔗 **Evo CSV:** ❌ Skipped — family not found.")

        # pokemon_forms.csv
        lines.append(f"🔀 **Forms CSV:** {commit_forms_add(name, self.base_poke)}")

        # dex_number.csv — variant row: Number | base_name | full_variant_name
        lines.append(f"📋 **Dex CSV:** {add_to_dex_csv(dex_number, self.base_poke, name)}")

        # egg_groups.csv
        if egg_groups:
            lines.append(f"🥚 **Egg Groups CSV:** {add_to_egg_csv(name, egg_groups)}")

        await interaction.response.edit_message(
            content="\n".join(lines), embed=None, view=None
        )
        self.stop()

    @discord.ui.button(label="✏️ Re-enter", style=discord.ButtonStyle.primary)
    async def redo(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_modal(
            VariantInfoModal(self.parsed, self.channel)
        )
        await interaction.message.edit(view=None)
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: Button):
        await interaction.response.edit_message(
            content=f"Cancelled all CSV/JSON updates for **{self.parsed['name']}**.",
            embed=None, view=None,
        )
        self.stop()


# ══════════════════════════════════════════════════════════════════════════════
#  Bot events
# ══════════════════════════════════════════════════════════════════════════════

@client.event
async def on_ready():
    print(f"✅  Logged in as {client.user} (ID: {client.user.id})")
    print(f"📂  Pokemon JSON : {os.path.abspath(POKEMON_JSON)}")
    print(f"📂  Evo CSV      : {os.path.abspath(EVOLUTION_CSV)}")
    print(f"📂  Forms CSV    : {os.path.abspath(FORMS_CSV)}")
    print(f"📂  Dex CSV      : {os.path.abspath(DEX_CSV)}")
    print(f"📂  Egg CSV      : {os.path.abspath(EGG_CSV)}")


@client.event
async def on_message(message: discord.Message):
    if message.author.id != POKETWO_ID:
        return
    if not message.embeds:
        return

    for embed in message.embeds:
        parsed = parse_poketwo_embed(embed)
        if parsed is None:
            continue

        name       = parsed["name"]
        dex_number = parsed["dex_number"]

        # ── Already in JSON? Skip entirely ───────────────────────────────
        if already_in_json(dex_number, name):
            print(f"⏭️  Already in JSON: #{dex_number} {name}")
            continue

        print(f"🆕 New Pokémon detected: #{dex_number} {name}")

        # ── Ask the user: variant or base? ───────────────────────────────
        embed_msg = discord.Embed(
            title=f"🆕 New Pokémon: #{dex_number} — {name}",
            description=(
                f"**Region:** {parsed['region']}\n"
                f"**Types:** {', '.join(parsed['types']) or '—'}\n"
                f"**Egg Groups:** {parsed['_egg_groups'] or '—'}\n"
                f"**Catchable:** {parsed['catchable']}"
            ),
            color=discord.Color.blurple(),
        )
        embed_msg.set_footer(text="Is this a variant/form of an existing Pokémon?")

        view = IsVariantView(parsed=parsed, channel=message.channel)
        await message.channel.send(embed=embed_msg, view=view)


# ══════════════════════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN not set. Add it to Secrets.")
    client.run(TOKEN)
