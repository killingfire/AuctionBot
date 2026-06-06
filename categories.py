"""
categories.py – category definitions for auction search.

After loading categories, this module calls
filters.register_category_shortcuts() so that every category key and alias
is registered as a zero-argument flag shortcut.

Usage examples:
  --category starters        (original syntax, unchanged)
  --starters                 (shortcut — equivalent to --category starters)
  --cat eevee                (original alias, unchanged)
  --eevee                    (shortcut — equivalent to --category eevee)
  --ub                       (shortcut — equivalent to --category ultrabeast)
  --legendary                (shortcut — equivalent to --category legendaries)
"""

from __future__ import annotations
from filters import register_category_shortcuts

# ─────────────────────────────────────────────────────────────────────────────
# CATEGORIES
# ─────────────────────────────────────────────────────────────────────────────
# To add a new category:
#   1. Add a new key under CATEGORIES
#   2. Set "name" (display name), "aliases" (alternate names users can type),
#      and "pokemon" (exact Pokémon names as they appear in the database)
#
# Aliases and category keys are all case-insensitive when matched.
# ─────────────────────────────────────────────────────────────────────────────

CATEGORIES: dict = {

    "legendaries": {
        "name": "Legendary Pokémon",
        "aliases": ["legendary", "legend", "legends", "leg"],
        "pokemon": [
            "Registeel","Articuno","Regirock","Rayquaza","Moltres","Suicune","Groudon","Mesprit","Zapdos","Mewtwo","Raikou","Regice","Latias","Latios","Kyogre","Entei","Lugia","Ho-Oh","Azelf","Uxie","Type: Null","Regigigas","Cresselia","Terrakion","Thundurus","Giratina","Cobalion","Virizion","Tornadus","Reshiram","Landorus","Silvally","Heatran","Xerneas","Yveltal","Zygarde","Dialga","Palkia","Zekrom","Kyurem","Tapu Koko","Tapu Lele","Tapu Bulu","Tapu Fini","Zamazenta","Eternatus","Regieleki","Regidrago","Glastrier","Spectrier","Solgaleo","Necrozma","Enamorus","Cosmoem","Urshifu","Calyrex","Cosmog","Lunala","Zacian","Kubfu","Therian Thundurus","Therian Tornadus","Therian Landorus","Origin Giratina","Mega Mewtwo X","Mega Mewtwo Y","Black Kyurem","White Kyurem","Mega Latias","Fezandipiti","Chien-Pao","Munkidori","Terapagos","Wo-Chien","Koraidon","Miraidon","Ting-Lu","Okidogi","Ogerpon","Chi-Yu","Gigantamax Single Strike Urshifu","Gigantamax Rapid Strike Urshifu","Rapid Strike Urshifu","Shadow Rider Calyrex","Dawn Wings Necrozma","Eternamax Eternatus","Dusk Mane Necrozma","Galarian Articuno","Crowned Zamazenta","Ice Rider Calyrex","Galarian Moltres","Complete Zygarde","Galarian Zapdos","Primal Groudon","Ultra Necrozma","Crowned Zacian","Primal Kyogre","Mega Rayquaza","Mega Latios","10% Zygarde","Sprinting Build Koraidon","Hearthflame Mask Ogerpon","Cornerstone Mask Ogerpon","Wellspring Mask Ogerpon","Gliding Build Koraidon","Drive Mode Miraidon","Glide Mode Miraidon","Terastal Terapagos","Electric Silvally","Fighting Silvally","Therian Enamorus","Neutral Xerneas","Dragon Silvally","Flying Silvally","Ghost Silvally","Origin Dialga","Origin Palkia","Dark Silvally","Fire Silvally","Bug Silvally","Olympic Flame Moltres","Psychic Silvally","Primal Glastrier","Fireworks Cosmog","Ground Silvally","Poison Silvally","Gradient Chi-Yu","Grass Silvally","Steel Silvally","Water Silvally","Fairy Silvally","Shadow Xerneas","Rock Silvally","Shadow Mewtwo","Zygarde Cell","Zygarde Core","Ice Silvally","Shadow Lugia","Ice Yveltal" ,
        ],
    },

    "mythicals": {
        "name": "Mythical Pokémon",
        "aliases": ["mythical", "myth", "my"],
        "pokemon": [
            "Volcanion","Marshadow","Meloetta","Genesect","Magearna","Jirachi","Manaphy","Darkrai","Shaymin","Victini","Diancie","Zeraora","Celebi","Deoxys","Phione","Arceus","Keldeo","Meltan","Hoopa","Mew","Gigantamax Melmetal","Pirouette Meloetta","Original Magearna","Electric Arceus","Fighting Arceus","Resolute Keldeo","Defense Deoxys","Attack Deoxys","Dragon Arceus","Hoopa Unbound","Speed Deoxys","Mega Diancie","Sky Shaymin","Dark Arceus","Fire Arceus","Dada Zarude","Bug Arceus","Pecharunt","Melmetal","Zarude","High-speed Flight Configuration Genesect","Spring Blooming Diancie","Zenith Marshadow","Bouquet Shaymin","Psychic Arceus","Flying Arceus","Ground Arceus","Poison Arceus","Festive Hoopa","Error Darkrai","Ghost Arceus","Grass Arceus","Steel Arceus","Water Arceus","Fairy Arceus","Pride Arceus","Rock Arceus","Ice Arceus","Lights Mew","Pride Mew","Glitched Beta Arceus","Druid Zarude" ,
        ],
    },

    "ultrabeast": {
        "name": "UltraBeast Pokémon",
        "aliases": ["ub", "ultrab", "ubs"],
        "pokemon": [
            'Ghost King Blacephalon', 'Corrupted Blacephalon', 'Flower Pheromosa', 'Blacephalon', 'Celesteela', 'Pheromosa', 'Xurkitree', 'Naganadel', 'Stakataka', 'Nihilego', 'Buzzwole', 'Guzzlord', 'Kartana', 'Poipole',
        ],
    },
    "paradox": {
        "name": "Paradox Pokémon",
        "aliases": ["para", "par", "dox"],
        "pokemon": [
            "Brute Bonnet","Flutter Mane","Slither Wing","Sandy Shocks","Iron Jugulis","Roaring Moon","Iron Valiant","Walking Wake","Gouging Fire","Scream Tail","Iron Treads","Iron Bundle","Iron Thorns","Iron Leaves","Raging Bolt","Great Tusk","Iron Hands","Iron Moth","Koraidon","Miraidon","Iron Boulder","Iron Crown" ,
        ],
    },
    "mega": {
        "name": "Mythical Pokémon",
        "aliases": ["megapokemon", "megas", "megapokemons"],
        "pokemon": [
        "Mega Charizard X","Mega Charizard Y","Mega Kangaskhan","Mega Aerodactyl","Mega Blastoise","Mega Heracross","Mega Tyranitar","Mega Gardevoir","Mega Venusaur","Mega Alakazam","Mega Gyarados","Mega Mewtwo X","Mega Mewtwo Y","Mega Ampharos","Mega Houndoom","Mega Blaziken","Mega Gengar","Mega Pinsir","Mega Scizor","Mega Mawile","Mega Manectric","Mega Abomasnow","Mega Sceptile","Mega Swampert","Mega Medicham","Mega Sharpedo","Mega Garchomp","Mega Pidgeot","Mega Slowbro","Mega Steelix","Mega Sableye","Mega Altaria","Mega Banette","Mega Lucario","Mega Gallade","Mega Aggron","Mega Latias","Mega Latios","Mega Audino","Mega Absol","Mega Salamence","Mega Metagross","Primal Groudon","Mega Beedrill","Mega Camerupt","Primal Kyogre","Mega Rayquaza","Mega Lopunny","Mega Diancie","Mega Glalie" ,
        ],
    },
    "alolan": {
        "name": "Alolan Pokémon",
        "aliases": ["alola", "alo"],
        "pokemon": [
            "Halloween Alolan Ninetales","Birthday Cake Alopix","Alolan Sandshrew","Alolan Sandslash","Alolan Ninetales","Alolan Exeggutor","Alolan Raticate","Alolan Graveler","Alolan Rattata","Alolan Diglett","Alolan Dugtrio","Alolan Persian","Alolan Geodude","Alolan Marowak","Alolan Raichu","Alolan Vulpix","Alolan Meowth","Alolan Grimer","Alolan Golem","Alolan Muk","Celebrating Alolan Exeggutor ft. Komala" ,
        ],
    },
    "galarian": {
        "name": "Galarian Pokémon",
        "aliases": ["galari", "gal"],
        "pokemon": [
            "Galarian Zen Darmanitan","Galarian Farfetch'd","Galarian Darmanitan","Galarian Zigzagoon","Galarian Rapidash","Galarian Slowpoke","Galarian Mr. Mime","Galarian Articuno","Galarian Slowking","Galarian Darumaka","Galarian Stunfisk","Galarian Slowbro","Galarian Weezing","Galarian Moltres","Galarian Corsola","Galarian Linoone","Galarian Meowth","Galarian Ponyta","Galarian Zapdos","Galarian Yamask" ,
        ],
    },
    "starters": {
        "name": "Starter Pokémon",
        "aliases": ["starter", "stars"],
        "pokemon": [
             "Bulbasaur", "Charmander", "Chespin", "Chikorita", "Chimchar", "Cyndaquil", "Fennekin", "Froakie", "Fuecoco", "Grookey", "Litten", "Mudkip", "Oshawott", "Piplup", "Popplio", "Quaxly", "Rowlet", "Scorbunny", "Snivy", "Sobble", "Sprigatito", "Squirtle", "Tepig", "Torchic", "Totodile", "Treecko", "Turtwig",
        ],
    },
    "hisuian": {
        "name": "Hisuian Pokémon",
        "aliases": ["hisui", "his"],
        "pokemon": [
            "La Catrina Hisuian Lilligant","Hisuian Typhlosion","Hisuian Growlithe","Hisuian Electrode","Hisuian Lilligant","Hisuian Decidueye","Hisuian Arcanine","Hisuian Qwilfish","Hisuian Samurott","Hisuian Braviary","Hisuian Voltorb","Hisuian Sneasel","Hisuian Zoroark","Hisuian Sliggoo","Hisuian Avalugg","Hisuian Goodra","Santa H. Zorua","Hisuian Zorua" ,
        ],
    },
    "paldean": {
        "name": "Paldean Pokémon",
        "aliases": ["paldea", "pal",],
        "pokemon": [
            "Combat Breed Tauros","Blaze Breed Tauros","Aqua Breed Tauros","Paldean Wooper" ,
        ],
    },

    "event": {
        "name": "Event Pokémon",
        "aliases": ["ev", "eve", "even"],
        "pokemon": [
            "Original Cap Pikachu","Partner Cap Pikachu","Sinnoh Cap Pikachu","Pikachu Rock Star","Hoenn Cap Pikachu","Unova Cap Pikachu","Kalos Cap Pikachu","Alola Cap Pikachu","Pikachu Pop Star","Small Pumpkaboo","Large Pumpkaboo","Super Pumpkaboo","Small Gourgeist","Large Gourgeist","Super Gourgeist","Ash's Greninja","Busted Mimikyu","Pikachu Belle","Pikachu Ph.D.","Pikachu Libre","Festive Farfetch'd","Anniversary Wooloo","Festive Sudowoodo","Festive Gardevoir","Festive Igglybuff","Autumn Bulbasaur","Festive Torchic","Festive Murkrow","Festive Miltank","Festive Gallade","Autumn Torterra","Festive Pidove","Festive Swanna","Festive Cubone","Autumn Turtwig","Festive Hoopa","Shadow Mewtwo","Autumn Grotle","Shadow Lugia","Autumn Eevee","Sandshrew of the Sarcophagus","Jack-O-Lantern Chandelure","Ghost King Blacephalon","Frankenstein Psyduck","Poinsettia Lilligant","Candy Corn Cutiefly","Candy Cane Marowak","Devil Jigglypuff","Christmas Mareep","Ornaments Spoink","Mrs. Claus Jynx","Autumn Leafeon","Vampire Raichu","Pumpkin Togepi","Shadow Xerneas","United Pikachu","Rudolph Vulpix","Santa Delibird","Angel Diglett","Devil Wooper","Winter Event Sawsbuck","Elsa Galarian Ponyta","Valentine's Nidoran","Ornament Eldegoss","Snowflake Bronzor","Christmas Rowlet","Lights Pyukumuku","Crystal Larvesta","Spikey Cyndaquil","Primal Glastrier","Sprouting Oddish","Presents Komala","Snowman Pikachu","Bouquet Shaymin","Choco Sinistea","Snowy Slowpoke","Wreath Comfey","Cake Appletun","Elf Impidimp","Snowy Amaura","Halloween Alolan Ninetales","Cherry Blossom Cottonee","Eternal Flower Floette","Spring Fever Cubchoo","Anniversary Sunflora","Ice Princess Kirlia","Halloween Morelull","Sharkfin Totodile","Halloween Carbink","Grilling Snorlax","Autumn Chikorita","Martini Dratini","Autumn Rapidash","Snowball Gastly","Floatie Piplup","Autumn Pansage","Ukulele Pichu","Autumn Skiddo","Surf Pikachu","Autumn Snivy","Spring Blooming Diancie","Christmas Tree Snorunt","Egg Hunter Kangaskhan","Egg Searching Steenee","Nutcrack Sirfetch'd","Ice Present Eiscue","Bug Catcher Weedle","Hatching Beautifly","Egg Basket Buneary","Anniversary Lapras","Bird Nest Nuzleaf","Lights Pachirisu","Flower Pheromosa","Cupid Decidueye","Choco Milcery","Coal Rolycoly","Flower Paras","Ice Yveltal","Lights Mew","Eggneton","Fishing Smeargle ft. Magikarp","Pride Gardevoir & Delphox","Camp Leader Quagsire","Cupcake Alcremie","Pride Masquerain","Pride Bellossom","Pride Zigzagoon","Pride Toucannon","Pride Tandemaus","Pride Roserade","Pride Tinkaton","Skater Wooper","Pride Milotic","Pride Rufflet","Pride Sylveon","Pride Piplup","Pride Arceus","Pride Comfey","Pride Unown","Pride Mew","Pile of Leaves Swalot","Pumpkaboo Spice Latte","Marshmallow Maushold","Overgrown Shiinotic","Kettle Polteageist","Pumpkin Gothorita","Camper Charjabug","Autumn Dachsbun","Sage of Foliage","Sage of Shadows","Sage of Snaring","Timber Timburr","Mushroom Nacli","Evil Mightyena","Sage of Flames","Voodoo Spinda","Ruined Golurk","Tent Snorunt","Hero Golurk","Toadsie","Christmas Tree Arboliva","Pyjama Plusle & Minun","Christmas Tree Smoliv","Christmas Tree Dolliv","Snow Leopard Sneasler","Conductor Dragonite","Harvesting Ledian","Nibbling Bunnelby","Reindeer Deerling","Lovebird Unfezant","Snoozing Meowstic","Fireworks Cosmog","Overgrown Mawile","Cooking Chespin","Pyjama Minccino","Santa H. Zorua","Polar Stufful","Pear Flapple","Train Varoom","Snowmadam","Strawberry Shortcake Applin","Pasta Bolognese Tangela","Fancy Cutlery Doublade","Painted Acorn Skwovet","Flower Family Swanna","Birthday Cake Alopix","Overgrown Carnivine","Egg Forager Lechonk","Egg Painter Meowth","Easter Egg Azurill","Easter Togedemaru","Ice Cream Spheals","Onigiri Bellibolt","Blossom Cherrim","Pride Ampharos","Rainbow Minior","Temaki Gulpin","Dango Falinks","Goomy Brûlée","Clamacaron","La Catrina Hisuian Lilligant","Olympic Flame Moltres","Cheerleader Oricorio","Flower Fairy Flabébé","Fire Fairy Salandit","Papel Picado Pidgey","Waterpolo Ducklett","Monarch Gothitelle","Relay Race Raboot","Moon Fairy Mudkip","Sweater Teddiursa","Gradient Chi-Yu","Archery Sentret","Honoring Yamask","Alebrije Pyroar","Sombrero Lotad","Sugar Duskull","Fencinteleon","Leafy Baltoy","Boxel","Gingerbread Gimmighoul","Paper Lantern Lampent","Music Box Bellossom","Candy Cane Wiglett","Lion Dancer Litleo","Good Luck Sinistea","Snowglobe Glaceon","Shamrock Meganium","Wooden Serperior","Pacifier Pancham","Treasure Turtwig","Cosy Perrserker","Baby Toy Klefki","Love Bombirdier","Hearts Fidough","Santa Snorlax","Baby Ducklett","Doll Lopunny","Grinchsnarl","Elf Audino","Barbarian Bloodmoon Ursaluna","Corrupted Blacephalon","Glitched Beta Arceus","Cursed Blade Honedge","Minotaur Bouffalant","Pride Queen Bruxish","Guardian Dragonite","Cube Slime Grimer","Wizard Kricketune","Egg Nest Lapras","Banshee Banette","Ranger Floatzel","Rogue Toxicroak","Error Darkrai","Cracked Ditto","Easter Bidoof","Bard Purrloin","Druid Zarude","Porygon-X","Sylvirus","Celebrating Alolan Exeggutor ft. Komala","Umbrella Farfetch'd","Raincoat Grafaiai","Proud Crocalor","Chicombusken","Muddy Goomy","Foombrella","Leavanette","Bonnersby","Cloubat","Fazwear","Foroark","Drifboy","Soluna","Beta Bellsprout", "Beta Bayleef", "Dandelion Jumpluff", "Cicada Vikavolt", "Swarming Ledyba", "Blossom Hydrapple", "Blooming Budew", "Pokopia Ditto", "Onion Petilil", "Mycena Paras"
        ],
    },

    "gmax": {
        "name": "Gigantamax Pokémon",
        "aliases": ["gigantamax", "gigantmax", "gm"],
        "pokemon": [
            "Gigantamax Corviknight","Gigantamax Butterfree","Gigantamax Charizard","Gigantamax Blastoise","Gigantamax Rillaboom","Gigantamax Cinderace","Gigantamax Venusaur","Gigantamax Garbodor","Gigantamax Melmetal","Gigantamax Inteleon","Gigantamax Orbeetle","Gigantamax Pikachu","Gigantamax Machamp","Gigantamax Kingler","Gigantamax Snorlax","Gigantamax Drednaw","Gigantamax Meowth","Gigantamax Gengar","Gigantamax Lapras","Gigantamax Eevee","Gigantamax Single Strike Urshifu","Gigantamax Rapid Strike Urshifu","Gigantamax Low Key Toxtricity","Gigantamax Amped Toxtricity","Gigantamax Centiskorch","Gigantamax Sandaconda","Gigantamax Grimmsnarl","Gigantamax Copperajah","Gigantamax Coalossal","Gigantamax Hatterene","Gigantamax Duraludon","Gigantamax Appletun","Gigantamax Alcremie","Eternamax Eternatus","Gigantamax Flapple",
        ],
    },

    "regionals": {
        "name": "Regional Pokémon",
        "aliases": ["regional", "reg", "regionalpokemons"],
        "pokemon": [
            "Galarian Slowpoke","Alolan Sandshrew","Alolan Sandslash","Alolan Ninetales","Alolan Exeggutor","Alolan Raticate","Galarian Meowth","Alolan Graveler","Alolan Rattata","Alolan Diglett","Alolan Dugtrio","Alolan Persian","Alolan Geodude","Alolan Marowak","Alolan Raichu","Alolan Vulpix","Alolan Meowth","Alolan Grimer","Alolan Golem","Alolan Muk","Galarian Zen Darmanitan","Galarian Farfetch'd","Galarian Darmanitan","Galarian Zigzagoon","Hisuian Growlithe","Galarian Rapidash","Galarian Mr. Mime","Galarian Articuno","Galarian Slowking","Galarian Darumaka","Galarian Stunfisk","Hisuian Arcanine","Galarian Slowbro","Galarian Weezing","Galarian Moltres","Galarian Corsola","Galarian Linoone","Galarian Ponyta","Galarian Zapdos","Galarian Yamask","Halloween Alolan Ninetales","Elsa Galarian Ponyta","Combat Breed Tauros","Blaze Breed Tauros","Hisuian Typhlosion","Hisuian Electrode","Aqua Breed Tauros","Hisuian Lilligant","Hisuian Decidueye","Hisuian Qwilfish","Hisuian Samurott","Hisuian Braviary","Hisuian Voltorb","Hisuian Sneasel","Hisuian Zoroark","Hisuian Sliggoo","Hisuian Avalugg","Paldean Wooper","Hisuian Goodra","Hisuian Zorua","Celebrating Alolan Exeggutor ft. Komala","La Catrina Hisuian Lilligant","Birthday Cake Alopix","Santa H. Zorua" ,
        ],
    },

    "rares": {
        "name": "Rare Pokémon",
        "aliases": ["rare","ra"],
        "pokemon": [
            "Registeel","Articuno","Regirock","Rayquaza","Moltres","Suicune","Groudon","Jirachi","Zapdos","Mewtwo","Raikou","Celebi","Regice","Latias","Latios","Kyogre","Entei","Lugia","Ho-Oh","Mew","Regigigas","Cresselia","Terrakion","Giratina","Cobalion","Virizion","Tornadus","Mesprit","Heatran","Manaphy","Darkrai","Shaymin","Victini","Deoxys","Dialga","Palkia","Phione","Arceus","Azelf","Uxie","Type: Null","Thundurus","Volcanion","Tapu Koko","Tapu Lele","Tapu Bulu","Tapu Fini","Reshiram","Landorus","Meloetta","Genesect","Silvally","Xerneas","Yveltal","Zygarde","Diancie","Zekrom","Kyurem","Keldeo","Hoopa","Blacephalon","Celesteela","Pheromosa","Xurkitree","Marshadow","Naganadel","Stakataka","Solgaleo","Nihilego","Buzzwole","Guzzlord","Necrozma","Magearna","Cosmoem","Kartana","Poipole","Zeraora","Cosmog","Lunala","Meltan","Zamazenta","Eternatus","Regieleki","Regidrago","Glastrier","Spectrier","Chien-Pao","Melmetal","Enamorus","Wo-Chien","Koraidon","Miraidon","Urshifu","Calyrex","Ting-Lu","Okidogi","Zacian","Zarude","Chi-Yu","Kubfu","Pirouette Meloetta","Therian Thundurus","Therian Tornadus","Therian Landorus","Origin Giratina","Resolute Keldeo","Defense Deoxys","Mega Mewtwo X","Mega Mewtwo Y","Attack Deoxys","Speed Deoxys","Black Kyurem","White Kyurem","Mega Latias","Sky Shaymin","Fezandipiti","Munkidori","Terapagos","Pecharunt","Ogerpon","Rapid Strike Urshifu","Shadow Rider Calyrex","Dawn Wings Necrozma","Dusk Mane Necrozma","Galarian Articuno","Original Magearna","Crowned Zamazenta","Ice Rider Calyrex","Galarian Moltres","Complete Zygarde","Galarian Zapdos","Primal Groudon","Ultra Necrozma","Crowned Zacian","Primal Kyogre","Mega Rayquaza","Hoopa Unbound","Mega Diancie","Mega Latios","10% Zygarde","Gigantamax Single Strike Urshifu","Gigantamax Rapid Strike Urshifu","Sprinting Build Koraidon","Hearthflame Mask Ogerpon","Cornerstone Mask Ogerpon","Wellspring Mask Ogerpon","Gliding Build Koraidon","Gigantamax Melmetal","Eternamax Eternatus","Drive Mode Miraidon","Glide Mode Miraidon","Terastal Terapagos","Therian Enamorus","Neutral Xerneas","Origin Dialga","Origin Palkia","Dragon Arceus","Dark Arceus","Dada Zarude","Bug Arceus","Electric Silvally","Fighting Silvally","Electric Arceus","Fighting Arceus","Dragon Silvally","Psychic Arceus","Flying Arceus","Ground Arceus","Poison Arceus","Dark Silvally","Fire Silvally","Ghost Arceus","Grass Arceus","Steel Arceus","Water Arceus","Fairy Arceus","Bug Silvally","Fire Arceus","Rock Arceus","Ice Arceus","High-speed Flight Configuration Genesect","Ghost King Blacephalon","Psychic Silvally","Zenith Marshadow","Flying Silvally","Ground Silvally","Poison Silvally","Ghost Silvally","Grass Silvally","Steel Silvally","Water Silvally","Fairy Silvally","Shadow Xerneas","Rock Silvally","Festive Hoopa","Shadow Mewtwo","Zygarde Cell","Zygarde Core","Ice Silvally","Shadow Lugia","Spring Blooming Diancie","Olympic Flame Moltres","Corrupted Blacephalon","Glitched Beta Arceus","Primal Glastrier","Flower Pheromosa","Fireworks Cosmog","Bouquet Shaymin","Gradient Chi-Yu","Error Darkrai","Pride Arceus","Druid Zarude","Ice Yveltal","Lights Mew","Pride Mew",
            
        ],
    },

    # ── ADD NEW CATEGORIES BELOW ──────────────────────────────────────────────
    # "my_category": {
    #     "name": "My Category Display Name",
    #     "aliases": ["alias1", "alias2"],
    #     "pokemon": ["Pokemon1", "Pokemon2"],
    # },
}


