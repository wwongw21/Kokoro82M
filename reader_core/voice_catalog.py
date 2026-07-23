from __future__ import annotations

from .config import LANG_MAP, VOICE_MAP


KOKORO_COLLECTION_LABELS = {
    "Official v1.0": "official_v1",
    "Official v1.1-zh": "official_v1_1_zh",
    "Gushi Labs": "gushi_labs",
    "Sethblocks": "sethblocks",
}
KOKORO_COLLECTION_LABEL_BY_KEY = {key: label for label, key in KOKORO_COLLECTION_LABELS.items()}
KOKORO_DEFAULT_COLLECTION = "official_v1"

KOKORO_COLLECTION_REPOSITORIES = {
    "official_v1": "hexgrad/Kokoro-82M",
    "official_v1_1_zh": "hexgrad/Kokoro-82M-v1.1-zh",
    "gushi_labs": "hexgrad/Kokoro-82M",
    "sethblocks": "hexgrad/Kokoro-82M",
}
KOKORO_VOICE_REPOSITORIES = {
    "official_v1": "hexgrad/Kokoro-82M",
    "official_v1_1_zh": "hexgrad/Kokoro-82M-v1.1-zh",
    "gushi_labs": "gushilabs/gushilabs-voices-for-kokoro-v1",
    "sethblocks": "Sethblocks/KokoroVoices",
}

_OFFICIAL_V11_ZH_FEMALE_NUMBERS = (
    "001", "002", "003", "004", "005", "006", "007", "008", "017", "018",
    "019", "021", "022", "023", "024", "026", "027", "028", "032", "036",
    "038", "039", "040", "042", "043", "044", "046", "047", "048", "049",
    "051", "059", "060", "067", "070", "071", "072", "073", "074", "075",
    "076", "077", "078", "079", "083", "084", "085", "086", "087", "088",
    "090", "092", "093", "094", "099",
)
_OFFICIAL_V11_ZH_MALE_NUMBERS = (
    "009", "010", "011", "012", "013", "014", "015", "016", "020", "025",
    "029", "030", "031", "033", "034", "035", "037", "041", "045", "050",
    "052", "053", "054", "055", "056", "057", "058", "061", "062", "063",
    "064", "065", "066", "068", "069", "080", "081", "082", "089", "091",
    "095", "096", "097", "098", "100",
)

OFFICIAL_V11_ZH_VOICE_MAP = {
    "American Female — Maple": "af_maple",
    "American Female — Sol": "af_sol",
    "British Female — Vale": "bf_vale",
    **{f"Mandarin Female — {number}": f"zf_{number}" for number in _OFFICIAL_V11_ZH_FEMALE_NUMBERS},
    **{f"Mandarin Male — {number}": f"zm_{number}" for number in _OFFICIAL_V11_ZH_MALE_NUMBERS},
}
GUSHI_LABS_VOICE_MAP = {
    "American Female — Vivien": "af_vivien",
    "American Male — Tony": "am_tony",
}
SETHBLOCKS_VOICE_MAP = {
    "American Female — Mika": "af_mika",
    "American Female — Mrs. Claus": "af_mrs_claus",
    "American Female — Heart Young": "heart_young",
    "American Male — Andy": "am_andy",
    "American Male — Dylan": "am_dylan",
}

KOKORO_COLLECTION_VOICE_MAPS = {
    "official_v1": VOICE_MAP,
    "official_v1_1_zh": OFFICIAL_V11_ZH_VOICE_MAP,
    "gushi_labs": GUSHI_LABS_VOICE_MAP,
    "sethblocks": SETHBLOCKS_VOICE_MAP,
}
KOKORO_COLLECTION_LANGUAGE_MAPS = {
    "official_v1": LANG_MAP,
    "official_v1_1_zh": {
        "American English": "a",
        "British English": "b",
        "Mandarin Chinese": "z",
    },
    "gushi_labs": {"American English": "a"},
    "sethblocks": {"American English": "a"},
}
KOKORO_SPECIAL_VOICE_LANGUAGES = {
    ("sethblocks", "heart_young"): "a",
}


def normalize_kokoro_collection(collection: str | None) -> str:
    return collection if collection in KOKORO_COLLECTION_VOICE_MAPS else KOKORO_DEFAULT_COLLECTION


def kokoro_collection_key(label: str) -> str:
    return KOKORO_COLLECTION_LABELS.get(label, KOKORO_DEFAULT_COLLECTION)


def kokoro_collection_label(collection: str) -> str:
    return KOKORO_COLLECTION_LABEL_BY_KEY[normalize_kokoro_collection(collection)]


def kokoro_language_names(collection: str) -> list[str]:
    collection = normalize_kokoro_collection(collection)
    return list(KOKORO_COLLECTION_LANGUAGE_MAPS[collection])


def kokoro_language_code(collection: str, language_name: str) -> str:
    collection = normalize_kokoro_collection(collection)
    return KOKORO_COLLECTION_LANGUAGE_MAPS[collection][language_name]


def kokoro_voice_names(collection: str, language_name: str) -> list[str]:
    collection = normalize_kokoro_collection(collection)
    language = kokoro_language_code(collection, language_name)
    voices = KOKORO_COLLECTION_VOICE_MAPS[collection]
    return [
        label for label, voice_id in voices.items()
        if KOKORO_SPECIAL_VOICE_LANGUAGES.get((collection, voice_id), voice_id[:1]) == language
    ]


def kokoro_voice_id(collection: str, voice_name: str) -> str:
    collection = normalize_kokoro_collection(collection)
    return KOKORO_COLLECTION_VOICE_MAPS[collection][voice_name]


def stable_kokoro_voice_id(collection: str, voice_id: str) -> str:
    collection = normalize_kokoro_collection(collection)
    # Preserve existing settings and favorites for the original official catalog.
    return voice_id if collection == KOKORO_DEFAULT_COLLECTION else f"{collection}::{voice_id}"


def split_stable_kokoro_voice_id(stable_id: str) -> tuple[str, str]:
    if "::" not in stable_id:
        return KOKORO_DEFAULT_COLLECTION, stable_id
    collection, voice_id = stable_id.split("::", 1)
    return normalize_kokoro_collection(collection), voice_id


def kokoro_model_repository(collection: str) -> str:
    return KOKORO_COLLECTION_REPOSITORIES[normalize_kokoro_collection(collection)]


def kokoro_voice_repository(collection: str) -> str:
    return KOKORO_VOICE_REPOSITORIES[normalize_kokoro_collection(collection)]


def kokoro_voice_filename(collection: str, voice_id: str) -> str:
    collection = normalize_kokoro_collection(collection)
    return f"{voice_id}.pt" if collection == "sethblocks" else f"voices/{voice_id}.pt"
