/* Setup file names and garage notes from templates.
   Fields: {track} {car} {class} {date} {time} {n} {stint} {lap} {feel}
   {changes}.  A field that is known but empty is dropped together with the
   separator before it (or after it, at the start); unknown fields stay as
   typed.  `{car}` is the car model, never the team.  */

use std::collections::HashMap;

pub const NAME: &str = "{track} {car} {date} {n}";
pub const NOTES: &str = "From stint {stint} ({lap}), {feel}. {changes}";

const MODELS: &[(&str, &str)] = &[
    ("AMR_LMGT3", "Aston Martin Vantage"),
    ("BMW_M4_LMGT3", "BMW M4"),
    ("Porsche_911_GT3_R_LMGT3", "Porsche 911 GT3 R"),
    ("Oreca_07", "Oreca 07"),
];
const CLASS_TAGS: &[&str] = &["lmgt3", "gt3", "lmp2", "lmp2_elms", "lmh", "lmdh", "hy", "hypercar", "gte", "lmgte", "lmp3"];
const SHORT_TRACKS: &[(&str, &str)] = &[
    ("Bahrain International Circuit", "Bahrain"),
    ("WeatherTech Raceway Laguna Seca", "Laguna Seca"),
    ("Lusail International Circuit", "Lusail"),
    ("Circuit de Barcelona", "Barcelona"),
    ("Fuji Speedway", "Fuji"),
    ("Circuit de Spa-Francorchamps", "Spa"),
    ("Autódromo José Carlos Pace", "Interlagos"),
    ("Sebring International Raceway", "Sebring"),
    ("Circuit de la Sarthe", "Le Mans"),
    ("Autodromo Enzo e Dino Ferrari", "Imola"),
    ("Autodromo Internazionale Enzo e Dino Ferrari", "Imola"),
    ("Circuit of the Americas", "COTA"),
    ("Autódromo Internacional do Algarve", "Portimão"),
    ("Autodromo Nazionale Monza", "Monza"),
];

// 'AMR_LMGT3' → 'Aston Martin Vantage'; otherwise underscores to spaces, class tags dropped.
pub fn car_name(model: &str) -> String {
    MODELS.iter().find(|(m, _)| *m == model).map_or_else(
        || {
            model
                .split('_')
                .filter(|w| !w.is_empty() && !CLASS_TAGS.contains(&w.to_ascii_lowercase().as_str()))
                .collect::<Vec<_>>()
                .join(" ")
        },
        |(_, name)| name.to_string(),
    )
}

pub fn short_track(name: &str) -> &str {
    SHORT_TRACKS.iter().find(|(long, _)| *long == name).map_or(name, |(_, short)| short)
}

fn is_sep(c: char) -> bool {
    matches!(c, ' ' | '_' | '-')
}

// Parsed template: literal text and {field} placeholders.
enum Piece<'a> {
    Text(&'a str),
    Field(&'a str),
}

fn pieces(template: &str) -> Vec<Piece<'_>> {
    let mut out = Vec::new();
    let mut rest = template;
    while let Some(open) = rest.find('{') {
        let Some(len) = rest[open + 1..].find('}') else { break };
        let name = &rest[open + 1..open + 1 + len];
        if name.is_empty() || !name.chars().all(|c| c.is_alphanumeric() || c == '_') {
            out.push(Piece::Text(&rest[..open + 1]));
            rest = &rest[open + 1..];
            continue;
        }
        out.push(Piece::Text(&rest[..open]));
        out.push(Piece::Field(name));
        rest = &rest[open + 2 + len..];
    }
    out.push(Piece::Text(rest));
    out
}

pub fn render(template: &str, fields: &HashMap<&str, String>) -> String {
    let mut out = String::new();
    let mut drop_next_sep = true;
    for piece in pieces(template) {
        match piece {
            Piece::Text(t) => {
                let t = if drop_next_sep { t.trim_start_matches(is_sep) } else { t };
                out.push_str(t);
                drop_next_sep = false;
            }
            Piece::Field(name) => match fields.get(name) {
                Some(v) if v.is_empty() => {
                    let kept = out.trim_end_matches(is_sep).len();
                    // At the start the separator after it goes instead.
                    drop_next_sep = kept == 0;
                    out.truncate(kept);
                }
                Some(v) => out.push_str(v),
                None => {
                    out.push('{');
                    out.push_str(name);
                    out.push('}');
                }
            },
        }
    }
    out.trim().to_string()
}

// A name Windows accepts: no \ / : * ? " < > |, no dots or spaces at the ends.
pub fn file_safe(name: &str) -> String {
    name.chars()
        .map(|c| if matches!(c, '\\' | '/' | ':' | '*' | '?' | '"' | '<' | '>' | '|') { '-' } else { c })
        .collect::<String>()
        .trim_matches([' ', '.'])
        .to_string()
}

// 1:43.46 for notes, 1-43.46 for file names.
pub fn lap_text(seconds: f64, for_file: bool) -> String {
    let m = (seconds / 60.0).floor();
    let s = seconds - 60.0 * m;
    format!("{}{}{:05.2}", m as i64, if for_file { '-' } else { ':' }, s)
}

// The sliders in words: 'Pointier 0.20° + Safer 0.10°', or 'As it was'.
pub fn feel_text(front: f64, rear: f64, fast: f64) -> String {
    let parts: Vec<String> = [("Pointier", "Lazier", front), ("Looser", "Safer", rear), ("Fast looser", "Planted", fast)]
        .into_iter()
        .filter(|(_, _, v)| v.abs() > 1e-9)
        .map(|(neg, pos, v)| format!("{} {:.2}°", if v < 0.0 { neg } else { pos }, v.abs()))
        .collect();
    if parts.is_empty() { "As it was".to_string() } else { parts.join(" + ") }
}
