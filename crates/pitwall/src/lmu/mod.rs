/* LMU outside telemetry: install and user-data paths, game builds, the
   Settings folder per track, the setup files the garage loads (.svm), the
   telemetry-to-.svm key mapping, per-car click-to-label scales, and the name
   and notes templates for written setups.  */

pub mod builds;
pub mod mapping;
pub mod paths;
pub mod scales;
pub mod svm;
pub mod templates;
pub mod tracks;
pub mod writer;

#[cfg(test)]
mod tests;
