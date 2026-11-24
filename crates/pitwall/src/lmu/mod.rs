/* LMU file formats outside telemetry: the setup files the garage loads
   (.svm), the telemetry-to-.svm key mapping, and the name and notes
   templates for written setups.  */

pub mod mapping;
pub mod svm;
pub mod templates;
pub mod writer;

#[cfg(test)]
mod tests;