# ─── Lookup helpers (built at import time for O(1) access) ───────────────────

def _build_lookup() -> dict[str, str]:
    """Returns {alias_lower -> category_key} including the key itself."""
    lookup: dict[str, str] = {}
    for key, data in CATEGORIES.items():
        lookup[key.lower()] = key
        for alias in data.get("aliases", []):
            lookup[alias.lower()] = key
    return lookup

_ALIAS_LOOKUP: dict[str, str] = _build_lookup()


def resolve_category(name: str) -> dict | None:
    """
    Given a user-supplied category name/alias, return the category dict
    (with keys 'name', 'aliases', 'pokemon') or None if not found.
    """
    key = _ALIAS_LOOKUP.get(name.lower())
    if key is None:
        return None
    return CATEGORIES[key]


def list_categories() -> list[dict]:
    """Return list of all categories with their display names and aliases."""
    return [
        {"key": k, "name": v["name"], "aliases": v["aliases"]}
        for k, v in CATEGORIES.items()
    ]


# ─── Register shortcut flags with filters.py ─────────────────────────────────
# Runs once at import time. Makes --starters, --rares, --eevee, --legendary,
# --gmax, --ub, --ev, --regional, etc. work as standalone flags without
# needing --category. Adding a new category above automatically gives it
# a shortcut flag — no extra steps needed.

register_category_shortcuts([
    (key, data.get("aliases", []))
    for key, data in CATEGORIES.items()
])
