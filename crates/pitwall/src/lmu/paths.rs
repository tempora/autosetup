/* Le Mans Ultimate install and user-data folders.
   Lookup order: $AUTOSETUP_LMU_DIR, then every Steam library (the one whose
   libraryfolders.vdf block lists LMU's app id first) under the Steam roots
   from HKCU\Software\Valve\Steam\SteamPath and the default Steam folder.  An
   install is a folder holding UserData/.  Steam's registry value, the
   library file and the running-game check are Windows-only; elsewhere only
   the override applies.  */

use std::path::{Path, PathBuf};
use std::process::Command;

use thiserror::Error;

pub const LMU_APP_ID: &str = "2399420";
pub const ENV_OVERRIDE: &str = "AUTOSETUP_LMU_DIR";
const LMU_DIR_NAME: &str = "Le Mans Ultimate";
const LMU_EXE: &str = "Le Mans Ultimate.exe";
const DEFAULT_STEAM: &str = r"C:\Program Files (x86)\Steam";

#[derive(Debug, Error)]
pub enum PathsError {
    #[error("{ENV_OVERRIDE}={0} has no UserData folder")]
    BadOverride(PathBuf),
    #[error("Le Mans Ultimate not found; set {ENV_OVERRIDE} to the install folder")]
    NotFound,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LmuPaths {
    pub root: PathBuf,
}

impl LmuPaths {
    pub fn user_data(&self) -> PathBuf {
        self.root.join("UserData")
    }

    pub fn telemetry(&self) -> PathBuf {
        self.user_data().join("Telemetry")
    }

    // Per-track setup folders: Settings/<Track>/*.svm.
    pub fn settings(&self) -> PathBuf {
        self.user_data().join("player").join("Settings")
    }

    // <library>/steamapps/appmanifest_<id>.acf, beside steamapps/common/<LMU>.
    pub fn manifest(&self) -> Option<PathBuf> {
        Some(self.root.parent()?.parent()?.join(format!("appmanifest_{LMU_APP_ID}.acf")))
    }

    fn is_install(root: &Path) -> bool {
        root.join("UserData").is_dir()
    }
}

pub fn find_install() -> Result<LmuPaths, PathsError> {
    if let Some(dir) = std::env::var_os(ENV_OVERRIDE).filter(|v| !v.is_empty()) {
        let root = PathBuf::from(dir);
        return if LmuPaths::is_install(&root) { Ok(LmuPaths { root }) } else { Err(PathsError::BadOverride(root)) };
    }
    steam_roots()
        .iter()
        .filter_map(|steam| std::fs::read_to_string(steam.join("steamapps").join("libraryfolders.vdf")).ok())
        .flat_map(|vdf| library_folders(&vdf))
        .map(|lib| lib.join("steamapps").join("common").join(LMU_DIR_NAME))
        .find(|root| LmuPaths::is_install(root))
        .map(|root| LmuPaths { root })
        .ok_or(PathsError::NotFound)
}

/* Library paths from libraryfolders.vdf, LMU's library first.  Each library
   block runs from its "path" key to the next one and lists its app ids.  */
pub fn library_folders(vdf: &str) -> Vec<PathBuf> {
    let starts: Vec<usize> = vdf.match_indices("\"path\"").map(|(i, _)| i).collect();
    let mut libs: Vec<(bool, PathBuf)> = starts
        .iter()
        .enumerate()
        .filter_map(|(n, &i)| {
            let block = &vdf[i..starts.get(n + 1).copied().unwrap_or(vdf.len())];
            let path = quoted_after(block, "\"path\"")?.replace("\\\\", "\\");
            Some((!block.contains(&format!("\"{LMU_APP_ID}\"")), PathBuf::from(path)))
        })
        .collect();
    libs.sort_by_key(|(other, _)| *other);
    libs.into_iter().map(|(_, p)| p).collect()
}

// First `"key" "value"` pair's value in Valve KeyValues text.
pub(crate) fn vdf_value<'a>(text: &'a str, key: &str) -> Option<&'a str> {
    let at = text.find(&format!("\"{key}\""))?;
    quoted_after(&text[at..], &format!("\"{key}\""))
}

fn quoted_after<'a>(text: &'a str, key: &str) -> Option<&'a str> {
    let rest = text.strip_prefix(key)?.trim_start_matches([' ', '\t']);
    let body = rest.strip_prefix('"')?;
    body.find('"').map(|end| &body[..end])
}

fn steam_roots() -> Vec<PathBuf> {
    let registry = hidden(Command::new("reg").args(["query", r"HKCU\Software\Valve\Steam", "/v", "SteamPath"]))
        .output()
        .ok()
        .and_then(|o| String::from_utf8(o.stdout).ok())
        .and_then(|out| out.lines().find_map(|l| l.split_once("REG_SZ").map(|(_, v)| PathBuf::from(v.trim()))));
    registry.into_iter().chain([PathBuf::from(DEFAULT_STEAM)]).collect()
}

// Whether Le Mans Ultimate is running (Windows only; false elsewhere).
pub fn game_running() -> bool {
    cfg!(windows)
        && hidden(Command::new("tasklist").args(["/FI", &format!("IMAGENAME eq {LMU_EXE}"), "/NH", "/FO", "CSV"]))
            .output()
            .is_ok_and(|o| String::from_utf8_lossy(&o.stdout).to_lowercase().contains(&LMU_EXE.to_lowercase()))
}

// No console window flashes up from the desktop app.
fn hidden(cmd: &mut Command) -> &mut Command {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    cmd
}
