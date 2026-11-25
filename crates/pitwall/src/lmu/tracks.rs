/* Telemetry TrackName to the Settings folder the garage reads setups from.
   Folder names come from the track's own data, not from TrackName ('Circuit
   de la Sarthe' → 'Lemans', 'Lusail International Circuit' → 'Qatar').
   Names compare normalised (accents folded, ASCII letters and digits only,
   lower case): an exact match, then a known alias, then the longest folder
   name that prefixes the track name.  The store learns real pairs from
   matched history; this is the fallback for tracks without any.  */

use std::path::{Path, PathBuf};

// Normalised TrackName fragment → normalised folder name.
const ALIASES: &[(&str, &str)] = &[
    ("sarthe", "lemans"),
    ("lusail", "qatar"),
    ("josecarlospace", "interlagos"),
    ("enzoedinoferrari", "imola"),
    ("algarve", "portimao"),
    ("catalunya", "circuitdebarcelona"),
    ("barcelona", "circuitdebarcelona"),
    ("paulricard", "paulricard"),
    ("circuitoftheamericas", "circuitoftheamericas"),
    ("spafrancorchamps", "spa"),
    ("longbeach", "longbeach"),
    ("monza", "monza"),
    ("lagunaseca", "lagunaseca"),
    ("roadatlanta", "roadatlanta"),
    ("daytona", "daytonarc"),
];

pub fn normalise(s: &str) -> String {
    s.chars().map(fold_accent).filter(char::is_ascii_alphanumeric).map(|c| c.to_ascii_lowercase()).collect()
}

const fn fold_accent(c: char) -> char {
    match c {
        'à'..='å' | 'À'..='Å' => 'a',
        'ç' | 'Ç' => 'c',
        'è'..='ë' | 'È'..='Ë' => 'e',
        'ì'..='ï' | 'Ì'..='Ï' => 'i',
        'ñ' | 'Ñ' => 'n',
        'ò'..='ö' | 'ø' | 'Ò'..='Ö' | 'Ø' => 'o',
        'ù'..='ü' | 'Ù'..='Ü' => 'u',
        'ý' | 'ÿ' | 'Ý' => 'y',
        _ => c,
    }
}

pub fn guess_folder<'a>(track: &str, folders: impl IntoIterator<Item = &'a str>) -> Option<&'a str> {
    let folders: Vec<(String, &str)> = folders.into_iter().map(|f| (normalise(f), f)).filter(|(n, _)| !n.is_empty()).collect();
    let find = |norm: &str| folders.iter().find(|(n, _)| n == norm).map(|&(_, f)| f);
    let name = normalise(track);
    find(&name)
        .or_else(|| ALIASES.iter().filter(|(frag, _)| name.contains(frag)).find_map(|(_, folder)| find(folder)))
        .or_else(|| folders.iter().filter(|(n, _)| name.starts_with(n.as_str())).max_by_key(|(n, _)| n.len()).map(|&(_, f)| f))
}

pub fn guess_folder_in(track: &str, settings_dir: &Path) -> Option<PathBuf> {
    let names: Vec<String> = std::fs::read_dir(settings_dir)
        .ok()?
        .filter_map(Result::ok)
        .filter(|e| e.path().is_dir())
        .filter_map(|e| e.file_name().into_string().ok())
        .collect();
    guess_folder(track, names.iter().map(String::as_str)).map(|f| settings_dir.join(f))
}
