"""Concrete picker factories for VRM expressions and emotes.

Two factories built on `SemanticPicker`:

- `build_expression_picker()` — the 6 VRM 1.0 facial expressions, aliased
  per Riko's personality.
- `build_emote_picker()` — autoloads `*.fbx` from `mixamo/` AND `*.vrma`
  from `vrma/` + `vrma_xr/`, normalizes filenames to embed-friendly names,
  and returns `(picker, name_to_rel_url)` so callers can resolve a picked
  name straight to the browser-relative path that goes to /animate.

Both pickers share the underlying MiniLM model (cached in `semantic_picker`).

Public surface:
    build_expression_picker() -> SemanticPicker
    build_emote_picker() -> (SemanticPicker, dict[str, str])
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Tuple

from server.config import (
    ANIMATION_SOURCES,
    CLIENT_ROOT,
    DEFAULT_EXPRESSION,
    MATCH_THRESHOLD,
    MINILM_PATH,
)
from server.llm.semantic_picker import SemanticPicker


# VRM 1.0 standard facial expressions.
VRM_EXPRESSIONS = ["angry", "happy", "neutral", "relaxed", "sad", "surprised"]

RIKO_EXPRESSION_QUERY_ALIASES = {
    "en colère": "angry",
    "fâchée": "angry",
    "furieuse": "angry",
    "heureuse": "happy",
    "joyeuse": "happy",
    "détendue": "relaxed",
    "malicieuse": "relaxed",
    "triste": "sad",
    "surprise": "surprised",
    "étonnée": "surprised",
    "choquée": "surprised",
}

RIKO_EMOTE_QUERY_ALIASES = {
    "faire coucou": "waving",
    "faire un signe de la main": "waving",
    "au revoir": "waving",
    "dire bonjour": "greeting",
    "saluer": "greeting",
    "hocher la tête": "head nod yes",
    "acquiescer": "head nod yes",
    "approuver": "head nod yes",
    "détourner le regard": "look away gesture",
    "regarder ailleurs": "look away gesture",
    "secouer la tête pour dire non": "shaking head no",
    "dire non": "shaking head no",
    "refuser": "shaking head no",
    "soupir de soulagement": "relieved sigh",
    "geste de colère": "angry gesture",
    "faire un signe de paix": "peace sign",
    "faire le v de la victoire": "peace sign",
    "faire un bisou": "kiss",
    "envoyer un bisou": "kiss kit",
    "s'agenouiller pour s'excuser": "dogeza",
    "slam": "punch combo",
}

RIKO_EXPRESSION_ALIASES = {
    "angry":     "angry, furious, mad, enraged, rage, hostile, livid, irritated, scowl, colère, fâché, furieux, agacé, punchy",
    "happy":     "happy, joyful, glad, cheerful, gleeful, delighted, beaming, laugh, laughing, smile, joy, heureux, joyeux, sourire, content",
    "neutral":   "neutral, plain, default, blank, expressionless, deadpan",
    "relaxed":   "relaxed, smug, smirk, smirking, grin, grinning, evilgrin, playful, mischievous, cocky, teasing, sly, confident, cheeky, flirty, mocking, détendu, malicieux, narquois, taquin, espiègle",
    "sad":       "sad, crying, weeping, tears, sorrowful, melancholy, upset, hurt, depressed, sobbing, triste, pleurer, larmes, chagriné",
    "surprised": "surprised, shocked, astonished, amazed, startled, wide-eyed, gasping, stunned, surpris, étonné, choqué, stupéfait",
}

RIKO_EMOTE_ALIASES = {
    # --- COMBAT & MOUVEMENTS ACROBATIQUES ---
    "punch combo":  "punch combo, shadowbox, boxing, combat, combo, fists, enchaînement de coups, boxe, combat, coups de poing, frapper plusieurs fois, slam, combat rapproché",
    "cross punch":  "cross punch, punch, jab, hit, strike, coup de poing, frapper, donner un coup, coup direct",
    "flying kick":  "flying kick, kick, jump kick, attack, coup de pied, donner un coup de pied, attaque sautée, coup de pied sauté",
    "spin":         "spin, twirl, quickspin, whirl, pirouette, rotate, tourner, tour sur soi-même, rotation",
    "backflip":     "backflip, back flip, somersault, salto arrière, faire un salto arrière, flip arrière, acrobatie, saltos, saut périlleux",
    "front flip":   "front flip, frontflip, forward flip, flip, salto avant, faire un salto avant, saut périlleux avant",
    "running forward flip": "running forward flip, sprint flip, running flip, dash flip, saut périlleux en courant",

    # --- GESTES DE LA MAIN & INTERACTION ---
    "acknowledging": "acknowledging, acknowledge, nod, agree, approval, yes, understood, acquiescer, faire oui de la tête, approuver, comprendre, ok",
    "angry gesture": "angry gesture, anger, furious, scold, angry hand gesture, geste de colère, gronder, se fâcher, geste énervé",
    "annoyed head shake": "annoyed head shake, annoyed, irritated, frustrated, exasperated, disapproval, agacé, irrité, contrarié, exaspéré, secouer la tête d'un air agacé",
    "being cocky": "being cocky, cocky, smug, confident, boastful, self-satisfied, arrogant, sûr de soi, se vanter, prendre un air supérieur, arrogant",
    "dismissing gesture": "dismissing gesture, dismiss, wave away, brush off, go away, no thanks, congédier, balayer de la main, repousser, faire signe de partir",
    "happy hand gesture": "happy hand gesture, happy, cheerful, excited, enthusiastic, joyful gesture, geste joyeux, content, enthousiaste, célébrer, victoire",
    "hard head nod": "hard head nod, emphatic nod, firm yes, strong agreement, insistently agree, hochement de tête ferme, approuver fermement, dire oui avec insistance",
    "head nod yes": "head nod yes, nod, yes, agree, approval, confirm, hocher la tête, dire oui, acquiescer, approuver",
    "lengthy head nod": "lengthy head nod, repeated nod, keep nodding, prolonged agreement, hocher plusieurs fois la tête, approuver longuement",
    "look away gesture": "look away gesture, look away, avert gaze, shy, embarrassed, bashful, détourner le regard, regarder ailleurs, timide, gêné, embarrassé",
    "relieved sigh": "relieved sigh, relief, sigh, relieved, finally relax, grateful, soupir de soulagement, soulagé, pousser un soupir",
    "sarcastic head nod": "sarcastic head nod, sarcastic, ironic, mock agreement, cynical nod, hochement sarcastique, ironique, approuver avec sarcasme",
    "shaking head no": "shaking head no, shake head, no, disagree, refuse, deny, secouer la tête, dire non, refuser, désapprouver",
    "thoughtful head shake": "thoughtful head shake, thoughtful, uncertain, considering, pondering, réflexion, pensif, hésitant, réfléchir, secouer la tête pensivement",
    "weight shift": "weight shift, shift weight, fidget, nervous, restless, awkward, changer d'appui, bouger nerveusement, s'agiter, mal à l'aise, nerveux",
    "waving":       "waving, wave, hello, hi, greeting, bye, faire coucou, saluer, signe de la main, au revoir, bonjour, salut",
    "greeting":     "greeting, hello, hi, salutation, saluer, dire bonjour, accueillir",
    "peace sign":   "peace sign, peace, victory, v sign, two fingers, signe de paix, faire le V de la victoire",
    "headpat1":     "headpat, pet, pat, head pat, caresser la tête, caresse",
    "headpat cover":"headpat cover, dodge headpat, avoid pat, block pat, esquiver caresse",
    "headpat swat": "headpat swat, swat headpat, slap away, deflect pat, repousser caresse",
    "shoot":        "shoot, finger gun, bang, gun, pew, faire un pistolet avec les doigts, tirer, pistolet",
    "stop it":      "stop it, stop, halt, no, dismiss, enough, arrête, stop, faire signe d'arrêter, assez",

    # --- DIRECTIONS & REGARDS ---
    "lookleft":     "look left, glance left, peek left, turn left, regarder à gauche, jeter un regard à gauche",
    "lookright":    "look right, glance right, peek right, turn right, regarder à droite, jeter un regard à droite",
    "dogeza":       "dogeza, kneel, apology, prostrate, bow, s'agenouiller, s'excuser, se prosterner, s'incliner, excuse",
    "touch ears":   "touch ears, fox ears, ear touch, perk ears, toucher ses oreilles, toucher ses oreilles de renard",
    "shikanoko":    "shikanoko, deer dance, noko, danse du cerf, danse shikanoko",

    # --- IDLES, POSES & ÉTATS ---
    "bashful":      "bashful, shy, embarrassed, flustered, blush, timide, rougir, gêné, embarrassé",
    "thinking":     "thinking, ponder, consider, hmm, contemplate, idea, réfléchir, penser, méditer, réflexion",
    "talking":      "talking, speak, chat, conversation, blabbing, parler, discuter, bavarder",
    "look around":  "look around, glance, survey, scan, looking, regarder autour, observer, scruter",
    "model pose":   "model pose, pose, model, fashion, poser comme un mannequin, pose de mannequin",
    "motion pose":  "motion pose, pose, stand, posing, prendre la pose, poser",
    "squat":        "squat, crouch, sit low, s'accroupir, accroupi, s'asseoir",

    # --- AFFECTION & RÉACTIONS ---
    "kiss":         "kiss, smooch, peck, embrasser, faire un bisou, baiser",
    "kiss kit":     "kiss kit, smooch, blow kiss, envoyer un baiser, envoyer un bisou, baiser soufflé",
    "kiss kitsune": "kiss kitsune, fox kiss, baiser de renard, bisou kitsune",
    "loser":        "loser, l sign, lose, fail, taunt, mock, perdant, se moquer, signe loser",
    "woah":         "woah, whoa, wow, amazement, astonishment, waouh, wow, étonnement, stupéfaction, impressionné",
    "excited":      "excited, hyped, pumped, eager, enthusiastic, excité, enthousiaste, impatient, ravi",
    "sad idle":     "sad idle, sad standing, mope, dejected, triste, abattu, morose, déprimé",
    "angry":        "angry, mad, furious, rage, scowl, frown, colère, furieux, fâché, énervé",
    "happy":        "happy, joyful, cheerful, glad, beam, heureux, joyeux, sourire, ravi",
}


def _normalize_emote_name(path: Path) -> str:
    """Filename stem -> embed-friendly normalized name.

    - lowercases
    - replaces underscores and hyphens with spaces
    - strips leading numeric prefixes like "001 ", "08 "

    Examples:
        Backflip.fbx                 -> 'backflip'
        Sad Idle.fbx                 -> 'sad idle'
        idle_calm.fbx                -> 'idle calm'
        001_motion_pose.vrma         -> 'motion pose'
        Bling-Bang-Bang-Born.vrma    -> 'bling bang bang born'
        Spin.vrma                    -> 'spin'
    """
    s = path.stem.replace("_", " ").replace("-", " ")
    parts = [p for p in s.split() if p]
    # Strip leading numeric tokens like "001", "08".
    while parts and parts[0].isdigit():
        parts.pop(0)
    return " ".join(parts).lower().strip()


def build_expression_picker() -> SemanticPicker:
    """Picker for the 6 VRM 1.0 facial expressions. Fallback comes from
    `character_config.yaml` (`presets.default.default_expression`) via
    `config.DEFAULT_EXPRESSION` — Riko defaults to "relaxed"."""
    return SemanticPicker(
        names=VRM_EXPRESSIONS,
        model_path=MINILM_PATH,
        threshold=MATCH_THRESHOLD,
        fallback=DEFAULT_EXPRESSION,
        aliases=RIKO_EXPRESSION_ALIASES,
        query_aliases=RIKO_EXPRESSION_QUERY_ALIASES,
    )


def build_emote_picker() -> Tuple[SemanticPicker, Dict[str, str]]:
    """Build a picker over every `.fbx` / `.vrma` file in `ANIMATION_SOURCES`.

    Returns `(picker, name_to_rel_url)`. Callers do:
        name, _ = picker.pick("shadowbox")
        url = name_to_rel_url[name]   # e.g. "animations/mixamo/Punch Combo.fbx"
        playback.enqueue(PlaybackItem(EMOTE, animate_url=url))

    The `name_to_rel_url` values are BROWSER-RELATIVE strings already (paths
    relative to `client/`), ready to drop into `vrm_animate(...)`. We never
    expose absolute filesystem paths from this function — that mistake is
    what broke Milestone 5's first test run.

    Folder iteration order is `ANIMATION_SOURCES`. First-seen wins on name
    collisions (so VRMA versions override Mixamo when both exist).
    """
    name_to_rel_url: Dict[str, str] = {}
    dropped_dupes: list = []

    for folder, pattern in ANIMATION_SOURCES:
        if not folder.exists():
            print(f"[pickers] WARN animation folder missing: {folder}")
            continue
        files = sorted(folder.glob(pattern))
        for p in files:
            name = _normalize_emote_name(p)
            if not name:
                continue  # empty after stripping leading digits — skip
            # .as_posix(), never str(): this is a URL the BROWSER fetches,
            # and str() on Windows returns backslashes.
            rel_url = p.relative_to(CLIENT_ROOT).as_posix()
            if name in name_to_rel_url:
                dropped_dupes.append((name, rel_url))
                continue
            name_to_rel_url[name] = rel_url

    if not name_to_rel_url:
        raise FileNotFoundError(
            f"no .fbx/.vrma animations found in any of: "
            f"{[str(f) for f, _ in ANIMATION_SOURCES]}"
        )

    names = list(name_to_rel_url.keys())
    fallback = "idle" if "idle" in name_to_rel_url else names[0]
    print(
        f"[pickers] emote pool: {len(names)} animations from "
        f"{len(ANIMATION_SOURCES)} source(s), fallback={fallback!r}"
    )
    if dropped_dupes:
        print(
            f"[pickers] dropped {len(dropped_dupes)} duplicate name(s) "
            f"(first-seen wins): {dropped_dupes[:3]}{'...' if len(dropped_dupes) > 3 else ''}"
        )

    picker = SemanticPicker(
        names=names,
        model_path=MINILM_PATH,
        threshold=MATCH_THRESHOLD,
        fallback=fallback,
        aliases=RIKO_EMOTE_ALIASES,
        query_aliases=RIKO_EMOTE_QUERY_ALIASES,
    )
    return picker, name_to_rel_url


if __name__ == "__main__":
    # Run with: python -m server.llm.pickers
    # Builds two pairs of pickers (bare vs aliased) for A/B comparison.

    # --- expression A/B ---
    print("[pickers.__main__] building BARE expression picker...")
    bare_expr = SemanticPicker(
        names=VRM_EXPRESSIONS, model_path=MINILM_PATH,
        threshold=MATCH_THRESHOLD, fallback=DEFAULT_EXPRESSION,
    )
    print("[pickers.__main__] building ALIASED expression picker (production)...")
    expr_picker = build_expression_picker()

    # --- emote A/B ---
    print("\n[pickers.__main__] building BARE emote picker (no aliases)...")
    # Reproduce the build steps without aliases for fair A/B.
    _bare_n2u: dict = {}
    for folder, pattern in ANIMATION_SOURCES:
        if not folder.exists():
            continue
        for p in sorted(folder.glob(pattern)):
            n = _normalize_emote_name(p)
            if n and n not in _bare_n2u:
                _bare_n2u[n] = p.relative_to(CLIENT_ROOT).as_posix()
    bare_emote = SemanticPicker(
        names=list(_bare_n2u.keys()), model_path=MINILM_PATH,
        threshold=MATCH_THRESHOLD,
        fallback="idle" if "idle" in _bare_n2u else next(iter(_bare_n2u)),
    )
    print("[pickers.__main__] building ALIASED emote picker (production)...")
    emote_picker, emote_urls = build_emote_picker()

    # --- expression queries ---
    expr_queries: list = [
        ("moneyeyes",  "happy"),
        ("furious",    "angry"),
        ("crying",     "sad"),
        ("sobbing",    "sad"),
        ("glee",       "happy"),
        ("laugh",      "happy"),       # new
        ("smug",       "relaxed"),
        ("mocking",    "relaxed"),
        ("teasing",    "relaxed"),
        ("smirk",      "relaxed"),     # new
        ("grin",       "relaxed"),     # new — Riko's grin is smug, not joyful
        ("evilgrin",   "relaxed"),     # new — user's actual LLM output
        ("shocked",    "surprised"),
        ("wow",        "surprised"),
        ("zzz",        None),
        ("nonsense_word", None),
    ]
    # --- emote queries ---
    emote_queries: list = [
        # User-reported cases:
        ("shadowbox",  "punch combo"),     # user's example #1
        ("quickspin",  "spin"),            # user's example #2
        # Other plausible LLM creativity:
        ("flip",       None),
        ("somersault", None),
        ("wave",       "waving"),
        ("hello",      None),
        ("kiss",       "kiss"),
        ("smooch",     None),
        ("punch",      None),
        ("jab",        None),
        ("kick",       None),
        ("pet",        None),
        ("headpat",    None),
        ("pose",       None),
        ("peace",      "peace sign"),
        ("loser",      "loser"),
        ("dance",      None),
        ("woah",       "woah"),
        ("whoa",       None),
        ("backflip",   "backflip"),
        ("spin",       "spin"),
        ("idle",       "idle"),
        ("nonsense_word", "idle"),         # fallback
    ]

    # --- run & collect ---
    expr_rows = []
    for q, exp in expr_queries:
        b, bs = bare_expr.pick(q)
        a, as_ = expr_picker.pick(q)
        expr_rows.append((q, exp, b, bs, a, as_))

    emote_rows = []
    for q, exp in emote_queries:
        b, bs = bare_emote.pick(q)
        a, as_ = emote_picker.pick(q)
        emote_rows.append((q, exp, b, bs, a, as_))

    # --- expression table ---
    print("\n" + "=" * 110)
    print("EXPRESSION PICKER A/B  (bare names vs aliased)")
    print("=" * 110)
    print(f"{'query':>15}  |  {'expected':<10}  |  {'BARE':<18}  |  {'ALIASED':<18}  |  result")
    print("-" * 110)
    fixed = regressed = 0
    for q, exp, b, bs, a, as_ in expr_rows:
        mark = ""
        if exp is not None:
            b_ok = (b == exp); a_ok = (a == exp)
            if a_ok and not b_ok: mark = "FIXED"; fixed += 1
            elif b_ok and not a_ok: mark = "REGRESSED"; regressed += 1
            elif a_ok and b_ok: mark = "ok (both)"
            else: mark = "still off"
        print(f"{q!r:>15}  |  {str(exp):<10}  |  {b:<10} {bs:.2f}     |  {a:<10} {as_:.2f}     |  {mark}")
    print(f"\nEXPRESSION: aliases fixed {fixed}, regressed {regressed}")

    # --- emote table ---
    print("\n" + "=" * 110)
    print("EMOTE PICKER A/B  (bare names vs aliased)")
    print("=" * 110)
    print(f"{'query':>15}  |  {'expected':<14}  |  {'BARE':<22}  |  {'ALIASED':<22}  |  result")
    print("-" * 110)
    fixed = regressed = 0
    for q, exp, b, bs, a, as_ in emote_rows:
        mark = ""
        if exp is not None:
            b_ok = (b == exp); a_ok = (a == exp)
            if a_ok and not b_ok: mark = "FIXED"; fixed += 1
            elif b_ok and not a_ok: mark = "REGRESSED"; regressed += 1
            elif a_ok and b_ok: mark = "ok (both)"
            else: mark = "still off"
        print(f"{q!r:>15}  |  {str(exp):<14}  |  {b:<14} {bs:.2f}     |  {a:<14} {as_:.2f}     |  {mark}")
    print(f"\nEMOTE: aliases fixed {fixed}, regressed {regressed}")

    # --- emote URL resolution preview ---
    print("\n" + "=" * 110)
    print("EMOTE URL RESOLUTION  (production picker only)")
    print("=" * 110)
    for q, exp, _b, _bs, a, as_ in emote_rows:
        url = emote_urls.get(a, "(missing)")
        print(f"{q!r:>15}  ->  {a!r:<24}  ->  {url}")

    # --- hard checks ---
    nw_expr = next(r for r in expr_rows if r[0] == "nonsense_word")
    assert nw_expr[4] == DEFAULT_EXPRESSION, (
        f"expr fallback broken: got {nw_expr[4]!r}"
    )
    nw_emote = next(r for r in emote_rows if r[0] == "nonsense_word")
    assert nw_emote[4] == "idle", f"emote fallback broken: got {nw_emote[4]!r}"
    print("\n[pickers.__main__] OK — fallbacks intact")
