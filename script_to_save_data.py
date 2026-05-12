import discord
from discord.ui import View, Button, Modal, TextInput
import json
import os
import re
import csv

# ── Config ───────────────────────────────────────────────────────────────────
TOKEN         = os.environ.get("DISCORD_BOT_TOKEN")
DATA_FILE     = "data/event_names.json"
EVOLUTION_CSV = "data/evolution.csv"
FORMS_CSV     = "data/pokemon_forms.csv"
POKETWO_ID    = 716390085896962058
# ─────────────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


# ══════════════════════════════════════════════════════════════════════════════
#  JSON helpers
# ══════════════════════════════════════════════════════════════════════════════

def load_json() -> list[dict]:
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    if not os.path.exists(DATA_FILE):
        return []
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(data: list[dict]) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def already_in_json(dex_number: str, name: str) -> bool:
    return any(
        e.get("dex_number") == dex_number and e.get("name") == name
        for e in load_json()
    )


# ══════════════════════════════════════════════════════════════════════════════
#  CSV helpers
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
    """
    Search for a family row containing ANY of the given members.
    Returns (row_index, non-empty cells in that row) or (None, []).
    """
    _, rows = load_evolution_csv()
    for idx, row in enumerate(rows):
        cells = [c.strip() for c in row]
        if any(m.strip() in cells for m in family_members):
            members = [c for c in cells[1:] if c.strip()]  # skip Family ID col
            return idx, members
    return None, []


def find_base_pokemon_forms(base_pokemon: str) -> list[str]:
    """Return existing forms for the given base Pokémon, or []."""
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
#  Embed parsing
# ══════════════════════════════════════════════════════════════════════════════

def parse_names_field(names_raw: str) -> dict:
    text = re.sub(r"<:[^>]+>", "", names_raw).strip()
    parts = re.split(r"[\U0001F1E6-\U0001F1FF]{2}", text)
    names = [p.strip() for p in parts if p.strip()]
    if len(names) <= 1:
        return {}
    other = [n for n in names[1:] if n]
    return {"en": other} if other else {}


