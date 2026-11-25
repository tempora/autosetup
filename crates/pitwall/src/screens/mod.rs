/* Screen payloads for the desktop UI: plain serde structs built from the
   store and, for track maps and lap traces, from the recordings.  Field
   names follow the web UI's.  Ids are store stint ids; times are seconds;
   recorded_at is LMU's "YYYY-MM-DDTHH_MM_SSZ" string (UTC).  */

pub mod driver;
pub mod history;
pub mod names;
pub mod setup;
pub mod trackmap;

#[cfg(test)]
mod tests;
