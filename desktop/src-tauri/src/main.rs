// В релизе без консольного окна рядом с приложением.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    booktts_lib::run()
}