def parse_poketwo_embed(embed: discord.Embed) -> dict | None:
    title = embed.title or ""
    m = re.match(r"#(\d+)\s+[—–-]\s+(.+)", title)
    if not m:
        return None

    dex_number   = m.group(1)
    primary_name = m.group(2).strip()
    rarity       = None
    names_raw    = None

    for field in embed.fields:
        fname = (field.name or "").strip().lower()
        fval  = (field.value or "").strip()
        if fname == "rarity":
            rarity = fval
        elif fname == "names":
            names_raw = fval

    if not rarity or rarity.lower() != "event":
        return None

    other_names = parse_names_field(names_raw) if names_raw else {}
    return {
        "dex_number":  dex_number,
        "name":        primary_name,
        "other_names": other_names,
        "is_variant":  True,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  Step 1 — Modal: ask which family + form-of
# ══════════════════════════════════════════════════════════════════════════════

class FamilySearchModal(Modal):
    def __init__(self, pokemon_name: str, channel: discord.TextChannel):
        super().__init__(title=f"Locate family for {pokemon_name}")
        self.pokemon_name = pokemon_name
        self.channel = channel

        self.family_input = TextInput(
            label="2 members of the target evolution family",
            placeholder="e.g. Bellsprout, Weepinbell",
            style=discord.TextStyle.short,
            required=True,
            max_length=200,
        )
        self.form_of_input = TextInput(
            label="It's a form of which Pokémon?",
            placeholder="e.g. Bellsprout",
            style=discord.TextStyle.short,
            required=True,
            max_length=100,
        )
        self.add_item(self.family_input)
        self.add_item(self.form_of_input)

    async def on_submit(self, interaction: discord.Interaction):
        members   = [
            m.strip()
            for m in self.family_input.value.replace(";", ",").split(",")
            if m.strip()
        ]
        base_poke = self.form_of_input.value.strip()

        # Look up the family
        row_idx, family_members = find_evolution_family(members)
        # Look up existing forms
        existing_forms = find_base_pokemon_forms(base_poke)

        # Build preview embed
        preview = discord.Embed(
            title=f"Preview — adding `{self.pokemon_name}`",
            color=discord.Color.orange(),
        )

        if row_idx is None:
            preview.add_field(
                name="❌ Evolution family",
                value=f"No family found containing: **{', '.join(members)}**\nDouble-check the names.",
                inline=False,
            )
            evo_ok = False
        else:
            preview.add_field(
                name="📋 Evolution family (current members)",
                value=", ".join(f"`{m}`" for m in family_members) or "*(empty)*",
                inline=False,
            )
            preview.add_field(
                name="➕ Will add",
                value=f"`{self.pokemon_name}` → appended to this family",
                inline=False,
            )
            evo_ok = True

        if existing_forms:
            preview.add_field(
                name=f"🔀 Current forms of `{base_poke}`",
                value=", ".join(f"`{f}`" for f in existing_forms),
                inline=False,
            )
        else:
            preview.add_field(
                name=f"🔀 Forms of `{base_poke}`",
                value="*(none yet)*" if base_poke else "❌ Base Pokémon not found — check spelling.",
                inline=False,
            )
        preview.add_field(
            name="➕ Will add",
            value=f"`{self.pokemon_name}` → form of `{base_poke}`",
            inline=False,
        )

        preview.set_footer(text="Confirm to write changes, or Cancel to abort.")

        view = ConfirmView(
            pokemon_name=self.pokemon_name,
            row_idx=row_idx,
            base_poke=base_poke,
            evo_ok=evo_ok,
        )

        await interaction.response.send_message(embed=preview, view=view)


# ══════════════════════════════════════════════════════════════════════════════
#  Step 2 — Confirm / Cancel
# ══════════════════════════════════════════════════════════════════════════════

class ConfirmView(View):
    def __init__(
        self,
        pokemon_name: str,
        row_idx: int | None,
        base_poke: str,
        evo_ok: bool,
    ):
        super().__init__(timeout=120)
        self.pokemon_name = pokemon_name
        self.row_idx      = row_idx
        self.base_poke    = base_poke
        self.evo_ok       = evo_ok

    @discord.ui.button(label="✅ Confirm & Save", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: Button):
        lines = [f"**Saving `{self.pokemon_name}`...**\n"]

        if self.evo_ok:
            lines.append(f"**Evolution family:** {commit_evolution_add(self.pokemon_name, self.row_idx)}")
        else:
            lines.append("**Evolution family:** ❌ Skipped — family not found.")

        lines.append(f"**Pokémon forms:** {commit_forms_add(self.pokemon_name, self.base_poke)}")

        await interaction.response.edit_message(
            content="\n".join(lines),
            embed=None,
            view=None,
        )
        self.stop()

    @discord.ui.button(label="✏️ Re-enter", style=discord.ButtonStyle.primary)
    async def redo(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_modal(
            FamilySearchModal(self.pokemon_name, interaction.channel)
        )
        await interaction.message.edit(view=None)
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: Button):
        await interaction.response.edit_message(
            content=f"Cancelled CSV update for **{self.pokemon_name}**.",
            embed=None,
            view=None,
        )
        self.stop()


# ══════════════════════════════════════════════════════════════════════════════
#  Step 0 — "Add to CSVs" trigger button
# ══════════════════════════════════════════════════════════════════════════════

class AddToCsvView(View):
    def __init__(self, pokemon_name: str):
        super().__init__(timeout=300)
        self.pokemon_name = pokemon_name

    @discord.ui.button(label="📋 Add to CSVs", style=discord.ButtonStyle.primary)
    async def open_form(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_modal(
            FamilySearchModal(self.pokemon_name, interaction.channel)
        )
        self.stop()

    @discord.ui.button(label="⏭️ Skip", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message(
            f"Skipped CSV update for **{self.pokemon_name}**.",
            ephemeral=True,
        )
        self.stop()


# ══════════════════════════════════════════════════════════════════════════════
#  Bot events
# ══════════════════════════════════════════════════════════════════════════════

@client.event
async def on_ready():
    print(f"✅  Logged in as {client.user} (ID: {client.user.id})")
    print(f"📂  JSON  : {os.path.abspath(DATA_FILE)}")
    print(f"📂  Evo   : {os.path.abspath(EVOLUTION_CSV)}")
    print(f"📂  Forms : {os.path.abspath(FORMS_CSV)}")


@client.event
async def on_message(message: discord.Message):
    if message.author.id != POKETWO_ID:
        return
    if not message.embeds:
        return

    for embed in message.embeds:
        entry = parse_poketwo_embed(embed)
        if entry is None:
            continue

        name       = entry["name"]
        dex_number = entry["dex_number"]

        # Save to JSON
        if already_in_json(dex_number, name):
            print(f"⏭️  Already in JSON: #{dex_number} {name}")
        else:
            data = load_json()
            data.append(entry)
            save_json(data)
            print(f"✅  Saved to JSON: #{dex_number} {name}")

        # Prompt for CSV update
        view = AddToCsvView(pokemon_name=name)
        await message.channel.send(
            f"🆕 **Event Pokémon detected:** `#{dex_number} — {name}`\n"
            f"Click **Add to CSVs** to place it in the evolution family and forms tables.",
            view=view,
        )


# ══════════════════════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN not set. Add it to Replit Secrets.")
    client.run(TOKEN)
